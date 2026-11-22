"""The plugin's durable state: SQLite at `$CLAUDE_PLUGIN_DATA/fido.sqlite`.

Several plugin processes use it at once — a Fido turn's server, a pinned tap
turn's server, Casa's once-a-minute sweep — so the database runs in WAL mode
with a 5 s busy timeout, and every read-modify-write is one `BEGIN IMMEDIATE`
transaction. The directory is 0700 and every database file 0600.

What it holds:

* **tap tokens** — only `sha256(token)`, never the token, with the exact
  decision (`kind`, canonical payload JSON) and the card (`group`) it
  belongs to. A card is answered by its first tap: redeeming any token of a
  group answers the group, so two buttons of one card tapped at once from
  two processes apply exactly one decision, and a used, voided, expired,
  unknown or wrong-tool token applies nothing;
* **run intents** — written before the `POST /v1/runs` that creates the run,
  then attached to the app's run id;
* **admission requests** — one row per `start_runs` call, keyed by the
  plugin's `request_key`, written before Casa is asked;
* **stop seals** — one row per request the operator stopped, written
  before any network call; Fido's own code never drives, wakes or tells a sealed request;
* **files** issued to a run; **driving leases** (the process driving a run);
  **deliveries** deposited with Casa and what Casa said became of them, with
  one current attempt per (run, file); the **notice cursor**.

No token, payload, binding or secret is ever logged. Standard library only.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import time
import zlib
from pathlib import Path
from typing import NamedTuple

from app_client import AppError

DB_NAME = "fido.sqlite"
ENV_DATA = "CLAUDE_PLUGIN_DATA"
BUSY_TIMEOUT_MS = 5000
LEASE_MAX_S = 660.0          # the 600 s turn ceiling plus margin
CLAIM_STALE_S = 120.0
MAX_TOKEN_CHARS = 128

#: a question's answers (the action model): each is applied once, its receipt kept
ANSWER_KINDS = frozenset({"q_confirm", "q_answer", "q_not_now"})
TAP_KINDS = frozenset({"forget", "keep", "keep_going", "stop", "q_live"} | ANSWER_KINDS)
#: what a card is for, by the kind of its buttons
PURPOSE_OF = {"forget": "forget", "keep": "forget", "keep_going": "keep_going",
              "stop": "keep_going", "q_confirm": "question", "q_answer": "question",
              "q_not_now": "question", "q_live": "question"}
#: the one tool that may redeem each kind
_TOOL_FOR = {kind: ("tap_live" if kind == "q_live" else "apply_tap") for kind in TAP_KINDS}
ADMISSION_OUTCOMES = frozenset({"submitted", "declined", "expired", "refused"})
#: `shot` is a question's screenshot, `qlive` a question card carrying the live-view link
DELIVERY_KINDS = frozenset({"notice", "file", "link", "card", "handoff", "shot", "qlive",
                            "sources"})
DELIVERY_STATUSES = frozenset({"pending", "delivered", "withheld", "unknown"})
_SETTLED = ("delivered", "withheld")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    grp TEXT PRIMARY KEY, run_id TEXT, minted_at REAL NOT NULL,
    expires_at REAL NOT NULL, answered_kind TEXT, voided INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL, purpose TEXT NOT NULL DEFAULT 'approval', voided_at REAL,
    void_reason TEXT);
CREATE INDEX IF NOT EXISTS cards_run ON cards(run_id, seq);
CREATE TABLE IF NOT EXISTS taps (
    hash TEXT PRIMARY KEY, grp TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL, expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS intents (
    request_id TEXT PRIMARY KEY, binding_ref TEXT NOT NULL, requester_id TEXT NOT NULL,
    requester_kind TEXT NOT NULL, request_group TEXT NOT NULL, position INTEGER NOT NULL,
    spec TEXT NOT NULL, run_id TEXT UNIQUE, terminal INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS admissions (
    request_key TEXT PRIMARY KEY, binding_ref TEXT NOT NULL, requester_id TEXT NOT NULL,
    requester_kind TEXT NOT NULL, specs TEXT NOT NULL, request_ids TEXT NOT NULL,
    digests TEXT NOT NULL, status TEXT NOT NULL, confirmation_id TEXT UNIQUE,
    admissions TEXT, claimed_at REAL, created_at REAL NOT NULL, swept_at REAL);
CREATE TABLE IF NOT EXISTS files (file_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
    safe_name TEXT, mime TEXT, size INTEGER);
CREATE TABLE IF NOT EXISTS beats (run_id TEXT PRIMARY KEY, n INTEGER NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS leases (
    run_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, start_time TEXT NOT NULL,
    taken_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (
    reference TEXT PRIMARY KEY, kind TEXT NOT NULL, run_id TEXT, file_id TEXT,
    notice_seq INTEGER, grp TEXT, status TEXT NOT NULL DEFAULT 'pending',
    current INTEGER NOT NULL DEFAULT 1, seq INTEGER NOT NULL,
    deposited_at REAL NOT NULL DEFAULT 0, reported TEXT, detail TEXT,
    told INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS deliveries_file ON deliveries(run_id, file_id, current);
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS question_shots (
    question_id TEXT NOT NULL, revision INTEGER NOT NULL, screenshot_id TEXT NOT NULL,
    reference TEXT NOT NULL, PRIMARY KEY (question_id, revision, screenshot_id));
CREATE TABLE IF NOT EXISTS question_live (question_id TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS wakes (run_id TEXT PRIMARY KEY, turn_start REAL NOT NULL,
    seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS touches (run_id TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS run_calls (run_id TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS code_holds (run_id TEXT PRIMARY KEY, site_id TEXT NOT NULL,
    token TEXT NOT NULL, requester_id TEXT NOT NULL, requester_kind TEXT NOT NULL,
    until REAL NOT NULL, redeemed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ended_owed (run_id TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS stops (request_key TEXT PRIMARY KEY, at REAL NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ended_named (request_key TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS app_ended (run_id TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS answered (run_id TEXT PRIMARY KEY, reference TEXT NOT NULL,
    finish TEXT NOT NULL, created_at REAL NOT NULL, finished_at REAL);
CREATE TABLE IF NOT EXISTS notice_batches (
    key TEXT PRIMARY KEY, body TEXT NOT NULL, first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL, count INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
"""

#: columns added after the first schema, added in place to an older database
_ADDED_COLUMNS = (("deliveries", "deposited_at", "REAL NOT NULL DEFAULT 0"),
                  ("cards", "purpose", "TEXT NOT NULL DEFAULT 'approval'"),
                  ("cards", "voided_at", "REAL"), ("cards", "void_reason", "TEXT"),
                  ("files", "safe_name", "TEXT"), ("files", "mime", "TEXT"),
                  ("files", "size", "INTEGER"), ("deliveries", "reported", "TEXT"),
                  ("admissions", "swept_at", "REAL"), ("deliveries", "detail", "TEXT"),
                  ("deliveries", "told", "INTEGER NOT NULL DEFAULT 0"),
                  ("taps", "progress", "TEXT"), ("taps", "answer", "TEXT"),
                  ("taps", "receipt", "TEXT"), ("taps", "owner_pid", "INTEGER"),
                  ("taps", "owner_start", "TEXT"), ("taps", "owner_at", "REAL"),
                  ("taps", "settled_at", "REAL"))

#: The schema's version is derived from its text, so any edit to the schema (a new table, a new
#: added column) migrates an older database in place on its next connection. A hand-kept number can
#: stay put while tables are added, leaving a database without them. Never 0, which is a new database's version.
SCHEMA_VERSION = zlib.crc32(repr((_SCHEMA, _ADDED_COLUMNS)).encode("utf-8")) % 0x7FFFFFFF + 1


class TapRecord(NamedTuple):
    kind: str
    payload: dict
    group: str


class AnswerTap(NamedTuple):
    """A question's answer tap: its decision, the exact body posted to the app (the same body
    on every resume), and its receipt once it is settled (None while in progress)."""
    hash: str
    kind: str
    payload: dict
    group: str
    body: dict
    receipt: str | None


class SettledAnswer(NamedTuple):
    kind: str
    payload: dict
    receipt: str
    settled_at: float | None        # None on a row settled before this was recorded


class CardGroup(NamedTuple):
    group: str
    run_id: str | None
    minted_at: float
    expires_at: float
    purpose: str


class RunBinding(NamedTuple):
    run_id: str | None
    request_id: str
    binding_ref: str
    requester_id: str
    requester_kind: str
    request_group: str
    position: int
    spec: dict


class PendingAdmission(NamedTuple):
    request_key: str
    binding_ref: str
    requester_id: str
    requester_kind: str
    specs: list
    request_ids: list
    digests: list
    status: str
    confirmation_id: str | None
    admissions: list | None


class DeliveryRow(NamedTuple):
    reference: str
    kind: str
    run_id: str | None
    file_id: str | None
    notice_seq: int | None
    group: str | None
    status: str
    current: bool
    deposited_at: float


# -- connection --------------------------------------------------------------

def _db_path() -> Path:
    raw = os.environ.get(ENV_DATA, "")
    if not raw:
        raise AppError("no_data_dir")
    return Path(raw) / DB_NAME


def _connect() -> sqlite3.Connection:
    path = _db_path()
    old = os.umask(0o077)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        con = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000,
                              isolation_level=None)
        con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _wal(con)
        con.execute("PRAGMA synchronous = FULL")
        if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            with _immediate(con):
                for stmt in filter(str.strip, _SCHEMA.split(";")):
                    con.execute(stmt)
                for table, column, decl in _ADDED_COLUMNS:
                    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                    if column not in have:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except OSError:
        raise AppError("no_data_dir") from None
    finally:
        os.umask(old)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(str(path) + suffix, 0o600)
        except FileNotFoundError:
            pass
    return con


def _wal(con: sqlite3.Connection) -> None:
    """Put the database in WAL mode. SQLite answers a mode switch that races
    another connection with an immediate "database is locked", without
    waiting on the busy handler, so the switch is retried within the busy
    timeout — and skipped once the database is already in WAL."""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            if con.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                return
            con.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


@contextlib.contextmanager
def _immediate(con: sqlite3.Connection):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


@contextlib.contextmanager
def _tx():
    con = _connect()
    try:
        with _immediate(con):
            yield con
    finally:
        con.close()


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _next_seq(con, table: str) -> int:
    return con.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table}").fetchone()[0]


# -- tap tokens -----------------------------------------------------------------

def mint_tap(kind: str, payload: dict, group: str, ttl_s: int = 3600, *,
             now: float | None = None) -> str:
    """A new single-use token for one button of card `group`; returns the raw
    token, which is stored nowhere. The card's run is `payload["run_id"]`
    when the payload names one."""
    if kind not in TAP_KINDS:
        raise ValueError("unknown tap kind")
    if not isinstance(payload, dict) or not isinstance(group, str) or not group:
        raise ValueError("bad tap")
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    expires = now + ttl_s
    run_id = payload.get("run_id") if isinstance(payload.get("run_id"), str) else None
    with _tx() as con:
        row = con.execute("SELECT answered_kind, voided FROM cards WHERE grp = ?",
                          (group,)).fetchone()
        if row is None:
            con.execute("INSERT INTO cards (grp, run_id, minted_at, expires_at, seq, purpose) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (group, run_id, now, expires, _next_seq(con, "cards"),
                         PURPOSE_OF[kind]))
        elif row[0] is not None or row[1]:
            raise ValueError("card already answered")
        elif con.execute("SELECT purpose FROM cards WHERE grp = ?",
                         (group,)).fetchone()[0] != PURPOSE_OF[kind]:
            raise ValueError("one card, one purpose")
        else:
            con.execute("UPDATE cards SET expires_at = MAX(expires_at, ?) WHERE grp = ?",
                        (expires, group))
        con.execute("INSERT INTO taps (hash, grp, kind, payload, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (_hash(token), group, kind, _canonical(payload), expires))
    return token


def redeem_tap(token, tool: str, now: float) -> TapRecord | None:
    """Apply-once: the token's decision, if the token is live, belongs to
    `tool`, and its card is still unanswered — in which case the card is
    answered in the same transaction. Anything else returns None and
    changes nothing."""
    if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_CHARS:
        return None
    with _tx() as con:
        row = con.execute("SELECT grp, kind, payload, expires_at, used FROM taps "
                          "WHERE hash = ?", (_hash(token),)).fetchone()
        if row is None:
            return None
        group, kind, payload, expires_at, used = row
        if used or expires_at <= now or _TOOL_FOR.get(kind) != tool:
            return None
        if kind in ANSWER_KINDS or kind == "q_live":
            return None                   # a question's buttons: redeem_answer / redeem_live
        card = con.execute("SELECT answered_kind, voided FROM cards WHERE grp = ?",
                           (group,)).fetchone()
        if card is None or card[0] is not None or card[1]:
            return None
        con.execute("UPDATE taps SET used = 1 WHERE grp = ?", (group,))
        con.execute("UPDATE cards SET answered_kind = ? WHERE grp = ?", (kind, group))
    return TapRecord(kind, json.loads(payload), group)


def _answer_of(row) -> AnswerTap:
    hash_, grp, kind, payload, answer, receipt = row
    return AnswerTap(hash_, kind, json.loads(payload), grp, json.loads(answer), receipt)


def redeem_answer(token, tool: str, now: float, operator_ref) -> AnswerTap | None:
    """A question's answer, applied once. A fresh live token answers
    its card in one transaction and is recorded ``in_progress`` with the exact body to post
    (``operator_ref(token)`` builds its operator reference). A token of a card that was
    already answered returns THAT answer — settled with its first receipt, or still in
    progress, to be resumed with the same body — never a second answer. A voided (superseded,
    withheld) card's token, an expired, unknown or wrong-tool token: None."""
    if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_CHARS:
        return None
    cols = "hash, grp, kind, payload, answer, receipt"
    with _tx() as con:
        row = con.execute("SELECT grp, kind, payload, expires_at, progress FROM taps "
                          "WHERE hash = ?", (_hash(token),)).fetchone()
        if row is None:
            return None
        group, kind, payload, expires_at, progress = row
        if kind not in ANSWER_KINDS or _TOOL_FOR.get(kind) != tool:
            return None
        if progress is not None:                       # this very token answered
            return _answer_of(con.execute(f"SELECT {cols} FROM taps WHERE hash = ?",
                                          (_hash(token),)).fetchone())
        card = con.execute("SELECT answered_kind, voided FROM cards WHERE grp = ?",
                           (group,)).fetchone()
        if card is None:
            return None
        if card[0] is not None:                        # another button of it answered
            first = con.execute(f"SELECT {cols} FROM taps WHERE grp = ? AND progress IS NOT "
                                "NULL LIMIT 1", (group,)).fetchone()
            return None if first is None else _answer_of(first)
        if card[1] or expires_at <= now:
            return None
        p = json.loads(payload)
        body = {"revision": p["revision"], "digest": p["digest"], "choice": p["choice"],
                "operator_ref": operator_ref(token)}
        con.execute("UPDATE taps SET used = 1 WHERE grp = ?", (group,))
        con.execute("UPDATE taps SET progress = 'in_progress', answer = ?, owner_pid = ?, "
                    "owner_start = ?, owner_at = ? WHERE hash = ?",
                    (_canonical(body), os.getpid(), _start_time(os.getpid()) or "", now,
                     _hash(token)))
        con.execute("UPDATE cards SET answered_kind = ? WHERE grp = ?", (kind, group))
        return AnswerTap(_hash(token), kind, p, group, body, None)


def claim_answer(tap_hash: str, now: float) -> AnswerTap | None:
    """One owner per answer, across processes: the posting, the relay and the
    settlement of an answer are done by the one process holding its claim. Returns the answer,
    re-read under the claim — settled (its receipt set: nothing more may be posted) or in
    progress and now owned by this process — or None while another live process holds it
    (that process's pid and start time, within `LEASE_MAX_S`)."""
    me, mine = os.getpid(), _start_time(os.getpid()) or ""
    with _tx() as con:
        row = con.execute("SELECT progress, owner_pid, owner_start, owner_at FROM taps "
                          "WHERE hash = ?", (tap_hash,)).fetchone()
        if row is None:
            return None
        progress, pid, start, at = row
        if progress == "in_progress" and pid is not None and (pid, start) != (me, mine) \
                and start and now - (at or 0) < LEASE_MAX_S and _start_time(pid) == start:
            return None                               # another live process owns it
        if progress == "in_progress":
            con.execute("UPDATE taps SET owner_pid = ?, owner_start = ?, owner_at = ? "
                        "WHERE hash = ?", (me, mine, now, tap_hash))
        return _answer_of(con.execute("SELECT hash, grp, kind, payload, answer, receipt "
                                      "FROM taps WHERE hash = ?", (tap_hash,)).fetchone())


def release_answer(tap_hash: str) -> None:
    """This process stops handling an answer it could not finish (it stays in progress)."""
    with _tx() as con:
        con.execute("UPDATE taps SET owner_pid = NULL, owner_start = NULL, owner_at = NULL "
                    "WHERE hash = ? AND owner_pid = ? AND progress = 'in_progress'",
                    (tap_hash, os.getpid()))


def finish_answer(tap_hash: str, receipt: str, *, card_stays_open: bool = False) -> str:
    """Settle an answer with its receipt (returned for every later press of its card) and
    return the receipt that stands: the FIRST one settled. A second process finishing the same
    answer (two resumes racing) changes nothing and gets the first receipt back.
    ``card_stays_open``: the app took no answer (the operator's live view was open), so the
    question still stands; its card is retired as superseded, not answered, and a new card
    is posted for it."""
    with _tx() as con:
        cur = con.execute("UPDATE taps SET progress = 'done', receipt = ?, settled_at = ?, "
                          "owner_pid = NULL, owner_start = NULL, owner_at = NULL WHERE hash = ? "
                          "AND progress = 'in_progress'", (receipt, time.time(), tap_hash))
        if cur.rowcount == 0:
            row = con.execute("SELECT receipt FROM taps WHERE hash = ?", (tap_hash,)).fetchone()
            return row[0] if row is not None and row[0] is not None else receipt
        if card_stays_open:
            con.execute("UPDATE cards SET answered_kind = NULL, voided = 1, voided_at = ?, "
                        "void_reason = 'superseded' WHERE grp = (SELECT grp FROM taps "
                        "WHERE hash = ?)", (time.time(), tap_hash))
    return receipt


def last_settled_answer(run_id: str) -> SettledAnswer | None:
    """The run's newest answer settled with a receipt (its `taps` row, by its card's order)."""
    con = _connect()
    try:
        row = con.execute("SELECT t.kind, t.payload, t.receipt, t.settled_at "
                          "FROM taps t JOIN cards c ON c.grp = t.grp WHERE c.run_id = ? "
                          "AND t.progress = 'done' ORDER BY c.seq DESC LIMIT 1",
                          (run_id,)).fetchone()
    finally:
        con.close()
    return None if row is None else SettledAnswer(row[0], json.loads(row[1]), row[2], row[3])


def answers_in_progress(run_id: str) -> list[AnswerTap]:
    """The run's answers recorded ``in_progress`` (their app reply was lost or not taken)."""
    con = _connect()
    try:
        rows = con.execute("SELECT t.hash, t.grp, t.kind, t.payload, t.answer, t.receipt "
                           "FROM taps t JOIN cards c ON c.grp = t.grp WHERE c.run_id = ? "
                           "AND t.progress = 'in_progress' ORDER BY c.seq", (run_id,)).fetchall()
    finally:
        con.close()
    return [_answer_of(r) for r in rows]


def redeem_live(token, now: float) -> TapRecord | None:
    """[Open live view] on a question card: a NON-answer redemption. The token must
    be a live, unexpired `q_live` token of a card neither answered nor voided; nothing is
    marked used, so the card's other buttons stay valid and the button works again."""
    if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_CHARS:
        return None
    con = _connect()
    try:
        row = con.execute("SELECT grp, kind, payload, expires_at, used FROM taps WHERE hash = ?",
                          (_hash(token),)).fetchone()
        if row is None:
            return None
        group, kind, payload, expires_at, used = row
        if kind != "q_live" or used or expires_at <= now:
            return None
        card = con.execute("SELECT answered_kind, voided FROM cards WHERE grp = ?",
                           (group,)).fetchone()
    finally:
        con.close()
    if card is None or card[0] is not None or card[1]:
        return None
    return TapRecord(kind, json.loads(payload), group)


def next_live_n(question_id: str) -> int:
    """The number of the operator's next live view from this question (the app's request
    identity for it, with the question and its revision)."""
    with _tx() as con:
        con.execute("INSERT INTO question_live (question_id, n) VALUES (?, 1) "
                    "ON CONFLICT(question_id) DO UPDATE SET n = n + 1", (question_id,))
        return con.execute("SELECT n FROM question_live WHERE question_id = ?",
                           (question_id,)).fetchone()[0]


def put_question_shot(question_id: str, revision: int, screenshot_id: str,
                      reference: str) -> None:
    """Which delivery carried this question revision's screenshot."""
    with _tx() as con:
        con.execute("INSERT INTO question_shots (question_id, revision, screenshot_id, reference) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(question_id, revision, screenshot_id) DO "
                    "UPDATE SET reference = excluded.reference",
                    (question_id, revision, screenshot_id, reference))


def question_shot_status(question_id: str, revision: int, screenshot_id: str) -> str | None:
    """What Casa said became of exactly this question revision's screenshot: `delivered`,
    `withheld`, `pending`, `unknown`, or None when it was never sent."""
    con = _connect()
    try:
        row = con.execute("SELECT d.status FROM question_shots q JOIN deliveries d "
                          "ON d.reference = q.reference WHERE q.question_id = ? AND "
                          "q.revision = ? AND q.screenshot_id = ?",
                          (question_id, revision, screenshot_id)).fetchone()
    finally:
        con.close()
    return None if row is None else row[0]


def question_cards(run_id: str, question_id: str) -> list[CardGroup]:
    """Every card ever minted for this question, oldest first."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT c.grp, c.run_id, c.minted_at, c.expires_at, c.purpose, c.seq "
            "FROM cards c JOIN taps t ON t.grp = c.grp WHERE c.run_id = ? AND "
            "c.purpose = 'question' AND json_extract(t.payload, '$.question_id') = ? "
            "ORDER BY c.seq", (run_id, question_id)).fetchall()
    finally:
        con.close()
    return [CardGroup(*r[:5]) for r in rows]


def card_question(group: str) -> str | None:
    """The question id a question card asks about."""
    con = _connect()
    try:
        row = con.execute("SELECT json_extract(payload, '$.question_id') FROM taps "
                          "WHERE grp = ? LIMIT 1", (group,)).fetchone()
    finally:
        con.close()
    return None if row is None else row[0]


VOID_REASONS = ("superseded", "withheld", "undelivered")


def void_group(group: str, reason: str = "superseded") -> None:
    """Every unused token of the card stops working: a newer question
    superseded it, Casa withheld it, or it was never delivered."""
    if reason not in VOID_REASONS:
        raise ValueError("unknown void reason")
    with _tx() as con:
        con.execute("UPDATE taps SET used = 1 WHERE grp = ?", (group,))
        con.execute("UPDATE cards SET voided = 1, voided_at = ?, void_reason = ? "
                    "WHERE grp = ? AND answered_kind IS NULL AND voided = 0",
                    (time.time(), reason, group))


def _latest_card(con, run_id: str, purpose: str | None):
    if purpose is None:
        return con.execute("SELECT grp, run_id, minted_at, expires_at, purpose, "
                           "answered_kind, voided FROM cards WHERE run_id = ? "
                           "ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
    return con.execute("SELECT grp, run_id, minted_at, expires_at, purpose, answered_kind, "
                       "voided FROM cards WHERE run_id = ? AND purpose = ? "
                       "ORDER BY seq DESC LIMIT 1", (run_id, purpose)).fetchone()


def open_card(run_id: str, now: float, purpose: str | None = None) -> CardGroup | None:
    """The run's latest card (of `purpose`, when given), when it is unexpired
    and unanswered."""
    con = _connect()
    try:
        row = _latest_card(con, run_id, purpose)
    finally:
        con.close()
    if row is None or row[5] is not None or row[6] or row[3] <= now:
        return None
    return CardGroup(*row[:5])


def expired_card(run_id: str, now: float, purpose: str | None = None) -> CardGroup | None:
    """The run's latest card (of `purpose`, when given), when it expired
    unanswered."""
    con = _connect()
    try:
        row = _latest_card(con, run_id, purpose)
    finally:
        con.close()
    if row is None or row[5] is not None or row[6] or row[3] > now:
        return None
    return CardGroup(*row[:5])


def void_run_cards(run_id: str, purpose: str) -> list[str]:
    """Void every unanswered card of `purpose` for the run (a newer question
    supersedes them); returns their groups."""
    with _tx() as con:
        groups = [r[0] for r in con.execute(
            "SELECT grp FROM cards WHERE run_id = ? AND purpose = ? AND answered_kind IS NULL "
            "AND voided = 0", (run_id, purpose)).fetchall()]
        for g in groups:
            con.execute("UPDATE taps SET used = 1 WHERE grp = ?", (g,))
            con.execute("UPDATE cards SET voided = 1, voided_at = ?, void_reason = 'superseded' "
                        "WHERE grp = ?", (time.time(), g))
    return groups


class CardState(NamedTuple):
    answered_kind: str | None
    voided: bool
    voided_at: float | None
    void_reason: str | None
    expires_at: float


def card_state(group: str) -> CardState | None:
    con = _connect()
    try:
        row = con.execute("SELECT answered_kind, voided, voided_at, void_reason, expires_at "
                          "FROM cards WHERE grp = ?", (group,)).fetchone()
    finally:
        con.close()
    return None if row is None else CardState(row[0], bool(row[1]), row[2], row[3], row[4])


def went_unanswered(group: str, now: float) -> bool:
    """The operator had the card for its whole hour and did not answer: Casa
    confirmed its delivery, and it expired unanswered, or a newer card
    superseded it only after it had expired. A card Casa withheld, or whose
    delivery was never confirmed, did not go unanswered."""
    s = card_state(group)
    if s is None or s.answered_kind is not None or s.expires_at > now:
        return False
    if card_delivery_status(group) != "delivered":
        return False                     # the operator never had it
    if not s.voided:
        return True
    return s.void_reason == "superseded" and (s.voided_at or 0) >= s.expires_at


# -- run intents --------------------------------------------------------------------

def put_intent(request_id: str, binding_ref: str, requester_id: str, requester_kind: str,
               request_group: str, position: int, spec: dict) -> None:
    """Record a run about to be created (before its `POST /v1/runs`). An
    intent already recorded for the request id is kept as it was."""
    with _tx() as con:
        con.execute("INSERT OR IGNORE INTO intents (request_id, binding_ref, requester_id, "
                    "requester_kind, request_group, position, spec, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (request_id, binding_ref, requester_id, requester_kind, request_group,
                     int(position), _canonical(spec), time.time()))


def attach_run(request_id: str, run_id: str) -> bool:
    """Bind the intent to the app's run id; True when it is (now) bound to
    exactly that run. An intent is never re-pointed at another run."""
    with _tx() as con:
        row = con.execute("SELECT run_id FROM intents WHERE request_id = ?",
                          (request_id,)).fetchone()
        if row is None:
            return False
        if row[0] is None:
            taken = con.execute("SELECT 1 FROM intents WHERE run_id = ?",
                                (run_id,)).fetchone()
            if taken:
                return False
            con.execute("UPDATE intents SET run_id = ? WHERE request_id = ?",
                        (run_id, request_id))
            return True
        return row[0] == run_id


def put_run(request_id: str, run_id: str, binding_ref: str, requester_id: str,
            requester_kind: str, request_group: str, position: int, spec: dict) -> bool:
    put_intent(request_id, binding_ref, requester_id, requester_kind, request_group,
               position, spec)
    return attach_run(request_id, run_id)


def _binding(row) -> RunBinding:
    run_id, request_id, binding_ref, requester_id, kind, group, position, spec = row
    return RunBinding(run_id, request_id, binding_ref, requester_id, kind, group,
                      position, json.loads(spec))


_BINDING_COLS = ("run_id, request_id, binding_ref, requester_id, requester_kind, "
                 "request_group, position, spec")


def run_binding(run_id: str) -> RunBinding | None:
    con = _connect()
    try:
        row = con.execute(f"SELECT {_BINDING_COLS} FROM intents WHERE run_id = ?",
                          (run_id,)).fetchone()
    finally:
        con.close()
    return None if row is None else _binding(row)


def intent(request_id: str) -> RunBinding | None:
    con = _connect()
    try:
        row = con.execute(f"SELECT {_BINDING_COLS} FROM intents WHERE request_id = ?",
                          (request_id,)).fetchone()
    finally:
        con.close()
    return None if row is None else _binding(row)


def next_in_group(request_group: str) -> str | None:
    """The run reference to continue next in a batch: the lowest position not
    terminal in plugin state — its run id once attached, else
    `intent:<request_id>` (its POST never returned). None for a request the operator stopped (its
    seal)."""
    con = _connect()
    try:
        if con.execute("SELECT 1 FROM stops WHERE request_key = ?",
                       (request_group,)).fetchone() is not None:
            return None
        row = con.execute("SELECT request_id, run_id FROM intents WHERE request_group = ? "
                          "AND terminal = 0 ORDER BY position, created_at LIMIT 1",
                          (request_group,)).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return row[1] if row[1] is not None else f"intent:{row[0]}"


def binding_runs(binding_ref: str) -> list[RunBinding]:
    """The non-terminal runs and intents recorded under a binding, in order."""
    con = _connect()
    try:
        rows = con.execute(f"SELECT {_BINDING_COLS} FROM intents WHERE binding_ref = ? "
                           "AND terminal = 0 ORDER BY created_at, position",
                           (binding_ref,)).fetchall()
    finally:
        con.close()
    return [_binding(r) for r in rows]


def is_terminal(run_id: str) -> bool:
    con = _connect()
    try:
        row = con.execute("SELECT terminal FROM intents WHERE run_id = ?",
                          (run_id,)).fetchone()
    finally:
        con.close()
    return bool(row and row[0])


def _answer_ended(con, run_id: str) -> None:
    """v0.1.27: the run ended, by whatever path: its answer's finish is no longer owed."""
    con.execute("UPDATE answered SET finished_at = ? WHERE run_id = ? AND finished_at IS NULL",
                (time.time(), run_id))


def mark_terminal(run_id: str, *, owe_ending_at: float | None = None) -> None:
    """The run ended; with `owe_ending_at`, a telling of its end is owed, recorded in
    the same transaction."""
    with _tx() as con:
        con.execute("UPDATE intents SET terminal = 1 WHERE run_id = ?", (run_id,))
        _answer_ended(con, run_id)
        if owe_ending_at is not None:
            con.execute("INSERT OR IGNORE INTO ended_owed (run_id, at) VALUES (?, ?)",
                        (run_id, owe_ending_at))


# -- admission requests ----------------------------------------------------------------

def put_pending_admission(request_key: str, binding_ref: str, requester_id: str,
                          requester_kind: str, specs: list, request_ids: list,
                          digests: list) -> None:
    """Record a `start_runs` request before Casa is asked (status
    `requested`). A key already recorded is kept as it was."""
    with _tx() as con:
        con.execute("INSERT OR IGNORE INTO admissions (request_key, binding_ref, "
                    "requester_id, requester_kind, specs, request_ids, digests, status, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', ?)",
                    (request_key, binding_ref, requester_id, requester_kind,
                     json.dumps([_canonical_obj(s) for s in specs]),
                     json.dumps(list(request_ids)), json.dumps(list(digests)), time.time()))


def _canonical_obj(spec: dict) -> dict:
    return json.loads(_canonical(spec))


def set_confirmation(request_key: str, confirmation_id: str) -> None:
    """Casa posted a confirmation card: `requested` → `pending`, recording its
    id. A row that has moved on keeps its status; an id is recorded once."""
    with _tx() as con:
        con.execute("UPDATE admissions SET confirmation_id = COALESCE(confirmation_id, ?) "
                    "WHERE request_key = ? AND NOT EXISTS (SELECT 1 FROM admissions "
                    "WHERE confirmation_id = ? AND request_key != ?)",
                    (confirmation_id, request_key, confirmation_id, request_key))
        con.execute("UPDATE admissions SET status = 'pending' WHERE request_key = ? "
                    "AND status = 'requested'", (request_key,))


def set_admitted(request_key: str, admissions: list) -> None:
    """Casa's admissions arrived: `requested`/`pending` → `admitted`.
    A row already admitted, submitting or finished is not changed."""
    with _tx() as con:
        con.execute("UPDATE admissions SET status = 'admitted', admissions = ? "
                    "WHERE request_key = ? AND status IN ('requested', 'pending')",
                    (json.dumps(admissions), request_key))


def by_confirmation(confirmation_id: str, request_key: str | None = None) -> str | None:
    """The request key whose confirmation this is. When no row recorded the
    id, the `request_key` Casa's answer carries is used — and the id recorded
    on that row, if it has none."""
    with _tx() as con:
        row = con.execute("SELECT request_key FROM admissions WHERE confirmation_id = ?",
                          (confirmation_id,)).fetchone()
        if row is not None:
            return row[0]
        if request_key is None:
            return None
        row = con.execute("SELECT request_key, confirmation_id FROM admissions "
                          "WHERE request_key = ?", (request_key,)).fetchone()
        if row is None:
            return None
        if row[1] is None:
            con.execute("UPDATE admissions SET confirmation_id = ? WHERE request_key = ?",
                        (confirmation_id, request_key))
        return row[0]


_ADM_COLS = ("request_key, binding_ref, requester_id, requester_kind, specs, request_ids, "
             "digests, status, confirmation_id, admissions")


def _pending(row) -> PendingAdmission:
    key, b, rid, kind, specs, ids, digests, status, cid, adm = row
    return PendingAdmission(key, b, rid, kind, json.loads(specs), json.loads(ids),
                            json.loads(digests), status, cid,
                            None if adm is None else json.loads(adm))


def admission(request_key: str) -> PendingAdmission | None:
    con = _connect()
    try:
        row = con.execute(f"SELECT {_ADM_COLS} FROM admissions WHERE request_key = ?",
                          (request_key,)).fetchone()
    finally:
        con.close()
    return None if row is None else _pending(row)


def claim_pending_admission(request_key: str, *, now: float | None = None
                            ) -> PendingAdmission | None:
    """Take the admitted request for submission: `admitted` → `submitting`,
    once. A `submitting` row claimed more than 120 s ago (its submitter died)
    can be claimed again; it keeps its request ids."""
    now = time.time() if now is None else now
    with _tx() as con:
        row = con.execute(f"SELECT {_ADM_COLS}, claimed_at FROM admissions "
                          "WHERE request_key = ?", (request_key,)).fetchone()
        if row is None:
            return None
        status, claimed_at = row[7], row[10]
        if status == "admitted" or (status == "submitting" and claimed_at is not None
                                    and now - claimed_at > CLAIM_STALE_S):
            con.execute("UPDATE admissions SET status = 'submitting', claimed_at = ? "
                        "WHERE request_key = ?", (now, request_key))
            return _pending(row[:7] + ("submitting",) + row[8:10])
    return None


def finish_pending_admission(request_key: str, outcome: str) -> None:
    """Record how the request ended; a request that already ended keeps its
    first outcome."""
    if outcome not in ADMISSION_OUTCOMES:
        raise ValueError("unknown outcome")
    with _tx() as con:
        con.execute("UPDATE admissions SET status = ? WHERE request_key = ? "
                    "AND status NOT IN ('submitted', 'declined', 'expired', 'refused')",
                    (outcome, request_key))


# -- files ------------------------------------------------------------------------------

class FileMeta(NamedTuple):
    file_id: str
    run_id: str
    safe_name: str
    mime: str
    size: int


def put_file(file_id: str, run_id: str, safe_name: str | None = None, mime: str | None = None,
             size: int | None = None) -> None:
    """Record a file the app issued to a run (its first run is kept)."""
    with _tx() as con:
        con.execute("INSERT OR IGNORE INTO files (file_id, run_id, safe_name, mime, size) "
                    "VALUES (?, ?, ?, ?, ?)", (file_id, run_id, safe_name, mime, size))


def file_meta(file_id: str) -> FileMeta | None:
    con = _connect()
    try:
        row = con.execute("SELECT file_id, run_id, safe_name, mime, size FROM files "
                          "WHERE file_id = ?", (file_id,)).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return FileMeta(row[0], row[1], row[2] or row[0], row[3] or "", int(row[4] or 0))


def take_beat(run_id: str, now: float, min_gap_s: float) -> int | None:
    """The next beat number for the run, or None when the last beat was less
    than `min_gap_s` ago."""
    with _tx() as con:
        row = con.execute("SELECT n, at FROM beats WHERE run_id = ?", (run_id,)).fetchone()
        if row is not None and now - row[1] < min_gap_s:
            return None
        n = 1 if row is None else row[0] + 1
        con.execute("INSERT OR REPLACE INTO beats (run_id, n, at) VALUES (?, ?, ?)",
                    (run_id, n, now))
        return n


def mark_reported(reference: str, outcome: str) -> None:
    with _tx() as con:
        con.execute("UPDATE deliveries SET reported = ? WHERE reference = ?",
                    (outcome, reference))


APP_OUTCOME = {"file": None, "handoff": "handed_off"}


def unreported_settled_files() -> list[tuple[DeliveryRow, str]]:
    """File attempts and handoffs whose final outcome the app has not been
    told yet, with that outcome (`delivered`, `withheld`, `handed_off`)."""
    con = _connect()
    try:
        rows = con.execute(f"SELECT {_DEL_COLS}, reported FROM deliveries "
                           "WHERE (kind = 'file' AND status IN ('delivered', 'withheld') "
                           "AND (reported IS NULL OR reported != status)) OR (kind = 'handoff' "
                           "AND (reported IS NULL OR reported != 'handed_off')) ORDER BY seq"
                           ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        d = _delivery(r[:9])
        out.append((d, APP_OUTCOME[d.kind] or d.status))
    return out


def file_run(file_id: str) -> str | None:
    con = _connect()
    try:
        row = con.execute("SELECT run_id FROM files WHERE file_id = ?",
                          (file_id,)).fetchone()
    finally:
        con.close()
    return None if row is None else row[0]


# -- driving leases ------------------------------------------------------------------------

def _start_time(pid: int) -> str | None:
    """A running process's start time (field 22 of /proc/<pid>/stat), which
    tells a live process from a later one that reused its pid; None for a
    process that is gone or has exited (a zombie)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("ascii", "replace")
    except OSError:
        return None
    fields = raw[raw.rfind(")") + 2:].split()
    if len(fields) <= 19 or fields[0] in ("Z", "X"):     # exited, reaped or not
        return None
    return fields[19]


def take_lease(run_id: str) -> None:
    pid = os.getpid()
    start = _start_time(pid) or ""
    with _tx() as con:
        con.execute("INSERT OR REPLACE INTO leases (run_id, pid, start_time, taken_at) "
                    "VALUES (?, ?, ?, ?)", (run_id, pid, start, time.time()))


# -- wakes and touches ------------------------------------------------------------
# A continuation records its wake: when its turn started and the run's last step number then.
# A step Fido sends to the app, an operator's tap on one of the run's cards and a resumed
# operator's answer touch the run (the time only grows). The next continuation compares: no
# touch since the previous wake's turn started and no app step after its number means a whole
# turn passed with the run running and nothing happening on it.

def count_call(run_id: str) -> int:
    """One more tool call of this run; the run's count over all its turns (the loop catch)."""
    with _tx() as con:
        con.execute("INSERT INTO run_calls (run_id, n) VALUES (?, 1) ON CONFLICT(run_id) "
                    "DO UPDATE SET n = n + 1", (run_id,))
        return con.execute("SELECT n FROM run_calls WHERE run_id = ?", (run_id,)).fetchone()[0]


def touch_run(run_id: str) -> None:
    now = time.time()
    with _tx() as con:
        con.execute("INSERT INTO touches (run_id, at) VALUES (?, ?) ON CONFLICT(run_id) "
                    "DO UPDATE SET at = max(at, excluded.at)", (run_id, now))


def touched_since(run_id: str, since: float) -> bool:
    con = _connect()
    try:
        row = con.execute("SELECT at FROM touches WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        con.close()
    return row is not None and row[0] >= since


def record_wake(run_id: str, turn_start: float, seq: int) -> None:
    with _tx() as con:
        con.execute("INSERT OR REPLACE INTO wakes (run_id, turn_start, seq) VALUES (?, ?, ?)",
                    (run_id, turn_start, seq))


def last_wake(run_id: str) -> tuple[float, int] | None:
    """The previous continuation's (turn start, step number), or None."""
    con = _connect()
    try:
        row = con.execute("SELECT turn_start, seq FROM wakes WHERE run_id = ?",
                          (run_id,)).fetchone()
    finally:
        con.close()
    return None if row is None else (row[0], row[1])


def drop_wake(run_id: str) -> None:
    with _tx() as con:
        con.execute("DELETE FROM wakes WHERE run_id = ?", (run_id,))


def lease_live(run_id: str, now: float) -> bool:
    """True while the process that took the run's lease is still running
    (same pid, same start time) and the lease is under 660 s old."""
    con = _connect()
    try:
        row = con.execute("SELECT pid, start_time, taken_at FROM leases WHERE run_id = ?",
                          (run_id,)).fetchone()
    finally:
        con.close()
    if row is None:
        return False
    pid, start, taken_at = row
    if not start or now - taken_at >= LEASE_MAX_S:
        return False
    return _start_time(pid) == start


# -- deliveries --------------------------------------------------------------------------------

_DEL_COLS = "reference, kind, run_id, file_id, notice_seq, grp, status, current, deposited_at"


def _delivery(row) -> DeliveryRow:
    return DeliveryRow(*row[:7], bool(row[7]), float(row[8]))


def put_delivery(reference: str, kind: str, run_id: str | None, file_id: str | None,
                 notice_seq: int | None, group: str | None) -> None:
    """Record a deposit Casa accepted. For a file, this deposit becomes the
    one current attempt for (run, file); earlier ones stay only as history."""
    if kind not in DELIVERY_KINDS:
        raise ValueError("unknown delivery kind")
    with _tx() as con:
        _put_delivery(con, reference, kind, run_id, file_id, notice_seq, group)


def _put_delivery(con, reference: str, kind: str, run_id: str | None, file_id: str | None,
                  notice_seq: int | None, group: str | None) -> None:
    if kind == "file":
        con.execute("UPDATE deliveries SET current = 0 WHERE kind = 'file' "
                    "AND run_id IS ? AND file_id IS ? AND reference != ?",
                    (run_id, file_id, reference))
    con.execute(f"INSERT INTO deliveries ({_DEL_COLS}, seq) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?) "
                "ON CONFLICT(reference) DO UPDATE SET current = 1",
                (reference, kind, run_id, file_id, notice_seq, group, time.time(),
                 _next_seq(con, "deliveries")))


# -- v0.1.27: a run's answer, posted by show_sources -------------------------------------

class Answered(NamedTuple):
    run_id: str
    reference: str
    finish: dict              # the run_finish body plugin code posts for it
    finished_at: float | None


def record_answer(reference: str, run_id: str, finish: dict, *, ended: bool) -> None:
    """The sources deposit and the run's `answered` record, in one transaction: from here the
    run's finish is owed by plugin code (show_sources, else the sweep's retry), never by a later
    model call. `ended`: the run had already ended, so nothing is owed."""
    with _tx() as con:
        _put_delivery(con, reference, "sources", run_id, None, None, None)
        now = time.time()
        con.execute("INSERT INTO answered (run_id, reference, finish, created_at, finished_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_id, reference, json.dumps(finish), now, now if ended else None))


def answer_of(run_id: str) -> Answered | None:
    con = _connect()
    try:
        row = con.execute("SELECT run_id, reference, finish, finished_at FROM answered "
                          "WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        con.close()
    return None if row is None else Answered(row[0], row[1], json.loads(row[2]), row[3])


def answered_runs() -> set[str]:
    con = _connect()
    try:
        return {r[0] for r in con.execute("SELECT run_id FROM answered")}
    finally:
        con.close()


def unfinished_answers() -> list[Answered]:
    """The answered runs whose finish is still owed, oldest first."""
    con = _connect()
    try:
        rows = con.execute("SELECT run_id, reference, finish, finished_at FROM answered "
                           "WHERE finished_at IS NULL ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [Answered(r[0], r[1], json.loads(r[2]), r[3]) for r in rows]


def recorded_status(reference: str) -> str | None:
    """What this plugin last recorded of a deposit's outcome (`pending` until settled)."""
    con = _connect()
    try:
        row = con.execute("SELECT status FROM deliveries WHERE reference = ?",
                          (reference,)).fetchone()
    finally:
        con.close()
    return None if row is None else row[0]


def settle_delivery(reference: str, status: str) -> None:
    """Record what Casa says became of a deposit. A `delivered` notice moves
    the notice cursor up to the last notice it covered (never back)."""
    if status not in DELIVERY_STATUSES:
        raise ValueError("unknown delivery status")
    with _tx() as con:
        row = con.execute("SELECT kind, notice_seq FROM deliveries WHERE reference = ?",
                          (reference,)).fetchone()
        if row is None:
            return
        con.execute("UPDATE deliveries SET status = ? WHERE reference = ?",
                    (status, reference))
        if status == "delivered" and row[0] == "notice" and row[1] is not None:
            con.execute("INSERT INTO meta (name, value) VALUES ('notice_cursor', ?) "
                        "ON CONFLICT(name) DO UPDATE SET value = MAX(value, excluded.value)",
                        (int(row[1]),))
            con.execute("UPDATE notice_batches SET delivered = 1 WHERE last_seq <= ?",
                        (int(row[1]),))


def pending_deliveries() -> list[DeliveryRow]:
    """Current Casa deposits whose outcome is not settled (`pending`,
    `unknown`); handoffs are not Casa deposits and are never pending."""
    con = _connect()
    try:
        rows = con.execute(f"SELECT {_DEL_COLS} FROM deliveries WHERE current = 1 "
                           "AND kind != 'handoff' "
                           "AND status NOT IN ('delivered', 'withheld') ORDER BY seq"
                           ).fetchall()
    finally:
        con.close()
    return [_delivery(r) for r in rows]


def file_delivery(run_id: str, file_id: str) -> DeliveryRow | None:
    con = _connect()
    try:
        row = con.execute(f"SELECT {_DEL_COLS} FROM deliveries WHERE kind = 'file' "
                          "AND run_id = ? AND file_id = ? AND current = 1",
                          (run_id, file_id)).fetchone()
    finally:
        con.close()
    return None if row is None else _delivery(row)


def run_file_deliveries(run_id: str) -> list[DeliveryRow]:
    con = _connect()
    try:
        rows = con.execute(f"SELECT {_DEL_COLS} FROM deliveries WHERE kind = 'file' "
                           "AND run_id = ? AND current = 1 ORDER BY seq", (run_id,)).fetchall()
    finally:
        con.close()
    return [_delivery(r) for r in rows]


def failed_file_attempts(binding_ref: str | None = None) -> list[tuple[DeliveryRow, bool]]:
    """Current file attempts that did not reach the operator (`withheld`) or
    whose outcome is lost (`unknown`), with whether a tool result has named
    them yet — for one binding's runs, or all runs."""
    con = _connect()
    try:
        rows = con.execute(
            f"SELECT {', '.join('d.' + c.strip() for c in _DEL_COLS.split(','))}, d.told "
            "FROM deliveries d JOIN intents i ON i.run_id = d.run_id "
            "WHERE d.kind = 'file' AND d.current = 1 AND d.status IN ('withheld', 'unknown') "
            "AND (? IS NULL OR i.binding_ref = ?) ORDER BY d.seq",
            (binding_ref, binding_ref)).fetchall()
    finally:
        con.close()
    return [(_delivery(r[:9]), bool(r[9])) for r in rows]


def mark_told(references: list[str]) -> None:
    with _tx() as con:
        con.executemany("UPDATE deliveries SET told = 1 WHERE reference = ?",
                        [(r,) for r in references])


def put_handoff(handoff_id: str, run_id: str, file_id: str, path: str) -> None:
    """A file published to the handoff folder: durable, delivered, recorded
    before the app is told, so a retry never publishes it again."""
    with _tx() as con:
        con.execute(f"INSERT OR IGNORE INTO deliveries ({_DEL_COLS}, seq, detail) "
                    "VALUES (?, 'handoff', ?, ?, NULL, NULL, 'delivered', 1, ?, ?, ?)",
                    (handoff_id, run_id, file_id, time.time(),
                     _next_seq(con, "deliveries"), path))


def handoff_of(run_id: str, file_id: str) -> tuple[DeliveryRow, str] | None:
    """The file's handoff record and its path, if it was handed off."""
    con = _connect()
    try:
        row = con.execute(f"SELECT {_DEL_COLS}, detail FROM deliveries WHERE kind = 'handoff' "
                          "AND run_id = ? AND file_id = ? ORDER BY seq LIMIT 1",
                          (run_id, file_id)).fetchone()
    finally:
        con.close()
    return None if row is None else (_delivery(row[:9]), row[9])


def latest_link(run_id: str) -> DeliveryRow | None:
    con = _connect()
    try:
        row = con.execute(f"SELECT {_DEL_COLS} FROM deliveries WHERE kind IN ('link', 'qlive') "
                          "AND run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
    finally:
        con.close()
    return None if row is None else _delivery(row)


class NoticeBatch(NamedTuple):
    key: str
    body: str
    first_seq: int
    last_seq: int
    count: int


def open_notice_batch() -> NoticeBatch | None:
    """The notice post not yet delivered, if any: it is offered again exactly
    as it was (same key, same body) until Casa says it was delivered."""
    con = _connect()
    try:
        row = con.execute("SELECT key, body, first_seq, last_seq, count FROM notice_batches "
                          "WHERE delivered = 0 ORDER BY first_seq LIMIT 1").fetchone()
    finally:
        con.close()
    return None if row is None else NoticeBatch(*row)


#: A notice reaches the operator at Fido's next call; one older than this waited because no
#: browser task came, and reports a run the operator has moved past.
NOTICE_MAX_AGE_S = 24 * 3600


def form_notice_batch(notices: list[dict], header: str, max_body: int = 11_000,
                      now: float | None = None, repeats: str = " (×{n})"
                      ) -> NoticeBatch | None:
    """The notice post to offer now, decided in one transaction: the open
    (undelivered) batch if there is one, unchanged; otherwise a new batch of
    the given notices beyond every sequence any batch ever covered — so a
    notice belongs to at most one batch, whatever stale fetch a concurrent
    process brings. None when nothing is left to post.

    A notice older than `NOTICE_MAX_AGE_S` is covered but not
    shown, as is the interruption notice of a run this plugin holds; the rest are one line per distinct text, in order of first
    appearance, with `repeats` when a text came more than once. A batch whose
    notices are all stale is recorded as delivered with nothing posted (the
    floor and the cursor move past it) and None is returned."""
    now = time.time() if now is None else now
    with _tx() as con:
        row = con.execute("SELECT key, body, first_seq, last_seq, count FROM notice_batches "
                          "WHERE delivered = 0 ORDER BY first_seq LIMIT 1").fetchone()
        if row is not None:
            return NoticeBatch(*row)
        floor = con.execute("SELECT MAX(last_seq) FROM notice_batches").fetchone()[0] or 0
        cursor = con.execute("SELECT value FROM meta WHERE name = 'notice_cursor'").fetchone()
        floor = max(floor, cursor[0] if cursor else 0)
        fresh = sorted((n for n in notices if n["seq"] > floor), key=lambda n: n["seq"])
        if not fresh:
            return None
        def render(c: dict[str, int]) -> str:
            return header + "".join("\n- " + t + (repeats.format(n=k) if k > 1 else "")
                                    for t, k in c.items())
        counts: dict[str, int] = {}
        covered = []
        # an interruption of a run this plugin drives is told by Fido itself (the
        # turn that drove it, or the continuation the sweep owes it), promptly; the app's notice
        # of it would only come again at the next call, often hours later
        mine = {r[0] for r in con.execute("SELECT run_id FROM intents WHERE run_id IS NOT NULL")}
        for n in fresh:
            text = " ".join(n["text_masked"].split())
            stale = n.get("at") is not None and now - n["at"] > NOTICE_MAX_AGE_S
            told = n.get("kind") == "run_interrupted" and n.get("run_id") in mine
            if not stale and not told:
                grown = {**counts, text: counts.get(text, 0) + 1}
                if counts and len(render(grown)) > max_body:
                    break          # the rest form the next batch; nothing shown is ever cut
                counts = grown
            covered.append(n)
        key = f"notices-{covered[0]['seq']}-{covered[-1]['seq']}"
        if not counts:
            con.execute("INSERT INTO notice_batches (key, body, first_seq, last_seq, count, "
                        "delivered) VALUES (?, '', ?, ?, 0, 1)",
                        (key, covered[0]["seq"], covered[-1]["seq"]))
            con.execute("INSERT INTO meta (name, value) VALUES ('notice_cursor', ?) "
                        "ON CONFLICT(name) DO UPDATE SET value = MAX(value, excluded.value)",
                        (covered[-1]["seq"],))
            return None
        batch = NoticeBatch(key, render(counts), covered[0]["seq"], covered[-1]["seq"],
                            sum(counts.values()))
        con.execute("INSERT INTO notice_batches (key, body, first_seq, last_seq, count) "
                    "VALUES (?, ?, ?, ?, ?)", batch)
        return batch


def card_delivery_status(group: str) -> str | None:
    """What Casa said became of a card's deposit (`pending` until settled)."""
    row = card_delivery(group)
    return None if row is None else row.status


def card_delivery(group: str) -> DeliveryRow | None:
    """A card's latest deposit, with its reference and what Casa said became of it."""
    con = _connect()
    try:
        row = con.execute(f"SELECT {_DEL_COLS} FROM deliveries WHERE kind IN ('card', 'qlive') "
                          "AND grp = ? ORDER BY seq DESC LIMIT 1", (group,)).fetchone()
    finally:
        con.close()
    return None if row is None else _delivery(row)


# -- a telling owed for a run the app ended while nothing drove it ------------------

def owed_endings(since: float) -> list[str]:
    """The runs whose ending is still owed, recorded after `since`; older ones lapse."""
    with _tx() as con:
        con.execute("DELETE FROM ended_owed WHERE at <= ?", (since,))
        return [r[0] for r in con.execute("SELECT run_id FROM ended_owed ORDER BY at")]


def take_owed_ending(run_id: str) -> bool:
    """Drop the run's owed telling; True when one was owed."""
    with _tx() as con:
        return con.execute("DELETE FROM ended_owed WHERE run_id = ?", (run_id,)).rowcount > 0


def heard_ending(run_id: str) -> None:
    """A turn's own tool call has just read that the run ended, so that turn tells
    whoever asked (or acts on it): the run is terminal and no continuation owes its ending, even
    when the sweep only notices after the turn has gone (or a stale
    "interrupted" is told after the retry the same turn started has succeeded)."""
    with _tx() as con:
        con.execute("UPDATE intents SET terminal = 1 WHERE run_id = ?", (run_id,))
        con.execute("DELETE FROM ended_owed WHERE run_id = ?", (run_id,))
        _answer_ended(con, run_id)


class IntentRow(NamedTuple):
    binding: RunBinding
    terminal: bool
    created_at: float


def all_intents() -> list[IntentRow]:
    con = _connect()
    try:
        rows = con.execute(f"SELECT {_BINDING_COLS}, terminal, created_at FROM intents "
                           "ORDER BY created_at, position").fetchall()
    finally:
        con.close()
    return [IntentRow(_binding(r[:8]), bool(r[8]), float(r[9])) for r in rows]


class AdmissionRow(NamedTuple):
    request_key: str
    binding_ref: str
    status: str
    created_at: float
    claimed_at: float | None
    swept_at: float | None


def open_admissions() -> list[AdmissionRow]:
    """Admission requests not yet ended (requested, pending, admitted, submitting)."""
    con = _connect()
    try:
        rows = con.execute("SELECT request_key, binding_ref, status, created_at, claimed_at, "
                           "swept_at FROM admissions WHERE status IN ('requested', 'pending', "
                           "'admitted', 'submitting') ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [AdmissionRow(*r) for r in rows]


def mark_swept(request_key: str, now: float) -> None:
    with _tx() as con:
        con.execute("UPDATE admissions SET swept_at = ? WHERE request_key = ?",
                    (now, request_key))


def notice_cursor() -> int:
    con = _connect()
    try:
        row = con.execute("SELECT value FROM meta WHERE name = 'notice_cursor'").fetchone()
    finally:
        con.close()
    return 0 if row is None else int(row[0])


# -- code holds (action model) -----------------------------------------------------------
# A run whose site emailed a sign-in code asks its requester for it (`request_login_code`) and
# holds: no sweep wakes it and a continuation turn ends at once, until one code is given with the
# request's token (or in the run's own turn) or the hold expires. The code itself is never here.

CODE_HOLD_S = 600.0


class CodeHold(NamedTuple):
    run_id: str
    site_id: str
    token: str
    requester_id: str
    requester_kind: str
    until: float
    redeemed: bool


def put_code_hold(run_id: str, site_id: str, token: str, requester_id: str,
                  requester_kind: str, now: float) -> None:
    with _tx() as con:
        con.execute("INSERT OR REPLACE INTO code_holds (run_id, site_id, token, requester_id, "
                    "requester_kind, until, redeemed) VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (run_id, site_id, token, requester_id, requester_kind, now + CODE_HOLD_S))


def code_hold(run_id: str) -> CodeHold | None:
    con = _connect()
    try:
        row = con.execute("SELECT run_id, site_id, token, requester_id, requester_kind, until, "
                          "redeemed FROM code_holds WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        con.close()
    return None if row is None else CodeHold(*row[:6], bool(row[6]))


def redeem_code_hold(run_id: str, token: str | None, requester_id: str | None,
                     requester_kind: str | None, now: float) -> CodeHold | None:
    """Mark the run's unexpired, unredeemed hold redeemed, in one statement: from another turn
    only with its token and its requester (`token` None: the run's own turn, which needs none).
    The hold, or None when nothing was redeemed."""
    with _tx() as con:
        if token is None:
            cur = con.execute("UPDATE code_holds SET redeemed = 1 WHERE run_id = ? "
                              "AND redeemed = 0", (run_id,))
        else:
            cur = con.execute("UPDATE code_holds SET redeemed = 1 WHERE run_id = ? AND token = ? "
                              "AND requester_id = ? AND requester_kind = ? AND redeemed = 0 "
                              "AND until > ?", (run_id, token, requester_id, requester_kind, now))
        if cur.rowcount != 1:
            return None
        row = con.execute("SELECT run_id, site_id, token, requester_id, requester_kind, until, "
                          "redeemed FROM code_holds WHERE run_id = ?", (run_id,)).fetchone()
    return CodeHold(*row[:6], bool(row[6]))


def drop_code_hold(run_id: str) -> None:
    with _tx() as con:
        con.execute("DELETE FROM code_holds WHERE run_id = ?", (run_id,))


def held_runs(now: float) -> set[str]:
    """The runs a code hold keeps from being woken (redeemed ones included, until dropped)."""
    con = _connect()
    try:
        rows = con.execute("SELECT run_id FROM code_holds WHERE until > ?", (now,)).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


# -- stop seals ---------------------------------------------------------------
# A request the operator stopped: written before the app is asked, so nothing of Fido's own drives,
# wakes or tells it again. Removed only when the app answers that no cancel of it won (the request
# had ended on its own). `settled`: every request id of it has been answered by the app's cancel.

def seal_request(request_key: str, now: float | None = None) -> bool:
    """Seal the request; True when this call wrote the seal. Touches no other row."""
    now = time.time() if now is None else now
    with _tx() as con:
        return con.execute("INSERT OR IGNORE INTO stops (request_key, at) VALUES (?, ?)",
                           (request_key, now)).rowcount == 1


def unseal_request(request_key: str) -> None:
    with _tx() as con:
        con.execute("DELETE FROM stops WHERE request_key = ?", (request_key,))


def settle_stop(request_key: str) -> None:
    with _tx() as con:
        con.execute("UPDATE stops SET settled = 1 WHERE request_key = ?", (request_key,))


def request_stopped(request_key: str) -> bool:
    con = _connect()
    try:
        row = con.execute("SELECT 1 FROM stops WHERE request_key = ?",
                          (request_key,)).fetchone()
    finally:
        con.close()
    return row is not None


def run_stopped(run_id: str) -> bool:
    """The run's request is sealed."""
    con = _connect()
    try:
        row = con.execute("SELECT 1 FROM intents i JOIN stops s ON s.request_key = "
                          "i.request_group WHERE i.run_id = ?", (run_id,)).fetchone()
    finally:
        con.close()
    return row is not None


def sealed_requests() -> set[str]:
    con = _connect()
    try:
        rows = con.execute("SELECT request_key FROM stops").fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


def unsettled_stops() -> list[str]:
    """The sealed requests whose cancel has not reached the app for every request id."""
    con = _connect()
    try:
        rows = con.execute("SELECT request_key FROM stops WHERE settled = 0 "
                           "ORDER BY at").fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def stop_settled(request_key: str) -> bool:
    con = _connect()
    try:
        row = con.execute("SELECT settled FROM stops WHERE request_key = ?",
                          (request_key,)).fetchone()
    finally:
        con.close()
    return bool(row and row[0])


def ended_requests(since: float, limit: int, now: float | None = None) -> list[tuple[str, str]]:
    """`(request_key, binding_ref)` of the requests asked for at or after *since* that ended on
    their own: every run created (submitted) and every one of them ended, not stopped by the operator (no
    seal). At most *limit*, those named longest ago first (never named first, then oldest), and
    each one returned is recorded as named *now*, so more than *limit* of them take turns
    across passes instead of the oldest crowding the rest out."""
    now = time.time() if now is None else now
    with _tx() as con:
        rows = con.execute(
            "SELECT a.request_key, a.binding_ref FROM admissions a "
            "LEFT JOIN ended_named n ON n.request_key = a.request_key "
            "WHERE a.status = 'submitted' AND a.created_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM stops s WHERE s.request_key = a.request_key) "
            "AND EXISTS (SELECT 1 FROM intents i WHERE i.request_group = a.request_key) "
            "AND NOT EXISTS (SELECT 1 FROM intents i WHERE i.request_group = a.request_key "
            "AND (i.run_id IS NULL OR i.terminal = 0)) "
            "ORDER BY COALESCE(n.at, 0), a.created_at LIMIT ?", (since, int(limit))).fetchall()
        con.executemany("INSERT INTO ended_named (request_key, at) VALUES (?, ?) "
                        "ON CONFLICT(request_key) DO UPDATE SET at = excluded.at",
                        [(r[0], now) for r in rows])
        con.execute("DELETE FROM ended_named WHERE at < ?", (since,))
        con.execute("DELETE FROM app_ended WHERE at < ?", (since,))
    return [(r[0], r[1]) for r in rows]


def request_runs(request_key: str) -> list[str]:
    """The app's run ids of the request's runs (its intents' attached runs)."""
    con = _connect()
    try:
        rows = con.execute("SELECT run_id FROM intents WHERE request_group = ? "
                           "AND run_id IS NOT NULL", (request_key,)).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def app_ended_runs(run_ids: list[str]) -> set[str]:
    """Those of *run_ids* the app itself was read to have ended (`record_app_ended`)."""
    if not run_ids:
        return set()
    con = _connect()
    try:
        marks = ",".join("?" * len(run_ids))
        rows = con.execute(f"SELECT run_id FROM app_ended WHERE run_id IN ({marks})",
                           list(run_ids)).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


def record_app_ended(run_id: str, now: float | None = None) -> None:
    """The app's `GET /v1/runs/{run}` read the run in a terminal state, which it never leaves."""
    with _tx() as con:
        con.execute("INSERT OR IGNORE INTO app_ended (run_id, at) VALUES (?, ?)",
                    (run_id, time.time() if now is None else now))


def admitted_requests() -> list[PendingAdmission]:
    """The requests admitted for running (admitted, submitting or submitted), oldest first."""
    con = _connect()
    try:
        rows = con.execute(f"SELECT {_ADM_COLS} FROM admissions WHERE status IN ('admitted', "
                           "'submitting', 'submitted') ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [_pending(r) for r in rows]

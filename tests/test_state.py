"""The plugin's durable state, shared by several plugin processes at once (a
Fido turn's server and a pinned tap turn's server): tap tokens, run intents,
admission requests, files, driving leases and deliveries.

Concurrency is reproduced with real processes (`spawn`), each with its own
SQLite connection, never with threads or mocks."""
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import subprocess
import sys
import time

import pytest

import state
from conftest import SERVER

SPAWN = multiprocessing.get_context("spawn")


@pytest.fixture(autouse=True)
def _data(plugin_data):
    return plugin_data


# -- workers (top level, so a spawned process can import them) ---------------

def _redeem_worker(data_dir, tokens, tool, barrier, out):
    os.environ["CLAUDE_PLUGIN_DATA"] = data_dir
    sys.path.insert(0, str(SERVER))
    import state as st
    barrier.wait()
    got = []
    for t in tokens:
        rec = st.redeem_tap(t, tool, time.time())
        got.append(None if rec is None else [rec.kind, rec.group])
    out.put(got)


def _claim_worker(data_dir, key, barrier, out):
    os.environ["CLAUDE_PLUGIN_DATA"] = data_dir
    sys.path.insert(0, str(SERVER))
    import state as st
    barrier.wait()
    out.put(st.claim_pending_admission(key) is not None)


def _run_pair(target, args_a, args_b):
    barrier = SPAWN.Barrier(2)
    out = SPAWN.Queue()
    procs = [SPAWN.Process(target=target, args=(*a, barrier, out)) for a in (args_a, args_b)]
    for p in procs:
        p.start()
    results = [out.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    return results


# -- tap tokens --------------------------------------------------------------

def test_tap_single_use_across_processes(plugin_data):
    tokens = [state.mint_tap("keep", {"run_id": "run1", "n": i}, f"g{i}")
              for i in range(50)]
    a, b = _run_pair(_redeem_worker, (str(plugin_data), tokens, "apply_tap"),
                     (str(plugin_data), tokens, "apply_tap"))
    for i in range(50):
        assert (a[i] is None) != (b[i] is None), i
    assert sum(x is not None for x in a + b) == 50


def test_one_answer_per_card_across_processes(plugin_data):
    """Two different buttons of one card, tapped at once from two processes:
    exactly one of them applies."""
    allow, notnow = [], []
    for i in range(40):
        allow.append(state.mint_tap("keep", {"run_id": "r", "d": "allow"}, f"c{i}"))
        notnow.append(state.mint_tap("keep", {"run_id": "r", "d": "not_now"}, f"c{i}"))
    a, b = _run_pair(_redeem_worker, (str(plugin_data), allow, "apply_tap"),
                     (str(plugin_data), notnow, "apply_tap"))
    for i in range(40):
        assert (a[i] is None) != (b[i] is None), i


def test_tap_returns_payload_and_group():
    t = state.mint_tap("forget", {"site_id": "s1", "run_id": "r1"}, "grp-1")
    rec = state.redeem_tap(t, "apply_tap", time.time())
    assert rec == state.TapRecord("forget", {"run_id": "r1", "site_id": "s1"}, "grp-1")
    assert state.redeem_tap(t, "apply_tap", time.time()) is None


def test_tap_tool_binding():
    live = state.mint_tap("q_live", {"run_id": "r"}, "g1")
    keep = state.mint_tap("keep", {"run_id": "r"}, "g2")
    now = time.time()
    assert state.redeem_tap(live, "apply_tap", now) is None
    assert state.redeem_tap(keep, "tap_live", now) is None
    # a refusal for the wrong tool consumes nothing; a question's live button is redeemed
    # only by `redeem_live`
    assert state.redeem_tap(live, "tap_live", now) is None
    assert state.redeem_tap(keep, "apply_tap", now).kind == "keep"
    for kind in ("keep_going", "stop", "forget"):
        t = state.mint_tap(kind, {"run_id": "r"}, f"g-{kind}")
        assert state.redeem_tap(t, "tap_live", now) is None
        assert state.redeem_tap(t, "apply_tap", now).kind == kind
    assert state.redeem_tap(live, "other_tool", now) is None
    # a kind no card makes any more (the approval cards' decide and drop) is never minted
    for gone in ("decide", "drop"):
        try:
            state.mint_tap(gone, {"run_id": "r"}, f"g-{gone}")
        except ValueError:
            continue
        raise AssertionError(gone)


def test_tap_expired_and_unknown():
    t = state.mint_tap("keep", {"run_id": "r"}, "g", ttl_s=10)
    now = time.time()
    assert state.redeem_tap(t, "apply_tap", now + 11) is None
    assert state.redeem_tap("not-a-token", "apply_tap", now) is None
    assert state.redeem_tap("", "apply_tap", now) is None
    assert state.redeem_tap(None, "apply_tap", now) is None
    assert state.redeem_tap("x" * 5000, "apply_tap", now) is None


def test_mint_refuses_unknown_kind_and_used_group():
    with pytest.raises(ValueError):
        state.mint_tap("allow_everything", {"run_id": "r"}, "g")
    t = state.mint_tap("keep", {"run_id": "r"}, "g")
    state.redeem_tap(t, "apply_tap", time.time())
    with pytest.raises(ValueError):
        state.mint_tap("keep", {"run_id": "r"}, "g")


def test_raw_token_not_stored(plugin_data):
    tokens = [state.mint_tap("keep", {"run_id": "r"}, "g") for _ in range(5)]
    state.redeem_tap(tokens[0], "apply_tap", time.time())
    blob = b"".join(p.read_bytes() for p in plugin_data.iterdir() if p.is_file())
    for t in tokens:
        assert t.encode() not in blob
        assert len(t) >= 43                      # token_urlsafe(32)


def test_void_group():
    a = state.mint_tap("keep", {"run_id": "r"}, "card-1")
    b = state.mint_tap("keep", {"run_id": "r"}, "card-1")
    c = state.mint_tap("keep", {"run_id": "r"}, "card-2")
    state.void_group("card-1")
    now = time.time()
    assert state.redeem_tap(a, "apply_tap", now) is None
    assert state.redeem_tap(b, "apply_tap", now) is None
    assert state.redeem_tap(c, "apply_tap", now) is not None


def test_open_and_expired_card():
    now = time.time()
    assert state.open_card("run1", now) is None and state.expired_card("run1", now) is None
    state.mint_tap("keep", {"run_id": "run1"}, "a1", ttl_s=100)
    state.mint_tap("keep", {"run_id": "run1"}, "a1", ttl_s=100)
    state.mint_tap("keep", {"run_id": "run2"}, "b1", ttl_s=100)
    card = state.open_card("run1", now)
    assert card.group == "a1" and card.run_id == "run1"
    assert state.expired_card("run1", now) is None
    # past its hour, unanswered: expired, no longer open
    assert state.open_card("run1", now + 101) is None
    assert state.expired_card("run1", now + 101).group == "a1"
    # a newer card is the one that counts
    t = state.mint_tap("keep_going", {"run_id": "run1"}, "a2", ttl_s=1000, now=now + 200)
    assert state.open_card("run1", now + 201).group == "a2"
    assert state.expired_card("run1", now + 201) is None
    # answered: neither open nor expired
    state.redeem_tap(t, "apply_tap", now + 202)
    assert state.open_card("run1", now + 203) is None
    assert state.expired_card("run1", now + 2000) is None
    # a voided card is neither
    state.mint_tap("keep", {"run_id": "run3"}, "c1", ttl_s=10, now=now)
    state.void_group("c1")
    assert state.open_card("run3", now) is None and state.expired_card("run3", now + 20) is None


# -- run intents --------------------------------------------------------------

def _intent(rid, pos, group="grp", binding="casa-bind-" + "a" * 32):
    state.put_intent(rid, binding, "assistant", "operator_resident", group, pos,
                     {"request_id": rid, "task_text": "t"})


def test_intent_then_attach():
    _intent("req-1", 0)
    assert state.run_binding("run-1") is None
    assert state.next_in_group("grp") == "intent:req-1"
    assert state.attach_run("req-1", "run-1") is True
    assert state.attach_run("req-1", "run-1") is True          # idempotent
    assert state.attach_run("req-1", "run-other") is False      # never re-pointed
    b = state.run_binding("run-1")
    assert (b.request_id, b.binding_ref, b.requester_id, b.requester_kind) == (
        "req-1", "casa-bind-" + "a" * 32, "assistant", "operator_resident")
    assert b.spec == {"request_id": "req-1", "task_text": "t"}
    # the first intent is kept when written again
    state.put_intent("req-1", "casa-bind-" + "b" * 32, "x", "specialist", "g2", 5, {})
    assert state.run_binding("run-1").binding_ref == "casa-bind-" + "a" * 32
    assert state.attach_run("req-unknown", "run-9") is False


def test_put_run_is_the_pair():
    state.put_run("req-2", "run-2", "casa-bind-" + "c" * 32, "specialist:finance",
                  "specialist", "g", 0, {"request_id": "req-2"})
    assert state.run_binding("run-2").requester_kind == "specialist"


def test_next_in_group_order():
    for i, rid in enumerate(["r-a", "r-b", "r-c"]):
        _intent(rid, i, group="batch")
    state.attach_run("r-a", "run-a")
    state.attach_run("r-c", "run-c")
    assert state.next_in_group("batch") == "run-a"
    state.mark_terminal("run-a")
    assert state.next_in_group("batch") == "intent:r-b"
    state.attach_run("r-b", "run-b")
    assert state.next_in_group("batch") == "run-b"
    state.mark_terminal("run-b")
    state.mark_terminal("run-c")
    assert state.next_in_group("batch") is None
    assert state.next_in_group("other") is None


# -- admission requests ---------------------------------------------------------

def _pending(key="k-1"):
    state.put_pending_admission(key, "casa-bind-" + "d" * 32, "assistant",
                                "operator_resident", [{"request_id": "q1"}, {"request_id": "q2"}],
                                ["q1", "q2"], ["sha256:" + "1" * 64, "sha256:" + "2" * 64])


def test_admission_state_machine():
    _pending()
    row = state.admission("k-1")
    assert row.status == "requested" and row.request_ids == ["q1", "q2"]
    state.set_confirmation("k-1", "conf-" + "e" * 32)
    assert state.admission("k-1").status == "pending"
    assert state.by_confirmation("conf-" + "e" * 32) == "k-1"
    adm = [{"spec_digest": "sha256:" + "1" * 64, "origin_kind": "operator_telegram",
            "origin_ref": "adm-" + "0" * 32}]
    state.set_admitted("k-1", adm)
    assert state.admission("k-1").status == "admitted"
    claimed = state.claim_pending_admission("k-1")
    assert claimed.admissions == adm and claimed.request_ids == ["q1", "q2"]
    # a late or replayed answer never moves a claimed row back
    state.set_admitted("k-1", [])
    state.set_confirmation("k-1", "conf-" + "f" * 32)
    assert state.admission("k-1").status == "submitting"
    assert state.admission("k-1").admissions == adm
    state.finish_pending_admission("k-1", "submitted")
    assert state.admission("k-1").status == "submitted"
    assert state.claim_pending_admission("k-1") is None
    with pytest.raises(ValueError):
        state.finish_pending_admission("k-1", "whatever")


def test_by_confirmation_falls_back_to_request_key():
    _pending("k-2")
    cid = "conf-" + "a" * 32
    assert state.by_confirmation(cid) is None
    assert state.by_confirmation(cid, request_key="k-2") == "k-2"
    assert state.by_confirmation(cid) == "k-2"          # now recorded
    assert state.by_confirmation(cid, request_key="k-missing") == "k-2"
    assert state.by_confirmation("conf-" + "b" * 32, request_key="k-missing") is None


def test_pending_admission_claim_once_and_recover(plugin_data):
    _pending("k-3")
    state.set_admitted("k-3", [])
    a, b = _run_pair(_claim_worker, (str(plugin_data), "k-3"), (str(plugin_data), "k-3"))
    assert sorted([a, b]) == [False, True]
    now = time.time()
    assert state.claim_pending_admission("k-3", now=now + 60) is None
    again = state.claim_pending_admission("k-3", now=now + 121)
    assert again is not None and again.request_ids == ["q1", "q2"]
    assert state.claim_pending_admission("k-3", now=now + 122) is None
    assert state.claim_pending_admission("k-unknown") is None


def test_requested_row_is_not_claimable():
    _pending("k-4")
    assert state.claim_pending_admission("k-4") is None
    state.finish_pending_admission("k-4", "refused")
    state.set_admitted("k-4", [])                # an answer after a refusal changes nothing
    assert state.admission("k-4").status == "refused"


# -- files, leases ---------------------------------------------------------------

def test_files():
    state.put_file("f1", "run-1")
    state.put_file("f1", "run-1")
    assert state.file_run("f1") == "run-1"
    assert state.file_run("f2") is None


_HOLD = r"""
import os, sys, time
sys.path.insert(0, {server!r})
import state
state.take_lease({run!r})
print("taken", flush=True)
sys.stdin.readline()
"""


def test_lease_follows_process(plugin_data):
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLD.format(server=str(SERVER), run="run-L")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, CLAUDE_PLUGIN_DATA=str(plugin_data)))
    try:
        assert child.stdout.readline().strip() == "taken"
        now = time.time()
        assert state.lease_live("run-L", now) is True
        assert state.lease_live("run-L", now + 661) is False
        assert state.lease_live("run-other", now) is False
    finally:
        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(30)
    assert state.lease_live("run-L", time.time()) is False


def test_lease_of_this_process_and_reused_pid(plugin_data):
    state.take_lease("run-M")
    assert state.lease_live("run-M", time.time()) is True
    # a different start time under the same pid is another process
    con = state._connect()
    con.execute("UPDATE leases SET start_time = 'not-it' WHERE run_id = 'run-M'")
    con.close()
    assert state.lease_live("run-M", time.time()) is False


# -- deliveries -------------------------------------------------------------------

REF = ["casa-cap-" + c * 32 for c in "0123456789"]


def test_delivery_current_attempt():
    state.put_delivery(REF[0], "file", "run-1", "f1", None, None)
    state.put_delivery(REF[1], "file", "run-1", "f2", None, None)
    state.put_delivery(REF[2], "file", "run-1", "f1", None, None)     # re-deposit of f1
    current = {d.reference for d in state.pending_deliveries()}
    assert current == {REF[1], REF[2]}
    assert state.file_delivery("run-1", "f1").reference == REF[2]
    state.settle_delivery(REF[0], "delivered")                        # history only
    assert state.file_delivery("run-1", "f1").status == "pending"
    state.settle_delivery(REF[2], "withheld")
    state.settle_delivery(REF[1], "delivered")
    assert state.pending_deliveries() == []
    assert state.file_delivery("run-1", "f1").status == "withheld"


def test_unknown_stays_pending():
    state.put_delivery(REF[3], "card", "run-2", None, None, "grp")
    state.settle_delivery(REF[3], "unknown")
    assert [d.reference for d in state.pending_deliveries()] == [REF[3]]
    with pytest.raises(ValueError):
        state.settle_delivery(REF[3], "lost")


def test_latest_link():
    assert state.latest_link("run-3") is None
    state.put_delivery(REF[4], "link", "run-3", None, None, None)
    state.put_delivery(REF[5], "link", "run-3", None, None, None)
    assert state.latest_link("run-3").reference == REF[5]


def test_notice_cursor_only_on_delivered():
    assert state.notice_cursor() == 0
    state.put_delivery(REF[6], "notice", None, None, 7, None)
    state.settle_delivery(REF[6], "withheld")
    assert state.notice_cursor() == 0
    state.put_delivery(REF[7], "notice", None, None, 9, None)
    state.settle_delivery(REF[7], "pending")
    assert state.notice_cursor() == 0
    state.settle_delivery(REF[7], "delivered")
    assert state.notice_cursor() == 9
    state.put_delivery(REF[8], "notice", None, None, 4, None)
    state.settle_delivery(REF[8], "delivered")
    assert state.notice_cursor() == 9                     # never moves back


# -- the file itself ----------------------------------------------------------------

def test_permissions(plugin_data):
    plugin_data.chmod(0o755)
    state.mint_tap("keep", {"run_id": "r"}, "g")
    assert stat.S_IMODE(plugin_data.stat().st_mode) == 0o700
    files = [p for p in plugin_data.iterdir() if p.name.startswith("fido.sqlite")]
    assert any(p.name == "fido.sqlite" for p in files)
    for p in files:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p.name


def test_wal_and_busy_timeout():
    con = state._connect()
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        con.close()


def test_no_data_dir(monkeypatch):
    from app_client import AppError
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    with pytest.raises(AppError) as e:
        state.mint_tap("keep", {"run_id": "r"}, "g")
    assert e.value.code == "no_data_dir"


def test_payload_canonical():
    t = state.mint_tap("keep", {"b": 1, "a": [1, {"z": 2, "y": 1}]}, "g")
    con = state._connect()
    stored = con.execute("SELECT payload FROM taps").fetchone()[0]
    con.close()
    assert stored == json.dumps({"a": [1, {"y": 1, "z": 2}], "b": 1},
                                sort_keys=True, separators=(",", ":"))
    assert state.redeem_tap(t, "apply_tap", time.time()).payload == {
        "a": [1, {"y": 1, "z": 2}], "b": 1}


def test_same_reference_recorded_twice_stays_current():
    state.put_delivery(REF[9], "file", "run-9", "f9", None, None)
    state.put_delivery(REF[9], "file", "run-9", "f9", None, None)
    assert state.file_delivery("run-9", "f9").reference == REF[9]
    assert [d.reference for d in state.pending_deliveries()] == [REF[9]]
    # A, B, then A again: A is the current attempt
    state.put_delivery(REF[8], "file", "run-9", "f9", None, None)
    state.put_delivery(REF[9], "file", "run-9", "f9", None, None)
    assert state.file_delivery("run-9", "f9").reference == REF[9]


def test_lease_of_exited_unreaped_process(plugin_data):
    """A driver that exited but is not yet reaped (a zombie) is not driving."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.path.insert(0, %r); import state; "
         "state.take_lease('run-Z')" % str(SERVER)],
        env=dict(os.environ, CLAUDE_PLUGIN_DATA=str(plugin_data)))
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            with open(f"/proc/{child.pid}/stat") as fh:
                if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                    break
            time.sleep(0.05)
        else:
            pytest.fail("child never exited")
        assert state.lease_live("run-Z", time.time()) is False
    finally:
        child.wait(30)


def test_finished_admission_keeps_its_outcome():
    _pending("k-7")
    state.set_admitted("k-7", [])
    state.claim_pending_admission("k-7")
    state.finish_pending_admission("k-7", "submitted")
    state.finish_pending_admission("k-7", "declined")
    assert state.admission("k-7").status == "submitted"


def test_schema_upgrade_from_first_version(plugin_data):
    """A database written by the first schema (no deposited_at) is upgraded
    in place, keeping its rows."""
    import sqlite3
    con = sqlite3.connect(str(plugin_data / "fido.sqlite"))
    con.executescript("""
CREATE TABLE deliveries (reference TEXT PRIMARY KEY, kind TEXT NOT NULL, run_id TEXT,
    file_id TEXT, notice_seq INTEGER, grp TEXT, status TEXT NOT NULL DEFAULT 'pending',
    current INTEGER NOT NULL DEFAULT 1, seq INTEGER NOT NULL);
INSERT INTO deliveries VALUES ('casa-cap-%s', 'notice', NULL, NULL, 3, NULL, 'pending', 1, 1);
PRAGMA user_version = 1;
""" % ("1" * 32))
    con.close()
    state.put_delivery(REF[0], "notice", None, None, 4, None)
    assert {d.reference for d in state.pending_deliveries()} == {"casa-cap-" + "1" * 32, REF[0]}


def test_a_database_from_the_0_1_1_schema_gains_the_tables_added_since(plugin_data):
    """A database written by 0.1.1 at schema version 10 lacks the wakes and touches tables,
    which 0.1.3 added under the same number. The version follows the schema text, so
    a database at an older version gains every table and keeps its rows."""
    import sqlite3
    state.put_file("f-old", "run-x")
    con = sqlite3.connect(str(plugin_data / "fido.sqlite"))
    con.executescript("DROP TABLE touches; DROP TABLE wakes; PRAGMA user_version = 10;")
    con.close()
    state.touch_run("run-x")
    assert state.touched_since("run-x", 0)
    con = sqlite3.connect(str(plugin_data / "fido.sqlite"))
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"touches", "wakes", "files"} <= tables
    assert con.execute("SELECT count(*) FROM files WHERE file_id = 'f-old'").fetchone()[0] == 1
    assert con.execute("PRAGMA user_version").fetchone()[0] == state.SCHEMA_VERSION
    con.close()


def test_the_schema_version_moves_with_the_schema(monkeypatch):
    """Any edit to the schema text or the added columns is a new version."""
    import zlib
    def version(schema, added):
        return zlib.crc32(repr((schema, added)).encode("utf-8")) % 0x7FFFFFFF + 1
    assert state.SCHEMA_VERSION == version(state._SCHEMA, state._ADDED_COLUMNS)
    assert version(state._SCHEMA + "CREATE TABLE IF NOT EXISTS x (a);", state._ADDED_COLUMNS) \
        != state.SCHEMA_VERSION
    assert state.SCHEMA_VERSION not in (0, 10)


_OPEN = r"""
import os, sys
sys.path.insert(0, {server!r})
import state
state.put_file("f%d" % os.getpid(), "run-x")
print("ok")
"""


def test_first_open_from_many_processes(tmp_path):
    """Several processes opening a fresh database at once (a Fido turn and
    Casa's sweep) all succeed: the switch to WAL waits like any write."""
    for attempt in range(5):
        data = tmp_path / f"d{attempt}"
        data.mkdir()
        procs = [subprocess.Popen([sys.executable, "-c", _OPEN.format(server=str(SERVER))],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env=dict(os.environ, CLAUDE_PLUGIN_DATA=str(data)))
                 for _ in range(6)]
        outs = [p.communicate(timeout=60) for p in procs]
        assert all(p.returncode == 0 for p in procs), [e[-500:] for _, e in outs]


def test_a_notice_batch_never_cuts_a_notice_and_stops_before_the_limit():
    """A repeat's count grows an earlier line; the batch stops before
    its body would pass the limit, every notice it covers is shown whole, the rest form the
    next batch."""
    now = time.time()
    notices = [{"seq": i, "text_masked": f"Site {i} " + "x" * 30, "at": now} for i in range(1, 4)]
    notices += [{"seq": 4, "text_masked": "Site 1 " + "x" * 30, "at": now},     # a repeat of 1
                {"seq": 5, "text_masked": "Last site " + "y" * 30, "at": now}]
    limit = len("H:") + 3 * 40 + 3                 # room for three lines, not for "(×2)"
    first = state.form_notice_batch(notices, "H:", max_body=limit)
    assert len(first.body) <= limit
    shown = [ln[2:] for ln in first.body.split("\n")[1:]]
    import re
    for n in notices[:first.last_seq]:
        text = n["text_masked"]
        assert any(re.fullmatch(re.escape(text) + r"( \(×\d+\))?", line) for line in shown), text
    assert first.last_seq == 3
    con = state._connect()
    con.execute("UPDATE notice_batches SET delivered = 1")
    con.execute("INSERT INTO meta (name, value) VALUES ('notice_cursor', ?) ON CONFLICT(name) "
                "DO UPDATE SET value = excluded.value", (first.last_seq,))
    con.commit()
    con.close()
    second = state.form_notice_batch(notices, "H:", max_body=10_000)
    assert second.first_seq == first.last_seq + 1 and "Last site" in second.body

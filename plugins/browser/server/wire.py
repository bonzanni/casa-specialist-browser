"""Every assumption this plugin makes about Casa Browser's JSON, in one place.

The app's protocol document fixes route names, member names of the main wire
types and the error CODES; it does not yet fix the error BODY, the HTTP
statuses, what the pairing claim answers when no window is armed, or the
exact reply shape of several routes. Each of those guesses lives here, and
nowhere else, so settling one is a single edit in this file:

* an error reply is `{"error": "<code>"}`, one HTTP status per code
  (`conflict` 409, `not_found` 404, `internal` 500 among
  them; an admission-digest mismatch is `blocked` 403; a wrong method or an
  unknown path is `not_found` 404). The app's policy reasons (for example
  `site_lock`, a provider request that misses the site's binding) are not
  `/v1` error codes: they reach the plugin only as free-text event outcomes,
  which no parser here enumerates;
* a claim outside an armed window answers 401 `unauthorized` (also accepted:
  any of `NO_WINDOW_STATUSES`);
* the protocol version is the top-level integer `protocol_version` of the
  hello reply and of the claim reply;
* list routes wrap their items: `{"sites": […]}`, `{"events": […]}`,
  `{"notices": […]}`, `{"records": […]}`, `{"runs": […]}`, `{"sources": […]}`;
* a step reply (`action`, `download`, `search`, `fetch`, and the control verdict route)
  is `{outcome, generation, …}` with the members its outcome names (see
  `parse_step`); a free download still answers a FileRecord and a free search
  an Observation; times are RFC 3339 strings.

Every parser here validates the shape it needs and raises
`AppError("bad_response")` on anything else; the only app text a tool may show a
model is what the protocol defines as masked for it (`text`, `label_masked`,
`value_masked`, `text_masked`, `summary_masked`, display names, a plain fetch's
`url`, and a lapsed step's `copy`, the app's own fixed sentence for the operator); a source's `url` and `title_masked` are operator-only (see `parse_sources`).
"""
from __future__ import annotations

import base64
import datetime
import json
import re
import urllib.parse

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: caller identifiers the app accepts (request_id, requester_id, …)
CALLER_ID_RE = re.compile(r"^[\x21-\x7e]{1,128}$")
SECRET_RE = re.compile(r"^[\x21-\x7e]{1,512}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{1,32}$")
RUN_STATES = ("queued", "starting", "running", "waiting_approval", "waiting_live", "live",
              "finishing", "completed", "failed", "cancelled", "interrupted")
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
NO_WINDOW_STATUSES = frozenset({401, 403, 404, 409, 410})
ERROR_MEMBER = "error"
MAX_TEXT = 100_000
MAX_READ = 30_000        # the app's `protocol.MAX_READ`, one window of `run_read`
MAX_PLAN = 12_000        # the app's `protocol.MAX_PLAN`
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class BadShape(Exception):
    pass


def _bad():
    from app_client import AppError
    return AppError("bad_response")


def loads(raw: bytes):
    def reject(_name):
        raise ValueError("non-standard JSON constant")
    return json.loads(raw.decode("utf-8"), parse_constant=reject)


# -- errors, pairing, version ---------------------------------------------------

def error_code(raw: bytes) -> str | None:
    """The code an error reply names, or None when it names none."""
    try:
        answer = loads(raw)
    except (ValueError, RecursionError):
        return None
    code = answer.get(ERROR_MEMBER) if isinstance(answer, dict) else None
    return code if isinstance(code, str) else None


def claim_had_no_window(err) -> bool:
    return err.code == "unauthorized" or err.status in NO_WINDOW_STATUSES


def protocol_version(obj: dict):
    """The protocol version a hello or claim reply states (unchecked)."""
    return obj.get("protocol_version")


def claim_reply(obj: dict) -> tuple[str, int]:
    secret, version = obj.get("secret"), protocol_version(obj)
    if (not isinstance(secret, str) or not SECRET_RE.fullmatch(secret)
            or not isinstance(version, int) or isinstance(version, bool)):
        raise _bad()
    return secret, version


# -- small checks -----------------------------------------------------------------

def _id(v) -> str:
    if not isinstance(v, str) or not ID_RE.fullmatch(v):
        raise _bad()
    return v


def _opt_id(v):
    return None if v is None else _id(v)


def _text(v, limit: int = MAX_TEXT, *, empty: bool = True) -> str:
    if not isinstance(v, str) or len(v) > limit or (not empty and not v.strip()):
        raise _bad()
    return v


def _int(v) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise _bad()
    return v


def _count(v) -> int:
    if _int(v) < 0:
        raise _bad()
    return v


def _list(obj, member: str) -> list:
    if not isinstance(obj, dict) or not isinstance(obj.get(member), list):
        raise _bad()
    return obj[member]


def _state(v) -> str:
    if v not in RUN_STATES:
        raise _bad()
    return v


# -- route replies ----------------------------------------------------------------

def parse_sites(obj) -> list[dict]:
    """`GET /v1/sites` → `[{site_id, display_name, status}]` (`status` a short
    word such as `ready`; absent → "")."""
    out = []
    for s in _list(obj, "sites"):
        if not isinstance(s, dict):
            raise _bad()
        status = s.get("status", "")
        out.append({"site_id": _id(s.get("site_id")),
                    "display_name": _text(s.get("display_name"), 200, empty=False),
                    "status": _text(status, 40) if status is not None else ""})
    return out


def parse_run_created(obj) -> tuple[str, str]:
    if not isinstance(obj, dict):
        raise _bad()
    return _id(obj.get("run_id")), _state(obj.get("state"))


def parse_request_cancel(obj, request_ids) -> list[dict]:
    """`POST /v1/requests/cancel` (Casa Browser 0.1.8, the operator's Stop of one request, all
    its ids in one call) → one `{request_id, run_id: str | None, stopped: bool, state}` per id
    asked, in order: `stopped` is the app's durable answer for the id (a Stop of it won, now
    or before)."""
    items = obj.get("requests") if isinstance(obj, dict) else None
    if not isinstance(items, list) or len(items) != len(request_ids):
        raise _bad()
    out = []
    for want, item in zip(request_ids, items):
        if (not isinstance(item, dict) or item.get("request_id") != want
                or not isinstance(item.get("stopped"), bool)):
            raise _bad()
        run_id = item.get("run_id")
        out.append({"request_id": want, "run_id": None if run_id is None else _id(run_id),
                    "stopped": item["stopped"], "state": _state(item.get("state"))})
    return out


def epoch(value) -> float | None:
    """An RFC 3339 time (or a number of seconds) as epoch seconds; None when
    it is neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and len(value) <= 40:
        try:
            when = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            return None
        return when.timestamp()
    return None


def parse_run(obj) -> dict:
    """`GET /v1/runs/{run}` → `{run_id, state}` (+ `created_at` in epoch
    seconds when the reply gives a time)."""
    if not isinstance(obj, dict):
        raise _bad()
    out = {"run_id": _id(obj.get("run_id")), "state": _state(obj.get("state"))}
    if isinstance(obj.get("stopped"), bool):
        out["stopped"] = obj["stopped"]     # Casa Browser 0.1.8: the Stop stopped its request
    created = epoch(obj.get("created_at"))
    if created is not None:
        out["created_at"] = created
    return out


#: an element's field kind, a closed set (`gate.FIELD_KINDS`);
#: `multi-line`, a real textarea, the one field that takes line breaks
FIELD_KINDS = ("time", "date", "datetime-local", "month", "week", "range", "color", "number",
               "multi-line")
#: an ordinary field's value as the app shows it (masked, cut at 500 characters
#: before masking, which may lengthen it)
MAX_FIELD_VALUE = 2000
#: whether a typed field shows the text after the step
TYPED = ("shown", "not_shown")

#: Casa Browser's step outcomes (`protocol.STEP_OUTCOMES`)
STEP_OUTCOMES = ("done", "judgement_required", "rejudgement_required", "asked", "look_first",
                 "not_run", "check_unavailable", "outcome_unknown", "declined", "answered",
                 "lapsed", "not_current",
                 # an app-started judgement's or a login question's outcome
                 "login_needed", "linked", "not_linked", "credential_saved",
                 "credential_not_saved", "not_saved",
                 # the sign-in card for a site that is not set up, and its answer
                 "judging", "signing_in", "not_opened", "already_saved", "not_configured", "question_open",
                 "account_site", "registered_site", "not_https", "page_changed")
#: the judgements Casa Browser starts itself, relayed by `_prelude`
APP_JUDGEMENT_KINDS = ("login_item", "signin")
#: the outcomes that carry a judgement for the plugin's code to relay to Casa's check
JUDGEMENT_OUTCOMES = ("judgement_required", "rejudgement_required")
#: `sign_in`: Casa Browser 0.1.7's look at a page of the site that shows sign-in fields, relayed
#: from the observe reply that made it
JUDGEMENT_KINDS = ("action", "rejudge", "sign_in")
#: an observation's notice: the sign-in step Casa Browser's look recorded (one line)
MAX_NOTICE = 500
#: a `not_run`/`lapsed` reason is a code word, never page text
REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
#: the check's note as the app bounds it (`gate.MAX_NOTE`, 600), with room for masking
MAX_NOTE = 1000


def parse_step(obj) -> dict:
    """A step reply: `{outcome, generation, repeat}` plus, by outcome,
    `judgement {id, kind, packet}` (the packet text untouched, never shown to
    a model), `question_id`, `note`, `reason` (with `copy` when a lapse brings one), `file`
    (a FileRecord), `observation` or a scroll's `position`. Any other outcome, or a member of the wrong shape, is
    `bad_response`."""
    if not isinstance(obj, dict):
        raise _bad()
    outcome = obj.get("outcome")
    if outcome not in STEP_OUTCOMES:
        raise _bad()
    repeat = obj.get("repeat", False)
    if not isinstance(repeat, bool):
        raise _bad()
    out = {"outcome": outcome, "generation": _int(obj.get("generation")), "repeat": repeat}
    if outcome in JUDGEMENT_OUTCOMES:
        j = obj.get("judgement")
        if (not isinstance(j, dict) or j.get("kind") not in JUDGEMENT_KINDS
                or not isinstance(j.get("packet"), str) or not j["packet"]):
            raise _bad()
        out["judgement"] = {"id": _id(j.get("id")), "kind": j["kind"], "packet": j["packet"]}
    elif outcome == "asked":
        out["question_id"] = _id(obj.get("question_id"))
    elif outcome == "look_first":
        out["note"] = " ".join(_text(obj.get("note") or "", MAX_NOTE).split())
    elif outcome in ("not_run", "lapsed", "login_needed"):
        reason = obj.get("reason")
        if reason is not None and not (isinstance(reason, str) and REASON_RE.fullmatch(reason)):
            raise _bad()
        out["reason"] = reason
        if outcome == "lapsed" and obj.get("copy") is not None:
            out["copy"] = " ".join(_text(obj["copy"], MAX_QUESTION_TEXT).split())
    elif outcome in ("credential_saved", "credential_not_saved"):
        out["site"] = _text(obj.get("site") or "", 200)
    elif outcome in ("linked", "signing_in", "not_opened", "already_saved"):
        out["label"] = _text(obj.get("label") or "", 400)
    elif outcome == "registered_site":
        out["site"] = _text(obj.get("site") or "", 200)
    elif outcome == "done":
        typed = obj.get("typed")
        if typed is not None:
            if typed not in TYPED:
                raise _bad()
            out["typed"] = typed
        if obj.get("file") is not None:
            out["file"] = parse_file_record(obj["file"])
        if obj.get("observation") is not None:
            out["observation"] = parse_observation(obj["observation"])
        position = obj.get("position")
        if position is not None:
            if not isinstance(position, dict) or not isinstance(position.get("moved"), bool):
                raise _bad()
            out["position"] = {"top": _count(position.get("top")), "end": _count(position.get("end")),
                               "moved": position["moved"]}
    return out


def parse_pending_judgements(obj) -> list[dict]:
    """`GET /v1/control/runs/{run}/judgements/pending` → `[{id, kind, packet}]`, the kinds Casa
    Browser starts itself (the packet text untouched, never shown to a model)."""
    out = []
    for j in _list(obj, "judgements"):
        if (not isinstance(j, dict) or j.get("kind") not in APP_JUDGEMENT_KINDS
                or not isinstance(j.get("packet"), str) or not j["packet"]):
            raise _bad()
        out.append({"id": _id(j.get("id")), "kind": j["kind"], "packet": j["packet"]})
    return out


def parse_active_runs(obj) -> list[dict]:
    """`GET /v1/runs?state=active` → `[{run_id, request_id, state,
    created_at, pending_question: <question id> | None, code_site: <site id> | None}]`
    (an older app's bundle member is ignored)."""
    out = []
    for r in _list(obj, "runs"):
        if not isinstance(r, dict):
            raise _bad()
        rid = r.get("request_id")
        if not isinstance(rid, str) or not CALLER_ID_RE.fullmatch(rid):
            raise _bad()
        state = _state(r.get("state"))
        if state in TERMINAL_STATES:
            raise _bad()
        created = r.get("created_at")
        out.append({"run_id": _id(r.get("run_id")), "request_id": rid, "state": state,
                    "created_at": created,
                    "pending_question": _opt_id(r.get("pending_question")),
                    # the site whose sign-in waits for a code it sent, or None
                    "code_site": _opt_id(r.get("code_site")),
                    # what the operator approved on a plan question in this run, or None
                    "approved": None if r.get("approved") is None
                    else _text(r["approved"], MAX_PLAN)})
    return out


EVENT_FIELDS = ("seq", "kind", "origin", "handle", "text_masked", "outcome",
                "screenshot_id")


def origin_host(origin) -> str | None:
    """An event's origin as its bare host name, only when it is exactly an
    `http(s)://host[:port]` origin; anything else is dropped, never shown."""
    if not isinstance(origin, str) or len(origin) > 300:
        return None
    try:
        parts = urllib.parse.urlsplit(origin)
        parts.port
    except ValueError:
        return None
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.path not in ("", "/") or parts.query
            or parts.fragment or not re.fullmatch(r"[a-z0-9.-]{1,253}", parts.hostname)):
        return None
    return parts.hostname


def parse_events(obj) -> list[dict]:
    out = []
    for e in _list(obj, "events"):
        if not isinstance(e, dict):
            raise _bad()
        out.append({"seq": _int(e.get("seq")),
                    "kind": _text(e.get("kind"), 40, empty=False),
                    "origin": origin_host(e.get("origin")),
                    "handle": _opt_id(e.get("handle")),
                    "text_masked": _text(e.get("text_masked") or "", 4000),
                    "outcome": _text(e.get("outcome") or "", 60),
                    "screenshot_id": _opt_id(e.get("screenshot_id")),
                    "at": epoch(e.get("at"))})
    return sorted(out, key=lambda e: e["seq"])


def parse_observation(obj) -> dict:
    if not isinstance(obj, dict):
        raise _bad()
    elements = obj.get("elements")
    if not isinstance(elements, list):
        raise _bad()
    els = []
    for el in elements:
        if not isinstance(el, dict):
            raise _bad()
        kind = el.get("kind")
        if kind is not None and kind not in FIELD_KINDS:
            raise _bad()
        checked = el.get("checked")
        if checked is not None and not isinstance(checked, bool):
            raise _bad()
        value = el.get("value_masked")
        els.append({"handle": _id(el.get("handle")),
                    "role": _text(el.get("role"), 40),
                    "label_masked": _text(el.get("label_masked") or "", 500),
                    "kind": kind,
                    # what an ordinary field holds; never a secret field's
                    "value_masked": None if value is None else _text(value, MAX_FIELD_VALUE),
                    "checked": checked,
                    # a search: the app's own answer, on what it masked
                    "found": el.get("found") is True})
    notice = obj.get("notice")
    search = obj.get("find")
    if search is not None and not (isinstance(search, dict)
                                   and isinstance(search.get("whole"), bool)):
        raise _bad()
    return {"page_handle": _id(obj.get("page_handle")),
            "generation": _int(obj.get("generation")),
            "text": _text(obj.get("text") or ""),
            "elements": els,
            "source_id": _opt_id(obj.get("source_id")),
            # Casa Browser 0.1.7: the sign-in step its look recorded, or None
            "notice": None if notice is None else " ".join(_text(notice, MAX_NOTICE).split()),
            "text_cut": obj.get("text_cut") is True,
            # what the app's cap left out; a page may have more controls than `elements`
            "controls_left": _count(obj.get("controls_left", 0)),
            # a search: whether all of the page was searched, how many it found did not fit
            "find": None if search is None else {"whole": search["whole"],
                                                 "left": _count(search.get("left", 0))}}


def parse_look(obj) -> str | None:
    """`POST /v1/runs/{run}/look` → the picture, base64 PNG, or None."""
    picture = obj.get("picture") if isinstance(obj, dict) else None
    if picture is None:
        return None
    try:
        if not base64.b64decode(picture, validate=True).startswith(PNG_MAGIC):
            raise _bad()
    except (TypeError, ValueError):
        raise _bad() from None
    return picture


def parse_read(obj) -> dict:
    """`POST /v1/runs/{run}/read` → `{text, next_offset: int | None, complete, masked}`."""
    if not isinstance(obj, dict):
        raise _bad()
    next_offset = obj.get("next_offset")
    if (not isinstance(obj.get("complete"), bool) or not isinstance(obj.get("masked"), bool)
            or (next_offset is not None and _int(next_offset) < 0)):
        raise _bad()
    return {"text": _text(obj.get("text"), MAX_READ), "next_offset": next_offset,
            "complete": obj["complete"], "masked": obj["masked"]}


def parse_fetch(obj) -> dict:
    """`POST /v1/runs/{run}/fetch`'s `done`: `file`, the address that answered (masked in place,
    for the model: it resolves relative links), a `run_read` window (`text` null for a type
    that is not text), `source_id` when the address could be a source, `not_json` when fields
    did not apply."""
    if not isinstance(obj, dict):
        raise _bad()
    window = (parse_read(obj) if obj.get("text") is not None else
              {"text": None, "next_offset": None, "complete": False, "masked": False})
    return {**window, "file": parse_file_record(obj.get("file")), "url": _text(obj.get("url")),
            "source_id": _opt_id(obj.get("source_id")), "not_json": obj.get("not_json") is True}


def is_observation(obj) -> bool:
    return isinstance(obj, dict) and "page_handle" in obj and "elements" in obj


def parse_file_record(obj) -> dict:
    if not isinstance(obj, dict):
        raise _bad()
    return {"file_id": _id(obj.get("file_id")), "run_id": _id(obj.get("run_id")),
            "safe_name": _text(obj.get("safe_name"), 255, empty=False),
            "mime": _text(obj.get("mime"), 128),
            "size": _int(obj.get("size"))}


def parse_records(obj) -> list[dict]:
    out = []
    for r in _list(obj, "records"):
        if not isinstance(r, dict):
            raise _bad()
        sites = r.get("sites") or []
        changes = r.get("changes") or []
        if not isinstance(sites, list) or not isinstance(changes, list):
            raise _bad()
        site_ids = []
        for s in sites:
            site_ids.append(_id(s.get("site_id") if isinstance(s, dict) else s))
        out.append({"run_id": _id(r.get("run_id")),
                    "summary_masked": _text(r.get("summary_masked") or "", 2000),
                    "sites": site_ids,
                    "changes": len(changes),
                    "ended_at": _text(str(r.get("ended_at", "")), 40)})
    return out


NOTICE_KIND_RE = re.compile(r"^[a-z][a-z_]{0,39}$")


def parse_notices(obj) -> list[dict]:
    """`GET /v1/notices?after=N` → `[{seq, kind, run_id, text_masked, at}]`, oldest first; `at`
    in epoch seconds, None when the app sent no readable time (such a notice counts as fresh);
    `kind` and `run_id` None when absent or not of their shape."""
    out = []
    for n in _list(obj, "notices"):
        if not isinstance(n, dict):
            raise _bad()
        kind, run_id = n.get("kind"), n.get("run_id")
        out.append({"seq": _int(n.get("seq")),
                    "kind": kind if isinstance(kind, str) and NOTICE_KIND_RE.fullmatch(kind)
                    else None,
                    "run_id": run_id if isinstance(run_id, str) and ID_RE.fullmatch(run_id)
                    else None,
                    "text_masked": _text(n.get("text_masked"), 2000, empty=False),
                    "at": epoch(n.get("at"))})
    return sorted(out, key=lambda n: n["seq"])


def parse_live(obj) -> dict:
    if not isinstance(obj, dict):
        raise _bad()
    path = obj.get("operator_delivery_path")
    return {"grant_id": _id(obj.get("grant_id")),
            "expires_at": _text(obj.get("expires_at"), 40, empty=False),
            "operator_delivery_path": _text(path, 256, empty=False)}


def parse_sources(obj) -> list[dict]:
    """Sources: `[{source_id, url, title_masked}]`. Operator-only: the url
    and title go into a Casa deposit, never into a tool result."""
    out = []
    for s in _list(obj, "sources"):
        if not isinstance(s, dict):
            raise _bad()
        out.append({"source_id": _id(s.get("source_id")),
                    "url": _text(s.get("url"), 4096, empty=False),
                    "title_masked": _text(s.get("title_masked") or "", 1000)})
    return out


#: a question's states as the app keeps them (`confirmations` rows)
QUESTION_STATES = ("open", "confirming", "executed", "outcome_unknown", "lapsed", "declined",
                   "answered", "expired")
DIGEST_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
#: the check's bounds (Casa's `action_judgement.parse_reply`), with room for the app's masking
MAX_QUESTION_TEXT = 2000
MAX_OPTION = 400


def parse_question(obj) -> dict:
    """`GET /v1/control/runs/{run}/questions/{question}` → `{question_id, run_id, revision,
    digest, state, view}`; the view `{text, confirm_label, extra_options (0-3), screenshot_id,
    confirm_offered, live_only_copy}` is what the question card shows (no exact destination
    is in it). Operator-facing: it goes into a card, never into a tool result."""
    if not isinstance(obj, dict):
        raise _bad()
    digest = obj.get("digest")
    if not isinstance(digest, str) or not DIGEST_HEX_RE.fullmatch(digest):
        raise _bad()
    if obj.get("state") not in QUESTION_STATES:
        raise _bad()
    rev = _int(obj.get("revision"))
    v = obj.get("view")
    if not isinstance(v, dict) or not isinstance(v.get("confirm_offered"), bool):
        raise _bad()
    extras = v.get("extra_options") or []
    if not isinstance(extras, list) or len(extras) > 3:
        raise _bad()
    copy = v.get("live_only_copy")
    view = {"text": _text(v.get("text"), MAX_QUESTION_TEXT, empty=False),
            "confirm_label": _text(v.get("confirm_label") or "", MAX_OPTION),
            "extra_options": [_text(x, MAX_OPTION, empty=False) for x in extras],
            "screenshot_id": _opt_id(v.get("screenshot_id")),
            "confirm_offered": v["confirm_offered"],
            "live_only_copy": None if copy is None else _text(copy, MAX_QUESTION_TEXT),
            "category": v.get("category") if isinstance(v.get("category"), str) else ""}
    if v.get("plan") is not None:
        view["plan"] = _text(v["plan"], MAX_PLAN, empty=False)
    if v.get("after_lapse") is True:     # it follows the operator's lapsed Yes: the card says so
        view["after_lapse"] = True
    if view["confirm_offered"] and not view["confirm_label"].strip():
        raise _bad()
    if v.get("kind") == "login_item":
        # which 1Password item signs in to a site (the browser's host, whole titles)
        options = v.get("options")
        if (view["confirm_offered"] or extras or not isinstance(options, list)
                or not 1 <= len(options) <= 5):
            raise _bad()
        view.update(kind="login_item", host=_text(v.get("host"), 253, empty=False),
                    options=[_text(x, MAX_OPTION, empty=False) for x in options])
    elif v.get("kind") == "site_signin":
        # the sign-in card for a site that is not set up — the browser's host, the
        # site's label, the one fitting item's whole title or none; Yes and No only
        item = v.get("item")
        if view["confirm_offered"] or extras or v.get("options"):
            raise _bad()
        view.update(kind="site_signin", host=_text(v.get("host"), 259, empty=False),
                    name=_text(v.get("name"), 80, empty=False),
                    item=None if item is None else _text(item, MAX_OPTION, empty=False))
    return {"question_id": _id(obj.get("question_id")), "run_id": _id(obj.get("run_id")),
            "revision": rev, "digest": digest, "state": obj["state"], "view": view}


def parse_screenshot_taken(obj) -> str:
    """`POST /v1/control/runs/{run}/screenshots` → the new screenshot's id."""
    if not isinstance(obj, dict):
        raise _bad()
    return _id(obj.get("screenshot_id"))


LIVE_OUTCOMES = ("live", "live_open", "not_current")


def parse_question_live(obj) -> str:
    """`POST …/questions/{question}/live` → its outcome: `live` (a new view), `live_open`
    (the view asked from this question is already there) or `not_current`."""
    if not isinstance(obj, dict) or obj.get("outcome") not in LIVE_OUTCOMES:
        raise _bad()
    return obj["outcome"]

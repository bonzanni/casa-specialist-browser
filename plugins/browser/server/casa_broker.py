"""What the plugin's code asks of Casa, over Casa's internal Unix socket.

Casa puts two variables in this server's environment: `CASA_BROKER_SOCKET`
(the socket) and `CASA_BROKER_CLIENT` (this session's broker client id).
During a declared tool call the plugin's code — never its model — presents
that client id; Casa finds the call by it and reads identity, requester and
the operator's words from the call's recorded origin, never from the request.

* **Deposits** (`POST /internal/broker/deposit`): a value Casa posts to the
  operator itself — a message, a link, a file, a card with buttons — answered
  by a reference `casa-cap-<32 hex>` the tool returns in its slot. Every
  deposit names its post with a `key`, so a retried call is the same post.
* **Calls during a `callsCasa` tool call**: `requester`,
  `run-admission`, `run-admission-status`, `continue`,
  `delivery-status`, `action-judgement` (the check of one browsing step,
  relayed by `judge.py` with its own longer timeout), and `pad`: the
  scratchpad of the conversation the call runs in, which Casa finds by the
  client id alone (`write`/`append` a section, `read` it in 30,000-character
  windows). Page text reaches it from `run_read`'s `to_pad`, and `run_act`'s
  `text_from` types a section, so neither passes through the model.

Every request also names the tool call it serves, as `tool_use_id`, when the
server has the call's id (`CALL_ID`, set for that call only); with none the
member is left out.

Nothing Casa sends back is echoed: an answer must have its documented shape,
an error must look like one of Casa's codes, or the failure is reported by a
fixed label (`broker_env_missing`, `broker_unreachable:<Class>`,
`broker_bad_response`, `unrecognized_error`, `bad_request`). Standard library
only.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
from typing import NamedTuple

ENV_CLIENT = "CASA_BROKER_CLIENT"
ENV_SOCKET = "CASA_BROKER_SOCKET"
REFERENCE_RE = re.compile(r"^casa-cap-[0-9a-f]{32}$")
BINDING_RE = re.compile(r"^casa-bind-[0-9a-f]{32}$")
KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CONFIRMATION_RE = re.compile(r"^conf-[0-9a-f]{32}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ORIGIN_REF_RE = re.compile(r"^adm-[0-9a-f]{32}$")
ROLE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_LABEL_CHARS = 40
MAX_CAPTION_CHARS = 200
DOMAINISH_RE = re.compile(r"\.[A-Za-z]")

ROUTE_DEPOSIT = "/internal/broker/deposit"
ROUTE_REQUESTER = "/internal/broker/requester"
ROUTE_ADMISSION = "/internal/broker/run-admission"
ROUTE_ADMISSION_STATUS = "/internal/broker/run-admission-status"
ROUTE_CONTINUE = "/internal/broker/continue"
ROUTE_DELIVERY_STATUS = "/internal/broker/delivery-status"
ROUTE_ACTION_JUDGEMENT = "/internal/broker/action-judgement"
ROUTE_PAD = "/internal/broker/pad"

#: the id of the tool call the server is handling, or None (no id, or no call)
CALL_ID: str | None = None

TIMEOUT_S = 10.0
#: the check's own call: Casa's model call is bounded at 90 s, so the
#: default above would cut most checks; only this route waits longer
JUDGEMENT_TIMEOUT_S = 100.0
MAX_RESPONSE_BYTES = 64 * 1024
#: a pad read's answer: 30,000 characters, each at most 12 bytes JSON-escaped, plus its members
PAD_RESPONSE_BYTES = 512 * 1024
PAD_READ_CHARS = 30_000
PAD_SECTION_MAX = 200
#: the pad's own cap (Casa's `scratchpad.MAX_BYTES`); a larger text is never sent
PAD_MAX_BYTES = 1024 * 1024
_CODE_RE = re.compile(r"^[a-z][a-z_]{0,39}$")
_WWW_DOT_RE = re.compile(r"(?i)(www)\.")
_ELLIPSIS = "…"

REQUESTER_KINDS = ("operator_resident", "specialist")
CONTINUE_REASONS = ("operator_answer", "next_run", "sweep")
RUN_REF_MAX = 64
REQUEST_KEY_MAX = 128
MAX_RUNS = 8
MAX_SITES = 8
MAX_CHAIN = 16
ADMISSION_STATUSES = ("admitted", "confirmation_pending", "declined", "expired", "refused")
CONFIRMATION_STATUSES = ("admitted", "pending", "declined", "expired")
DELIVERY_STATUSES = ("delivered", "withheld", "pending", "unknown")
JUDGEMENT_KINDS = ("action", "rejudge", "login_item", "signin", "sign_in")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
#: Casa Browser's bound on a verdict's `latency_ms` (`protocol.parse_judgement_verdict`)
MAX_LATENCY_MS = 10 ** 7


class DepositFailed(Exception):
    """A deposit was not accepted. `code` is Casa's error code or one of this
    module's fixed labels, never text Casa or the transport wrote."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BrokerFailed(Exception):
    """One of those calls failed; `code` as for `DepositFailed`."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Requester(NamedTuple):
    binding_ref: str
    requester_id: str
    requester_kind: str
    chain: tuple


class AdmissionAnswer(NamedTuple):
    status: str
    request_key: str | None
    confirmation_id: str | None
    admissions: list | None = None
    reason: str | None = None


class Judgement(NamedTuple):
    """Casa's answer to one check: the verdict object exactly as Casa sent it,
    the SHA-256 Casa computed over the packet it received, and its latency."""
    verdict: dict
    packet_sha256: str
    latency_ms: int


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = TIMEOUT_S):
        super().__init__("localhost", timeout=timeout)
        self._path = path
        self._timeout = timeout

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect(self._path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _post(route: str, fields: dict, fail, *, timeout: float = TIMEOUT_S,
          max_bytes: int = MAX_RESPONSE_BYTES) -> dict:
    """POST `fields` plus this session's client id (and the call's id when there
    is one); the answer as a dict, or
    `fail(code)` raised. A Casa error code is raised as itself (its `message`, if
    any, is never read). `timeout` bounds the connect and every read (a timeout
    is `broker_unreachable`); an answer over `max_bytes` is `broker_bad_response`."""
    path = os.environ.get(ENV_SOCKET, "")
    client = os.environ.get(ENV_CLIENT, "")
    if not path or not client:
        raise fail("broker_env_missing")
    body = dict(fields, client=client)
    if CALL_ID is not None:
        body["tool_use_id"] = CALL_ID
    conn = _UnixHTTP(path, timeout)
    try:
        conn.request("POST", route, body=json.dumps(body).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        raw = conn.getresponse().read(max_bytes + 1)
    except Exception as exc:                     # noqa: BLE001 — the class only
        raise fail("broker_unreachable:%s" % type(exc).__name__) from None
    finally:
        conn.close()
    try:
        answer = json.loads(raw.decode("utf-8")) if len(raw) <= max_bytes else None
    except (ValueError, RecursionError):
        answer = None
    if not isinstance(answer, dict):
        raise fail("broker_bad_response")
    error = answer.get("error")
    if error is not None:
        if isinstance(error, str) and _CODE_RE.fullmatch(error):
            raise fail(error)
        raise fail("unrecognized_error")
    return answer


# -- deposits -----------------------------------------------------------------------

def _deposit(fields: dict) -> str:
    key = fields.get("key")
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise DepositFailed("bad_key")
    answer = _post(ROUTE_DEPOSIT, fields, DepositFailed)
    reference = answer.get("reference")
    if isinstance(reference, str) and REFERENCE_RE.fullmatch(reference):
        return reference
    raise DepositFailed("unrecognized_error")


def deposit_message(slot: str, text: str, *, key: str) -> str:
    """An `operator_message`: Casa posts `text`, headed by Fido's label."""
    return _deposit({"slot": slot, "value": text, "key": key})


def deposit_link(slot: str, url: str, *, label: str, caption: str, key: str) -> str:
    """An `operator_link`: Casa posts one link whose text is `label` plus the
    host Casa prints from `url`, with `caption` beneath it."""
    return _deposit({"slot": slot, "value": url, "label": label, "caption": caption,
                     "key": key})


def deposit_file(slot: str, path: str, *, kind: str, caption: str,
                 filename: str | None, key: str) -> str:
    """An `operator_file`: Casa sends the file at `path` as media `kind`.
    `caption` and `filename` are sent only when non-empty."""
    fields = {"slot": slot, "value": path, "kind": kind}
    if caption:
        fields["caption"] = caption
    if filename:
        fields["filename"] = filename
    fields["key"] = key
    return _deposit(fields)


def deposit_proposal(slot: str, card: dict, *, key: str) -> str:
    """An `operator_proposal`: the card object as one compact JSON string."""
    value = json.dumps(card, separators=(",", ":"), ensure_ascii=False)
    return _deposit({"slot": slot, "value": value, "key": key})


# -- calls during a tool call --------------------------------------------------------------------

def _printable(value, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and value.isprintable()


def requester() -> Requester:
    """Whom this call serves — the run's binding on a continuation turn,
    else this plugin's binding for this turn."""
    a = _post(ROUTE_REQUESTER, {}, BrokerFailed)
    binding, rid, kind, chain = (a.get("binding_ref"), a.get("requester_id"),
                                 a.get("requester_kind"), a.get("chain"))
    if (not isinstance(binding, str) or not BINDING_RE.fullmatch(binding)
            or not isinstance(rid, str) or not ROLE_RE.fullmatch(rid)
            or kind not in REQUESTER_KINDS or not isinstance(chain, list)
            or len(chain) > MAX_CHAIN
            or not all(isinstance(c, str) and ROLE_RE.fullmatch(c) for c in chain)):
        raise BrokerFailed("broker_bad_response")
    return Requester(binding, rid, kind, tuple(chain))


def _check_runs(runs) -> None:
    if not isinstance(runs, list) or not 1 <= len(runs) <= MAX_RUNS:
        raise BrokerFailed("bad_request")
    for r in runs:
        if (not isinstance(r, dict)
                or set(r) != {"spec_digest", "mode", "task_kind", "task_text", "sites"}
                or not isinstance(r["spec_digest"], str)
                or not DIGEST_RE.fullmatch(r["spec_digest"])
                or r["mode"] not in ("account", "open_web")
                or not isinstance(r["task_kind"], str) or len(r["task_kind"]) > 60
                or not r["task_kind"].isprintable()
                or not isinstance(r["task_text"], str) or len(r["task_text"]) > 2000
                or not isinstance(r["sites"], list) or len(r["sites"]) > MAX_SITES):
            raise BrokerFailed("bad_request")
        for s in r["sites"]:
            if (not isinstance(s, dict) or set(s) != {"site_id", "access", "name"}
                    or not _printable(s["site_id"], 128)
                    or s["access"] not in ("account", "public")
                    or not _printable(s["name"], 64) or not s["name"].strip()):
                raise BrokerFailed("bad_request")


def _admission_answer(a: dict, statuses, runs: list | None, request_key: str | None,
                      confirmation_id: str | None = None) -> AdmissionAnswer:
    status = a.get("status")
    key = a.get("request_key")
    cid = a.get("confirmation_id")
    if status not in statuses:
        raise BrokerFailed("broker_bad_response")
    if key is not None and not _printable(key, REQUEST_KEY_MAX):
        raise BrokerFailed("broker_bad_response")
    if request_key is not None and key != request_key:
        raise BrokerFailed("broker_bad_response")
    if confirmation_id is not None:
        # Casa's answer always carries the id asked about (an unknown one
        # too, echoed with no request key).
        if cid != confirmation_id:
            raise BrokerFailed("broker_bad_response")
    elif cid is not None and not (isinstance(cid, str) and CONFIRMATION_RE.fullmatch(cid)):
        raise BrokerFailed("broker_bad_response")
    if status in ("confirmation_pending", "pending") and cid is None:
        raise BrokerFailed("broker_bad_response")
    admissions = None
    if status == "admitted":
        admissions = a.get("admissions")
        if not isinstance(admissions, list) or not all(
                isinstance(x, dict) and set(x) == {"spec_digest", "origin_kind", "origin_ref"}
                and isinstance(x["spec_digest"], str) and DIGEST_RE.fullmatch(x["spec_digest"])
                and x["origin_kind"] == "operator_telegram"
                and isinstance(x["origin_ref"], str) and ORIGIN_REF_RE.fullmatch(x["origin_ref"])
                for x in admissions):
            raise BrokerFailed("broker_bad_response")
        if runs is not None and [x["spec_digest"] for x in admissions] != [
                r["spec_digest"] for r in runs]:
            raise BrokerFailed("broker_bad_response")
    reason = a.get("reason") if status == "refused" else None
    if status == "refused" and reason != "not_delivered":
        raise BrokerFailed("broker_bad_response")
    return AdmissionAnswer(status, key, cid, admissions, reason)


def run_admission(request_key: str, runs: list[dict]) -> AdmissionAnswer:
    """`request_key` is the key persisted with the request: Casa answers a
    key it has seen with that first answer again (no second reading, no
    second card). An `admitted` answer must admit every run asked, in order."""
    if not _printable(request_key, REQUEST_KEY_MAX):
        raise BrokerFailed("bad_request")
    _check_runs(runs)
    a = _post(ROUTE_ADMISSION, {"request_key": request_key, "runs": runs}, BrokerFailed)
    return _admission_answer(a, ADMISSION_STATUSES, runs, request_key)


def run_admission_status(confirmation_id: str) -> AdmissionAnswer:
    """The confirmation's answer, identical on every read; an id Casa does
    not know reads `expired` (with no request key)."""
    if not _printable(confirmation_id, REQUEST_KEY_MAX):
        raise BrokerFailed("bad_request")
    a = _post(ROUTE_ADMISSION_STATUS, {"confirmation_id": confirmation_id}, BrokerFailed)
    return _admission_answer(a, CONFIRMATION_STATUSES, None, None, confirmation_id)


def request_continuation(binding_ref: str, run_ref: str, reason: str) -> str:
    """Queue a fresh Fido turn for `run_ref` under the binding; `queued`
    or `already_queued`."""
    if (not isinstance(binding_ref, str) or not BINDING_RE.fullmatch(binding_ref)
            or not _printable(run_ref, RUN_REF_MAX) or reason not in CONTINUE_REASONS):
        raise BrokerFailed("bad_request")
    a = _post(ROUTE_CONTINUE, {"binding_ref": binding_ref, "run_ref": run_ref,
                               "reason": reason}, BrokerFailed)
    status = a.get("status")
    if status not in ("queued", "already_queued"):
        raise BrokerFailed("broker_bad_response")
    return status


def delivery_status(reference: str) -> str:
    """What became of a deposit — `delivered`, `withheld`, `pending`, or
    `unknown` (Casa keeps outcomes in memory; a restart forgets them)."""
    if not isinstance(reference, str) or not REFERENCE_RE.fullmatch(reference):
        raise BrokerFailed("bad_request")
    a = _post(ROUTE_DELIVERY_STATUS, {"reference": reference}, BrokerFailed)
    status, repeat = a.get("status"), a.get("repeat")
    if status not in DELIVERY_STATUSES or not isinstance(repeat, bool):
        raise BrokerFailed("broker_bad_response")
    return status


def action_judgement(kind: str, packet: str, spec_digest: str | None,
                     picture: str | None = None) -> Judgement:
    """The check of one browsing step: the app's `judgement` kind and packet
    text, relayed untouched (Casa hashes exactly the string it receives), the
    run's admitted spec digest, and the page's picture when there is one (base64
    PNG, beside the packet, never in its digest). Waits up to `JUDGEMENT_TIMEOUT_S`. Any
    failure — Casa's `check_unavailable`, a typed request error, a timeout, a
    transport error, an answer of another shape — is `BrokerFailed`; this
    function never supplies a verdict of its own."""
    if (kind not in JUDGEMENT_KINDS or not isinstance(packet, str) or not packet
            or (spec_digest is not None and not _printable(spec_digest, REQUEST_KEY_MAX))):
        raise BrokerFailed("bad_request")
    fields = {"kind": kind, "packet": packet, "spec_digest": spec_digest}
    if picture is not None:
        fields["picture"] = picture
    a = _post(ROUTE_ACTION_JUDGEMENT, fields, BrokerFailed, timeout=JUDGEMENT_TIMEOUT_S)
    verdict, sha, latency = a.get("verdict"), a.get("packet_sha256"), a.get("latency_ms")
    if (not isinstance(verdict, dict) or not isinstance(sha, str)
            or not SHA256_HEX_RE.fullmatch(sha) or not isinstance(latency, int)
            or isinstance(latency, bool) or not 0 <= latency <= MAX_LATENCY_MS):
        raise BrokerFailed("broker_bad_response")
    return Judgement(verdict, sha, latency)


# -- the conversation's pad --------------------------------------------------------------

def _pad_section(section) -> None:
    if (not isinstance(section, str) or len(section) > PAD_SECTION_MAX
            or not (section == "" or section.isprintable())):
        raise BrokerFailed("bad_request")


def pad_write(section: str, text: str, *, append: bool = False) -> tuple[int, int]:
    """Write (or append) `text` to the pad section `section` of the conversation this
    call runs in: Casa finds the pad by the client id alone, never by an argument.
    A write replaces the section, creating it when it is missing. Returns Casa's
    `(lines, bytes)`. Text over the pad's cap is refused here, before any call."""
    _pad_section(section)
    if not isinstance(text, str) or len(text.encode("utf-8", "surrogatepass")) > PAD_MAX_BYTES:
        raise BrokerFailed("pad_full")
    a = _post(ROUTE_PAD, {"op": "append" if append else "write", "section": section,
                          "text": text}, BrokerFailed)
    lines, size = a.get("lines"), a.get("bytes")
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (lines, size)):
        raise BrokerFailed("broker_bad_response")
    return lines, size


def pad_read(section: str, offset: int = 0) -> tuple[str, int | None]:
    """One window of the pad section `section`: its text from `offset` (at most
    30,000 characters) and the offset of the next window, or None at its end."""
    _pad_section(section)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise BrokerFailed("bad_request")
    a = _post(ROUTE_PAD, {"op": "read", "section": section, "offset": offset}, BrokerFailed,
              max_bytes=PAD_RESPONSE_BYTES)
    text, nxt = a.get("text"), a.get("next")
    if (not isinstance(text, str) or len(text) > PAD_READ_CHARS
            or not (nxt is None or (isinstance(nxt, int) and not isinstance(nxt, bool)
                                    and nxt > offset))):
        raise BrokerFailed("broker_bad_response")
    return text, nxt


def pad_text(section: str, limit: int) -> str:
    """The whole pad section, read window by window; `pad_too_long` once it passes
    `limit` characters (nothing past that is fetched)."""
    whole, offset = "", 0
    while True:
        text, nxt = pad_read(section, offset)
        whole += text
        if len(whole) > limit:
            raise BrokerFailed("pad_too_long")
        if nxt is None:
            return whole
        offset = nxt


# -- label and caption fitting -------------------------------------------------------------

def _one_line(text) -> str:
    """Printable, single line, whitespace collapsed, with no `://` and no `www.`;
    replacements run until nothing changes."""
    s = "".join(ch if ch.isprintable() else " "
                for ch in ("" if text is None else str(text)))
    s = " ".join(s.split())
    while True:
        new = _WWW_DOT_RE.sub(r"\1", s.replace("://", ": //"))
        if new == s:
            return s
        s = new


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:max(limit - 1, 0)].rstrip() + _ELLIPSIS


def fit_caption(text, limit: int = MAX_CAPTION_CHARS) -> str:
    """`text` as a caption Casa accepts, at most `limit` characters."""
    return _clip(_one_line(text), min(limit, MAX_CAPTION_CHARS))


def fit_label(prefix: str, name) -> str:
    """`prefix` followed by `name`, as a label Casa accepts: dots removed from
    `name` (before `_one_line`, so removal cannot join a scheme separator),
    `name` clipped so `prefix` survives."""
    room = MAX_LABEL_CHARS - len(prefix)
    raw = "" if name is None else str(name)
    return prefix + _clip(_one_line(raw.replace(".", "")), room)

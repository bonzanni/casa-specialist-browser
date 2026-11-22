"""A test double of Casa Browser's `/v1` API (protocol 1), in memory.

It serves the app's route table with the wire names the plugin consumes, and
the app's gates as the app states them: every route needs the bearer secret
except `POST /v1/pair/claim` while a pairing window is armed; every POST needs
`X-Casa-Browser-Protocol: 1`, refused `protocol_mismatch` before any side
effect; hello needs the bearer but not the header; a run's admission digest
must be RFC 8785 + SHA-256 of the RunSpec members except `admission` (checked
here with the `rfc8785` package, independently of the plugin's own code).

The action gate, as Casa Browser builds it: with `judge` set, an
action, a download or a search answers `judgement_required` with the app's
canonical packet text, frozen as the run's one pending judgement; the control
verdict route `POST /v1/control/runs/{run}/judgements/{id}` applies a verdict
for that id once (`digest_mismatch` for another packet's digest,
`judgement_stale` for any other id), stores the complete response before a
successor replaces the pending judgement, and answers a repeat of a stored id
with that response plus `repeat: true`, dispatching nothing again.

The question: an `ask` verdict creates a question whose view
`GET /v1/control/runs/{run}/questions/{question}` serves; its answer route is a
compare-and-set (a repeat of an answered revision returns the stored response
with `repeat: true`; a non-open question is `not_current`; a run whose operator
has the live view open is `not_ready`); a confirm answers a `rejudge`
judgement, applied through the verdict route (`same` presses once, anything
else lapses the question into a fresh judgement); the live route opens an
assist view from the open question, its response stored per `(question,
revision, n)`.

Logins: a judgement the app starts itself (`login_item`, `signin`) is served by
`GET /v1/control/runs/{run}/judgements/pending` (a `login_item` only while the run is running)
and applied once through the same verdict route: a `login_item` with candidates asks the login
question (`kind: login_item`, the host and the titles), none is `login_needed`; a `signin`
`signed_in` is `credential_saved` (or `credential_not_saved` for a kept item), anything else
`not_saved`. The login question's answer `{"item": i}` is `linked`.

Errors answer `{"error": "<code>"}` (the same assumption as the plugin's
`wire.py`). Every request lands in `calls` before it is judged, so a test can
assert that a refused request had no effect and that a client made no call.

It runs aiohttp on its own loop in a background thread, because the plugin's
client is synchronous `http.client`. It is a stand-in: no test here runs the
client against the real application.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
import hmac
import io
import itertools
import json
import re
import secrets
import threading
import time
import zipfile
from typing import Any

import rfc8785
from aiohttp import web

PROTOCOL_HEADER = "X-Casa-Browser-Protocol"
ID = r"{%s:[A-Za-z0-9_-]{1,64}}"
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
ACTION_MEMBERS = {"kind", "handle", "generation", "text", "choice_handle", "direction",
                  "amount"}
#: Casa Browser's `protocol.printable_text`: no C0/C1 control character, no U+2028/2029
NOT_PRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
DIGEST_HEX = re.compile(r"^[0-9a-f]{64}$")
RUNSPEC_MEMBERS = {"request_id", "requester_id", "mode", "task_text", "task_kind", "sites",
                   "result_expectation", "admission"}


@dataclasses.dataclass
class Call:
    method: str
    path: str
    query: str
    headers: dict
    body: Any


def _err(code: str, status: int) -> web.Response:
    return web.json_response({"error": code}, status=status)


def digest_of(members: dict) -> str:
    return "sha256:" + hashlib.sha256(rfc8785.dumps(members)).hexdigest()


class FakeApp:
    def __init__(self) -> None:
        self.secret: str | None = None
        self.armed = False
        self.hello = {"protocol_version": 1, "app_version": "0.1.0",
                      "configured": True, "needs_locale": False,
                      "language_source": "home_assistant"}
        self.calls: list[Call] = []
        #: hello replies served (in order) before falling back to `hello`
        self.hello_queue: list[dict] = []
        #: (method, path) -> (status, body bytes/str or JSON-able, content type)
        self.overrides: dict[tuple[str, str], tuple[int, Any, str]] = {}
        #: (method, path) -> number of further requests answered 500 (no JSON)
        self.fail_next: dict[tuple[str, str], int] = {}
        self.sites: list[dict] = [
            {"site_id": "s1", "display_name": "Utility Co", "status": "ready"},
            {"site_id": "s2", "display_name": "Bank Example", "status": "ready"},
            {"site_id": "pub", "display_name": "Public Site", "status": "ready"}]
        self.runs: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.files: dict[str, dict] = {}
        self.notices: list[dict] = []
        self.notices_page = 0
        self.sources: dict[str, list[dict]] = {}
        self.screenshots: dict[tuple[str, str], bytes] = {}
        #: what the page looks like now (`POST …/screenshots`)
        self.page_png = b"\x89PNG\r\n\x1a\n" + b"PAGE" * 8
        self.deliveries: list[dict] = []
        self.forgets: list[dict] = []
        self.records: list[dict] = []
        self.observation = {"page_handle": "p1", "generation": 1, "text": "Welcome",
                            "elements": [{"handle": "e1", "role": "link",
                                          "label_masked": "Invoices"}],
                            "source_id": None}
        #: answer actions with an Observation instead of {outcome, generation}
        self.action_returns_observation = False
        self.event_page: int | None = None
        #: called with the RunSpec body as each POST /v1/runs arrives (before it is judged)
        self.on_create = None
        #: ordinals (1-based) of POST /v1/runs to answer 500 before any effect
        self.fail_creates: set[int] = set()
        self.create_count = 0
        # -- the action gate --------------------------------------------------
        #: a judged search is a signed-in address: its verdict's `done` carries no observation
        self.search_is_signed_in_address = False
        #: what `POST …/fetch` was sent, and a whole reply to answer it with (None: a text page)
        self.fetch_bodies = []
        self.fetch_reply = None
        #: answer actions (but scroll), downloads and searches `judgement_required`
        self.judge = False
        #: run_id -> the pending judgement {id, digest, packet, op, ...}
        self.pending: dict[str, dict] = {}
        #: (run_id, judgement id) -> the complete stored response
        self.responses: dict[tuple[str, str], dict] = {}
        #: how each successive `go` ends: done | changed (judged afresh) | moved |
        #: unknown | lapsed | blocked; empty means done
        self.go_results: list[str] = []
        #: verdict replies to lose in transport AFTER the response was stored
        self.lose_verdict_replies = 0
        #: seconds a verdict's application takes (a go that runs its full time)
        self.verdict_delay_s = 0.0
        #: every dispatch a verdict caused, in order (a replay adds none)
        self.dispatches: list[tuple[str, str]] = []
        # -- the question ------------------------------------------
        #: question_id -> {run_id, revision, digest, state, view, response, op}
        self.questions: dict[str, dict] = {}
        #: the next question's screenshot id (its PNG is served from `screenshots`)
        self.question_shot: str | None = None
        #: members of the next question's view to replace
        self.question_view: dict = {}
        #: how each successive `same` re-judgement ends: done | unknown | cut (the press may
        #: have happened and the work was cut: `interrupted`); empty means done
        self.rejudge_results: list[str] = []
        #: answers whose question is found already done by the operator (target gone)
        self.answer_gone = False
        #: members of a lapsed answer's reply (`reason`, `copy`)
        self.answer_lapse: dict | None = None
        #: answer replies to lose in transport AFTER the response was stored
        self.lose_answer_replies = 0
        #: every answer body received, in order
        self.answers: list[dict] = []
        #: (question_id, revision, n) -> the stored live response
        self.live_responses: dict[tuple, dict] = {}
        # -- logins ------------------------------------------------
        #: run_id -> the app-started judgements: {id, kind, packet, digest, meta}
        self.app_pending: dict[str, list[dict]] = {}
        # -- signing in off the start page (Casa Browser 0.1.7) --------------------------------
        #: every observe body received, in order
        self.observe_bodies: list = []
        #: where a scroll says it left the page
        self.scroll_position: dict | None = None
        #: what a search says of itself
        self.find_answer: dict = {"whole": True, "left": 0}
        #: an observe with `sign_in` looks at the page: True answers the next one with a
        #: `sign_in` judgement (once); its `sign_in` verdict signs in (the notice below)
        self.sign_in_look = False
        self.sign_in_notice = "Casa Browser signed in to Site A by itself."
        #: every sign_in verdict applied, in order
        self.sign_in_verdicts: list = []
        # -- sign-in codes -----------------------------------------
        #: run_id -> the site whose sign-in waits for a code it sent (the active listing's
        #: `code_site`); `POST …/login-code` for exactly that site uses it up
        self.code_sites: dict[str, str] = {}
        # -- general abilities -------------------------------------------------
        #: the open judgement's picture (`GET …/judgements/{id}/picture`): bytes, an error code
        #: to answer instead, or None for not_found
        self.judgement_picture: bytes | str | None = None
        #: the run_look answer, and every body it received
        self.look_picture: str | None = None
        self.look_bodies: list[dict] = []
        #: what run_read reads, whether Casa Browser could read it whole, and every body received
        self.read_source = ""
        self.rows_source = ""
        self.read_whole = True
        self.read_masked = False
        self.read_bodies: list[dict] = []
        #: run_id -> the plan the operator approved (the active listing's `approved`)
        self.approved: dict[str, str] = {}
        self.stale_next_action = False
        self.fail_question_view = False
        #: the next code fill's reply (outcome, and `reason` for login_needed)
        self.code_reply: dict = {"outcome": "done"}
        #: the sign-in card — the host the browser shows, the vault titles (none: the
        #: card at once), a refusal outcome to answer instead, and every site_name received
        self.sign_in_host = "login.cloudco.example"
        self.sign_in_titles: list[str] = []
        self.sign_in_refusal: dict | None = None
        self.sign_in_names: list[str] = []
        self.sign_in_answer = "signing_in"          # the Yes's outcome
        #: test-private: every code the route received
        self.codes: list[str] = []
        # -- a Stop's request cancels (Casa Browser 0.1.8) -------------
        #: the request ids a cancel won (durable in the app): a later create of one is born
        #: cancelled, and every repeat of its cancel answers `stopped: true`
        self.cancelled_requests: set[str] = set()
        #: called with each Call as it arrives, before any gate (a test's interleaving)
        self.on_request = None
        self._ids = itertools.count(1)
        self._loop = None
        self._runner = None
        self._thread = None
        self.port: int | None = None

    # -- test controls -------------------------------------------------------
    def arm(self) -> None:
        self.armed = True

    def pair(self, secret: str | None = None) -> str:
        self.secret = secret or secrets.token_urlsafe(32)
        return self.secret

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def new_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def paths(self) -> list[str]:
        return [c.path for c in self.calls]

    def posts(self, suffix: str = "") -> list[Call]:
        return [c for c in self.calls if c.method == "POST" and c.path.endswith(suffix)]

    def set_state(self, run_id: str, state: str, pending_question=None) -> None:
        self.runs[run_id]["state"] = state
        self.runs[run_id]["pending_question"] = pending_question

    def add_event(self, run_id: str, kind: str, **fields) -> dict:
        e = {"seq": len(self.events[run_id]) + 1, "run_id": run_id, "kind": kind,
             "origin": None, "handle": None, "text_masked": "", "outcome": "",
             "screenshot_id": None,
             # the app stamps a step when it records it, in whole seconds (protocol.rfc3339)
             "at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
             .isoformat().replace("+00:00", "Z")}
        e.update(fields)
        self.events[run_id].append(e)
        return e

    def add_file(self, run_id: str, data: bytes, safe_name: str, mime: str) -> str:
        file_id = self.new_id("file")
        self.files[file_id] = {"file_id": file_id, "run_id": run_id,
                               "requester_id": self.runs[run_id]["spec"]["requester_id"],
                               "safe_name": safe_name, "mime": mime, "size": len(data),
                               "sha256": hashlib.sha256(data).hexdigest(),
                               "source_handle": None, "bytes": data}
        return file_id

    @staticmethod
    def public(rec: dict) -> dict:
        return {k: v for k, v in rec.items() if k != "bytes"}

    # -- gates ---------------------------------------------------------------
    @web.middleware
    async def _gate(self, request: web.Request, handler):
        try:
            body = await request.json() if request.can_read_body else None
        except Exception:  # noqa: BLE001
            body = "<not json>"
        self.calls.append(Call(request.method, request.path, request.query_string,
                               dict(request.headers), body))
        if self.on_request is not None:
            self.on_request(self.calls[-1])
        key = (request.method, request.path)
        over = self.overrides.get(key)
        if over is not None:
            status, payload, ctype = over
            if isinstance(payload, (bytes, str)):
                return web.Response(status=status, body=payload, content_type=ctype)
            return web.json_response(payload, status=status)
        if self.fail_next.get(key, 0) > 0:
            self.fail_next[key] -= 1
            return web.Response(status=500, body=b"boom", content_type="text/plain")
        claim = request.method == "POST" and request.path == "/v1/pair/claim"
        if not (claim and self.armed):
            auth = request.headers.get("Authorization", "")
            ok = (self.secret is not None and auth.startswith("Bearer ")
                  and hmac.compare_digest(auth[7:].encode(), self.secret.encode()))
            if not ok:
                return _err("unauthorized", 401)
        if request.method != "GET" and request.headers.get(PROTOCOL_HEADER) != "1":
            return _err("protocol_mismatch", 400)
        if request.match_info.http_exception is not None:
            # a wrong method or an unknown path, AFTER the bearer and protocol gates
            # (as the real app answers): not_found
            return _err("not_found", 404)
        request["json"] = body
        return await handler(request)

    # -- routes --------------------------------------------------------------
    async def hello_route(self, request):
        if self.hello_queue:
            return web.json_response(self.hello_queue.pop(0))
        return web.json_response(self.hello)

    async def claim(self, request):
        body = request["json"]
        if (not isinstance(body, dict) or set(body) != {"caller_label"}
                or not isinstance(body["caller_label"], str)
                or len(body["caller_label"]) > 64):
            return _err("unsupported", 400)
        self.armed = False
        self.secret = secrets.token_urlsafe(32)
        return web.json_response({"secret": self.secret, "protocol_version": 1})

    async def sites_route(self, request):
        return web.json_response({"sites": self.sites})

    async def create_run(self, request):
        spec = request["json"]
        if self.on_create is not None:
            self.on_create(spec)
        self.create_count += 1
        if self.create_count in self.fail_creates:
            return web.Response(status=500, body=b"boom", content_type="text/plain")
        if not isinstance(spec, dict) or not RUNSPEC_MEMBERS <= set(spec) <= RUNSPEC_MEMBERS | {
                "start_url"}:
            return _err("unsupported", 400)
        if "start_url" in spec and spec["mode"] != "open_web":     # the app's rule
            return _err("bad_request", 400)
        adm = spec["admission"]
        if (not isinstance(adm, dict) or adm.get("origin_kind") != "operator_telegram"
                or not adm.get("origin_ref")):
            return _err("unsupported", 400)
        members = {k: v for k, v in spec.items() if k != "admission"}
        if adm.get("admitted_spec_digest") != digest_of(members):
            return _err("blocked", 403)
        for run in self.runs.values():
            if run["spec"]["request_id"] == spec["request_id"]:
                if run["spec"] != spec:
                    return _err("conflict", 409)
                return web.json_response({"run_id": run["run_id"], "state": run["state"]})
        run_id = self.new_id("run")
        born = "cancelled" if spec["request_id"] in self.cancelled_requests else "queued"
        self.runs[run_id] = {"run_id": run_id, "spec": spec, "state": born,
                             "created_at": time.time() + len(self.runs) * 1e-3,
                             "pending_question": None,
                             "finish": None, "cancel": None}
        self.events[run_id] = []
        return web.json_response({"run_id": run_id, "state": born})

    async def list_runs(self, request):
        if request.query.get("state") != "active":
            return _err("unsupported", 400)
        runs = [{"run_id": r["run_id"], "request_id": r["spec"]["request_id"],
                 "state": r["state"], "created_at": r["created_at"],
                 "pending_question": r.get("pending_question"),
                 "code_site": self.code_sites.get(r["run_id"]),
                 "approved": self.approved.get(r["run_id"])}
                for r in sorted(self.runs.values(), key=lambda r: r["created_at"])
                if r["state"] not in TERMINAL]
        return web.json_response({"runs": runs})

    def _run(self, request):
        return self.runs.get(request.match_info["run"])

    async def get_run(self, request):
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        import datetime
        when = datetime.datetime.fromtimestamp(run["created_at"], datetime.timezone.utc)
        return web.json_response({"run_id": run["run_id"], "request_id": run["spec"]["request_id"],
                                  "state": run["state"],
                                  "created_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  "mode": run["spec"]["mode"],
                                  "stopped": run["spec"]["request_id"] in self.cancelled_requests})

    async def run_events(self, request):
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        after = int(request.query.get("after", "0"))
        events = [e for e in self.events[run["run_id"]] if e["seq"] > after]
        if self.event_page is not None:          # the app's per-fetch cap (store.events_after)
            events = events[:self.event_page]
        return web.json_response({"events": events})

    async def observe(self, request):
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        body = request["json"]
        self.observe_bodies.append(body)
        if not isinstance(body, dict) or set(body) - {"sign_in", "find"} or not isinstance(
                body.get("sign_in", False), bool) or not (
                    "find" not in body or (isinstance(body["find"], str)
                                           and 1 <= len(body["find"]) <= 200)):
            return _err("unsupported", 400)
        if body.get("sign_in") and self.sign_in_look:
            self.sign_in_look = False
            packet = {"kind": "sign_in", "page": {"blocks": [{"n": 1, "text": "Login Page"}]},
                      "planned": {"fields": [{"role": "username", "label": "Username"}],
                                  "button": "Login", "passkey": False}}
            text = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            jid = self.new_id("j")
            self.app_pending.setdefault(run["run_id"], []).append(
                {"id": jid, "kind": "sign_in", "packet": text, "meta": {},
                 "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()})
            return web.json_response({"outcome": "judgement_required",
                                      "generation": self.observation["generation"],
                                      "judgement": {"id": jid, "kind": "sign_in",
                                                    "packet": text}})
        if "find" in body:
            # The app's search: its own answer on each element, over the whole page.
            words = body["find"].casefold()
            found = [{**el, "found": words in
                      f'{el["label_masked"]} {el.get("value_masked") or ""}'.casefold()}
                     for el in self.observation["elements"]]
            return web.json_response({**self.observation, "elements": found, "find": self.find_answer})
        return web.json_response(self.observation)

    async def action(self, request):
        if self._run(request) is None:
            return _err("not_found", 404)
        body = request["json"]
        if not isinstance(body, dict) or set(body) - {"enter", "plan", "ask_first"} != ACTION_MEMBERS:
            return _err("unsupported", 400)
        if body.get("ask_first", False) is not True and "ask_first" in body:
            return _err("unsupported", 400)        # the app's optional member: true when sent
        typed, plan = body["text"], body.get("plan")
        if (isinstance(typed, str) and (len(typed) > 32_000 or (
                len(typed) > 2000 and not ("\n" in typed or "\r" in typed)))) or (
                plan is not None and not 1 <= len(plan) <= 12_000):
            return _err("unsupported", 400)
        enter = body.get("enter", False)
        if not isinstance(enter, bool) or (enter and body["kind"] != "type"):
            return _err("unsupported", 400)
        if isinstance(typed, str) and NOT_PRINTABLE.search(typed.replace("\n", "").replace("\r", "")):
            return _err("unsupported", 400)
        if plan is not None and NOT_PRINTABLE.search(plan.replace("\n", "").replace("\r", "")):
            return _err("unsupported", 400)
        if (body["kind"] == "scroll") != (body["direction"] is not None
                                          and body["amount"] is not None):
            return _err("unsupported", 400)
        if self._run(request)["state"] in ("waiting_approval", "waiting_live", "live"):
            return _err("not_ready", 409)          # the app's `act` takes a running run only
        if self.stale_next_action:
            self.stale_next_action = False
            return _err("stale_handle", 409)
        if body["generation"] != self.observation["generation"]:
            return _err("stale_handle", 409)
        if self.judge and body["kind"] != "scroll":
            return web.json_response(self._freeze(request.match_info["run"], "act",
                                                  {"kind": body["kind"],
                                                   "then_enter": enter}, plan=plan))
        if self.action_returns_observation:
            return web.json_response(self.observation)
        self.observation["generation"] += 1
        done = {"outcome": "done", "generation": self.observation["generation"]}
        if body["kind"] == "scroll" and self.scroll_position is not None:
            done["position"] = self.scroll_position
        return web.json_response(done)

    async def search(self, request):
        if self._run(request) is None:
            return _err("not_found", 404)
        body = request["json"]
        if not isinstance(body, dict) or set(body) != {"query"} or len(body["query"]) > 300:
            return _err("unsupported", 400)
        if self.judge:
            op = "address" if self.search_is_signed_in_address else "search"
            return web.json_response(self._freeze(request.match_info["run"], op,
                                                  {"kind": "navigate"}))
        return web.json_response(self.observation)

    async def fetch(self, request):
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        body = request["json"]
        self.fetch_bodies.append(body)
        if not isinstance(body, dict) or "url" not in body or set(body) - {"url", "fields"}:
            return _err("unsupported", 400)
        if self.fetch_reply is not None:
            return web.json_response(self.fetch_reply)
        file_id = self.add_file(run["run_id"], self.read_source.encode(), "page.html", "text/html")
        end = 30_000
        more = end < len(self.read_source)
        return web.json_response({
            "outcome": "done", "generation": self.observation["generation"],
            "file": self.public(self.files[file_id]), "url": "https://a.example/answered",
            "source_id": "src1",
            "text": self.read_source[:end], "next_offset": end if more else None,
            "complete": self.read_whole and not more, "masked": self.read_masked})

    async def download(self, request):
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        if self.judge:
            return web.json_response(self._freeze(run["run_id"], "download",
                                                  {"kind": "download"}))
        file_id = self.add_file(run["run_id"], b"%PDF-1.4 fake", "invoice-march.pdf",
                                "application/pdf")
        return web.json_response(self.public(self.files[file_id]))

    def _site_question(self, run: dict, digest: str, item: str | None) -> dict:
        qid = self.new_id("q")
        run["state"], run["pending_question"] = "waiting_approval", qid
        host = self.sign_in_host
        first = (f"Sign in to {host} (Cloud Co) with your 1Password item \"{item}\"?" if item
                 else f"Sign in to {host} (Cloud Co)?")
        view = {"kind": "site_signin", "host": host, "name": "Cloud Co", "item": item,
                "text": first + "\nYes saves it as one of your sites in Casa Browser.",
                "options": [], "confirm_label": "", "extra_options": [], "highlight": [],
                "screenshot_id": None, "confirm_offered": False, "live_only_copy": None,
                "category": "login"}
        self.questions[qid] = {"run_id": run["run_id"], "revision": 1, "digest": digest,
                               "state": "open", "view": view, "response": None, "op": "site"}
        return {"outcome": "asked", "generation": self.observation["generation"],
                "question_id": qid}

    async def sign_in(self, request):
        """The sign-in card for the public page the run is on."""
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        body = request["json"]
        if (not isinstance(body, dict) or set(body) != {"site_name"}
                or not isinstance(body["site_name"], str) or not 0 < len(body["site_name"]) <= 60):
            return _err("unsupported", 400)
        if run["state"] != "running":
            return _err("not_ready", 503)
        self.sign_in_names.append(body["site_name"])
        gen = self.observation["generation"]
        if self.sign_in_refusal is not None:
            return web.json_response({**self.sign_in_refusal, "generation": gen})
        if self.sign_in_titles:
            packet = {"kind": "login_item",
                      "site": {"name": body["site_name"], "sign_in_host": self.sign_in_host,
                               "registered_origins": ["https://" + self.sign_in_host]},
                      "items": [{"index": i, "title": t, "website_hosts": []}
                                for i, t in enumerate(self.sign_in_titles)],
                      "items_truncated": False}
            self.add_app_judgement(run["run_id"], "login_item", packet, proposal=True,
                                   titles=list(self.sign_in_titles))
            return web.json_response({"outcome": "judging", "generation": gen})
        return web.json_response(self._site_question(run, "0" * 64, None))

    async def look(self, request):
        if self._run(request) is None:
            return _err("not_found", 404)
        self.look_bodies.append(request["json"])
        if set(request["json"]) - {"handle"}:
            return _err("unsupported", 400)
        return web.json_response({"picture": self.look_picture})

    async def read(self, request):
        if self._run(request) is None:
            return _err("not_found", 404)
        body = request["json"]
        self.read_bodies.append(body)
        if (set(body) - {"handle", "file_id", "offset", "rows"}
                or ("handle" in body) + ("file_id" in body) + (body.get("rows") is True) > 1):
            return _err("unsupported", 400)
        offset = body.get("offset", 0)
        end = offset + 30_000
        source = self.rows_source if body.get("rows") else self.read_source
        more = end < len(source)
        return web.json_response({"text": source[offset:end],
                                  "next_offset": end if more else None,
                                  "complete": self.read_whole and not more,
                                  "masked": self.read_masked})

    async def judgement_picture_route(self, request):
        p = self.pending.get(request.match_info["run"])
        if p is None or p["id"] != request.match_info["judgement"]:
            return _err("not_found", 404)
        if self.judgement_picture is None:
            return _err("not_found", 404)
        if isinstance(self.judgement_picture, str):
            return _err(self.judgement_picture, 500)
        return web.Response(body=self.judgement_picture, content_type="image/png")

    async def login_code(self, request):
        """The code for the site whose sign-in asked for it in this run."""
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        body = request["json"]
        if (not isinstance(body, dict) or set(body) != {"code", "site_id"}
                or not isinstance(body["code"], str)
                or not re.fullmatch(r"[A-Za-z0-9-]{4,10}", body["code"])
                or not isinstance(body["site_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", body["site_id"])):
            return _err("unsupported", 400)
        if run["state"] != "running" or self.code_sites.get(run["run_id"]) != body["site_id"]:
            return _err("not_ready", 503)
        del self.code_sites[run["run_id"]]
        self.codes.append(body["code"])
        reply = dict(self.code_reply)
        if reply.get("outcome") == "login_needed" and reply.get("reason") == "needs_code":
            self.code_sites[run["run_id"]] = body["site_id"]
        self.add_event(run["run_id"], "login_code",
                       outcome="logged_in" if reply["outcome"] == "done" else "not_signed_in",
                       text_masked="A sign-in code was given.")
        return web.json_response({**reply, "generation": self.observation["generation"]})

    # -- the action gate -----------------------------------------------------
    def _freeze(self, run_id: str, op: str, action: dict, plan: str | None = None) -> dict:
        """The run's one pending judgement (a new one supersedes it), its packet the
        app's canonical JSON text (non-ASCII kept, as the app serialises it)."""
        packet = {"action": action, "page": {"blocks": [{"text": "Rechnung März €12,50"}],
                                             "controls": []},
                  "target": {"name": "Weiter"}, "task": "Download the March invoice."}
        if plan is not None:
            packet["plan"] = plan
        text = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        jid = self.new_id("j")
        self.pending[run_id] = {"id": jid, "op": op, "packet": text,
                                "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        return {"outcome": "judgement_required", "generation": self.observation["generation"],
                "judgement": {"id": jid, "kind": "action", "packet": text}}

    def add_app_judgement(self, run_id: str, kind: str, packet: dict, **meta) -> str:
        """Freeze an app-started judgement; ``meta``: ``host``/``titles`` for a
        login_item, ``kept=True`` for a signin whose item is kept, ``site``."""
        text = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        jid = self.new_id("j")
        self.app_pending.setdefault(run_id, []).append(
            {"id": jid, "kind": kind, "packet": text,
             "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(), "meta": meta})
        return jid

    async def pending_judgements(self, request):
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        return web.json_response({"judgements": [
            {"id": j["id"], "kind": j["kind"], "packet": j["packet"]}
            for j in self.app_pending.get(run["run_id"], ())
            if j["kind"] != "sign_in"
            and (j["kind"] != "login_item" or run["state"] == "running")]})

    def _apply_app(self, run: dict, j: dict, verdict: dict | None) -> dict:
        gen = self.observation["generation"]
        meta = j["meta"]
        if j["kind"] == "login_item" and meta.get("proposal"):
            items = (verdict or {}).get("items") or []
            return self._site_question(run, j["digest"],
                                       meta["titles"][items[0]] if len(items) == 1 else None)
        if j["kind"] == "login_item":
            items = (verdict or {}).get("items") or []
            if not items:
                return {"outcome": "login_needed", "generation": gen}
            qid = self.new_id("q")
            run["state"], run["pending_question"] = "waiting_approval", qid
            titles = [meta["titles"][i] for i in items]
            view = {"kind": "login_item", "host": meta["host"], "options": titles,
                    "text": "Which 1Password item?", "confirm_label": "", "extra_options": [],
                    "highlight": [], "screenshot_id": None, "confirm_offered": False,
                    "live_only_copy": None, "category": "login"}
            self.questions[qid] = {"run_id": run["run_id"], "revision": 1,
                                   "digest": j["digest"], "state": "open", "view": view,
                                   "response": None, "op": "login"}
            return {"outcome": "asked", "generation": gen, "question_id": qid}
        if (verdict or {}).get("verdict") != "signed_in":
            return {"outcome": "not_saved", "generation": gen}
        return {"outcome": "credential_not_saved" if meta.get("kept") else "credential_saved",
                "generation": gen, "site": meta.get("site", "Site A")}

    async def judgement(self, request):
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        run_id, jid, body = run["run_id"], request.match_info["judgement"], request["json"]
        if isinstance(body, dict) and "no_verdict" in body:
            if body != {"no_verdict": "check_unavailable"}:
                return _err("unsupported", 400)
        elif (not isinstance(body, dict) or not {"packet_sha256", "verdict"} <= set(body)
              or set(body) - {"packet_sha256", "verdict", "latency_ms"}
              or not isinstance(body["packet_sha256"], str)
              or not DIGEST_HEX.fullmatch(body["packet_sha256"])
              or not isinstance(body["verdict"], dict)):
            return _err("unsupported", 400)
        stored = self.responses.get((run_id, jid))
        if stored is not None:
            return self._answer(request, dict(stored, repeat=True))
        app = next((j for j in self.app_pending.get(run_id, ()) if j["id"] == jid), None)
        if app is not None:
            if "no_verdict" not in body and body["packet_sha256"] != app["digest"]:
                return _err("digest_mismatch", 409)
            if app["kind"] == "sign_in":
                self.app_pending[run_id].remove(app)
                v = (body.get("verdict") or {}).get("verdict")
                self.sign_in_verdicts.append(v)
                obs = dict(self.observation, notice=self.sign_in_notice if v == "sign_in" else None)
                response = {"outcome": "done", "generation": obs["generation"], "observation": obs}
                self.responses[(run_id, jid)] = response
                return self._answer(request, response)
            if app["kind"] == "login_item" and run["state"] != "running":
                return _err("not_ready", 409)
            self.app_pending[run_id].remove(app)
            response = self._apply_app(run, app, body.get("verdict"))
            self.responses[(run_id, jid)] = response
            return self._answer(request, response)
        p = self.pending.get(run_id)
        if p is None or p["id"] != jid:
            return _err("judgement_stale", 409)
        if "no_verdict" not in body and body["packet_sha256"] != p["digest"]:
            return _err("digest_mismatch", 409)
        del self.pending[run_id]
        if self.verdict_delay_s:
            await asyncio.sleep(self.verdict_delay_s)
        # a successor (judged afresh) is frozen by `_apply`; the response is stored with it,
        # in the same loop step, before any other request can read either
        response = self._apply(run, p, body)
        if "__error__" in response:
            return _err(response["__error__"], 409)
        self.responses[(run_id, jid)] = response
        return self._answer(request, response)

    def _answer(self, request, response: dict, lose: str = "verdict") -> web.Response:
        if lose == "verdict" and self.lose_verdict_replies > 0:
            self.lose_verdict_replies -= 1
            request.transport.close()          # the reply is lost in transport
        elif lose == "answer" and self.lose_answer_replies > 0:
            self.lose_answer_replies -= 1
            request.transport.close()
        return web.json_response(response)

    def _apply(self, run: dict, p: dict, body: dict) -> dict:
        gen = self.observation["generation"]
        verdict = body.get("verdict")
        if p.get("kind") == "rejudge":
            return self._apply_rejudge(run, p, verdict or {})
        if verdict is None:
            return {"outcome": "check_unavailable", "generation": gen}
        v = verdict.get("verdict")
        if v == "look_first":
            return {"outcome": "look_first", "generation": gen,
                    "note": str(verdict.get("look_first") or verdict.get("reason") or "")}
        if v == "ask" or (v == "go" and verdict.get("category") not in ("reading", "local")):
            qid = self.new_id("q")
            run["state"], run["pending_question"] = "waiting_approval", qid
            q = verdict.get("question") if v == "ask" else None
            view = {"text": (q or {}).get("text") or "Ellen was about to press a control.",
                    "confirm_label": (q or {}).get("confirm_label", ""),
                    "extra_options": list((q or {}).get("extra_options") or []),
                    "highlight": list((q or {}).get("highlight") or []),
                    "screenshot_id": self.question_shot,
                    "confirm_offered": q is not None and not q.get("text_incomplete"),
                    "live_only_copy": None if q is not None and not q.get("text_incomplete")
                    else "I can't check the amount on this page myself, so please finish "
                         "it in the live view.",
                    "category": verdict.get("category", "unclear")}
            plan = json.loads(p["packet"]).get("plan")
            if plan and view["category"] == "account_change" and view["confirm_offered"]:
                view.update(plan=plan, screenshot_id=None)     # a plan card has no picture
            view.update(self.question_view)
            self.questions[qid] = {"run_id": run["run_id"], "revision": 1,
                                   "digest": p["digest"], "state": "open", "view": view,
                                   "response": None, "op": p["op"]}
            return {"outcome": "asked", "generation": gen, "question_id": qid}
        if v != "go":
            return {"outcome": "check_unavailable", "generation": gen}
        result = self.go_results.pop(0) if self.go_results else "done"
        if result == "changed":                # judged afresh on the current page
            return self._freeze(run["run_id"], p["op"], {"kind": p["op"]})
        if result in ("moved", "lapsed", "blocked"):
            return {"outcome": "lapsed" if result == "lapsed" else "not_run",
                    "generation": gen, "reason": "changed" if result == "lapsed" else result}
        self.dispatches.append((run["run_id"], p["id"]))
        if result == "unknown":
            return {"outcome": "outcome_unknown", "generation": gen}
        self.observation["generation"] = gen + 1
        response = {"outcome": "done", "generation": gen + 1}
        if p["op"] == "download":
            file_id = self.add_file(run["run_id"], b"%PDF-1.4 fake", "invoice-march.pdf",
                                    "application/pdf")
            response["file"] = self.public(self.files[file_id])
        elif p["op"] == "search":
            response["observation"] = dict(self.observation)
        return response

    # -- the question ----------------------------------------------
    def _apply_rejudge(self, run: dict, p: dict, verdict: dict) -> dict:
        gen = self.observation["generation"]
        q = self.questions[p["question_id"]]
        if verdict.get("verdict") != "same" or verdict.get("text_incomplete"):
            q["state"] = "lapsed"
            run["state"], run["pending_question"] = "running", None
            return self._freeze(run["run_id"], q["op"], {"kind": "click"})
        result = self.rejudge_results.pop(0) if self.rejudge_results else "done"
        self.dispatches.append((run["run_id"], p["id"]))
        run["state"], run["pending_question"] = "running", None
        if result in ("unknown", "cut"):
            q["state"] = "outcome_unknown"
            if result == "cut":                  # the work was cut after mouse-down
                return {"__error__": "interrupted"}
            return {"outcome": "outcome_unknown", "generation": gen}
        q["state"] = "executed"
        self.observation["generation"] = gen + 1
        return {"outcome": "done", "generation": gen + 1}

    def _question(self, request):
        q = self.questions.get(request.match_info["question"])
        run = self._run(request)
        if q is None or run is None or q["run_id"] != run["run_id"]:
            return None, None
        return q, run

    async def get_question(self, request):
        q, run = self._question(request)
        if q is None:
            return _err("not_found", 404)
        if self.fail_question_view:
            return _err("internal", 500)
        return web.json_response({"question_id": request.match_info["question"],
                                  "run_id": run["run_id"], "revision": q["revision"],
                                  "digest": q["digest"], "state": q["state"],
                                  "view": q["view"]})

    async def answer(self, request):
        q, run = self._question(request)
        if q is None:
            return _err("not_found", 404)
        body = request["json"]
        if (not isinstance(body, dict)
                or set(body) != {"revision", "digest", "choice", "operator_ref"}):
            return _err("unsupported", 400)
        self.answers.append(body)
        gen = self.observation["generation"]
        if q["response"] is not None and (body["revision"], body["digest"]) == (
                q["revision"], q["digest"]):
            return self._answer(request, dict(q["response"], repeat=True), lose="answer")
        if q["state"] != "open" or (body["revision"], body["digest"]) != (q["revision"],
                                                                          q["digest"]):
            return web.json_response({"outcome": "not_current", "generation": gen})
        if run["state"] == "live":
            return _err("not_ready", 409)
        if run["state"] == "waiting_live":
            run["state"] = "waiting_approval"            # a view asked but never opened
        qid, choice = request.match_info["question"], body["choice"]
        if choice == "not_now":
            q["state"], response = "declined", {"outcome": "declined", "generation": gen}
            run["state"], run["pending_question"] = "running", None
        elif choice == "confirm" and q["view"].get("kind") == "site_signin":
            q["state"] = "answered"
            response = {"outcome": self.sign_in_answer, "generation": gen,
                        "label": q["view"]["host"]}
            run["state"], run["pending_question"] = "running", None
        elif isinstance(choice, dict) and q["view"].get("kind") == "login_item":
            options = q["view"]["options"]
            if set(choice) != {"item"} or choice["item"] >= len(options):
                return _err("unsupported", 400)
            q["state"] = "answered"
            response = {"outcome": "linked", "generation": gen, "label": options[choice["item"]]}
            run["state"], run["pending_question"] = "running", None
        elif isinstance(choice, dict):
            options = q["view"]["extra_options"]
            if choice.get("extra", 99) >= len(options):
                return _err("unsupported", 400)
            q["state"] = "answered"
            response = {"outcome": "answered", "generation": gen,
                        "label": options[choice["extra"]]}
            run["state"], run["pending_question"] = "running", None
        elif choice == "confirm":
            if not q["view"]["confirm_offered"]:
                return _err("unsupported", 400)
            if self.answer_gone or self.answer_lapse:
                q["state"] = "lapsed"
                run["state"], run["pending_question"] = "running", None
                response = {"outcome": "lapsed", "generation": gen, "reason": "target_gone",
                            **(self.answer_lapse or {})}
            else:
                text = json.dumps({"approved_question": q["view"]["text"], "q": qid},
                                  sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                jid = self.new_id("j")
                self.pending[run["run_id"]] = {
                    "id": jid, "op": q["op"], "kind": "rejudge", "question_id": qid,
                    "packet": text, "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()}
                q["state"] = "confirming"
                response = {"outcome": "rejudgement_required", "generation": gen,
                            "question_id": qid,
                            "judgement": {"id": jid, "kind": "rejudge", "packet": text}}
        else:
            return _err("unsupported", 400)
        q["response"] = response
        return self._answer(request, response, lose="answer")

    async def question_live(self, request):
        q, run = self._question(request)
        if q is None:
            return _err("not_found", 404)
        body = request["json"]
        if not isinstance(body, dict) or set(body) != {"revision", "digest", "n",
                                                       "operator_ref"}:
            return _err("unsupported", 400)
        key = (request.match_info["question"], body["revision"], body["n"])
        if key in self.live_responses:
            return web.json_response(dict(self.live_responses[key], repeat=True))
        gen = self.observation["generation"]
        if q["state"] != "open" or (body["revision"], body["digest"]) != (q["revision"],
                                                                          q["digest"]):
            response = {"outcome": "not_current", "generation": gen}
        elif run["state"] in ("waiting_live", "live"):
            response = {"outcome": "live_open", "generation": gen}
        else:
            run["state"] = "waiting_live"
            response = {"outcome": "live", "generation": gen,
                        "grant": {"grant_id": self.new_id("g"),
                                  "expires_at": "2030-01-01T00:00:00Z",
                                  "operator_delivery_path": "live/" + secrets.token_hex(8)}}
        self.live_responses[key] = response
        return web.json_response(response)

    async def archive(self, request):
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        ids = (request["json"] or {}).get("file_ids", [])
        if not ids or any(self.files.get(f, {}).get("run_id") != run["run_id"] for f in ids):
            return _err("unsupported", 400)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in ids:
                z.writestr(self.files[f]["safe_name"], self.files[f]["bytes"])
        file_id = self.add_file(run["run_id"], buf.getvalue(), "files.zip", "application/zip")
        return web.json_response(self.public(self.files[file_id]))

    async def end(self, request):
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        verb = request.match_info["verb"]
        if run["state"] in TERMINAL:
            if verb == "cancel":     # controller.cancel answers an ended run with the run itself
                return web.json_response({"run_id": run["run_id"], "state": run["state"]})
            return _err("conflict", 409)
        run[verb] = request["json"]
        run["state"] = "completed" if verb == "finish" else "cancelled"
        return web.json_response({"run_id": run["run_id"], "state": run["state"]})

    async def request_cancel(self, request):
        """The app's Stop of one request, every id in one call: each id's run is cancelled at
        once (its id recorded), or, with no run yet, the id is recorded so its create is born
        cancelled. `stopped`: the id is recorded after this call."""
        out = []
        for rid in (request["json"] or {}).get("request_ids", []):
            run = next((r for r in self.runs.values() if r["spec"]["request_id"] == rid), None)
            if run is None:
                self.cancelled_requests.add(rid)
            elif run["state"] not in TERMINAL:
                run["state"] = "cancelled"
                run["cancel"] = {}
                self.cancelled_requests.add(rid)
            out.append({"request_id": rid, "run_id": None if run is None else run["run_id"],
                        "stopped": rid in self.cancelled_requests,
                        "state": "cancelled" if run is None else run["state"]})
        return web.json_response({"requests": out})

    async def forget(self, request):
        self.forgets.append({"site_id": request.match_info["site"], **(request["json"] or {})})
        return web.json_response({"status": "forgotten"})

    async def live(self, request):
        run = self._run(request)
        if run is None:
            return _err("not_found", 404)
        if (request["json"] or {}).get("purpose") not in ("login", "assist"):
            return _err("unsupported", 400)
        if run["state"] in ("waiting_live", "live"):
            # controller.request_live takes a request only from a running run (`_refuse`); a
            # view already asked for cannot be asked again
            return _err("not_ready", 409)
        run["state"] = "waiting_live"
        # the app records the request as a step (controller.request_live: live_requested)
        self.add_event(run["run_id"], "live_requested", outcome=request["json"]["purpose"])
        return web.json_response({"grant_id": self.new_id("g"),
                                  "expires_at": "2030-01-01T00:00:00Z",
                                  "operator_delivery_path": "live/" + secrets.token_hex(8)})

    async def delivery(self, request):
        self.deliveries.append({"file_id": request.match_info["file"],
                                **(request["json"] or {})})
        return web.json_response({"status": "recorded"})

    async def screenshot(self, request):
        data = self.screenshots.get((request.match_info["run"], request.match_info["shot"]))
        if data is None:
            return _err("not_found", 404)
        return web.Response(body=data, content_type="image/png")

    async def take_screenshot(self, request):
        """The page as it stands, for a running run (the app's real rule)."""
        run = request.match_info["run"]
        if self.runs.get(run, {}).get("state") != "running":
            return _err("not_ready", 409)
        shot = f"now{len(self.screenshots) + 1}"
        self.screenshots[(run, shot)] = self.page_png
        return web.json_response({"screenshot_id": shot})

    async def sources_route(self, request):
        return web.json_response({"sources": self.sources.get(request.match_info["run"], [])})

    async def records_route(self, request):
        rid = request.query.get("requester_id")
        return web.json_response({"records": [r for r in self.records
                                              if r.get("requester_id") == rid]})

    async def file_bytes(self, request):
        rec = self.files.get(request.match_info["file"])
        if rec is None:
            return _err("not_found", 404)
        return web.Response(body=rec["bytes"], content_type="application/octet-stream")

    async def notices_route(self, request):
        after = int(request.query.get("after", "0"))
        page = [n for n in self.notices if n["seq"] > after]
        if self.notices_page:                       # the app's own fetch bound (200)
            page = page[:self.notices_page]
        return web.json_response({"notices": page})

    def make_app(self) -> web.Application:
        app = web.Application(middlewares=[self._gate])
        r = app.router
        run = ID % "run"
        r.add_get("/v1/hello", self.hello_route)
        r.add_post("/v1/pair/claim", self.claim)
        r.add_get("/v1/sites", self.sites_route)
        r.add_post("/v1/runs", self.create_run)
        r.add_get("/v1/runs", self.list_runs)
        r.add_get(f"/v1/runs/{run}", self.get_run)
        r.add_get(f"/v1/runs/{run}/events", self.run_events)
        r.add_post(f"/v1/runs/{run}/observe", self.observe)
        r.add_post(f"/v1/runs/{run}/action", self.action)
        r.add_post(f"/v1/runs/{run}/look", self.look)
        r.add_post(f"/v1/runs/{run}/read", self.read)
        r.add_post(f"/v1/runs/{run}/search", self.search)
        r.add_post(f"/v1/runs/{run}/fetch", self.fetch)
        r.add_post(f"/v1/runs/{run}/download", self.download)
        r.add_post(f"/v1/runs/{run}/login-code", self.login_code)
        r.add_post(f"/v1/runs/{run}/sign-in", self.sign_in)
        r.add_post(f"/v1/runs/{run}/archive", self.archive)
        r.add_post(f"/v1/runs/{run}/{{verb:finish|cancel}}", self.end)
        r.add_post(f"/v1/runs/{run}/live", self.live)
        r.add_post("/v1/requests/cancel", self.request_cancel)
        r.add_post(f"/v1/control/sites/{ID % 'site'}/forget", self.forget)
        r.add_post(f"/v1/control/files/{ID % 'file'}/delivery", self.delivery)
        r.add_get(f"/v1/control/runs/{run}/screenshots/{ID % 'shot'}", self.screenshot)
        r.add_post(f"/v1/control/runs/{run}/screenshots", self.take_screenshot)
        r.add_get(f"/v1/control/runs/{run}/sources", self.sources_route)
        r.add_get(f"/v1/control/runs/{run}/judgements/pending", self.pending_judgements)
        r.add_post(f"/v1/control/runs/{run}/judgements/{ID % 'judgement'}", self.judgement)
        r.add_get(f"/v1/control/runs/{run}/judgements/{ID % 'judgement'}/picture",
                  self.judgement_picture_route)
        r.add_get(f"/v1/control/runs/{run}/questions/{ID % 'question'}", self.get_question)
        r.add_post(f"/v1/control/runs/{run}/questions/{ID % 'question'}/answer", self.answer)
        r.add_post(f"/v1/control/runs/{run}/questions/{ID % 'question'}/live",
                   self.question_live)
        r.add_get("/v1/records", self.records_route)
        r.add_get(f"/v1/files/{ID % 'file'}", self.file_bytes)
        r.add_get("/v1/notices", self.notices_route)
        return app

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "FakeApp":
        ready = threading.Event()

        def serve():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._runner = web.AppRunner(self.make_app(), access_log=None)
            self._loop.run_until_complete(self._runner.setup())
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            self._loop.run_until_complete(site.start())
            self.port = site._server.sockets[0].getsockname()[1]
            ready.set()
            self._loop.run_forever()
            self._loop.run_until_complete(self._runner.cleanup())
            self._loop.close()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        if not ready.wait(10):
            raise RuntimeError("fake app did not start")
        return self

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(10)

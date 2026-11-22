"""A test double of Casa's internal broker socket, in memory: the deposit
route with Casa's reference shape and per-kind judgement, and the routes the
browser plugin calls during a `callsCasa` tool call (requester, run
admission, its status, continue, delivery status, the check of
one browsing step, `action-judgement`, and the conversation's pad, `pad`), with
the request and answer shapes of Casa's `operator_calls` / `run_admission` /
`action_judgement` and the pad route's stated contract.

The check is scriptable: each call takes the next entry of `judge_script` (a
verdict object, or one of `check_unavailable`, `timeout`, `bad_packet`,
`malformed`, `wrong_sha`), else answers `default_verdict`. Like Casa, it hashes
exactly the packet string it received.

It answers on a Unix socket from a background thread (the plugin's client is
synchronous). `calls` records every request before it is judged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from typing import Any

from aiohttp import web

KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MEDIA_KINDS = {"document", "photo", "zip", "text", "audio", "video", "voice"}
MAX_VALUE_BYTES = 64 * 1024
CONTINUE_REASONS = ("operator_answer", "next_run", "sweep")
JUDGEMENT_KINDS = ("action", "rejudge", "login_item", "signin", "sign_in")
#: how long a scripted `timeout` holds the answer (a test lowers the plugin's timeout)
TIMEOUT_HOLD_S = 1.5


def _ref() -> str:
    return "casa-cap-" + secrets.token_hex(16)


class FakeBroker:
    def __init__(self, client: str = "client-1") -> None:
        self.client = client
        self.in_call = True                  # a declared call is in flight
        #: slot -> delivered kind (operator_message|operator_link|operator_file|operator_proposal)
        self.slots: dict[str, str] = {
            "notice": "operator_message", "link": "operator_link",
            "file": "operator_file", "card": "operator_proposal",
            "live": "operator_link", "setup": "operator_link",
            "beat": "operator_message", "sources": "operator_message"}
        self.requester = {"binding_ref": "casa-bind-" + "a" * 32,
                          "requester_id": "assistant",
                          "requester_kind": "operator_resident",
                          "chain": ["assistant"]}
        #: how run admission decides a new key: admit | pending | refuse
        self.admit_mode = "admit"
        self.by_key: dict[str, dict] = {}
        self.runs_by_key: dict[str, list] = {}
        self.queue: set[tuple[str, str]] = set()
        #: every binding Casa issued (`continue` accepts any of them for this plugin)
        self.issued: set[str] = set()
        self.delivery: dict[str, dict] = {}
        self.deposits: list[dict] = []
        #: deposit keys Casa delivered (durable memory, Casa's INV-PLUG-050) and what it posted
        self.delivered_keys: set[str] = set()
        self.posted: list[dict] = []
        self.calls: list[tuple[str, Any]] = []
        #: the check: one entry per call, then `default_verdict`
        self.judge_script: list = []
        self.default_verdict = {"verdict": "go", "category": "reading",
                                "reason": "It opens the invoice list."}
        self.latency_ms = 4321
        #: the conversation's pad: section name -> its text; and its cap in bytes
        self.pad_sections: dict[str, str] = {}
        self.pad_cap = 1024 * 1024
        #: route -> (status, raw body bytes or JSON-able)
        self.overrides: dict[str, tuple[int, Any]] = {}
        self._dir = tempfile.mkdtemp(prefix="fb-")
        self.socket_path = os.path.join(self._dir, "internal.sock")
        self._loop = None
        self._thread = None

    # -- helpers ---------------------------------------------------------------
    async def _body(self, request) -> tuple[dict | None, web.Response | None]:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        self.calls.append((request.path, body))
        over = self.overrides.get(request.path)
        if over is not None:
            status, payload = over
            if isinstance(payload, (bytes, str)):
                return None, web.Response(status=status, body=payload)
            return None, web.json_response(payload, status=status)
        if not isinstance(body, dict):
            return None, web.json_response({"error": "bad_json"})
        if not isinstance(body.get("client"), str) or not body["client"]:
            return None, web.json_response({"error": "bad_client"})
        if body["client"] != self.client or not self.in_call:
            return None, web.json_response({"error": "no_call_in_flight"})
        return body, None

    # -- routes ------------------------------------------------------------------
    async def deposit(self, request):
        body, bad = await self._body(request)
        if bad is not None:
            return bad
        slot, value = body.get("slot"), body.get("value")
        if not isinstance(slot, str) or not slot:
            return web.json_response({"error": "bad_slot"})
        if not isinstance(value, str) or not value:
            return web.json_response({"error": "bad_value"})
        if len(value.encode()) > MAX_VALUE_BYTES:
            return web.json_response({"error": "value_too_large"})
        kind = self.slots.get(slot)
        if kind is None:
            return web.json_response({"error": "no_call_in_flight"})
        key = body.get("key")
        if key is not None and not (isinstance(key, str) and KEY_RE.fullmatch(key)):
            return web.json_response({"error": "bad_key"})
        if kind == "operator_file" and body.get("kind") not in MEDIA_KINDS:
            return web.json_response({"error": "bad_kind"})
        if kind == "operator_proposal":
            try:
                card = json.loads(value)
            except ValueError:
                card = None
            if not isinstance(card, dict) or not isinstance(card.get("text"), str) \
                    or not isinstance(card.get("buttons"), list):
                return web.json_response({"error": "bad_proposal"})
        if kind == "operator_link" and not value.startswith("https://"):
            return web.json_response({"error": "bad_link"})
        ref = _ref()
        self.deposits.append(dict(body, reference=ref))
        if key is not None and key in self.delivered_keys:
            self.delivery[ref] = {"status": "delivered", "repeat": True}
        return web.json_response({"reference": ref})

    def deliver(self, reference: str, status: str = "delivered") -> None:
        """Casa's delivery of a deposit: posts it unless its key was delivered."""
        dep = next(d for d in self.deposits if d["reference"] == reference)
        if status == "delivered":
            if dep.get("key") not in self.delivered_keys:
                self.posted.append(dep)
            if dep.get("key"):
                self.delivered_keys.add(dep["key"])
        self.delivery[reference] = {"status": status, "repeat": False}

    async def requester_route(self, request):
        body, bad = await self._body(request)
        if bad is not None:
            return bad
        self.issued.add(self.requester["binding_ref"])
        return web.json_response(self.requester)

    async def admission(self, request):
        body, bad = await self._body(request)
        if bad is not None:
            return bad
        key = body.get("request_key")
        if not isinstance(key, str) or not 0 < len(key) <= 128 or not key.isprintable():
            return web.json_response({"error": "bad_request_key"})
        runs = body.get("runs")
        if not isinstance(runs, list) or not 1 <= len(runs) <= 8:
            return web.json_response({"error": "bad_runs"})
        for r in runs:
            if not isinstance(r, dict) or set(r) - {"spec_digest", "mode", "task_kind",
                                                    "task_text", "sites"}:
                return web.json_response({"error": "bad_runs"})
            for s in r.get("sites", []):
                if not isinstance(s, dict) or set(s) != {"site_id", "access", "name"}:
                    return web.json_response({"error": "bad_sites"})
        if key in self.by_key:
            return web.json_response(self.by_key[key])
        cid = "conf-" + secrets.token_hex(16)
        self.runs_by_key[key] = runs
        if self.admit_mode == "admit":
            answer = {"status": "admitted", "request_key": key, "confirmation_id": cid,
                      "admissions": [{"spec_digest": r["spec_digest"],
                                      "origin_kind": "operator_telegram",
                                      "origin_ref": "adm-" + secrets.token_hex(16)}
                                     for r in runs]}
        elif self.admit_mode == "pending":
            answer = {"status": "confirmation_pending", "request_key": key,
                      "confirmation_id": cid}
        else:
            answer = {"status": "refused", "reason": "not_delivered", "request_key": key,
                      "confirmation_id": cid}
        self.by_key[key] = answer
        return web.json_response(answer)

    def settle_confirmation(self, cid: str, status: str) -> None:
        for key, answer in self.by_key.items():
            if answer.get("confirmation_id") == cid:
                if status == "admitted":
                    answer.update(status="admitted", admissions=[
                        {"spec_digest": r["spec_digest"], "origin_kind": "operator_telegram",
                         "origin_ref": "adm-" + secrets.token_hex(16)}
                        for r in self.runs_by_key[key]])
                else:
                    answer.pop("admissions", None)
                    answer["status"] = status

    async def admission_status(self, request):
        body, bad = await self._body(request)
        if bad is not None:
            return bad
        cid = body.get("confirmation_id")
        for answer in self.by_key.values():
            if answer.get("confirmation_id") == cid:
                out = dict(answer)
                if out["status"] == "confirmation_pending":
                    out["status"] = "pending"
                return web.json_response(out)
        return web.json_response({"status": "expired",
                                  "confirmation_id": cid if isinstance(cid, str) else ""})

    async def continue_route(self, request):
        body, bad = await self._body(request)
        if bad is not None:
            return bad
        run_ref, reason = body.get("run_ref"), body.get("reason")
        if not isinstance(run_ref, str) or not 0 < len(run_ref) <= 64 \
                or not run_ref.isprintable():
            return web.json_response({"error": "bad_run_ref"})
        if reason not in CONTINUE_REASONS:
            return web.json_response({"error": "bad_reason"})
        if body.get("binding_ref") not in self.issued | {self.requester["binding_ref"]}:
            return web.json_response({"error": "unknown_binding"})
        key = (body["binding_ref"], run_ref)
        if key in self.queue:
            return web.json_response({"status": "already_queued"})
        self.queue.add(key)
        return web.json_response({"status": "queued"})

    async def delivery_status(self, request):
        body, bad = await self._body(request)
        if bad is not None:
            return bad
        ref = body.get("reference")
        default = ("pending" if any(d["reference"] == ref for d in self.deposits)
                   else "unknown")             # held, not yet delivered / never seen
        return web.json_response(self.delivery.get(ref, {"status": default, "repeat": False}))

    def judgements(self) -> list[dict]:
        """The bodies of every action-judgement call, in order."""
        return [b for p, b in self.calls if p == "/internal/broker/action-judgement"]

    async def action_judgement(self, request):
        body, bad = await self._body(request)
        if bad is not None:
            return bad
        kind, packet, digest = body.get("kind"), body.get("packet"), body.get("spec_digest")
        if kind not in JUDGEMENT_KINDS:
            return web.json_response({"error": "bad_kind"})
        if digest is not None and (not isinstance(digest, str) or not digest
                                   or len(digest) > 128 or not digest.isprintable()):
            return web.json_response({"error": "bad_digest"})
        if not isinstance(packet, str) or not packet:
            return web.json_response({"error": "bad_packet"})
        step = self.judge_script.pop(0) if self.judge_script else self.default_verdict
        sha = hashlib.sha256(packet.encode("utf-8")).hexdigest()
        if step == "timeout":
            await asyncio.sleep(TIMEOUT_HOLD_S)
            return web.json_response({"error": "check_unavailable"})
        if step in ("check_unavailable", "bad_packet"):
            return web.json_response({"error": step})
        if step == "malformed":
            return web.json_response({"verdict": "go", "packet_sha256": sha,
                                      "latency_ms": self.latency_ms})
        if step == "wrong_sha":
            return web.json_response({"verdict": self.default_verdict,
                                      "packet_sha256": "0" * 64,
                                      "latency_ms": self.latency_ms})
        return web.json_response({"verdict": step, "packet_sha256": sha,
                                  "latency_ms": self.latency_ms})

    async def pad(self, request):
        """Casa's pad route as its contract states: write and append ``{lines, bytes}``, read
        ``{text, next}`` (30,000 characters a window), a refusal ``{error, message}``. A line
        beginning "## " is stored with one leading space; a section is created on write."""
        body, bad = await self._body(request)
        if bad is not None:
            return bad
        op, section = body.get("op"), body.get("section")
        if op not in ("write", "append", "read") or not isinstance(section, str):
            return web.json_response({"error": "bad_request", "message": "Not a pad request."})
        if op == "read":
            if section not in self.pad_sections:
                return web.json_response({"error": "no_section",
                                          "message": "No section by that name."})
            text, offset = self.pad_sections[section], body.get("offset", 0)
            end = offset + 30_000
            return web.json_response({"text": text[offset:end],
                                      "next": end if end < len(text) else None})
        text = body.get("text")
        if not isinstance(text, str):
            return web.json_response({"error": "bad_request", "message": "Text is needed."})
        text = "\n".join(" " + line if line.startswith("## ") else line
                         for line in text.split("\n"))
        old = self.pad_sections.get(section, "") if op == "append" else ""
        after = {**self.pad_sections, section: (old + "\n" + text) if old else text}
        # Casa's cap is the whole pad's, each section's mark line included (its code `too_large`)
        if sum(len(f"## {k}\n{v}\n".encode()) for k, v in after.items()) > self.pad_cap:
            return web.json_response({"error": "too_large",
                                      "message": "This would make the pad too large."})
        self.pad_sections[section] = after[section]
        new = self.pad_sections[section]
        return web.json_response({"lines": len(new.split("\n")) if new else 0,
                                  "bytes": len(new.encode())})

    # -- lifecycle -----------------------------------------------------------------
    def env(self) -> dict:
        return {"CASA_BROKER_SOCKET": self.socket_path, "CASA_BROKER_CLIENT": self.client}

    def start(self) -> "FakeBroker":
        ready = threading.Event()

        def serve():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            app = web.Application(client_max_size=6 * 1024 * 1024)   # Casa's internal app's
            r = app.router
            r.add_post("/internal/broker/deposit", self.deposit)
            r.add_post("/internal/broker/requester", self.requester_route)
            r.add_post("/internal/broker/run-admission", self.admission)
            r.add_post("/internal/broker/run-admission-status", self.admission_status)
            r.add_post("/internal/broker/continue", self.continue_route)
            r.add_post("/internal/broker/delivery-status", self.delivery_status)
            r.add_post("/internal/broker/action-judgement", self.action_judgement)
            r.add_post("/internal/broker/pad", self.pad)
            runner = web.AppRunner(app, access_log=None)
            self._loop.run_until_complete(runner.setup())
            self._loop.run_until_complete(web.UnixSite(runner, self.socket_path).start())
            ready.set()
            self._loop.run_forever()
            self._loop.run_until_complete(runner.cleanup())
            self._loop.close()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        if not ready.wait(10):
            raise RuntimeError("fake broker did not start")
        return self

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(10)
        shutil.rmtree(self._dir, ignore_errors=True)

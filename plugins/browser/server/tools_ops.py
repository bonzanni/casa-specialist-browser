"""The operator's side of a run: cards, taps, screenshots.

Operator-only decisions are taken only by a tap on a card this
plugin deposited: each button stores a call of `apply_tap` or `tap_live`
whose only argument is a single-use token. A token is visible only to the
pinned turn that executes that one tap; any other string is "no longer
valid" and changes nothing. No question id, digest or answer is a
model-suppliable argument of the tap tools.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import string as _string
import time
import unicodedata
import urllib.parse

import cards
import casa_broker
import judge
import casa_handoff
import continuation
import copy_text
import deliveries
import files
import pairing
import state
import tooling
import wire
from app_client import AppError, compatible_client
from tooling import ID, Refused, enum, obj, text, tool
from tools_run import _active, _prelude, _spec_digest, close_run, undelivered, unresolved_files

CARD_SLOT = "card"


# -- carding --------------------------------------------------------------------------

def _deposit_card(card: dict, key: str, run_id: str | None, group: str, delivery,
                  kind: str = "card") -> None:
    """Deposit a card and record its delivery. If either step fails, the card
    is voided and no reference is returned: Casa then withholds the result and
    drops the deposit, so no card exists that the plugin does not track."""
    try:
        reference = casa_broker.deposit_proposal(CARD_SLOT, card, key=key)
        state.put_delivery(reference, kind, run_id, None, None, group)
    except BaseException:
        state.void_group(group, "undelivered")
        raise
    delivery.reference = reference


@tool("ask_forget_login", "Ask the operator whether Casa Browser should forget a site's "
      "saved login.", obj(site_id=ID), slot=CARD_SLOT)
def ask_forget_login(args: dict, delivery) -> str:
    continuation.current_requester()
    client = compatible_client()
    sites = {s["site_id"]: s for s in wire.parse_sites(client.request("GET", "/v1/sites"))}
    site = sites.get(args["site_id"])
    if site is None:
        return copy_text.NO_SUCH_SITE.format(site_id=args["site_id"])
    group = f"forget-{secrets.token_hex(8)}"
    card = cards.forget_card(site["site_id"], site["display_name"], group)
    _deposit_card(card, f"{group}", None, group, delivery)
    return copy_text.FORGET_ASKED.format(site=site["display_name"])


@tool("ask_keep_going",
      "At each 20 minutes of a run, tell the operator it is still going and offer Stop; "
      "the run does not pause.", obj(run_id=ID, done_text=text(200, 0)), slot=CARD_SLOT)
def ask_keep_going(args: dict, delivery) -> str:
    run_id = args["run_id"]
    binding, client = _prelude(run_id)
    run = wire.parse_run(client.request("GET", f"/v1/runs/{run_id}"))
    minutes = int((time.time() - run["created_at"]) // 60) if "created_at" in run else 0
    group = f"keep-{secrets.token_hex(8)}"
    card = cards.keep_going_card(run_id, binding.spec.get("task_kind", "the task"), minutes,
                                 args["done_text"], group, binding.request_group)
    _deposit_card(card, group, run_id, group, delivery)
    return copy_text.KEEP_GOING_ASKED


@tool("show_screenshot", "Show the operator a picture of the page with one plain line as its "
      "caption saying what the picture is (\"The order confirmation\"), never the outcome — "
      "your reply says that: the page as it is now, or the screenshot of a step that sent "
      "something (screenshot_id).",
      obj(["run_id", "caption"], run_id=ID, caption=text(120), screenshot_id=ID), slot="file")
def show_screenshot(args: dict, delivery) -> str:
    """A picture for the operator outside a question: after a confirmed order
    or booking, the page it led to, with Fido's one line ("Order accepted, number 1234"). A
    question's picture is the question card itself (post_question)."""
    run_id = args["run_id"]
    binding, client = _prelude(run_id)
    shot = args.get("screenshot_id")
    if shot is None:
        try:
            shot = wire.parse_screenshot_taken(client.request(
                "POST", f"/v1/control/runs/{run_id}/screenshots", {}, control=True))
        except AppError as err:
            if err.code == "not_ready":
                return copy_text.SHOT_NOT_RUNNING
            raise
    data = client.request("GET", f"/v1/control/runs/{run_id}/screenshots/{shot}",
                          control=True, raw=True)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return copy_text.NOT_A_PNG
    path = files.stage(data, "png")
    delivery.reference = casa_broker.deposit_file(
        "file", str(path), kind="photo", caption=casa_broker.fit_caption(args["caption"]),
        filename=None, key=f"shot-{run_id}-{shot}")
    return copy_text.SCREENSHOT_SENT


# -- the question (action model; one message with its picture) ------------

def _question(client, run_id: str, question_id: str) -> dict:
    q = wire.parse_question(client.request(
        "GET", f"/v1/control/runs/{run_id}/questions/{question_id}", control=True))
    if q["run_id"] != run_id or q["question_id"] != question_id:
        raise AppError("bad_response")
    return q


def _confirm_and_copy(q: dict) -> tuple[bool | None, str | None, bool]:
    """Whether the card may offer the confirm button, the copy it carries instead, and whether
    it goes as the question's picture. A question with a picture goes as that picture with
    its buttons (the confirm is on the picture itself); after that revision's picture card was
    withheld it goes as text, without the confirm, with `PICTURE_WITHHELD_COPY`. None: its
    picture card is still being delivered."""
    view = q["view"]
    shot = view["screenshot_id"]
    if shot is None:
        return view["confirm_offered"], None, False
    status = state.question_shot_status(q["question_id"], q["revision"], shot)
    if status == "withheld":                 # withheld at the delivery layer: no confirm
        return False, copy_text.PICTURE_WITHHELD_COPY if view["confirm_offered"] else None, False
    if status == "pending":
        return None, None, False
    return view["confirm_offered"], None, True


def _post_card(run_id: str, q: dict, confirm: bool, copy: str | None, delivery, client, *,
               photo: bool = False, live_line: str | None = None, kind: str = "card",
               key_suffix: str = "") -> None:
    state.void_run_cards(run_id, "question")
    cid = cards.question_card_id(q)
    group = f"q-{cid[:16]}-{secrets.token_hex(4)}"
    path = None
    if photo:
        shot = q["view"]["screenshot_id"]
        data = client.request("GET", f"/v1/control/runs/{run_id}/screenshots/{shot}",
                              control=True, raw=True)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AppError("bad_response")
        path = str(files.stage(data, "png"))
    card = cards.question_card(run_id, q, group, confirm=confirm, copy=copy,
                               live_line=live_line, photo=path,
                               live_url=None if live_line is not None else _live_url())
    attempt = len(state.question_cards(run_id, q["question_id"]))
    _deposit_card(card, f"qcard-{cid}-{attempt}{key_suffix}", run_id, group, delivery, kind)
    if photo:
        # this card is the question revision's picture: a re-post reads what became of it
        state.put_question_shot(q["question_id"], q["revision"], q["view"]["screenshot_id"],
                                delivery.reference)


@tool("post_question",
      "Post the question Casa Browser is waiting on for this run to the operator, as the "
      "check worded it, with the page's picture and its buttons.", obj(run_id=ID),
      slot=CARD_SLOT)
def post_question(args: dict, delivery) -> str:
    run_id = args["run_id"]
    binding, client = _prelude(run_id, posting=True)
    return _post_pending(run_id, binding, client, delivery)


def _post_pending(run_id: str, binding, client, delivery, *, posted: str | None = None) -> str:
    """The run's waiting question as its card (`post_question`'s body, shared by `ask_sign_in`).
    `posted`: the result when it went (default: the question's own)."""
    entry = _active(client).get(run_id)
    qid = entry["pending_question"] if entry else None
    if qid is None:
        return copy_text.NO_QUESTION
    q = _question(client, run_id, qid)
    if q["state"] != "open":
        return copy_text.QUESTION_NOT_OPEN.format(state=q["state"])
    confirm, copy, photo = _confirm_and_copy(q)
    if confirm is None:
        return copy_text.CARD_IN_FLIGHT
    _post_card(run_id, q, confirm, copy, delivery, client, photo=photo)
    if posted is not None:
        return posted.format(ending=handed_over(binding))
    return (copy_text.QUESTION_POSTED_NO_CONFIRM if copy == copy_text.PICTURE_WITHHELD_COPY
            else copy_text.QUESTION_POSTED).format(ending=handed_over(binding))


#: `ask_sign_in` outcomes that make no card, and what Fido is told
_SIGN_IN_REFUSALS = {"not_configured": copy_text.SIGN_IN_NOT_CONFIGURED,
                     "account_site": copy_text.SIGN_IN_ACCOUNT_SITE,
                     "not_https": copy_text.SIGN_IN_NOT_HTTPS,
                     "page_changed": copy_text.SIGN_IN_PAGE_CHANGED,
                     "question_open": copy_text.SIGN_IN_QUESTION_OPEN}


@tool("ask_sign_in",
      "The task needs signing in to a site that is not set up, and the run is on its sign-in "
      "page on the public web: Casa Browser sends the operator one card (the page's picture, "
      "its address as the browser shows it, a fitting 1Password item if exactly one fits) and "
      "their Yes saves the site and signs in. site_name is the site's everyday name, a label "
      "only. End your turn after it.",
      obj(run_id=ID, site_name=text(60)), slot=CARD_SLOT)
def ask_sign_in(args: dict, delivery) -> str:
    run_id = args["run_id"]
    binding, client = _prelude(run_id, posting=True)
    try:
        step = wire.parse_step(client.request("POST", f"/v1/runs/{run_id}/sign-in",
                                              {"site_name": args["site_name"]}))
    except AppError as err:
        if err.code == "not_ready":
            return copy_text.SIGN_IN_QUESTION_OPEN
        raise
    outcome = step["outcome"]
    if outcome == "registered_site":
        return copy_text.SIGN_IN_REGISTERED.format(site=cards.md(step.get("site") or "", 80))
    if outcome in _SIGN_IN_REFUSALS:
        return _SIGN_IN_REFUSALS[outcome]
    if outcome == "judging":
        # the check of the vault's items, relayed by this code (a failed check is no verdict:
        # the card still goes, naming no item)
        judge.relay_pending(client, run_id)
    if (_active(client).get(run_id) or {}).get("pending_question") is None:
        return copy_text.SIGN_IN_JUDGING
    return _post_pending(run_id, binding, client, delivery, posted=copy_text.SIGN_IN_ASKED)


def handed_over(binding) -> str:
    """How the turn ends once the operator has the hand-over (the model sees this only
    on Casa's proof of delivery: a withheld post replaces the result)."""
    return (copy_text.HANDED_OVER_SILENT if binding.requester_kind == "operator_resident"
            else copy_text.HANDED_OVER_LINE)


# -- the tap tools -----------------------------------------------------------------------

TOKEN = obj(t={"type": "string", "minLength": 1, "maxLength": 64})


def _receipt(sentence: str, *, on_card: bool = False, not_done: bool = False) -> str:
    """A tap's receipt. `on_card`: Casa writes it as the tapped card's last line
    instead of a message — for a confirmed step, whose outcome Fido reports next anyway.
    `not_done`: a Yes that pressed nothing, so the tapped card must not read as a success."""
    out = {"receipt": sentence}
    if on_card:
        out["on_card"] = True
    if not_done:
        out["not_done"] = True
    return json.dumps(out, ensure_ascii=False)


def _operator_ref(token: str) -> str:
    return "tap:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _continue(run_id: str) -> None:
    """Queue the continuation under the run's stored binding — never the tap turn's own."""
    binding = state.run_binding(run_id)
    if binding is not None:
        continuation.queue(binding.binding_ref, run_id, "operator_answer")


def _queue_next_of(run_id: str) -> None:
    binding = state.run_binding(run_id)
    if binding is None:
        return
    nxt = state.next_in_group(binding.request_group)
    if nxt is not None:
        continuation.queue(binding.binding_ref, nxt, "next_run")


#: judgements one confirm may relay: two re-judgements, a post-typing one and the fresh
#: judgement after a lapse fit; a chain past it is finished by the in-progress resume
TAP_JUDGEMENTS = 6
ANSWER_TRIES = 3


def _post_answer(client, run_id: str, question_id: str, body: dict) -> dict | None:
    """The answer, posted with the identical body up to `ANSWER_TRIES` times while the app
    cannot be reached (its compare-and-set returns the stored response to a repeat). None
    when it stays unreachable; a typed app error is raised as itself."""
    path = f"/v1/control/runs/{run_id}/questions/{question_id}/answer"
    for _ in range(ANSWER_TRIES):
        try:
            return client.request("POST", path, body, control=True,
                                  timeout_s=judge._verdict_timeout())
        except AppError as err:
            if err.code != "unreachable":
                raise
    return None


#: why a Yes pressed nothing, when the app brings no sentence for it
_NOT_PRESSED = {"target_gone": copy_text.R_GONE, "incomplete": copy_text.R_NOT_CHECKED,
                "too_much": copy_text.R_NOT_CHECKED, "unreadable": copy_text.R_NOT_CHECKED}
#: how a Yes's receipt begins when it did or may have pressed: every other one pressed nothing
_MAY_HAVE_PRESSED = (copy_text.R_DONE.split("{")[0], copy_text.R_UNKNOWN.split("{")[0],
                     copy_text.R_RAN_AFRESH, copy_text.R_UNSURE, copy_text.R_LATER)


def _answer_receipt(step: dict, p: dict) -> str:
    """The receipt the step's own outcome gives. For a confirm, `apply_answer`
    then lets the app's record of the question override it (`executed`, `outcome_unknown`):
    no receipt claims the confirmed press from a step's outcome alone."""
    label = cards.md(p.get("label") or "", 60)
    outcome = step["outcome"]
    if p.get("site"):                           # the sign-in card
        if outcome == "signing_in":
            return copy_text.R_SIGNING_IN.format(label=label)
        if outcome == "declined":
            return copy_text.R_SITE_DECLINED
        if outcome == "not_saved":
            return copy_text.R_SITE_NOT_SAVED.format(label=label)
        if outcome == "not_opened":
            return copy_text.R_SITE_NOT_OPENED.format(label=label)
        if outcome == "already_saved":
            return copy_text.R_SITE_ALREADY.format(label=label)
        return copy_text.R_NOT_CURRENT
    if p.get("login"):                          # the login question
        if outcome == "linked":
            return copy_text.R_LINKED.format(label=label)
        if outcome == "declined":
            return copy_text.R_NO_ITEM
        if outcome == "not_linked":
            return copy_text.R_NOT_LINKED
        return copy_text.R_NOT_CURRENT
    if p.get("choice") == "confirm":
        if outcome == "asked":
            return copy_text.R_ASK_AGAIN
        if outcome == "check_unavailable":
            return copy_text.R_CHECK_DOWN
        if outcome == "not_current":
            return copy_text.R_NOT_CURRENT
        if outcome == "outcome_unknown":
            return copy_text.R_UNSURE           # a step, not known to be the confirmed press
        if outcome == "done":
            return copy_text.R_RAN_AFRESH       # `executed` overrides this for the confirmed press
        if step.get("copy"):
            return cards.md(step["copy"])       # the app's own sentence for this lapse
        return _NOT_PRESSED.get(step.get("reason"), copy_text.R_CHANGED)   # lapsed, not_run
    if outcome == "declined":
        return copy_text.R_DECLINED.format(label=label) if label else copy_text.R_DECLINED_PLAIN
    if outcome == "answered":
        return copy_text.R_CHOSEN.format(label=label)
    return copy_text.R_NOT_CURRENT


#: transport failures and the turn bound: the answer stays in progress and is resumed
_RESUMABLE = ("unreachable", "turn_bound")


def apply_answer(ans: state.AnswerTap, client) -> str | None:
    """Post one answer and relay any re-judgement and fresh judgement; its
    receipt, settled in plugin state, or None while it could not be finished (it stays
    `in_progress`, and the next run tool or continuation resumes the same body).

    A confirm's receipt never rests on how this call ended: on EVERY settling path the app's
    record of the question decides first — `executed` is "Done", `outcome_unknown` is its own
    sentence — and only otherwise the path's own receipt applies (a step
    judged afresh, a cut after mouse-down)."""
    p = ans.payload
    run_id = p["run_id"]
    stays_open = False
    step = None
    try:
        reply = _post_answer(client, run_id, p["question_id"], ans.body)
    except AppError as err:
        if err.code in _RESUMABLE:
            return None
        reply = None
        if err.code == "not_ready" and wire.parse_run(client.request(
                "GET", f"/v1/runs/{run_id}"))["state"] in ("waiting_live", "live"):
            # the operator has the live view: nothing was taken; the question still stands
            receipt, stays_open = copy_text.R_LIVE_FIRST, True
        elif err.code in ("not_found", "conflict", "interrupted"):
            receipt = copy_text.R_NOT_CURRENT
        else:
            receipt, stays_open = copy_text.R_NOT_TAKEN, True
    else:
        if reply is None:
            return None
        binding = state.run_binding(run_id)
        try:
            step = judge.relay(client, run_id, wire.parse_step(reply),
                               _spec_digest(binding) if binding else None,
                               limit=TAP_JUDGEMENTS)
        except Refused:
            return None             # the turn bound or a long chain: resumed with the same body
        except AppError as err:
            if err.code in _RESUMABLE:
                return None
            receipt = copy_text.R_UNSURE
        else:
            receipt = _answer_receipt(step, p)
    if p.get("choice") == "confirm" and not p.get("site"):   # a sign-in card is never "executed"
        try:
            question_state = _question(client, run_id, p["question_id"])["state"]
        except AppError as err:
            if err.code in _RESUMABLE:
                return None          # resumed: the identical answer replays its stored chain
            question_state = None
        if question_state in ("executed", "outcome_unknown"):
            label = cards.md(p.get("label") or "", 60)
            receipt = (copy_text.R_DONE if question_state == "executed"
                       else copy_text.R_UNKNOWN).format(label=label)
            stays_open = False
        elif question_state is None and (step is None or step["outcome"] != "asked"):
            receipt = copy_text.R_UNSURE        # the question's record could not be read
    return state.finish_answer(ans.hash, receipt, card_stays_open=stays_open)


def _apply_owned(ans: state.AnswerTap, client) -> str | None:
    """`apply_answer` under the answer's one-owner claim: the answer is re-read once claimed
    and a settled one is never posted again. None: in progress (owned elsewhere, or not
    finished here)."""
    owned = state.claim_answer(ans.hash, time.time())
    if owned is None:
        return None
    if owned.receipt is not None:
        return owned.receipt
    try:
        receipt = apply_answer(owned, client)
    finally:
        state.release_answer(owned.hash)          # a no-op once settled
    return receipt


def resume_answers(run_id: str) -> None:
    """Every answer of the run left `in_progress` is posted again with its exact body (plugin
    code, no model; `_prelude`), by whichever process holds its claim; each one
    settled is noted on the current result."""
    pending = state.answers_in_progress(run_id)
    if not pending:
        return
    client = compatible_client()
    for ans in pending:
        try:
            receipt = _apply_owned(ans, client)
        except Exception:  # noqa: BLE001 — retried at the next call
            receipt = None
        if receipt is None:
            tooling.note(copy_text.ANSWER_IN_PROGRESS)
        else:
            tooling.note(copy_text.ANSWER_APPLIED.format(receipt=receipt))


def _question_tap(token: str) -> str | None:
    """A question's answer button, as its receipt tool result; None when the token is not one."""
    ans = state.redeem_answer(token, "apply_tap", time.time(), _operator_ref)
    if ans is None:
        return None
    return _tap_result(ans.kind, _applied_receipt(ans))


def _tap_result(kind: str, receipt: str) -> str:
    done = receipt.startswith(copy_text.R_DONE.split("{")[0])
    return _receipt(receipt, on_card=done, not_done=(
        kind == "q_confirm" and not receipt.startswith(_MAY_HAVE_PRESSED)))


def _applied_receipt(ans: state.AnswerTap) -> str:
    if ans.receipt is not None:
        return ans.receipt                      # the first answer's receipt, nothing again
    run_id = ans.payload["run_id"]
    state.take_lease(run_id)                    # the sweep leaves the run to this tap
    try:
        receipt = _apply_owned(ans, compatible_client())
    except AppError:
        receipt = None
    if receipt is None:
        receipt = copy_text.R_LATER
    _continue(run_id)
    return receipt


@tool("apply_tap", "Apply the operator's tap (only ever called by the button itself).", TOKEN,
      receipt=True)
def apply_tap(args: dict) -> str:
    token = args["t"]
    answered = _question_tap(token)
    if answered is not None:
        return answered
    rec = state.redeem_tap(token, "apply_tap", time.time())
    if rec is None:
        return _receipt(copy_text.UNKNOWN_TAP)
    p = rec.payload
    if p.get("run_id"):
        state.touch_run(p["run_id"])            # an operator's tap on the run
    if rec.kind == "keep_going":
        return _receipt(copy_text.RECEIPT_KEEP_GOING)
    if rec.kind == "keep":
        return _receipt(copy_text.RECEIPT_KEPT.format(site=cards.md(p.get("site", ""))))
    try:
        client = compatible_client()
        if rec.kind == "forget":
            client.request("POST", f"/v1/control/sites/{p['site_id']}/forget",
                           {"operator_ref": _operator_ref(token)}, control=True)
            return _receipt(copy_text.RECEIPT_FORGOT.format(site=cards.md(p["site"])))
        else:                                                   # stop
            client.request("POST", f"/v1/runs/{p['run_id']}/cancel", {})
            state.mark_terminal(p["run_id"])
            _queue_next_of(p["run_id"])
            return _receipt(copy_text.RECEIPT_STOPPED)
    except AppError:
        return _receipt(copy_text.RECEIPT_NOT_TAKEN)


@tool("tap_live", "Open the live view from a question and show the question again (only "
      "ever called by the button itself).", TOKEN, slot=CARD_SLOT)
def tap_live(args: dict, delivery) -> str:
    """[Open live view] on a question card: the app opens
    an assist view from the question, and the same question is posted again as a new card
    carrying the live-view link. The link goes only into that deposit (Casa's INV-PLUG-025's
    reason: no model context); this result never names it. The token is redeemed without
    answering, so the button works again after Done."""
    rec = state.redeem_live(args["t"], time.time())
    if rec is None:
        return copy_text.UNKNOWN_TAP
    p = rec.payload
    run_id, qid = p["run_id"], p["question_id"]
    client = compatible_client()
    body = {"revision": p["revision"], "digest": p["digest"], "n": state.next_live_n(qid),
            "operator_ref": _operator_ref(args["t"])}
    path = f"/v1/control/runs/{run_id}/questions/{qid}/live"
    reply = None
    for _ in range(ANSWER_TRIES):
        try:
            reply = client.request("POST", path, body, control=True)
            break
        except AppError as err:
            if err.code == "not_ready":
                return copy_text.R_LIVE_FIRST
            if err.code in ("not_found", "conflict", "interrupted"):
                return copy_text.R_NOT_CURRENT
            if err.code != "unreachable":
                raise
    if reply is None:
        return copy_text.LIVE_NOT_OPENED.format(code="unreachable")
    if wire.parse_question_live(reply) == "not_current":
        return copy_text.R_NOT_CURRENT
    q = _question(client, run_id, qid)
    if q["state"] != "open" or (q["revision"], q["digest"]) != (p["revision"], p["digest"]):
        return copy_text.R_NOT_CURRENT
    url = _page_url()
    if not linkable(url):
        return copy_text.LIVE_NOT_OPENED.format(code="no_link")
    deliveries.settle_deliveries()             # what became of the card the operator just tapped
    confirm, copy, photo = _confirm_and_copy(q)
    _post_card(run_id, q, bool(confirm), copy, delivery, client, photo=photo, kind="qlive",
               live_line=copy_text.LIVE_LINE.format(url=_escape_all(url)),
               key_suffix=f"-live{body['n']}")
    return copy_text.LIVE_POSTED


# -- links ---------------------------------------------------------------------

HA_URL_ENV = "CASA_PLUGIN_BROWSER_HA_URL"
MY_HA = "https://my.home-assistant.io/redirect/supervisor_app/?app={slug}"


def app_page_url(app_slug: str, ha_url: str | None) -> str:
    """Casa Browser's page: the ingress path under the operator's own HA
    address when that is a valid `https://` URL, else My Home Assistant's
    redirect to the app. HA documents no link to an ingress sub-path, so the
    app's home page shows the run's waiting live view first."""
    if ha_url:
        parts = urllib.parse.urlsplit(ha_url.strip())
        try:
            parts.port
            ok = (parts.scheme == "https" and parts.hostname and not parts.username
                  and not parts.password and not parts.query and not parts.fragment)
        except ValueError:
            ok = False
        if ok:
            base = urllib.parse.urlunsplit(("https", parts.netloc, parts.path.rstrip("/"),
                                            "", ""))
            return f"{base}/hassio/ingress/{app_slug}"
    return MY_HA.format(slug=urllib.parse.quote(app_slug, safe="_-"))


def _page_url() -> str:
    return app_page_url(pairing.sibling(socket.gethostname()).app_slug,
                        os.environ.get(HA_URL_ENV, ""))


def _live_url() -> str | None:
    """The question card's [Open live view] link (Casa Browser's page, which offers
    the question's live view); None (the `tap_live` callback instead) when it cannot be made."""
    try:
        url = _page_url()
    except Exception:  # noqa: BLE001 — the callback button still works
        return None
    return url if linkable(url) else None


@tool("request_live_view",
      "Ask the operator to take over the run's browser for a moment: purpose login (a "
      "login needs a person) or assist. reason is one short sentence "
      "of what the operator does there, in everyday words (\"Set the pickup time to 19:30.\"); never "
      "mention Done, the message says it. This message is the whole hand-over: send no "
      "progress line before it. End your turn after it.",
      obj(run_id=ID, purpose=enum("login", "assist"), reason=text(200)), slot="live")
def request_live_view(args: dict, delivery) -> str:
    run_id = args["run_id"]
    binding, client = _prelude(run_id)
    url = _page_url()                                  # before the grant: no orphan grant
    envelope = wire.parse_live(client.request("POST", f"/v1/runs/{run_id}/live",
                                              {"purpose": args["purpose"]}))
    reason = casa_broker.fit_caption(args["reason"], 120).rstrip(".")
    caption = copy_text.LIVE_CAPTION.format(reason=reason[:1].upper() + reason[1:])
    key = "live-" + hashlib.sha256(f"{run_id}\0{envelope['grant_id']}".encode()).hexdigest()[:24]
    reference = casa_broker.deposit_link(
        "live", url, label=casa_broker.fit_label("", copy_text.LIVE_LABEL),
        caption=casa_broker.fit_caption(caption), key=key)
    delivery.reference = reference
    state.put_delivery(reference, "link", run_id, None, None, None)
    return copy_text.LIVE_SENT.format(ending=handed_over(binding))


@tool("site_setup_link",
      "Send the operator a button to Casa Browser's page: its setup (when ask_sign_in says "
      "Casa Browser cannot sign in at all).", obj(site_name=text(60)),
      slot="setup")
def site_setup_link(args: dict, delivery) -> str:
    continuation.current_requester()
    url = _page_url()
    caption = copy_text.SETUP_CAPTION.format(site=casa_broker.fit_caption(args["site_name"], 60))
    delivery.reference = casa_broker.deposit_link(
        "setup", url, label=casa_broker.fit_label("", copy_text.SETUP_LABEL),
        caption=casa_broker.fit_caption(caption), key=f"setup-{secrets.token_hex(8)}")
    return copy_text.SETUP_SENT.format(site=casa_broker.fit_caption(args["site_name"], 60))


# -- files, beats and sources -------------------------------------------------------

BEAT_GAP_S = 90.0
MAX_SOURCES_BODY = 12_000


def _file_of_run(run_id: str, file_ids: list[str]) -> None:
    for fid in file_ids:
        if state.file_run(fid) != run_id:
            raise Refused(copy_text.FOREIGN_FILE.format(file_id=fid))


def _report(client, binding, file_id: str, delivery_id: str, outcome: str) -> None:
    client.request("POST", f"/v1/control/files/{file_id}/delivery",
                   {"requester_id": binding.requester_id, "delivery_id": delivery_id,
                    "outcome": outcome}, control=True)


@tool("deliver_file", "Send one of the run's files to the operator in Telegram (when the "
      "requester is the operator's assistant).",
      obj(run_id=ID, file_id=ID, caption=text(200, 0)), slot="file")
def deliver_file(args: dict, delivery) -> str:
    run_id, file_id = args["run_id"], args["file_id"]
    _file_of_run(run_id, [file_id])
    binding, client = _prelude(run_id)
    if binding.requester_kind == "specialist":
        return copy_text.FILE_FOR_SPECIALIST
    meta = state.file_meta(file_id)
    if meta.size > files.TELEGRAM_MAX:
        return copy_text.FILE_TOO_BIG.format(name=meta.safe_name, size=files.megabytes(meta.size))
    data = client.request("GET", f"/v1/files/{file_id}", raw=True)
    kind = files.kind_for(meta.safe_name, meta.mime, data)
    if len(data) > files.TELEGRAM_MAX:
        return copy_text.FILE_TOO_BIG.format(name=meta.safe_name, size=files.megabytes(len(data)))
    if kind is None or len(data) > files.CAPS[kind.kind]:
        return copy_text.FILE_NEEDS_ZIP.format(name=meta.safe_name)
    path = files.stage(data, os.path.splitext(kind.filename)[1].lstrip(".").lower())
    reference = casa_broker.deposit_file(
        "file", str(path), kind=kind.kind, caption=casa_broker.fit_caption(args["caption"]),
        filename=kind.filename, key=f"file-{file_id}")
    delivery.reference = reference
    state.put_delivery(reference, "file", run_id, file_id, None, None)
    try:
        _report(client, binding, file_id, reference, "deposited")
    except AppError:
        pass                         # the final outcome is reported once settled
    return copy_text.FILE_SENT.format(name=kind.filename)


@tool("handoff_files", "Hand the run's files to the specialist that asked, through Casa's "
      "handoff folder; returns the paths to give it.",
      obj(run_id=ID, file_ids={"type": "array", "items": ID, "minItems": 1, "maxItems": 20}))
def handoff_files(args: dict) -> str:
    run_id = args["run_id"]
    _file_of_run(run_id, args["file_ids"])
    binding, client = _prelude(run_id)
    if binding.requester_kind != "specialist":
        return copy_text.FILE_FOR_OPERATOR
    done = []
    for fid in args["file_ids"]:
        meta = state.file_meta(fid)
        earlier = state.handoff_of(run_id, fid)
        if earlier is not None:               # published before: never twice
            done.append(f"{meta.safe_name} at {earlier[1]}")
            continue
        if meta.size > files.HANDOFF_MAX:
            done.append(copy_text.FILE_HANDOFF_TOO_BIG.format(
                name=meta.safe_name, size=files.megabytes(meta.size)))
            continue
        data = client.request("GET", f"/v1/files/{fid}", raw=True)
        try:
            out = casa_handoff.publish("browser", meta.safe_name, data=data)
        except casa_handoff.HandoffError as exc:
            done.append(copy_text.HANDOFF_FAILED.format(name=meta.safe_name, code=exc.kind))
            continue
        handoff_id = os.path.basename(os.path.dirname(out["path"]))
        state.put_handoff(handoff_id, run_id, fid, out["path"])
        done.append(f"{meta.safe_name} at {out['path']}")
    deliveries.report_file_outcomes()
    return copy_text.HANDED_OFF.format(items="; ".join(done))


@tool("progress", "A short line to the operator while something slow runs (at most one "
      "every 90 seconds per run); never to announce a step the check may ask about.", obj(run_id=ID, text=text(200)), slot="beat")
def progress(args: dict, delivery) -> str:
    run_id = args["run_id"]
    _prelude(run_id)
    n = state.take_beat(run_id, time.time(), BEAT_GAP_S)
    if n is None:
        return copy_text.BEAT_TOO_SOON
    delivery.reference = casa_broker.deposit_message(
        "beat", cards.md(args["text"], 200), key=f"beat-{run_id}-{n}")
    return copy_text.BEAT_SENT


def _escape_all(text_: str) -> str:
    """Every ASCII punctuation character backslash-escaped, so Casa's markdown
    shows the text exactly and nothing in it becomes a link or format."""
    return "".join("\\" + ch if ch in _string.punctuation else ch for ch in text_)


_LINKABLE_RE = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:@/?#]|%[0-9A-Fa-f]{2})+")


def linkable(url: str) -> bool:
    """An address Casa's renderer carries byte for byte: `https`, a host,
    only URL characters, and every `%` followed by two hex digits."""
    if not url.startswith("https://") or not _LINKABLE_RE.fullmatch(url):
        return False
    try:
        return bool(urllib.parse.urlsplit(url).hostname)
    except ValueError:
        return False


#: the answer posted with its sources (v0.1.26): what a reply would be, in one message
MAX_ANSWER = 4000
#: the app's limit on a finished run's summary (show_sources's finish records the answer)
MAX_SUMMARY = 2000


def _answer_text(answer: str) -> str:
    """The answer as Casa accepts a message body: no control character but newline and tab."""
    return "".join(ch if ch in "\n\t" or unicodedata.category(ch) != "Cc" else " "
                   for ch in answer).strip()


def _finish_answered(binding, client, run_id: str, finish: dict, ended: bool, n: int) -> str:
    """A run left going after show_sources would be woken by the sweep and answered again by
    each continuation. The answer is deposited and
    its `answered` record holds the finish, so the run is finished here, with no turn bound
    (the client refuses nothing now); a run that already ended is not; one with a file not yet
    confirmed, or one the app would not finish, is left to run_finish, and the sweep's retry
    finishes it if no call does."""
    if ended:
        return copy_text.SOURCES_PREPARED.format(n=n, ending=handed_over(binding))
    files = [d.file_id for d in state.run_file_deliveries(run_id)]
    if unresolved_files(run_id) or undelivered(run_id, files):
        return copy_text.SOURCES_PREPARED.format(n=n, ending=copy_text.SOURCES_FINISH)
    client.bounded = False
    try:
        after = close_run(client, binding, run_id, "finish", finish)
    except (AppError, Refused):
        return copy_text.SOURCES_PREPARED.format(n=n, ending=copy_text.SOURCES_FINISH)
    return copy_text.SOURCES_FINISHED.format(n=n, status=finish["expectation_status"],
                                             after=after, ending=handed_over(binding))


@tool("show_sources", "Send the operator your answer to an open-web question with its sources "
      "beneath it, as one message, and finish the run (when the requester is the operator's "
      "assistant): answer is exactly what you would have replied; source_ids are 1-8 source ids "
      "from observations or run_fetch, in the order you rely on them; expectation_status as for run_finish.",
      obj(run_id=ID, answer=text(MAX_ANSWER),
          source_ids={"type": "array", "items": ID, "minItems": 1, "maxItems": 8},
          expectation_status=enum("matched", "mismatch", "unknown")),
      slot="sources")
def show_sources(args: dict, delivery) -> str:
    """The answer and its sources are one message, the sources beneath it, so the sources
    never reach the operator before the answer the turn ends with. A recorded source whose address cannot be
    linked is cited by its title and host, plain. The same call finishes a run still
    going, past the turn bound too once the answer is deposited (the deposit is what is owed),
    and the turn ends silent; a run with a file not yet confirmed is left to run_finish. The
    deposit's `answered` record makes the finish plugin code's (the sweep retries it) and
    refuses a second answer for the run."""
    run_id, wanted = args["run_id"], args["source_ids"]
    binding, client = _prelude(run_id)
    if binding.requester_kind == "specialist":
        return copy_text.SOURCES_FOR_SPECIALIST
    if state.answer_of(run_id) is not None:     # v0.1.27: an answer is never sent twice
        return copy_text.SOURCES_ALREADY
    answer = _answer_text(args["answer"])
    recorded = {s["source_id"]: s for s in wire.parse_sources(client.request(
        "GET", f"/v1/control/runs/{run_id}/sources", control=True))}
    body = f"{answer}\n\n{copy_text.SOURCES_HEADER}"
    used, left = [], []
    for sid in wanted:
        src = recorded.get(sid)
        if src is None or sid in used:
            left.append(sid)
            continue
        title = " ".join(src["title_masked"].split())
        if linkable(src["url"]):
            host = urllib.parse.urlsplit(src["url"]).hostname
            cite = f"[{_escape_all(host)}]({_escape_all(src['url'])})"
        else:
            try:
                host = urllib.parse.urlsplit(src["url"]).hostname
            except ValueError:
                host = None
            cite = _escape_all(host) if host else ""
        if not (title or cite):
            left.append(sid)
            continue
        n = len(used) + 1
        line = (f"\n{n}. {_escape_all(title)} — {cite}" if title and cite
                else f"\n{n}. {_escape_all(title) or cite}")
        if len(body) + len(line) > MAX_SOURCES_BODY:
            left.append(sid)
            continue
        body += line
        used.append(sid)
    if not answer or not used:
        return copy_text.SOURCES_NONE
    digest = hashlib.sha256((",".join(used) + "\0" + answer).encode()).hexdigest()[:16]
    delivery.reference = casa_broker.deposit_message("sources", body,
                                                     key=f"sources-{run_id}-{digest}")
    finish = {"summary_masked": answer if len(answer) <= MAX_SUMMARY
              else answer[:MAX_SUMMARY - 1] + "…",
              "file_ids": [], "source_ids": used,
              "expectation_status": args["expectation_status"]}
    ended = state.is_terminal(run_id)
    state.record_answer(delivery.reference, run_id, finish, ended=ended)
    note = _finish_answered(binding, client, run_id, finish, ended, len(used))
    if left:
        note += copy_text.SOURCES_LEFT_OUT.format(ids=", ".join(left))
    return note

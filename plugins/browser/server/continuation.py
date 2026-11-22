"""One run per Fido turn, and how a run is continued.

Casa builds a fresh specialist session — so a fresh plugin process — for
every delegated, desk or pinned turn, so this process's age is the turn's
age. A turn drives one run and ends when the run must wait on the operator,
or once `TURN_BOUND_S` has passed; a fresh Fido turn then continues it,
queued by the operator's tap or by the plugin (the fast path), or found
by Casa's once-a-minute sweep, which is what correctness rests
on: a fast-path failure is ignored.
"""
from __future__ import annotations

import json
import logging
import time

import app_client
import casa_broker
import copy_text
import state
import tooling
import wire
from tooling import Refused

TURN_BOUND_S = 540.0

log = logging.getLogger("browser.continuation")

_STARTED = time.monotonic()
#: when this turn started, on the wall clock (one plugin process per turn; wakes record it)
TURN_STARTED_AT = time.time()
#: the clock the turn bound reads (a test injects another)
clock = time.monotonic


def turn_elapsed() -> float:
    return clock() - _STARTED


def turn_remaining() -> float:
    return TURN_BOUND_S - turn_elapsed()


def _turn_over() -> bool:
    return check_turn_bound() is not None


app_client.turn_over = _turn_over


#: A turn's tool calls. Casa ends a Fido session at its role's `max_turns` (500, role.yaml), and
#: a session cut there loses its answer. Every call of this turn counts (`tooling.call`, one
#: plugin process per turn), since each takes a model turn; past this many, the driving tools
#: are refused so the turn hands over with turns left. Reaching it loses nothing: a fresh turn
#: carries on in the same conversation.
STEP_BOUND = 400
#: A run's tool calls across all its turns (`state.count_call`). This catches a loop, not a long
#: job, which stays well below it. Past it, driving is refused with `FINISH_NOW`.
LOOP_BOUND = 2000


def check_step_bound() -> str | None:
    """The wrap-up sentence once this turn has made more than `STEP_BOUND` calls (the one now
    running included), else None."""
    if tooling.calls_made > STEP_BOUND:
        return copy_text.END_TURN_STEPS
    return None


def check_turn_bound(run_id: str | None = None) -> str | None:
    """The end-turn sentence once this turn has run `TURN_BOUND_S`, else None. The first time, the
    run this turn drives is queued for a fresh turn, which Casa starts once this one has
    left the specialist's desk, rather than at the sweep's next minute."""
    if turn_elapsed() >= TURN_BOUND_S:
        _hand_over()
        return copy_text.END_TURN_BOUND
    return None


#: the run this turn drives (`take`), until the turn bound hands it on
_driven: str | None = None


def _hand_over() -> None:
    """The fast path of a turn that ran its time; any failure leaves the run to the sweep. A run
    that ended, whose answer the operator has, or that waits for a sign-in code needs no turn."""
    global _driven
    run_id, _driven = _driven, None
    if run_id is None:
        return
    try:
        binding = state.run_binding(run_id)
        if (binding is not None and not state.is_terminal(run_id)
                and state.answer_of(run_id) is None and state.code_hold(run_id) is None):
            queue(binding.binding_ref, run_id, "sweep")
    except Exception as exc:  # noqa: BLE001 — the turn's own answer stands
        log.warning("turn bound: not handed over (%s)", type(exc).__name__)


def current_requester() -> casa_broker.Requester:
    """The requester this call serves (BrokerFailed propagates as a plain sentence)."""
    return casa_broker.requester()


def require_binding(run_id: str, *, operators: bool = False) -> state.RunBinding:
    """The run's stored binding, which must be the binding this call serves;
    else refused before any app call. `operators`: a call from the operator's own
    request (`operator_resident`) may act on any run, since every run here is theirs (Casa
    serves one operator); a specialist's call stays on its own binding."""
    binding = state.run_binding(run_id)
    if binding is None:
        raise Refused(copy_text.UNKNOWN_RUN)
    who = current_requester()
    if who.binding_ref != binding.binding_ref and not (
            operators and who.requester_kind == "operator_resident"):
        raise Refused(copy_text.NOT_YOUR_RUN)
    return binding


def refuse_if_stopped(run_id: str) -> None:
    """A run of a request the operator stopped is not driven, asked about or told by
    any call — refused before the turn bound, the binding and any app call."""
    if state.run_stopped(run_id):
        raise Refused(copy_text.STOPPED)


def ref_stopped(run_ref: str) -> bool:
    """A continuation reference (a run id, `intent:`, `request:` or `admission:`) that names a
    request the operator stopped."""
    kind, _, rest = run_ref.partition(":")
    if rest and kind == "request":
        return state.request_stopped(rest)
    if rest and kind == "intent":
        b = state.intent(rest)
        return b is not None and state.request_stopped(b.request_group)
    if rest and kind == "admission":
        key = state.by_confirmation(rest)
        return key is not None and state.request_stopped(key)
    return state.run_stopped(run_ref)


def queue(binding_ref: str, run_ref: str, reason: str) -> str | None:
    """The fast path; a failure is logged by its code word and ignored (the
    sweep recovers the run). A request the operator stopped is never queued."""
    if ref_stopped(run_ref):
        return None
    try:
        return casa_broker.request_continuation(binding_ref, run_ref, reason)
    except casa_broker.BrokerFailed as exc:
        log.warning("continuation not queued: %s", exc.code.split(":", 1)[0])
        return None


def take(run_id: str) -> None:
    global _driven
    _driven = run_id
    state.take_lease(run_id)


# -- a Stop's request cancels ----------------------------

#: the whole of one stop's app calls, its hello included, in elapsed time: well inside the
#: minute Casa waits for the stop tool, so an app too slow to answer still gets `not_taken`
#: with its refs
STOP_BUDGET_S = 40.0


def _stop_in_app(request_ids: list) -> list[dict]:
    """The app's Stop of one request: every id in ONE call (Casa Browser records them all and
    cancels every queued sibling in one step, then ends the run that drives)."""
    client = app_client.compatible_client()
    return wire.parse_request_cancel(client.request(
        "POST", "/v1/requests/cancel", {"request_ids": list(request_ids)}), request_ids)


def _within_budget(request_ids: list) -> list[dict]:
    """`_stop_in_app` bounded by `STOP_BUDGET_S` of elapsed time, however slowly the app
    answers: past it, `AppError("unreachable")` (the call goes on in its own daemon thread and
    changes nothing here; the sweep's retry asks again)."""
    import threading
    box: dict = {}

    def run() -> None:
        try:
            box["answer"] = _stop_in_app(request_ids)
        except BaseException as exc:  # noqa: BLE001 — handed to the caller
            box["error"] = exc
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(STOP_BUDGET_S)
    if worker.is_alive():
        raise app_client.AppError("unreachable")
    if "error" in box:
        raise box["error"]
    return box["answer"]


def cancel_request(key: str, row: state.PendingAdmission) -> str:
    """Stop every request id of the request in the app, in one call, and classify from the
    app's durable answers alone: `not_taken` (no answer: the seal stays unsettled, the sweep
    repeats this), `stopped` (some id was stopped: the seal stays), or `ended` (none: every run
    had ended on its own; the seal is removed and nothing of the request's own ending is
    touched). Only a run whose id answered `stopped` is marked heard (the Stop's line is its
    telling). The same on the stop call and on the sweep's retry."""
    try:
        answers = _within_budget(list(row.request_ids))
    except app_client.AppError as exc:
        log.warning("stop not taken: %s", exc.code)
        return "not_taken"
    stopped = False
    for answer in answers:
        if answer["run_id"] is not None:
            state.attach_run(answer["request_id"], answer["run_id"])
        if answer["stopped"]:
            stopped = True
            if answer["run_id"] is not None:
                state.heard_ending(answer["run_id"])
    state.settle_stop(key)
    if stopped:
        return "stopped"
    state.unseal_request(key)
    return "ended"


def retry_stops() -> None:
    """Each sweep pass, in plugin code: repeat the request cancels of every seal the app has not
    answered for every id (one cancel per request id per pass, no model, no Casa call)."""
    for key in state.unsettled_stops():
        row = state.admission(key)
        if row is None:
            state.settle_stop(key)
            continue
        cancel_request(key, row)


# -- v0.1.27: the finish of a run whose answer is posted ------------------------------------

def retry_answers(client, active: list[dict], attached_before: set | None = None) -> None:
    """Each sweep pass, in plugin code (no model, no Casa call): finish every run whose answer
    show_sources posted and whose finish is still owed, with the finish its record holds. A run
    the app no longer lists has ended (nothing more is owed, and no telling: the operator has its answer);
    a request the operator stopped is never finished here (their Stop ends it); a file of the run whose
    delivery is not settled is waited for."""
    listed = {r["run_id"]: r for r in active}
    sealed = state.sealed_requests()
    for rec in state.unfinished_answers():
        binding = state.run_binding(rec.run_id)
        if binding is None or binding.request_group in sealed:
            continue
        entry = listed.get(rec.run_id)
        if entry is None:
            # only a run attached before the listing was read can be ended by its absence
            # (one attached after it is simply not listed yet)
            if attached_before is None or rec.run_id in attached_before:
                state.heard_ending(rec.run_id)
            continue
        if entry["state"] in ("waiting_live", "live") or any(
                d.status in ("pending", "unknown")
                for d in state.run_file_deliveries(rec.run_id)):
            continue
        try:
            client.request("POST", f"/v1/runs/{rec.run_id}/finish", rec.finish)
        except app_client.AppError as exc:
            log.warning("answered run not finished: %s", exc.code)
            continue
        state.heard_ending(rec.run_id)


# -- the driver sweep -------------------------------------------

STALE_S = 120.0
CONFIRM_GRACE_S = 3600.0 + 300.0
RESWEEP_S = 3600.0
MAX_RESULTS = 50


def _question_holds(run_id: str, question_id: str | None, now: float) -> "bool | str":
    """A `waiting_approval` run on a question is the operator's while this plugin holds a card
    for exactly that question that is unexpired, unanswered and confirmed delivered;
    a card not yet asked about gives its reference. An answered card
    does not hold it: the continuation resumes the answer."""
    if question_id is None:
        return False
    card = state.open_card(run_id, now, "question")
    if card is None or state.card_question(card.group) != question_id:
        return False
    return _held_by(card.group)


def _held_by(group: str) -> "bool | str":
    row = state.card_delivery(group)
    if row is None:
        return False
    if row.status == "withheld":
        state.void_group(group, "withheld")
    # only a card Casa confirmed delivered holds the run; one still `pending` here is named for
    # Casa's sweep to confirm; any other card's outcome must be settled by a Fido
    # call, which this sweep queues
    if row.status == "delivered":
        return True
    return row.reference if row.status == "pending" else False


def needs_driver(entry: dict, binding: state.RunBinding, active: list[dict],
                 bindings: dict[str, state.RunBinding], now: float) -> "bool | list[str]":
    """The mechanical predicate for one run in the app's active listing:
    the app's state decides first; plugin state only says whether this
    plugin's own question or link holds the run. A list of deposit
    references means "needs a driver unless Casa delivered these" — the
    operator's card or link this plugin has not yet asked Casa about."""
    run_id, st = entry["run_id"], entry["state"]
    if state.lease_live(run_id, now):
        return False
    if st == "waiting_approval":
        held = _question_holds(run_id, entry.get("pending_question"), now)
        if held is True:
            return False
        return [held] if isinstance(held, str) else True
    if st == "live":
        return False                # the operator is in the view: no link is owed; Done resumes the run
    if st == "waiting_live":
        # The view waits for the operator on Casa Browser's page, and the app takes a live request only
        # from a running run, so a turn cannot send it again: nothing wakes Fido while it waits
        # (a link whose outcome a Casa restart forgot would wake a model turn
        # every minute). The app ends a view nobody opens within an hour. Only a
        # link not yet asked about is named, for Casa to check: one turn settles it.
        link = state.latest_link(run_id)
        return [link.reference] if link is not None and link.status == "pending" else False
    if st == "queued":
        mine = [r for r in active
                if bindings.get(r["run_id"]) is not None
                and bindings[r["run_id"]].request_group == binding.request_group]
        first = bool(mine) and mine[0]["run_id"] == run_id
        busy = any(r["state"] != "queued" for r in active if r["run_id"] != run_id)
        return first and not busy
    return True                     # starting, running, finishing


def _unresolved_file(run_id: str, now: float) -> bool:
    return any(d.status == "unknown" or (d.status == "pending"
                                         and now - d.deposited_at > STALE_S)
               for d in state.run_file_deliveries(run_id)
               if now - d.deposited_at > STALE_S)


#: while the operator is in a live view, Casa is asked to sweep again this soon, so
#: their Done (which reaches no one: the app answers only Fido) resumes the run within seconds
SWEEP_AGAIN_S = 5
#: a telling owed for a run the app ended while nothing drove it lapses after this
OWED_MAX_S = 3600.0


#: v0.1.28: a request that ended on its own is named in the sweep's answer (`ended`) for this
#: long after it was asked for, so Casa marks its card ended and takes its Stop away; Casa
#: edits each card once, however often the request is named
ENDED_TELL_S = 24 * 3600.0


def sweep(now: float | None = None) -> list[dict]:
    """The runs nothing is driving, as `[{run_ref, binding_ref}]` (at most 50)."""
    return sweep_answer(now)["runs"]


def sweep_answer(now: float | None = None) -> dict:
    """The sweep tool's answer: `{"runs": [{run_ref, binding_ref, unless_delivered?}]}` (at
    most 50), plus `sweep_again_s` while the operator is in a live view, and `ended`: the
    requests of the last day that ended on their own, `[{ref: "request:<key>", binding_ref}]`
    (at most 50; v0.1.28), each confirmed by reading its runs from the app. Reads plugin state
    and the app (its active listing, and a run's own state to confirm an end) only: no Casa call (the sweep runs outside any turn) and no
    action on any run. A run under a code hold is never listed, on any path."""
    now = time.time() if now is None else now
    out: list[dict] = []
    seen: dict[str, dict] = {}
    held = state.held_runs(now)          # waiting for a sign-in code it asked for
    retry_stops()                        # a cancel that did not land
    sealed = state.sealed_requests()     # a request the operator stopped: never listed, woken or owed
    again = False

    def add(ref: str, binding_ref: str, unless: list[str] | None = None) -> None:
        if ref in held:
            return
        if ref in seen:
            if unless is None:
                seen[ref].pop("unless_delivered", None)     # another need: wake it whatever
            return
        if len(out) < MAX_RESULTS:
            item = {"run_ref": ref, "binding_ref": binding_ref}
            if unless:
                item["unless_delivered"] = unless
            seen[ref] = item
            out.append(item)

    # v0.1.28: only a run attached BEFORE the app's listing was read can be
    # found ended by its absence from it. A run a concurrent start_runs created and attached
    # after the listing is simply not in it yet; marking it terminal would end a request still
    # going (and, with `ended`, take the Stop off the operator's card).
    attached_before = {row.binding.run_id for row in state.all_intents()
                       if row.binding.run_id is not None}
    try:
        client = app_client.compatible_client()
        active = wire.parse_active_runs(client.request("GET", "/v1/runs?state=active"))
    except app_client.AppError as exc:
        log.warning("sweep cannot read the app: %s", exc.code)
        active = None
    answered = state.answered_runs()     # v0.1.27: the operator has their answers
    if active is not None:
        retry_answers(client, active, attached_before)
        by_request = {r["request_id"]: r for r in active}
        for row in state.all_intents():
            b = row.binding
            if b.run_id is None and b.request_id in by_request:
                state.attach_run(b.request_id, by_request[b.request_id]["run_id"])
        active_ids = {r["run_id"] for r in active}
        for row in state.all_intents():
            b = row.binding
            if b.request_group in sealed:
                continue
            if (b.run_id is not None and not row.terminal and b.run_id not in active_ids
                    and b.run_id in attached_before):
                if state.lease_live(b.run_id, now):
                    # a turn drives it: a call of that turn that reads the end marks it heard;
                    # otherwise the first sweep after that turn owes its telling
                    # (an end the app made during the turn, e.g. a live view's hour, is told)
                    continue
                # the app ended it; the group moves on. Nothing drove it as it ended,
                # so whoever asked is told by a continuation (the app's late notice of it is not
                # posted) — owed in the same commit, so no crash between them loses it
                state.mark_terminal(b.run_id, owe_ending_at=now)
        rows = [r for r in state.all_intents() if r.binding.request_group not in sealed]
        bindings = {r.binding.run_id: r.binding for r in rows if r.binding.run_id}
        for entry in active:
            b = bindings.get(entry["run_id"])
            if b is None or entry["run_id"] in answered:
                continue                # an answered run is woken only for its files (below)
            need = needs_driver(entry, b, active, bindings, now)
            if need:
                add(entry["run_id"], b.binding_ref, need if isinstance(need, list) else None)
            elif entry["state"] == "live" and not state.lease_live(entry["run_id"], now):
                again = True
        for row in rows:
            b = row.binding
            if b.run_id is None and not row.terminal and now - row.created_at > STALE_S:
                add(f"intent:{b.request_id}", b.binding_ref)
        unacknowledged = {d.run_id for d, _ in state.unreported_settled_files()}
        unacknowledged |= {d.run_id for d, told in state.failed_file_attempts()
                           if d.status == "withheld" and not told}
        for b in bindings.values():
            if not state.lease_live(b.run_id, now) and (
                    _unresolved_file(b.run_id, now) or b.run_id in unacknowledged):
                add(b.run_id, b.binding_ref)
        for run_id in state.owed_endings(now - OWED_MAX_S):
            b = bindings.get(run_id)
            if b is not None and run_id not in answered and not state.lease_live(run_id, now):
                add(run_id, b.binding_ref)
    for adm in state.open_admissions():
        if adm.request_key in sealed:
            continue
        if adm.status in ("requested", "admitted") and now - adm.created_at > STALE_S:
            add(f"request:{adm.request_key}", adm.binding_ref)
        elif adm.status == "submitting" and now - (adm.claimed_at or adm.created_at) > STALE_S:
            add(f"request:{adm.request_key}", adm.binding_ref)
        elif (adm.status == "pending" and now - adm.created_at > CONFIRM_GRACE_S
              and (adm.swept_at is None or now - adm.swept_at > RESWEEP_S)):
            add(f"request:{adm.request_key}", adm.binding_ref)
            state.mark_swept(adm.request_key, now)
    answer: dict = {"runs": out}
    if again:
        answer["sweep_again_s"] = SWEEP_AGAIN_S
    if active is not None:
        ended = [{"ref": f"request:{key}", "binding_ref": binding_ref}
                 for key, binding_ref in state.ended_requests(now - ENDED_TELL_S, MAX_RESULTS,
                                                              now)
                 if _app_says_ended(client, key, now)]
        if ended:
            answer["ended"] = ended
    return answer


def _app_says_ended(client, request_key: str, now: float) -> bool:
    """v0.1.28: the request's runs, which plugin state holds ended, are each read
    from the app itself (`GET /v1/runs/{run}`, once per run: an end is final) and must all be
    in a terminal state. Plugin state's own end flags are written from several inferences (a
    run's absence from a listing among them), and a card must never lose its Stop while a run
    goes on; any read that fails names nothing this pass."""
    runs = state.request_runs(request_key)
    if not runs:
        return False
    known = state.app_ended_runs(runs)
    for run_id in runs:
        if run_id in known:
            continue
        try:
            st = wire.parse_run(client.request("GET", f"/v1/runs/{run_id}"))["state"]
        except app_client.AppError:
            return False
        if st not in wire.TERMINAL_STATES:
            return False
        state.record_app_ended(run_id, now)
    return True

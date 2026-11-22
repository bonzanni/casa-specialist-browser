"""The plugin's own relay of Casa Browser's judgements to Casa's check
(action model).

When the app answers a step with `judgement_required`, this code — never the
model — posts the judgement's packet, untouched, to Casa's
`action-judgement` route and posts Casa's answer to the app's control route
`POST /v1/control/runs/{run}/judgements/{id}`:

* **One retry, then no verdict.** When Casa's call fails (its
  `check_unavailable`, a typed request error, a timeout, a transport error or
  an answer of another shape), Casa is called once more. Only after both calls
  failed does the app get `{"no_verdict": "check_unavailable"}`. Nothing here
  ever writes a verdict: the verdict object, the digest and the latency the app
  receives are Casa's own, untouched.
* **The picture travels with the packet.** For kind `action` this code fetches the
  page's picture from the app's control route and hands it to Casa beside the packet,
  on both attempts. No picture, or a failed fetch, is the text check.
* **A lost reply is re-posted once.** When the app's reply to a verdict is lost
  in transport (`unreachable`, not a typed app error), the identical body is
  posted once more; the app answers the response it stored for that id (with
  `repeat: true`) and dispatches nothing again.
* **A go is waited for.** Applying a `go` runs the step itself, which can take
  longer than the client's 30 s (a navigation 30 s plus its load 10 s, a browser
  download's 30 s wait, a download hop up to 120 s). Each verdict post waits up to
  `VERDICT_TIMEOUT_S`, never past what is left of the turn.
* **Bounded.** A step whose verdict brings a successor judgement (the page
  changed, so the step is judged afresh) is relayed again, at most
  `MAX_JUDGEMENTS` judgements per tool call; past that the model is told the
  page keeps changing.

No verdict reason and no packet text is logged; a failure is logged by its
code word only.
"""
from __future__ import annotations

import base64
import logging

import casa_broker
import continuation
import copy_text
import wire
from app_client import AppError
from tooling import Refused

#: judgements relayed in one tool call (at most three)
MAX_JUDGEMENTS = 3
#: Casa's check is called at most this often per judgement (one retry)
CHECK_ATTEMPTS = 2
NO_VERDICT = {"no_verdict": "check_unavailable"}
#: how long one verdict post waits for the app's reply (the step it applies included)
VERDICT_TIMEOUT_S = 200.0

log = logging.getLogger("browser.judge")


def _picture(client, run_id: str, judgement: dict) -> str | None:
    """The page's picture for an `action` judgement, base64; None for any other kind, or when
    the app has none."""
    if judgement["kind"] != "action":
        return None
    try:
        data = client.request(
            "GET", f"/v1/control/runs/{run_id}/judgements/{judgement['id']}/picture",
            control=True, raw=True)
    except AppError:
        return None
    return base64.b64encode(data).decode("ascii") if data.startswith(wire.PNG_MAGIC) else None


def ask_check(judgement: dict, spec_digest: str | None, picture: str | None = None) -> dict:
    """The body to post for one judgement: Casa's answer, or the no-verdict
    body after `CHECK_ATTEMPTS` failed calls."""
    for _ in range(CHECK_ATTEMPTS):
        try:
            answer = casa_broker.action_judgement(judgement["kind"], judgement["packet"],
                                                  spec_digest, picture)
        except casa_broker.BrokerFailed as exc:
            log.warning("action check failed: %s", exc.code)
            continue
        return {"packet_sha256": answer.packet_sha256, "verdict": answer.verdict,
                "latency_ms": answer.latency_ms}
    return dict(NO_VERDICT)


def post_verdict(client, run_id: str, judgement_id: str, body: dict) -> dict:
    """The app's reply to a verdict. A reply lost in transport is asked for
    once more with the identical body (the app answers its stored response);
    a typed app error is raised as itself."""
    path = f"/v1/control/runs/{run_id}/judgements/{judgement_id}"
    try:
        return client.request("POST", path, body, control=True, timeout_s=_verdict_timeout())
    except AppError as err:
        if err.code != "unreachable":
            raise
    return client.request("POST", path, body, control=True, timeout_s=_verdict_timeout())


def _verdict_timeout() -> float:
    """`VERDICT_TIMEOUT_S`, never past the turn bound (past it, the client
    refuses the request before sending it)."""
    return max(min(VERDICT_TIMEOUT_S, continuation.turn_remaining()), 0.001)


def relay_pending(client, run_id: str) -> list[dict]:
    """The judgements Casa Browser started itself (`login_item` when a
    site has no linked login, `signin` after a live-view sign-in), relayed by this code — never
    the model — exactly as a step's judgement: Casa's check (one retry), then the app's
    verdict route. Returns each one's parsed outcome. A judgement the app does not take now (a
    typed error) stays with the app and is relayed again by a later call; the turn bound ends
    the relay."""
    try:
        pending = wire.parse_pending_judgements(client.request(
            "GET", f"/v1/control/runs/{run_id}/judgements/pending", control=True))
    except AppError as err:
        log.warning("pending judgements unavailable: %s", err.code)
        return []
    out = []
    for judgement in pending:
        if continuation.check_turn_bound(run_id) is not None:
            break
        body = ask_check(judgement, None)
        try:
            out.append(wire.parse_step(post_verdict(client, run_id, judgement["id"], body)))
        except AppError as err:
            log.warning("app judgement not applied: %s", err.code)
    return out


def relay(client, run_id: str, step: dict, spec_digest: str | None, *,
          limit: int = MAX_JUDGEMENTS) -> dict:
    """Relay `step` (a parsed step reply) while it carries a judgement; the
    first step that does not, as parsed by `wire.parse_step`. Raises `Refused`
    when the page keeps changing (a judgement past `MAX_JUDGEMENTS`) or the
    turn bound has passed before a check."""
    relayed = 0
    while step["outcome"] in wire.JUDGEMENT_OUTCOMES:
        if relayed == limit:
            raise Refused(copy_text.PAGE_KEEPS_CHANGING)
        bound = continuation.check_turn_bound(run_id)
        if bound is not None:
            raise Refused(bound)
        relayed += 1
        judgement = step["judgement"]
        body = ask_check(judgement, spec_digest, _picture(client, run_id, judgement))
        step = wire.parse_step(post_verdict(client, run_id, judgement["id"], body))
    return step

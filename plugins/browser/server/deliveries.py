"""What became of the plugin's deposits with Casa, and the app's notices.

Casa keeps delivery outcomes in memory, and only a `callsCasa` call can ask,
so outcomes settle at the next Fido call: `delivered` and `withheld`
are final; `pending` stays; `unknown` (a restart forgot it) stays unresolved
until a new attempt under the same deposit key settles.
"""
from __future__ import annotations

import logging

import casa_broker
import copy_text
import state
import tooling
import wire

log = logging.getLogger("browser.deliveries")

NOTICE_SLOT = "notice"
MAX_NOTICE_BODY = 11_000
NOTICE_HEADER = "Casa Browser:"


def settle_deliveries(binding_ref: str | None = None) -> list[str]:
    """Ask Casa about every unsettled current attempt and settle it; tell the
    app what became of each file once its outcome is final; and — from the
    durable state, never from this call's transitions, so a process that dies
    after saving an outcome loses nothing — put on the current result a
    sentence for each of this binding's files whose current attempt did not
    reach the operator or whose outcome is lost. Returns [] (sentences go
    through `tooling.note`)."""
    for d in state.pending_deliveries():
        try:
            status = casa_broker.delivery_status(d.reference)
        except casa_broker.BrokerFailed as exc:
            log.warning("delivery status unavailable: %s", exc.code.split(":", 1)[0])
            break
        if status != "pending":
            state.settle_delivery(d.reference, status)
    report_file_outcomes()
    if binding_ref is not None:
        failed = state.failed_file_attempts(binding_ref)
        for d, _ in failed:
            meta = state.file_meta(d.file_id)
            name = meta.safe_name if meta else d.file_id
            tooling.note((copy_text.FILE_WITHHELD if d.status == "withheld"
                          else copy_text.FILE_UNCONFIRMED).format(
                              name=name, run_id=d.run_id, file_id=d.file_id))
        state.mark_told([d.reference for d, told in failed if not told])
    return []


def report_file_outcomes() -> None:
    """Tell the app, once, the final outcome of each settled file attempt
    (`delivered` / `withheld`), under the attempt's own reference."""
    pending = state.unreported_settled_files()
    if not pending:
        return
    try:
        from app_client import compatible_client
        client = compatible_client()
        for d, outcome in pending:
            binding = state.run_binding(d.run_id) if d.run_id else None
            if binding is None:
                continue
            client.request("POST", f"/v1/control/files/{d.file_id}/delivery", {
                "requester_id": binding.requester_id, "delivery_id": d.reference,
                "outcome": outcome}, control=True)
            state.mark_reported(d.reference, outcome)
    except Exception as exc:  # noqa: BLE001 — told again at the next call
        log.warning("file outcome not reported: %s", type(exc).__name__)


def deliver_notices(client, delivery) -> str | None:
    """Post the app's notices newer than the cursor as one operator message,
    key `notices-<first>-<last>`: those older than a day passed over, a repeated
    text once with its count. The post is recorded (key and body) before
    it is deposited and offered again exactly as it was — same key, same body,
    whatever arrived since — until Casa says it was delivered; only then does
    the cursor move and the next batch form. Returns the sentence for the
    result, if any."""
    if any(d.kind == "notice" and d.status == "pending" for d in state.pending_deliveries()):
        return None                     # one still in flight; offer again later
    batch = state.open_notice_batch()
    while batch is None:
        # notices older than a day are passed over without a post; when a
        # whole page of them was, the next page is fetched in the same call
        cursor = state.notice_cursor()
        notices = wire.parse_notices(client.request("GET", f"/v1/notices?after={cursor}"))
        batch = state.form_notice_batch(notices, NOTICE_HEADER, MAX_NOTICE_BODY,
                                        repeats=copy_text.NOTICE_REPEATS)
        if batch is None and state.notice_cursor() == cursor:
            return None
    reference = casa_broker.deposit_message(NOTICE_SLOT, batch.body, key=batch.key)
    delivery.reference = reference
    state.put_delivery(reference, "notice", None, None, batch.last_seq, None)
    return copy_text.NOTICES_SENT.format(n=batch.count)

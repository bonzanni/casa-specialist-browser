"""The plugin's calls to Casa over the internal broker socket: deposits (what
Casa posts to the operator) and the routes a `callsCasa` tool call may use.
Nothing Casa sends back is echoed: an answer must have its documented shape
or it is reported by a fixed code."""
from __future__ import annotations

import json

import pytest

import casa_broker
from casa_broker import BrokerFailed, DepositFailed
from conftest import ROOT, SERVER, casa_eval

KEY = "notices-1-3"
BIND = "casa-bind-" + "a" * 32
CANARY = "CANARY-casa-text https://canary.invalid/x"


def _last(broker):
    return broker.calls[-1][1]


def test_deposit_shapes(broker):
    ref = casa_broker.deposit_message("notice", "Three notices.", key=KEY)
    assert casa_broker.REFERENCE_RE.fullmatch(ref)
    assert _last(broker) == {"client": "client-1", "slot": "notice",
                             "value": "Three notices.", "key": KEY}

    casa_broker.deposit_link("link", "https://ha.example/x", label="Open Casa Browser",
                             caption="The live view is waiting.", key="link-r1-1")
    assert _last(broker) == {"client": "client-1", "slot": "link",
                             "value": "https://ha.example/x", "label": "Open Casa Browser",
                             "caption": "The live view is waiting.", "key": "link-r1-1"}

    casa_broker.deposit_file("file", "/data/outbox/a.pdf", kind="document",
                             caption="March invoice", filename="invoice.pdf",
                             key="file-f1-1")
    assert _last(broker) == {"client": "client-1", "slot": "file",
                             "value": "/data/outbox/a.pdf", "kind": "document",
                             "caption": "March invoice", "filename": "invoice.pdf",
                             "key": "file-f1-1"}
    casa_broker.deposit_file("file", "/data/outbox/b.png", kind="photo", caption="",
                             filename=None, key="file-f2-1")
    assert _last(broker) == {"client": "client-1", "slot": "file",
                             "value": "/data/outbox/b.png", "kind": "photo",
                             "key": "file-f2-1"}

    card = {"text": "Allow?", "buttons": [{"label": "Allow", "tool": "apply_tap",
                                           "arguments": {"t": "x"}}], "revision": "b-1"}
    casa_broker.deposit_proposal("card", card, key="card-1")
    sent = _last(broker)
    assert set(sent) == {"client", "slot", "value", "key"}
    assert sent["value"] == json.dumps(card, separators=(",", ":"), ensure_ascii=False)


@pytest.mark.parametrize("key", ["", "a b", "x" * 129, "é", None])
def test_deposit_key_required_and_well_formed(broker, key):
    with pytest.raises(DepositFailed) as e:
        casa_broker.deposit_message("notice", "hi", key=key)
    assert e.value.code == "bad_key"
    assert broker.calls == []


def test_casa_error_code_passthrough(broker):
    with pytest.raises(DepositFailed) as e:
        casa_broker.deposit_message("unknown-slot", "x", key=KEY)
    assert e.value.code == "no_call_in_flight"
    broker.in_call = False
    with pytest.raises(BrokerFailed) as e:
        casa_broker.requester()
    assert e.value.code == "no_call_in_flight"
    broker.in_call = True
    with pytest.raises(DepositFailed) as e:
        casa_broker.deposit_file("file", "/p", kind="spreadsheet", caption="",
                                 filename=None, key=KEY)
    assert e.value.code == "bad_kind"


@pytest.mark.parametrize("status, payload, code", [
    (200, b"not json " + CANARY.encode(), "broker_bad_response"),
    (200, b"[1, 2]", "broker_bad_response"),
    (200, {"reference": "casa-cap-NOTHEX" + CANARY}, "unrecognized_error"),
    (200, {"error": CANARY}, "unrecognized_error"),
    (500, {"error": "Some Text With Spaces"}, "unrecognized_error"),
    (200, {"reference": "casa-cap-" + "a" * 31}, "unrecognized_error"),
    (200, b"{" * 100000, "broker_bad_response"),
])
def test_unrecognised_answer(broker, status, payload, code):
    broker.overrides["/internal/broker/deposit"] = (status, payload)
    with pytest.raises(DepositFailed) as e:
        casa_broker.deposit_message("notice", "x", key=KEY)
    assert e.value.code == code
    text = " ".join([str(e.value), repr(e.value), e.value.code])
    assert "CANARY" not in text and "canary" not in text


def test_oversized_answer(broker):
    broker.overrides["/internal/broker/requester"] = (200, b'{"a":"' + b"x" * 70000 + b'"}')
    with pytest.raises(BrokerFailed) as e:
        casa_broker.requester()
    assert e.value.code == "broker_bad_response"


def test_env_missing(monkeypatch):
    monkeypatch.delenv("CASA_BROKER_SOCKET", raising=False)
    monkeypatch.delenv("CASA_BROKER_CLIENT", raising=False)
    with pytest.raises(DepositFailed) as e:
        casa_broker.deposit_message("notice", "x", key=KEY)
    assert e.value.code == "broker_env_missing"
    with pytest.raises(BrokerFailed) as e:
        casa_broker.requester()
    assert e.value.code == "broker_env_missing"


def test_unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("CASA_BROKER_SOCKET", str(tmp_path / "nothing.sock"))
    monkeypatch.setenv("CASA_BROKER_CLIENT", "c")
    with pytest.raises(BrokerFailed) as e:
        casa_broker.request_continuation(BIND, "run1", "sweep")
    assert e.value.code.startswith("broker_unreachable:")
    assert str(tmp_path) not in str(e.value)


def test_requester(broker):
    r = casa_broker.requester()
    assert r == casa_broker.Requester(BIND, "assistant", "operator_resident", ("assistant",))
    assert _last(broker) == {"client": "client-1"}


@pytest.mark.parametrize("answer", [
    {"binding_ref": "bind-1", "requester_id": "assistant",
     "requester_kind": "operator_resident", "chain": []},
    {"binding_ref": BIND, "requester_id": "assistant", "requester_kind": "god", "chain": []},
    {"binding_ref": BIND, "requester_id": "has space", "requester_kind": "specialist",
     "chain": []},
    {"binding_ref": BIND, "requester_id": "a", "requester_kind": "specialist",
     "chain": "a"},
    {"binding_ref": BIND, "requester_id": "a", "requester_kind": "specialist"},
])
def test_requester_shape_checked(broker, answer):
    broker.requester = answer
    with pytest.raises(BrokerFailed) as e:
        casa_broker.requester()
    assert e.value.code == "broker_bad_response"


RUNS = [{"spec_digest": "sha256:" + "1" * 64, "mode": "account", "task_kind": "invoice",
         "task_text": "Download the March invoice.",
         "sites": [{"site_id": "s1", "access": "account", "name": "Utility"}]}]


def test_broker_routes_shapes(broker):
    a = casa_broker.run_admission("key-1", RUNS)
    sent = _last(broker)
    assert set(sent) == {"client", "request_key", "runs"}
    assert sent["runs"][0]["sites"][0] == {"site_id": "s1", "access": "account",
                                           "name": "Utility"}
    assert a.status == "admitted" and a.request_key == "key-1"
    assert a.admissions[0]["origin_kind"] == "operator_telegram"
    assert a.admissions[0]["spec_digest"] == RUNS[0]["spec_digest"]
    # the same key answers the same, without a second reading
    assert casa_broker.run_admission("key-1", RUNS) == a

    broker.admit_mode = "pending"
    p = casa_broker.run_admission("key-2", RUNS)
    assert p.status == "confirmation_pending" and p.confirmation_id.startswith("conf-")
    s = casa_broker.run_admission_status(p.confirmation_id)
    assert _last(broker) == {"client": "client-1", "confirmation_id": p.confirmation_id}
    assert (s.status, s.request_key) == ("pending", "key-2")
    broker.settle_confirmation(p.confirmation_id, "declined")
    assert casa_broker.run_admission_status(p.confirmation_id).status == "declined"
    unknown = casa_broker.run_admission_status("conf-" + "9" * 32)
    assert (unknown.status, unknown.request_key) == ("expired", None)

    broker.admit_mode = "refuse"
    r = casa_broker.run_admission("key-3", RUNS)
    assert (r.status, r.reason) == ("refused", "not_delivered")

    for reason in ("operator_answer", "next_run", "sweep"):
        assert casa_broker.request_continuation(BIND, f"run-{reason}", reason) == "queued"
        assert _last(broker) == {"client": "client-1", "binding_ref": BIND,
                                 "run_ref": f"run-{reason}", "reason": reason}
    assert casa_broker.request_continuation(BIND, "run-sweep", "sweep") == "already_queued"

    ref = "casa-cap-" + "b" * 32
    assert casa_broker.delivery_status(ref) == "unknown"
    broker.delivery[ref] = {"status": "delivered", "repeat": True}
    assert casa_broker.delivery_status(ref) == "delivered"
    assert _last(broker) == {"client": "client-1", "reference": ref}


@pytest.mark.parametrize("call", [
    lambda: casa_broker.request_continuation(BIND, "run1", "because"),
    lambda: casa_broker.request_continuation(BIND, "x" * 65, "sweep"),
    lambda: casa_broker.request_continuation(BIND, "", "sweep"),
    lambda: casa_broker.request_continuation("not-a-binding", "run1", "sweep"),
    lambda: casa_broker.delivery_status("casa-cap-short"),
    lambda: casa_broker.run_admission("", RUNS),
    lambda: casa_broker.run_admission("k", []),
    lambda: casa_broker.run_admission("k", RUNS * 9),
    lambda: casa_broker.run_admission("k", [dict(RUNS[0], sites=[{"site_id": "s1",
                                                                  "access": "account"}])]),
    lambda: casa_broker.run_admission_status(""),
])
def test_requests_refused_before_sending(broker, call):
    with pytest.raises(BrokerFailed) as e:
        call()
    assert e.value.code == "bad_request"
    assert broker.calls == []


@pytest.mark.parametrize("route, answer", [
    ("/internal/broker/run-admission", {"status": "admitted", "request_key": "key-9",
                                        "confirmation_id": "conf-" + "c" * 32,
                                        "admissions": [{"spec_digest": "x"}]}),
    ("/internal/broker/run-admission", {"status": "maybe", "request_key": "key-9"}),
    ("/internal/broker/run-admission", {"status": "admitted", "request_key": "other",
                                        "confirmation_id": "conf-" + "c" * 32,
                                        "admissions": []}),
    ("/internal/broker/continue", {"status": "started"}),
    ("/internal/broker/delivery-status", {"status": "sent", "repeat": False}),
])
def test_broker_answers_shape_checked(broker, route, answer):
    broker.overrides[route] = (200, answer)
    with pytest.raises(BrokerFailed) as e:
        if route.endswith("run-admission"):
            casa_broker.run_admission("key-9", RUNS)
        elif route.endswith("continue"):
            casa_broker.request_continuation(BIND, "run1", "sweep")
        else:
            casa_broker.delivery_status("casa-cap-" + "d" * 32)
    assert e.value.code == "broker_bad_response"


def test_admitted_answer_must_cover_every_run(broker):
    """An admitted answer whose admissions do not match the runs asked for,
    digest for digest, is not taken as an admission."""
    broker.overrides["/internal/broker/run-admission"] = (200, {
        "status": "admitted", "request_key": "key-5", "confirmation_id": "conf-" + "c" * 32,
        "admissions": [{"spec_digest": "sha256:" + "f" * 64, "origin_kind": "operator_telegram",
                        "origin_ref": "adm-" + "0" * 32}]})
    with pytest.raises(BrokerFailed) as e:
        casa_broker.run_admission("key-5", RUNS)
    assert e.value.code == "broker_bad_response"


def test_fit_label_and_caption():
    assert casa_broker.fit_label("Open ", "ING Bank N.V.") == "Open ING Bank NV"
    assert len(casa_broker.fit_label("Open ", "x" * 100)) == casa_broker.MAX_LABEL_CHARS
    cap = casa_broker.fit_caption("line one\nhttps://x.example www.y")
    assert "\n" not in cap and "://" not in cap and "www." not in cap


def test_handoff_copy_identical(casa):
    ours = (SERVER / "casa_handoff.py").read_bytes()
    theirs = (casa / "casa" / "rootfs" / "opt" / "casa" / "casa_handoff.py").read_bytes()
    assert ours == theirs


def test_answer_for_another_key_refused(broker):
    """Run admission's answer must be for the key asked: an answer naming another key is
    not this request's admission."""
    broker.overrides["/internal/broker/run-admission"] = (200, {
        "status": "confirmation_pending", "request_key": "someone-else",
        "confirmation_id": "conf-" + "c" * 32})
    with pytest.raises(BrokerFailed) as e:
        casa_broker.run_admission("key-6", RUNS)
    assert e.value.code == "broker_bad_response"


def test_status_answer_for_another_confirmation_refused(broker):
    """Run admission status's answer must be about the confirmation asked for (Casa's own answer
    always carries the queried id)."""
    asked, other = "conf-" + "a" * 32, "conf-" + "b" * 32
    broker.overrides["/internal/broker/run-admission-status"] = (200, {
        "status": "admitted", "request_key": "other-key", "confirmation_id": other,
        "admissions": []})
    with pytest.raises(BrokerFailed) as e:
        casa_broker.run_admission_status(asked)
    assert e.value.code == "broker_bad_response"
    broker.overrides["/internal/broker/run-admission-status"] = (200, {
        "status": "expired"})
    with pytest.raises(BrokerFailed):
        casa_broker.run_admission_status(asked)


# -- the check of one browsing step (action model) -------------------------------

PACKET = '{"action":{"kind":"click"},"page":{"blocks":[{"text":"Rechnung März"}]}}'
SPEC = "sha256:" + "5" * 64


def test_action_judgement_shapes(broker):
    import hashlib
    a = casa_broker.action_judgement("action", PACKET, SPEC)
    assert _last(broker) == {"client": "client-1", "kind": "action", "packet": PACKET,
                             "spec_digest": SPEC}
    # the packet travels as the same string: Casa's digest is over exactly those bytes
    assert a.packet_sha256 == hashlib.sha256(PACKET.encode("utf-8")).hexdigest()
    assert a.verdict == broker.default_verdict and a.latency_ms == broker.latency_ms
    casa_broker.action_judgement("rejudge", PACKET, None)
    assert _last(broker)["spec_digest"] is None


def test_action_judgement_has_its_own_timeout(broker, monkeypatch):
    """Only the check waits longer than the broker's default (10 s): the check's
    own call is bounded at 90 s in Casa."""
    assert casa_broker.TIMEOUT_S == 10.0 and casa_broker.JUDGEMENT_TIMEOUT_S == 100.0
    seen = []
    real = casa_broker._UnixHTTP

    def spy(path, timeout=casa_broker.TIMEOUT_S):
        seen.append(timeout)
        return real(path, timeout)
    monkeypatch.setattr(casa_broker, "_UnixHTTP", spy)
    casa_broker.action_judgement("action", PACKET, SPEC)
    casa_broker.requester()
    assert seen == [100.0, 10.0]


def test_action_judgement_timeout_is_a_failure(broker, monkeypatch):
    monkeypatch.setattr(casa_broker, "JUDGEMENT_TIMEOUT_S", 0.3)
    broker.judge_script = ["timeout"]
    with pytest.raises(BrokerFailed) as e:
        casa_broker.action_judgement("action", PACKET, SPEC)
    assert e.value.code == "broker_unreachable:TimeoutError"


@pytest.mark.parametrize("args", [("decide", PACKET, SPEC), ("action", "", SPEC),
                                  ("action", {"a": 1}, SPEC), ("action", PACKET, ""),
                                  ("action", PACKET, "x" * 129),
                                  ("action", PACKET, "sha256:\n")])
def test_action_judgement_refused_before_sending(broker, args):
    with pytest.raises(BrokerFailed) as e:
        casa_broker.action_judgement(*args)
    assert e.value.code == "bad_request" and broker.calls == []


@pytest.mark.parametrize("answer", [
    {"verdict": "go", "packet_sha256": "a" * 64, "latency_ms": 1},
    {"verdict": {"verdict": "go"}, "packet_sha256": "A" * 64, "latency_ms": 1},
    {"verdict": {"verdict": "go"}, "packet_sha256": "a" * 63, "latency_ms": 1},
    {"verdict": {"verdict": "go"}, "packet_sha256": "a" * 64, "latency_ms": True},
    {"verdict": {"verdict": "go"}, "packet_sha256": "a" * 64, "latency_ms": -1},
    {"verdict": {"verdict": "go"}, "packet_sha256": "a" * 64},
    {"packet_sha256": "a" * 64, "latency_ms": 1},
])
def test_action_judgement_answer_shape_checked(broker, answer):
    broker.overrides["/internal/broker/action-judgement"] = (200, answer)
    with pytest.raises(BrokerFailed) as e:
        casa_broker.action_judgement("action", PACKET, SPEC)
    assert e.value.code == "broker_bad_response"


@pytest.mark.parametrize("code", ["check_unavailable", "bad_packet"])
def test_action_judgement_casa_error_is_a_failure(broker, code):
    broker.judge_script = [code]
    with pytest.raises(BrokerFailed) as e:
        casa_broker.action_judgement("action", PACKET, SPEC)
    assert e.value.code == code


def test_action_judgement_route_matches_casa(casa):
    """Casa's route takes this request shape and answers within the plugin's timeout."""
    got = casa_eval(casa, """
import action_judgement, operator_calls
print(json.dumps({"kinds": list(action_judgement.KINDS),
                  "check_timeout": action_judgement.CHECK_TIMEOUT_S,
                  "digest_chars": action_judgement.MAX_DIGEST_CHARS,
                  "handler": hasattr(operator_calls, "build_action_judgement_handler"),
                  "route": "/internal/broker/action-judgement" in open(
                      operator_calls.__file__.replace("operator_calls.py", "casa_core.py")).read()}))
""")
    assert got["kinds"] == list(casa_broker.JUDGEMENT_KINDS)
    assert got["check_timeout"] < casa_broker.JUDGEMENT_TIMEOUT_S
    assert got["digest_chars"] == casa_broker.REQUEST_KEY_MAX
    assert got["handler"] and got["route"]


# -- the conversation's pad ------------------------------------------------------------------

def test_pad_write_read_shapes(broker):
    assert casa_broker.pad_write("list", "a\nb") == (2, 3)
    assert _last(broker) == {"client": "client-1", "op": "write", "section": "list",
                             "text": "a\nb"}
    casa_broker.pad_write("list", "c", append=True)
    assert _last(broker)["op"] == "append"
    assert casa_broker.pad_read("list") == ("a\nb\nc", None)
    assert _last(broker) == {"client": "client-1", "op": "read", "section": "list", "offset": 0}
    broker.pad_sections["big"] = "x" * 70_001
    assert casa_broker.pad_read("big", 30_000) == ("x" * 30_000, 60_000)
    assert casa_broker.pad_text("big", 80_000) == "x" * 70_001
    with pytest.raises(BrokerFailed) as err:
        casa_broker.pad_text("big", 60_000)
    assert err.value.code == "pad_too_long"
    assert [b["offset"] for _p, b in broker.calls[-3:]] == [0, 30_000, 60_000]
    broker.calls.clear()
    with pytest.raises(BrokerFailed):
        casa_broker.pad_text("big", 20_000)
    assert len(broker.calls) == 1                           # nothing read past the limit


@pytest.mark.parametrize("answer", [
    {"text": 5, "next": None}, {"text": "x", "next": 0}, {"text": "x", "next": "30000"},
    {"text": "x" * 30_001, "next": None}, {"next": None}, {"text": "x", "next": True}])
def test_a_pad_read_of_another_shape_is_bad_response(broker, answer):
    """`next` must move on (a reply that never does would read forever) and a window stays
    within 30,000 characters."""
    broker.overrides["/internal/broker/pad"] = (200, answer)
    with pytest.raises(BrokerFailed) as err:
        casa_broker.pad_read("s")
    assert err.value.code == "broker_bad_response"


@pytest.mark.parametrize("answer", [{"lines": -1, "bytes": 0}, {"lines": True, "bytes": 1},
                                    {"bytes": 1}, {"lines": 1, "bytes": "1"}])
def test_a_pad_write_of_another_shape_is_bad_response(broker, answer):
    broker.overrides["/internal/broker/pad"] = (200, answer)
    with pytest.raises(BrokerFailed) as err:
        casa_broker.pad_write("s", "x")
    assert err.value.code == "broker_bad_response"


def test_a_pad_refusal_raises_its_code_and_never_its_message(broker):
    broker.overrides["/internal/broker/pad"] = (200, {"error": "duplicate_section",
                                                      "message": CANARY})
    with pytest.raises(BrokerFailed) as err:
        casa_broker.pad_write("s", "x")
    assert err.value.code == "duplicate_section" and CANARY not in str(err.value)


@pytest.mark.parametrize("section", ["a\nb", "x" * 201, 5, "tab\there"])
def test_a_bad_section_name_or_an_oversized_text_never_calls(broker, section):
    with pytest.raises(BrokerFailed) as err:
        casa_broker.pad_write(section, "x")
    assert err.value.code == "bad_request"
    with pytest.raises(BrokerFailed) as err:
        casa_broker.pad_write("s", "x" * (casa_broker.PAD_MAX_BYTES + 1))
    assert err.value.code == "pad_full"
    assert not [p for p, _b in broker.calls if p == "/internal/broker/pad"]

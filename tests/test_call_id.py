"""Every request to Casa names its call: the server keeps the id of the tool call
from the request's metadata (`_meta["claudecode/toolUseId"]`) for that call only,
and every route the plugin uses carries it as `tool_use_id`. A call with no id
(an older client, or Casa's own calls with no model: the sweep, the Stop) makes
exactly the requests it made before."""
from __future__ import annotations

import json
import threading

import pytest

import browser_server
import casa_broker
import state
import tooling
from conftest import admitted_run

ID = "toolu_x"
RUNS = [{"spec_digest": "sha256:" + "1" * 64, "mode": "account", "task_kind": "invoice",
         "task_text": "Download the March invoice.",
         "sites": [{"site_id": "s1", "access": "account", "name": "Utility"}]}]

#: route -> (one call of its helper, the body's keys without an id)
ROUTES = {
    casa_broker.ROUTE_DEPOSIT: (
        lambda b: casa_broker.deposit_message("notice", "Three notices.", key="notices-1"),
        {"client", "slot", "value", "key"}),
    casa_broker.ROUTE_REQUESTER: (
        lambda b: casa_broker.requester(),
        {"client"}),
    casa_broker.ROUTE_ADMISSION: (
        lambda b: casa_broker.run_admission("key-1", RUNS),
        {"client", "request_key", "runs"}),
    casa_broker.ROUTE_ADMISSION_STATUS: (
        lambda b: casa_broker.run_admission_status("conf-" + "9" * 32),
        {"client", "confirmation_id"}),
    casa_broker.ROUTE_CONTINUE: (
        lambda b: casa_broker.request_continuation(b.requester["binding_ref"], "run1", "sweep"),
        {"client", "binding_ref", "run_ref", "reason"}),
    casa_broker.ROUTE_DELIVERY_STATUS: (
        lambda b: casa_broker.delivery_status("casa-cap-" + "0" * 32),
        {"client", "reference"}),
    casa_broker.ROUTE_ACTION_JUDGEMENT: (
        lambda b: casa_broker.action_judgement("action", "Click Invoices.", None),
        {"client", "kind", "packet", "spec_digest"}),
    casa_broker.ROUTE_PAD: (
        lambda b: casa_broker.pad_write("notes", "The March invoice."),
        {"client", "op", "section", "text"}),
}


def _tools_call(name="browser_status", meta=None, **arguments):
    params = {"name": name, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}


@pytest.fixture
def no_call_id():
    casa_broker.set_call_id(None)
    yield
    casa_broker.set_call_id(None)


@pytest.mark.parametrize("route", list(ROUTES))
def test_every_route_carries_the_id(broker, no_call_id, route):
    helper, keys = ROUTES[route]
    casa_broker.set_call_id(ID)
    helper(broker)
    path, body = broker.calls[-1]
    assert path == route
    assert body == {**{k: body[k] for k in keys}, "tool_use_id": ID}


@pytest.mark.parametrize("route", list(ROUTES))
def test_without_an_id_every_request_is_todays(broker, no_call_id, route):
    helper, keys = ROUTES[route]
    helper(broker)
    path, body = broker.calls[-1]
    assert path == route
    assert set(body) == keys


@pytest.mark.parametrize("raises", [False, True])
def test_the_server_keeps_the_calls_id_for_the_call_only(broker, no_call_id, monkeypatch,
                                                          raises):
    seen = []

    def tool_call(name, arguments):
        seen.append(casa_broker.call_id())
        casa_broker.deposit_message("notice", "Three notices.", key="notices-1")
        if raises:
            raise RuntimeError("the tool failed")
        return "done", False

    monkeypatch.setattr(tooling, "call", tool_call)
    req = _tools_call(meta={"claudecode/toolUseId": ID, "progressToken": 3})
    if raises:
        with pytest.raises(RuntimeError):
            browser_server.handle(req)
    else:
        assert browser_server.handle(req)["result"]["isError"] is False
    assert seen == [ID]
    assert broker.calls[-1][1]["tool_use_id"] == ID
    assert casa_broker.call_id() is None


@pytest.mark.parametrize("meta", [{"claudecode/toolUseId": 5},
                                  {"claudecode/toolUseId": ""},
                                  {"claudecode/toolUseId": "t" * 200},
                                  {"claudecode/toolUseId": None},
                                  {"claudecode/toolUseId": ["toolu_x"]},
                                  {"progressToken": 3},
                                  "toolu_x", 5])
def test_a_bad_id_is_ignored(broker, no_call_id, monkeypatch, meta):
    seen = []

    def tool_call(name, arguments):
        seen.append(casa_broker.call_id())
        casa_broker.deposit_message("notice", "Three notices.", key="notices-1")
        return "done", False

    monkeypatch.setattr(tooling, "call", tool_call)
    assert browser_server.handle(_tools_call(meta=meta))["result"]["isError"] is False
    assert seen == [None]
    assert "tool_use_id" not in broker.calls[-1][1]


def test_a_call_casa_makes_itself_has_no_id(env, no_call_id, monkeypatch):
    """The sweep and the Stop arrive as `tools/call` with no `_meta`, right after a call
    that had one: they run with no id kept, answer as before, and make no request to
    Casa (so none can name a call)."""
    (run,) = admitted_run(env)
    key = state.run_binding(run).request_group
    seen = []
    real_call = tooling.call

    def spy(name, arguments):
        seen.append((name, casa_broker.call_id()))
        return real_call(name, arguments)

    monkeypatch.setattr(tooling, "call", spy)
    env.casa.calls.clear()
    with_id = browser_server.handle(
        _tools_call("browser_status", meta={"claudecode/toolUseId": ID}))
    assert with_id["result"]["isError"] is False
    assert env.casa.calls and all(body["tool_use_id"] == ID for _, body in env.casa.calls)
    env.casa.calls.clear()
    answers = []
    for req in (_tools_call("runs_needing_driver"), _tools_call("stop_runs"),
                _tools_call("stop_runs", ref=f"request:{key}")):
        resp = browser_server.handle(req)
        assert resp["result"]["isError"] is False, resp
        answers.append(json.loads(resp["result"]["content"][0]["text"]))
    assert seen == [("browser_status", ID), ("runs_needing_driver", None),
                    ("stop_runs", None), ("stop_runs", None)]
    assert answers[2]["outcome"] == "stopped"
    assert env.casa.calls == []
    assert casa_broker.call_id() is None


def _bodies(broker, route):
    return [body for path, body in broker.calls if path == route]


def test_only_the_serving_thread_names_the_call(broker, no_call_id):
    """The id belongs to the thread that serves the call: a request another thread posts at
    the same moment is today's request."""
    casa_broker.set_call_id(ID)
    worker = threading.Thread(
        target=lambda: casa_broker.deposit_message("notice", "From elsewhere.", key="other-1"))
    try:
        worker.start()
        worker.join(10)
        assert not worker.is_alive()
        casa_broker.deposit_message("notice", "From the call.", key="served-1")
    finally:
        worker.join(10)
    by_key = {b["key"]: b for b in _bodies(broker, casa_broker.ROUTE_DEPOSIT)}
    assert set(by_key) == {"other-1", "served-1"}
    assert "tool_use_id" not in by_key["other-1"]
    assert by_key["served-1"]["tool_use_id"] == ID


def test_a_thread_left_from_an_earlier_call_does_not_name_a_later_one(broker, no_call_id,
                                                                     monkeypatch):
    """A Stop's worker that outlives its call can reach Casa while a later call is served
    (its client's turn-deadline check queues a continuation): that request names no call,
    while the call being served names its own."""
    go, done = threading.Event(), threading.Event()
    binding = broker.requester["binding_ref"]

    def stale_worker():
        if go.wait(10):
            try:
                casa_broker.request_continuation(binding, "run1", "next_run")
            finally:
                done.set()

    def tool_call(name, arguments):
        casa_broker.deposit_message("notice", "From the call.", key="served-1")
        go.set()
        assert done.wait(10)
        return "done", False

    monkeypatch.setattr(tooling, "call", tool_call)
    worker = threading.Thread(target=stale_worker, daemon=True)
    worker.start()
    try:
        resp = browser_server.handle(_tools_call(meta={"claudecode/toolUseId": ID}))
        assert resp["result"]["isError"] is False
    finally:
        go.set()
        worker.join(10)
    assert not worker.is_alive()
    (cont,) = _bodies(broker, casa_broker.ROUTE_CONTINUE)
    (dep,) = _bodies(broker, casa_broker.ROUTE_DEPOSIT)
    assert "tool_use_id" not in cont
    assert dep["tool_use_id"] == ID

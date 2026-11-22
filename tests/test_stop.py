"""The operator's Stop: `stop_runs`,
the seal, and every
seam that reads it. Casa calls `stop_runs` directly (no model, no session); the seal is written
before any network call, every request id of the request is cancelled in the app, and Fido's
own code never drives, wakes or tells a sealed request again."""
from __future__ import annotations

import json
import time

import pytest

import continuation
import copy_text
import state
import tools_run
from conftest import admitted_run, casa_eval


def _stop(env, key: str) -> dict:
    return json.loads(env.ok("stop_runs", ref=f"request:{key}"))


def _listing(env) -> dict:
    return json.loads(env.ok("stop_runs"))


def _key(run_id: str) -> str:
    return state.run_binding(run_id).request_group


def _cancels(env) -> list[str]:
    """Each request id the app was asked to stop, as `/v1/requests/<id>/cancel`, in the order
    of the ONE call that carries them all (Casa Browser 0.1.8's `POST /v1/requests/cancel`)."""
    return [f"/v1/requests/{rid}/cancel" for c in env.app.calls
            if c.method == "POST" and c.path == "/v1/requests/cancel"
            for rid in (c.body or {}).get("request_ids", [])]


def _stop_posts(env) -> int:
    return sum(1 for c in env.app.calls
               if c.method == "POST" and c.path == "/v1/requests/cancel")


def _owed() -> list[str]:
    con = state._connect()
    try:
        return [r[0] for r in con.execute("SELECT run_id FROM ended_owed")]
    finally:
        con.close()


def _drop_leases() -> None:
    con = state._connect()
    con.execute("DELETE FROM leases")
    con.close()


def _sweep(env) -> list[str]:
    return [r["run_ref"] for r in json.loads(env.ok("runs_needing_driver"))["runs"]]


def _continues(env) -> list:
    return [b for p, b in env.casa.calls if p.endswith("/continue")]


# -- the listing ------------------------------------------------------------------------

def test_listing_names_running_requests_only(env):
    (a,) = admitted_run(env)
    (b,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(a, "running")
    (c,) = admitted_run(env)
    _stop(env, _key(c))                                  # sealed: not listed
    env.casa.admit_mode = "pending"                      # waiting for the operator's Start: not listed
    env.ok("start_runs", runs=[{"mode": "account", "sites": [{"site_id": "s1",
                                                             "access": "account"}],
                                "task_kind": "bill", "task_text": "Pay it.",
                                "result_expectation": ""}])
    got = _listing(env)["requests"]
    assert [r["ref"] for r in got] == [f"request:{_key(a)}", f"request:{_key(b)}"]
    assert got[0] == {"ref": f"request:{_key(a)}", "tasks": ["invoice: Download invoice 0."],
                      "state": "running"}
    assert got[1]["state"] == "queued"


# -- the stop ---------------------------------------------------------------------------

def test_stop_cancels_every_request_id(env):
    env.app.fail_creates = {3}                           # the third run is never created
    env.casa.admit_mode = "admit"
    r1, r2 = (None, None)
    before = set(env.app.runs)
    env.ok("start_runs", runs=[{"mode": "account", "sites": [{"site_id": "s1",
                                                             "access": "account"}],
                                "task_kind": "invoice", "task_text": f"Download invoice {i}.",
                                "result_expectation": ""} for i in range(3)])
    r1, r2 = [r for r in env.app.runs if r not in before]
    env.app.set_state(r1, "running")
    key = _key(r1)
    row = state.admission(key)
    env.app.calls.clear()
    out = _stop(env, key)
    assert _cancels(env) == [f"/v1/requests/{rid}/cancel" for rid in row.request_ids]
    assert len(_cancels(env)) == 3
    assert state.sealed_requests() == {key} and state.stop_settled(key)
    assert _owed() == []
    assert out["outcome"] == "stopped"
    assert out["tasks"] == [f"invoice: Download invoice {i}." for i in range(3)]
    assert out["refs"] == [f"request:{key}", f"admission:{row.confirmation_id}",
                           r1, f"intent:{row.request_ids[0]}", r2,
                           f"intent:{row.request_ids[1]}", f"intent:{row.request_ids[2]}"]
    assert env.app.runs[r1]["state"] == env.app.runs[r2]["state"] == "cancelled"
    assert state.is_terminal(r1) and state.is_terminal(r2)       # the Stop's line tells them
    # the third id was recorded: a create of it now is born cancelled
    assert row.request_ids[2] in env.app.cancelled_requests


# -- classification -----------------------------------------------------------------------

def test_every_run_ended_on_its_own_is_ended_and_unsealed(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "completed")
    _drop_leases()
    assert run in _sweep(env)                            # its ending is owed
    assert _owed() == [run] and state.is_terminal(run)
    out = _stop(env, _key(run))
    assert out == {"outcome": "ended", "tasks": ["invoice: Download invoice 0."]}
    assert state.sealed_requests() == set()
    assert _owed() == [run] and state.is_terminal(run)   # untouched: Fido still tells it
    assert run in _sweep(env)


def test_one_ended_and_one_recorded_id_is_stopped(env):
    env.app.fail_creates = {2}
    before = set(env.app.runs)
    env.ok("start_runs", runs=[{"mode": "account", "sites": [{"site_id": "s1",
                                                             "access": "account"}],
                                "task_kind": "invoice", "task_text": f"Invoice {i}.",
                                "result_expectation": ""} for i in range(2)])
    (r1,) = [r for r in env.app.runs if r not in before]
    env.app.set_state(r1, "completed")
    out = _stop(env, _key(r1))
    assert out["outcome"] == "stopped"
    assert state.sealed_requests() == {_key(r1)}


def test_a_recovered_stop_that_never_won_unseals(env):
    """The first Stop never reached the app; the sweep's retry finds every id
    `stopped: false` (the run had ended on its own), so the seal goes and its ending is told."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "completed")
    _drop_leases()
    env.app.fail_next[("GET", "/v1/hello")] = 1
    assert _stop(env, _key(run))["outcome"] == "not_taken"
    assert state.sealed_requests() == {_key(run)} and not state.stop_settled(_key(run))
    assert run in _sweep(env)                            # unsealed, then owed
    assert state.sealed_requests() == set()
    assert _owed() == [run]


# -- a second stop ---------------------------------------------------------------------------

def test_a_second_stop_is_already_stopped(env):
    (run,) = admitted_run(env)
    assert _stop(env, _key(run))["outcome"] == "stopped"
    out = _stop(env, _key(run))
    assert out["outcome"] == "already_stopped"
    assert out["refs"][0] == f"request:{_key(run)}" and run in out["refs"]


def test_a_stop_not_taken_still_names_its_continuations(env):
    """A Stop that did not reach the app keeps its seal, so Casa is given every
    continuation reference of the request to drop — none of them may start a turn."""
    (run,) = admitted_run(env)
    rid = state.run_binding(run).request_id
    env.app.fail_next[("GET", "/v1/hello")] = 1
    out = _stop(env, _key(run))
    assert out["outcome"] == "not_taken"
    assert out["refs"] == [f"request:{_key(run)}",
                           *([f"admission:{state.admission(_key(run)).confirmation_id}"]
                             if state.admission(_key(run)).confirmation_id else []),
                           run, f"intent:{rid}"]


def test_a_second_stop_after_not_taken_settles(env):
    (run,) = admitted_run(env)
    rid = state.run_binding(run).request_id
    env.app.fail_next[("POST", "/v1/requests/cancel")] = 1
    assert _stop(env, _key(run))["outcome"] == "not_taken"
    assert not state.stop_settled(_key(run))
    assert _stop(env, _key(run))["outcome"] == "already_stopped"
    assert state.stop_settled(_key(run))


# -- the seal comes before any network call -------------------------------------------------

def _record_seal_at_first_call(env, monkeypatch, key):
    seen = []
    real = continuation.app_client.compatible_client

    def spy():
        seen.append(state.request_stopped(key))
        return real()
    monkeypatch.setattr(continuation.app_client, "compatible_client", spy)
    return seen


def test_hello_failing_leaves_the_seal(env, monkeypatch):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    key = _key(run)
    seen = _record_seal_at_first_call(env, monkeypatch, key)
    env.app.fail_next[("GET", "/v1/hello")] = 1
    assert _stop(env, key)["outcome"] == "not_taken"
    assert seen == [True]
    assert not state.stop_settled(key)
    env.app.calls.clear()
    assert env.ok("run_observe", run_id=run) == copy_text.STOPPED
    assert env.app.calls == []


def test_a_failing_stop_call_leaves_the_seal(env, monkeypatch):
    r1, r2 = admitted_run(env, n=2)
    key = _key(r1)
    seen = _record_seal_at_first_call(env, monkeypatch, key)
    env.app.fail_next[("POST", "/v1/requests/cancel")] = 1
    assert _stop(env, key)["outcome"] == "not_taken"
    assert seen == [True] and not state.stop_settled(key)
    env.app.calls.clear()
    for run in (r1, r2):
        assert env.ok("run_observe", run_id=run) == copy_text.STOPPED
    assert env.app.calls == []


# -- every tool taking a run_id ------------------------------------------------------------------

RUN_TOOLS = {
    "run_observe": {}, "run_look": {}, "run_read": {},
    "run_act": {"kind": "click", "handle": "e1", "generation": 1},
    "run_search": {"query": "pizza"}, "run_fetch": {"url": "https://a.example/"},
    "run_wait": {"max_wait_s": 1},
    "run_download": {"handle": "e1"}, "run_archive": {"file_ids": ["file1"]},
    "run_finish": {"summary": "Done.", "file_ids": [], "source_ids": [],
                   "expectation_status": "matched"},
    "run_cancel": {}, "request_login_code": {}, "run_login_code": {"code": "123456"},
    "ask_keep_going": {"done_text": ""}, "show_screenshot": {"caption": "The page"},
    "post_question": {}, "ask_sign_in": {"site_name": "Cloud Co"},
    "request_live_view": {"purpose": "assist", "reason": "Pick the time."},
    "deliver_file": {"file_id": "file1", "caption": ""},
    "handoff_files": {"file_ids": ["file1"]}, "progress": {"text": "Still reading."},
    "show_sources": {"answer": "The answer.", "source_ids": ["src1"],
                     "expectation_status": "matched"},
}


def test_the_run_tools_are_the_twenty_two():
    import browser_server  # noqa: F401
    import tooling
    taking = {n for n, spec in tooling.TOOLS.items()
              if "run_id" in spec["schema"].get("properties", {})}
    assert taking == set(RUN_TOOLS) and len(RUN_TOOLS) == 22


@pytest.mark.parametrize("name", sorted(RUN_TOOLS))
def test_every_run_tool_on_a_sealed_request_is_stopped(env, name):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    state.put_file("file1", run, "a.pdf", "application/pdf", 3)
    _stop(env, _key(run))
    env.app.calls.clear()
    env.casa.calls.clear()
    text, is_error = env.call(name, run_id=run, **RUN_TOOLS[name])
    assert not is_error
    import tooling
    if tooling.TOOLS[name]["slot"]:
        assert json.loads(text) == {tooling.TOOLS[name]["slot"]: None,
                                    "note": copy_text.STOPPED}
    else:
        assert text == copy_text.STOPPED
    assert env.app.calls == []
    assert env.casa.calls == [] and env.casa.deposits == []


# -- a call the Stop cut, and a result on a sealed request ---------------------------------

def _seal_when(env, run: str, suffix: str, *, app_stops: bool = True):
    """The operator's Stop lands while the call ending in `suffix` is in flight: the plugin's seal, and —
    with `app_stops` — the app's own record that it stopped the run's request (only
    that record makes a result STOPPED). Without it the seal is provisional and the app did
    not stop the request (another ending won)."""
    key = _key(run)
    rid = state.run_binding(run).request_id

    def hook(call):
        if call.path.endswith(suffix):
            state.seal_request(key)
            if app_stops:
                env.app.cancelled_requests.add(rid)
    env.app.on_request = hook


def test_an_in_flight_step_cut_by_the_stop_is_stopped(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    _seal_when(env, run, "/action")
    env.app.overrides[("POST", f"/v1/runs/{run}/action")] = (
        409, {"error": "interrupted"}, "application/json")
    env.casa.calls.clear()
    assert env.ok("run_act", run_id=run, kind="click", handle="e1",
                  generation=1) == copy_text.STOPPED
    assert not state.is_terminal(run)          # the Stop's classification keeps its ending
    assert _continues(env) == []


def test_a_successful_step_on_a_sealed_request_is_stopped(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    _seal_when(env, run, "/search")
    assert env.ok("run_search", run_id=run, query="pizza") == copy_text.STOPPED


def test_swallowed_cuts_are_stopped_and_queue_nothing(env, monkeypatch):
    """`run_login_code` from another turn swallows `interrupted` in its
    `finally` (it would queue operator_answer); a replayed `outcome_unknown` verdict is
    accepted by judge.post_verdict. On a sealed request both answer STOPPED and queue
    nothing."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    env.app.code_sites[run] = "s1"
    env.ok("request_login_code", run_id=run)
    hold = state.code_hold(run)
    _seal_when(env, run, "/login-code")
    env.app.overrides[("POST", f"/v1/runs/{run}/login-code")] = (
        409, {"error": "interrupted"}, "application/json")
    env.casa.requester = dict(env.casa.requester, binding_ref="casa-bind-" + "b" * 32)
    env.casa.calls.clear()
    assert env.ok("run_login_code", run_id=run, code="123456",
                  request=hold.token) == copy_text.STOPPED
    assert _continues(env) == []
    # a replayed outcome_unknown: the check's verdict lands while the Stop seals
    (run2,) = admitted_run(env)
    env.casa.requester = dict(env.casa.requester, binding_ref=state.run_binding(
        run2).binding_ref)
    env.app.set_state(run2, "running")
    env.app.judge = True
    env.app.go_results = ["unknown"]
    key2 = _key(run2)

    def on_verdict(call):
        if call.method == "POST" and "/judgements/" in call.path:
            state.seal_request(key2)
            env.app.cancelled_requests.add(state.run_binding(run2).request_id)
    env.app.on_request = on_verdict
    env.casa.calls.clear()
    assert env.ok("run_act", run_id=run2, kind="click", handle="e1",
                  generation=1) == copy_text.STOPPED
    assert any(c.method == "POST" and "/judgements/" in c.path for c in env.app.calls)
    assert _continues(env) == []


def test_a_finish_that_won_is_told_under_a_provisional_seal(env):
    """The finish is in flight, the operator's Stop seals, the finish
    completes — the app records no Stop of the request — then
    the cancel answers `stopped: false`: the finish's result is the run's own ending and is
    told as usual; the Stop answers ended and unseals."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    key = _key(run)
    _seal_when(env, run, "/finish", app_stops=False)
    out = json.loads(env.ok("run_finish", run_id=run, summary="The invoice.", file_ids=[],
                            source_ids=[], expectation_status="matched"))
    assert copy_text.FINISHED.format(status="matched") in out["note"]
    assert copy_text.STOPPED not in out["note"]
    assert state.request_stopped(key)                    # the Stop's seal, still provisional
    env.app.on_request = None
    assert continuation.cancel_request(key, state.admission(key)) == "ended"
    assert state.sealed_requests() == set()
    assert env.app.runs[run]["state"] == "completed"
    assert state.is_terminal(run) and _owed() == []      # told by the finish, once


# -- run_wait ------------------------------------------------------------------------------------

def test_run_wait_reading_cancelled_on_a_sealed_run_is_stopped(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    key = _key(run)

    def hook(call):
        if call.method == "GET" and call.path.endswith("/events"):
            state.seal_request(key)
            env.app.cancelled_requests.add(state.run_binding(run).request_id)
            env.app.runs[run]["state"] = "cancelled"
    env.app.on_request = hook
    assert env.ok("run_wait", run_id=run, max_wait_s=3) == copy_text.STOPPED
    assert _owed() == []                       # the app says the operator's Stop ended it: its line tells it


def test_a_wait_that_heard_the_ending_is_told_under_a_provisional_seal(env):
    """`run_wait` reads an ending the app made (here an
    interruption) and takes over its telling; the operator's Stop seals in that instant; the wait's
    result is the run's own ending and is told as usual, and the Stop then answers ended."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "interrupted")
    key = _key(run)
    real = state.heard_ending

    def heard_then_sealed(run_id):
        real(run_id)
        state.seal_request(key)
    state.heard_ending = heard_then_sealed
    try:
        out = env.ok("run_wait", run_id=run, max_wait_s=1)
    finally:
        state.heard_ending = real
    assert copy_text.RUN_ENDED.format(state="interrupted") in out
    assert copy_text.STOPPED not in out
    assert continuation.cancel_request(key, state.admission(key)) == "ended"
    assert state.sealed_requests() == set()
    assert state.is_terminal(run) and _owed() == []      # told by the wait, once


# -- continue_run ----------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["run", "intent", "request", "admission"])
def test_continue_run_on_a_sealed_request_is_stopped(env, kind):
    (run,) = admitted_run(env)
    key = _key(run)
    row = state.admission(key)
    _stop(env, key)
    ref = {"run": run, "intent": f"intent:{row.request_ids[0]}", "request": f"request:{key}",
           "admission": f"admission:{row.confirmation_id}"}[kind]
    env.app.calls.clear()
    env.casa.calls.clear()
    assert env.ok("continue_run", run_ref=ref) == copy_text.STOPPED
    assert env.app.calls == []
    assert [p for p, _ in env.casa.calls] == []


def test_submit_creates_nothing_more_once_sealed(env):
    """Submit reads the seal before each POST (a Stop landing between two creates)."""
    key_box = []

    def on_create(spec):
        if not key_box:
            key_box.append(1)
            row = next(r for r in state.admitted_requests())
            state.seal_request(row.request_key)
    env.app.on_create = on_create
    text = env.ok("start_runs", runs=[{"mode": "account", "sites": [{"site_id": "s1",
                                                                    "access": "account"}],
                                       "task_kind": "invoice", "task_text": f"Invoice {i}.",
                                       "result_expectation": ""} for i in range(3)])
    assert text == copy_text.STOPPED
    assert len(env.app.posts("/v1/runs")) == 1


def test_nothing_is_queued_for_a_sealed_request(env):
    r1, r2 = admitted_run(env, n=2)
    key = _key(r1)
    row = state.admission(key)
    _stop(env, key)
    env.casa.calls.clear()
    b = state.run_binding(r1).binding_ref
    for ref in (r1, f"intent:{row.request_ids[1]}", f"request:{key}",
                f"admission:{row.confirmation_id}"):
        assert continuation.queue(b, ref, "next_run") is None
    assert state.next_in_group(key) is None
    assert _continues(env) == []


# -- the sweep ------------------------------------------------------------------------------

def test_the_sweep_skips_a_sealed_request(env):
    r1, r2 = admitted_run(env, n=2)
    key = _key(r1)
    env.app.set_state(r1, "running")
    env.app.set_state(r2, "completed")
    state.seal_request(key)
    state.settle_stop(key)
    _drop_leases()
    env.app.calls.clear()
    assert _sweep(env) == []
    assert _owed() == [] and not state.is_terminal(r2)
    assert _cancels(env) == []                            # settled: no retry


def test_an_unsettled_seal_gets_one_cancel_per_id_per_pass(env):
    r1, r2 = admitted_run(env, n=2)
    key = _key(r1)
    env.app.fail_next[("GET", "/v1/hello")] = 1
    assert _stop(env, key)["outcome"] == "not_taken"
    env.app.calls.clear()
    _sweep(env)
    row = state.admission(key)
    assert _cancels(env) == [f"/v1/requests/{rid}/cancel" for rid in row.request_ids]


def test_a_settled_seal_gets_no_more_cancels(env):
    (run,) = admitted_run(env)
    key = _key(run)
    env.app.fail_next[("GET", "/v1/hello")] = 1
    _stop(env, key)
    _sweep(env)
    assert state.stop_settled(key)
    env.app.calls.clear()
    _sweep(env)
    assert _cancels(env) == []


def test_a_sealed_open_admission_is_not_swept(plugin_data):
    state.put_pending_admission("key-open", "casa-bind-" + "a" * 32, "assistant",
                                "operator_resident", [{}], ["rid-1"], ["d"])
    later = time.time() + continuation.STALE_S + 10
    refs = [e["run_ref"] for e in continuation.sweep_answer(now=later)["runs"]]
    assert refs == ["request:key-open"]
    state.seal_request("key-open")
    state.settle_stop("key-open")
    assert continuation.sweep_answer(now=later)["runs"] == []


def test_the_next_pass_settles_a_stop_the_app_missed(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    key = _key(run)
    env.app.fail_next[("GET", "/v1/hello")] = 1
    assert _stop(env, key)["outcome"] == "not_taken"
    assert env.app.runs[run]["state"] == "running"
    _drop_leases()
    assert _sweep(env) == []
    assert state.stop_settled(key) and state.request_stopped(key)
    assert env.app.runs[run]["state"] == "cancelled"
    assert state.is_terminal(run)


# -- in the live view -------------------------------------------------------------------------

def test_a_stop_in_the_live_view_ends_it(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "live")
    out = _stop(env, _key(run))
    assert out["outcome"] == "stopped"
    assert env.app.runs[run]["state"] == "cancelled"
    assert copy_text.LIVE_IS_OPERATORS not in json.dumps(out)


# -- declared ---------------------------------------------------------------------------------------

def test_stop_tool_declared(casa_optional):
    from conftest import ROOT
    pj = json.loads((ROOT / "plugins/browser/.claude-plugin/plugin.json").read_text())
    assert pj["casa"]["stopTool"] == "stop_runs"
    assert pj["casa"]["resultContract"]["tools"]["stop_runs"] == {"result": "safe"}
    assert "mcp__plugin_browser_browser__stop_runs" in pj["casa"]["provides_tools"]
    import browser_server  # noqa: F401
    import tooling
    assert set(tooling.TOOLS["stop_runs"]["schema"]["properties"]) == {"ref"}
    assert tooling.TOOLS["stop_runs"]["schema"]["required"] == []
    if casa_optional is not None:
        got = casa_eval(casa_optional, f"""
import plugin_store
m = json.loads(open({str(ROOT / "plugins/browser/.claude-plugin/plugin.json")!r}).read())
f = getattr(plugin_store, "manifest_stop_tool", None)
print(json.dumps(None if f is None else f(m)))
""")
        assert got in ("stop_runs", None)


def test_an_unknown_request_is_unknown_with_no_call(env):
    env.app.calls.clear()
    assert _stop(env, "no-such-key") == {"outcome": "unknown"}
    assert env.app.calls == [] and state.sealed_requests() == set()


def test_a_sealed_group_has_no_next_run(env):
    """`_queue_next` / `_queue_next_of` read `next_in_group`, None for a sealed group even
    while its runs are not yet known to have ended (the app's cancel still to land)."""
    r1, r2 = admitted_run(env, n=2)
    key = _key(r1)
    assert state.next_in_group(key) == r1
    state.seal_request(key)
    assert state.next_in_group(key) is None


# -- the whole request in one call, and bounded time ---------------------------------------

def test_a_request_is_stopped_in_one_call_with_every_id(env):
    """The whole request goes to the app in ONE call carrying every request id, which
    the app records and cancels at once (no per-id order to race)."""
    runs = admitted_run(env, n=3)
    env.app.set_state(runs[0], "running")
    key = _key(runs[0])
    ids = state.admission(key).request_ids
    env.app.calls.clear()
    assert _stop(env, key)["outcome"] == "stopped"
    assert _stop_posts(env) == 1
    assert _cancels(env) == [f"/v1/requests/{rid}/cancel" for rid in ids]


def test_a_cancel_racing_their_stop_is_told_by_whoever_the_app_says_won(env):
    """Fido's own run_cancel racing the operator's Stop. When the
    app says the Stop stopped the request, the cancel answers STOPPED (the Stop's line tells
    it); when Fido's cancel won and the app recorded no Stop, its own result stands and is
    told — the Stop then classifies `ended`."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    _seal_when(env, run, f"/v1/runs/{run}/cancel")
    assert env.ok("run_cancel", run_id=run) == copy_text.STOPPED
    (run2,) = admitted_run(env)
    env.app.set_state(run2, "running")
    key2 = _key(run2)
    _seal_when(env, run2, f"/v1/runs/{run2}/cancel", app_stops=False)
    out = env.ok("run_cancel", run_id=run2)
    assert copy_text.CANCELLED in out and copy_text.STOPPED not in out
    env.app.on_request = None
    assert continuation.cancel_request(key2, state.admission(key2)) == "ended"
    assert state.is_terminal(run2) and run2 not in _owed()        # told by the cancel, once


def test_a_stop_the_app_answers_too_slowly_is_not_taken_with_its_refs(env, monkeypatch):
    """The stop is bounded by elapsed time, whatever the app's socket does; an app
    too slow to answer still gets `not_taken` with every ref, so Casa drops the request's
    queued continuations."""
    runs = admitted_run(env, n=3)
    key = _key(runs[0])
    monkeypatch.setattr(continuation, "STOP_BUDGET_S", 0.5)

    def slow(call):
        if call.path == "/v1/requests/cancel":
            time.sleep(1.5)
    env.app.on_request = slow
    t0 = time.monotonic()
    out = _stop(env, key)
    assert time.monotonic() - t0 < 1.4
    assert out["outcome"] == "not_taken"
    assert f"request:{key}" in out["refs"] and all(r in out["refs"] for r in runs)
    assert state.request_stopped(key) and not state.stop_settled(key)

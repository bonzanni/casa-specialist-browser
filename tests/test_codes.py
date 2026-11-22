"""Sign-in codes (action model): a site that emailed a code, the run's
request to its requester, the code hold, and the code handed back — in the run's own turn, or
from the requester's next turn with the request's token only."""
from __future__ import annotations

import json
import logging
import re

import pytest

import copy_text
import state
from conftest import admitted_run

BIND_B = "casa-bind-" + "b" * 32
CODE = "48-15Ax"


def _running(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    env.app.code_sites[run] = "s1"
    env.app.add_event(run, "login_needed", outcome="needs_code",
                      text_masked="Utility Co asks for a sign-in code it sent.")
    return run


def _request(env, run) -> str:
    text = env.ok("request_login_code", run_id=run)
    m = re.search(r"request (c[A-Za-z0-9_-]{16}), code <the code>", text)
    assert m, text
    return m.group(1)


def _drop_leases():
    con = state._connect()
    con.execute("DELETE FROM leases")
    con.commit()
    con.close()


def _expire_hold(run):
    con = state._connect()
    con.execute("UPDATE code_holds SET until = 0 WHERE run_id = ?", (run,))
    con.commit()
    con.close()


def _swept(env) -> list[str]:
    return [r["run_ref"] for r in json.loads(env.ok("runs_needing_driver"))["runs"]]


def _other_turn(env):
    """The requester's next delegation: a fresh binding, the same requester."""
    env.casa.requester["binding_ref"] = BIND_B


# -- asking ------------------------------------------------------------------------------

def test_request_names_the_waiting_site_and_holds(env):
    run = _running(env)
    text = env.ok("request_login_code", run_id=run)
    assert "The Utility Co site emailed a sign-in code" in text
    assert "only an email from Utility Co, received in the last few minutes" in text
    assert f"run {run}, request c" in text and "End your turn now" in text
    hold = state.code_hold(run)
    assert hold.site_id == "s1" and not hold.redeemed
    assert (hold.requester_id, hold.requester_kind) == ("assistant", "operator_resident")


def test_request_names_the_site_the_app_reports_not_the_first(env):
    """A run of two sites names the one whose sign-in waits."""
    (run,) = admitted_run(env, sites=[{"site_id": "s1", "access": "account"},
                                      {"site_id": "s2", "access": "account"}])
    env.app.set_state(run, "running")
    env.app.code_sites[run] = "s2"
    text = env.ok("request_login_code", run_id=run)
    assert "Bank Example" in text and "Utility Co" not in text
    assert state.code_hold(run).site_id == "s2"


def test_request_without_a_waiting_site_is_refused(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    assert env.ok("request_login_code", run_id=run) == copy_text.NO_CODE_WAITING
    assert state.code_hold(run) is None


def test_request_only_in_the_runs_own_turn(env):
    run = _running(env)
    _other_turn(env)
    assert copy_text.NOT_YOUR_RUN in env.ok("request_login_code", run_id=run)
    assert state.code_hold(run) is None


# -- the hold ------------------------------------------------------------------------------

def test_a_held_run_is_never_swept_until_the_hold_expires(env):
    run = _running(env)
    _request(env, run)
    _drop_leases()
    assert run not in _swept(env)
    _expire_hold(run)
    assert run in _swept(env)


def test_a_held_run_is_not_swept_for_a_file_either(env, monkeypatch):
    """The file-delivery paths honour the hold too."""
    import continuation
    import time
    run = _running(env)
    fid = env.app.add_file(run, b"%PDF-1.4", "a.pdf", "application/pdf")
    state.put_file(fid, run, "a.pdf", "application/pdf", 8)
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.deliver(ref)
    env.casa.delivery[ref] = {"status": "unknown", "repeat": False}
    env.ok("browser_status")
    _request(env, run)
    _drop_leases()
    later = time.time() + 121
    monkeypatch.setattr(continuation.time, "time", lambda: later)
    assert run not in _swept(env)
    _expire_hold(run)
    assert run in _swept(env)


def test_a_wake_while_held_ends_at_once_and_is_not_an_idle_wake(env):
    run = _running(env)
    _request(env, run)
    _drop_leases()
    for _ in range(3):                          # already-queued wakes, one after another
        import tools_run
        tools_run._WOKEN.clear()
        text = env.ok("continue_run", run_ref=run)
        assert text.startswith(copy_text.CODE_WAITING)
        assert copy_text.CONTINUATION_ENDING in text
    assert env.app.runs[run]["state"] == "running"       # never cancelled
    assert not env.app.posts("/cancel") and state.last_wake(run) is None
    assert not state.lease_live(run, __import__("time").time())


def test_an_expired_request_is_said_once(env):
    run = _running(env)
    _request(env, run)
    _expire_hold(run)
    _drop_leases()
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.CODE_NEVER_CAME in text and "Download invoice 0." in text
    assert state.code_hold(run) is None
    import tools_run
    tools_run._WOKEN.clear()
    assert copy_text.CODE_NEVER_CAME not in env.ok("continue_run", run_ref=run)


# -- the code, in the run's own turn -----------------------------------------------------------

@pytest.mark.parametrize("reply,expected", [
    ({"outcome": "done"}, copy_text.CODE_SIGNED_IN),
    ({"outcome": "login_needed", "reason": "needs_code"}, copy_text.CODE_AGAIN),
    ({"outcome": "login_needed", "reason": "code_page_gone"},
     copy_text.CODE_NOT_SIGNED_IN.format(reason="code page gone"))])
def test_run_login_code_in_the_runs_own_turn(env, reply, expected):
    run = _running(env)
    _request(env, run)
    env.app.code_reply = reply
    assert env.ok("run_login_code", run_id=run, code=CODE) == expected
    (post,) = env.app.posts("/login-code")
    assert post.body == {"code": CODE, "site_id": "s1"}
    assert state.code_hold(run) is None and env.app.codes == [CODE]
    assert env.casa.queue == set()              # this turn drives on


def test_run_login_code_not_waiting(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    env.app.calls.clear()
    assert env.ok("run_login_code", run_id=run, code=CODE) == copy_text.NO_CODE_WAITING
    assert not env.app.posts("/login-code")


# -- the code, from the requester's next turn ---------------------------------------------------

def test_the_requesters_answer_hands_one_code_and_nothing_else(env):
    run = _running(env)
    token = _request(env, run)
    own = state.run_binding(run).binding_ref
    _other_turn(env)
    text = env.ok("run_login_code", run_id=run, code=CODE, request=token)
    assert text == copy_text.CODE_HANDED.format(site="Utility Co")
    assert env.app.codes == [CODE] and state.code_hold(run) is None
    assert (own, run) in env.casa.queue        # the run's own turn carries on
    assert state.run_binding(run).binding_ref == own
    # One request, one code; and nothing else of the run is reachable from this turn.
    env.app.code_sites[run] = "s1"
    assert env.ok("run_login_code", run_id=run, code=CODE, request=token) == \
        copy_text.CODE_REQUEST_UNKNOWN
    for name, args in (("run_observe", {"run_id": run}), ("request_login_code", {"run_id": run}),
                       ("continue_run", {"run_ref": run})):
        assert copy_text.NOT_YOUR_RUN in env.ok(name, **args)
    assert env.app.codes == [CODE]


@pytest.mark.parametrize("case", ["no_request", "wrong_token", "other_requester", "other_kind",
                                  "expired", "no_hold"])
def test_a_code_from_another_turn_needs_the_open_request(env, case):
    run = _running(env)
    token = _request(env, run) if case != "no_hold" else "c" + "A" * 16
    _other_turn(env)
    args = {"run_id": run, "code": CODE, "request": token}
    if case == "no_request":
        del args["request"]
    elif case == "wrong_token":
        args["request"] = "c" + "B" * 16
    elif case == "other_requester":
        env.casa.requester["requester_id"] = "specialist:finance"
    elif case == "other_kind":
        env.casa.requester["requester_kind"] = "specialist"
    elif case == "expired":
        _expire_hold(run)
    env.app.calls.clear()
    text = env.ok("run_login_code", **args)
    assert text in (copy_text.NOT_YOUR_RUN, copy_text.CODE_REQUEST_UNKNOWN)
    assert not env.app.posts("/login-code") and env.app.codes == []
    assert env.casa.queue == set()


@pytest.mark.parametrize("reply,expected", [
    ({"outcome": "login_needed", "reason": "needs_code"},
     copy_text.CODE_HANDED_AGAIN.format(site="Utility Co")),
    ({"outcome": "login_needed", "reason": "rejected"},
     copy_text.CODE_HANDED_FAILED.format(site="Utility Co"))])
def test_the_requesters_answer_reports_the_outcome_in_one_line(env, reply, expected):
    run = _running(env)
    token = _request(env, run)
    env.app.code_reply = reply
    _other_turn(env)
    assert env.ok("run_login_code", run_id=run, code=CODE, request=token) == expected


def test_a_late_answer_after_the_app_stopped_waiting(env):
    run = _running(env)
    token = _request(env, run)
    del env.app.code_sites[run]                  # e.g. a new sign-in replaced the report
    _other_turn(env)
    assert env.ok("run_login_code", run_id=run, code=CODE, request=token) == \
        copy_text.CODE_HANDED_NOT_WAITING
    assert state.code_hold(run) is None and (state.run_binding(run).binding_ref, run) in \
        env.casa.queue


def test_a_redeemed_hold_still_stops_wakes_while_the_code_is_entered(env, monkeypatch):
    """The hold is redeemed before the app call and dropped after it: a sweep in between does
    not wake the run, and a second answer with the token is refused."""
    import app_client
    import time
    run = _running(env)
    token = _request(env, run)
    _other_turn(env)
    _drop_leases()
    seen = {}
    real = app_client.AppClient.request

    def during(self, method, path, body=None, **kw):
        if path.endswith("/login-code"):
            env.casa.requester["binding_ref"] = "casa-bind-" + "a" * 32
            seen["swept"] = _swept(env)
            seen["hold"] = state.code_hold(run)
            seen["again"] = state.redeem_code_hold(run, token, "assistant", "operator_resident",
                                                   time.time())
            env.casa.requester["binding_ref"] = BIND_B
        return real(self, method, path, body, **kw)
    monkeypatch.setattr(app_client.AppClient, "request", during)
    env.ok("run_login_code", run_id=run, code=CODE, request=token)
    assert run not in seen["swept"] and seen["hold"].redeemed
    assert seen["again"] is None                 # a second answer while the first is entered


# -- the code is kept nowhere -------------------------------------------------------------------

def test_the_code_is_in_no_result_state_or_log(env, caplog):
    caplog.set_level(logging.DEBUG)
    run = _running(env)
    token = _request(env, run)
    results = [env.ok("run_login_code", run_id=run, code=CODE)]
    env.app.code_sites[run] = "s1"
    token = _request(env, run)
    _other_turn(env)
    results.append(env.ok("run_login_code", run_id=run, code=CODE, request=token))
    results.append(env.call("run_login_code", run_id=run, code=CODE + "!")[0])   # refused arg
    assert env.app.codes == [CODE, CODE]
    joined = "\n".join(results) + caplog.text + json.dumps(env.casa.calls, default=str)
    assert CODE not in joined
    for path in env.data.rglob("*"):
        if path.is_file():
            assert CODE.encode() not in path.read_bytes(), path


def test_asking_again_keeps_the_open_request(env):
    """A second request while one is open returns the same line (same token, same
    deadline), so the requester's answer to the first still lands."""
    run = _running(env)
    first = _request(env, run)
    until = state.code_hold(run).until
    assert _request(env, run) == first and state.code_hold(run).until == until
    _other_turn(env)
    assert env.ok("run_login_code", run_id=run, code=CODE, request=first) == \
        copy_text.CODE_HANDED.format(site="Utility Co")


def test_an_expired_request_is_replaced_by_a_new_one(env):
    run = _running(env)
    first = _request(env, run)
    _expire_hold(run)
    assert _request(env, run) != first

"""Fido's run tools against the fake app and the fake Casa broker."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import copy_text
import state
from conftest import SERVER, admitted_run

BIND_B = "casa-bind-" + "b" * 32


def _env_json(text: str) -> dict:
    return json.loads(text)


# -- start_runs -----------------------------------------------------------------------

def test_start_runs_admitted(env):
    from tools_run import spec_digest
    env.casa.requester["requester_id"] = "specialist:finance"
    env.casa.requester["requester_kind"] = "specialist"
    text = env.ok("start_runs", runs=[
        {"mode": "account", "sites": [{"site_id": "s1", "access": "account"}],
         "task_kind": "invoice", "task_text": "Get March.", "result_expectation": "PDF"},
        {"mode": "open_web", "sites": [], "task_kind": "compare",
         "task_text": "Compare kettles.", "result_expectation": ""}])
    posts = env.app.posts("/v1/runs")
    assert len(posts) == 2
    for p in posts:
        members = {k: v for k, v in p.body.items() if k != "admission"}
        assert p.body["admission"]["admitted_spec_digest"] == spec_digest(members)
        assert p.body["requester_id"] == "specialist:finance"
        assert p.body["admission"]["origin_kind"] == "operator_telegram"
    first = next(iter(env.app.runs))
    assert f"Drive the first ({first})" in text
    assert state.lease_live(first, time.time())
    # admission got the app's display names
    admission = [b for p, b in env.casa.calls if p.endswith("/run-admission")][0]
    assert admission["runs"][0]["sites"] == [{"site_id": "s1", "access": "account",
                                              "name": "Utility Co"}]


def test_an_open_web_run_starts_at_the_tasks_address(env):
    """The task's exact address goes to the app as `start_url` (a digest member), only
    for an open-web run."""
    from tools_run import spec_digest
    env.ok("start_runs", runs=[{"mode": "open_web", "sites": [], "task_kind": "order",
                                "task_text": "Order a pizza on https://pizza.example.com/forms/post",
                                "result_expectation": "", "start_url":
                                "https://pizza.example.com/forms/post"}])
    (post,) = env.app.posts("/v1/runs")
    assert post.body["start_url"] == "https://pizza.example.com/forms/post"
    members = {k: v for k, v in post.body.items() if k != "admission"}
    assert post.body["admission"]["admitted_spec_digest"] == spec_digest(members)
    env.app.calls.clear()
    text = env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k", "task_text": "t",
                                       "sites": [{"site_id": "s1", "access": "account"}],
                                       "result_expectation": "",
                                       "start_url": "https://example.com/"}])
    assert text == copy_text.START_URL_RULE and env.app.calls == []


def test_intent_before_post(env):
    seen = []
    env.app.on_create = lambda spec: seen.append(state.intent(spec["request_id"]))
    admitted_run(env, n=3)
    assert len(seen) == 3 and all(i is not None for i in seen)
    # every intent of the batch existed before the FIRST post
    first_ids = {i.request_id for i in seen}
    assert len(first_ids) == 3


def test_intent_rows_all_exist_before_first_post(env):
    counts = []
    def hook(spec):
        con = state._connect()
        counts.append(con.execute("SELECT COUNT(*) FROM intents").fetchone()[0])
        con.close()
    env.app.on_create = hook
    admitted_run(env, n=3)
    assert counts == [3, 3, 3]


@pytest.mark.parametrize("mode, sites, sentence", [
    ("account", [{"site_id": "pub", "access": "public"}], copy_text.SITE_RULE_ACCOUNT),
    ("account", [], copy_text.SITE_RULE_ACCOUNT),
    ("open_web", [{"site_id": "s1", "access": "account"}], copy_text.SITE_RULE_OPEN_WEB),
    ("account", [{"site_id": "s1", "access": "account"}, {"site_id": "s1", "access": "public"}],
     copy_text.DUPLICATE_SITE),
])
def test_site_rule_checked_before_admission(env, mode, sites, sentence):
    env.app.calls.clear()
    text = env.ok("start_runs", runs=[{"mode": mode, "sites": sites, "task_kind": "k",
                                       "task_text": "t", "result_expectation": ""}])
    assert text == sentence
    assert env.app.calls == [] and env.casa.calls == []


def test_unknown_site_refused(env):
    text = env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                       "sites": [{"site_id": "nope", "access": "account"}],
                                       "task_text": "t", "result_expectation": ""}])
    assert text == copy_text.NO_SUCH_SITE.format(site_id="nope")
    assert not [p for p, _ in env.casa.calls if p.endswith("run-admission")]


def test_start_runs_confirmation_pending(env):
    env.casa.admit_mode = "pending"
    text = env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                       "sites": [{"site_id": "s1", "access": "account"}],
                                       "task_text": "t", "result_expectation": ""}])
    # the operator's assistant asked, so the card is the whole message
    assert text == f"{copy_text.CONFIRMATION_PENDING} {copy_text.CONFIRMATION_SILENT}"
    assert text.endswith("exactly <silent/> and nothing else, in a first turn too.")
    assert env.app.posts("/v1/runs") == []
    con = state._connect()
    (key,) = con.execute("SELECT request_key FROM admissions").fetchone()
    con.close()
    row = state.admission(key)
    assert row.status == "pending" and row.confirmation_id.startswith("conf-")
    assert [s["request_id"] for s in row.specs] == row.request_ids


def test_start_runs_refused(env):
    env.casa.admit_mode = "refuse"
    text = env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                       "sites": [{"site_id": "s1", "access": "account"}],
                                       "task_text": "t", "result_expectation": ""}])
    assert text == copy_text.ADMISSION_REFUSED
    assert env.app.posts("/v1/runs") == []


def _pending_key(env):
    env.casa.admit_mode = "pending"
    env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                "sites": [{"site_id": "s1", "access": "account"}],
                                "task_text": f"t{i}", "result_expectation": ""}
                               for i in range(2)])
    con = state._connect()
    key, cid = con.execute("SELECT request_key, confirmation_id FROM admissions").fetchone()
    con.close()
    return key, cid


_CONTINUE = r"""
import sys, json
sys.path.insert(0, {server!r})
import browser_server, tooling
print(json.dumps(tooling.call("continue_run", {{"run_ref": {ref!r}}})))
"""


def test_continue_admission_after_restart(env):
    key, cid = _pending_key(env)
    env.casa.settle_confirmation(cid, "admitted")
    procs = [subprocess.Popen([sys.executable, "-c", _CONTINUE.format(
        server=str(SERVER), ref=f"admission:{cid}")], stdout=subprocess.PIPE, text=True,
        env=dict(os.environ)) for _ in range(2)]
    outs = [json.loads(p.communicate(timeout=60)[0].strip().splitlines()[-1]) for p in procs]
    assert len(env.app.posts("/v1/runs")) == 2          # each run posted exactly once
    row = state.admission(key)
    assert row.status == "submitted"
    posted = [p.body["request_id"] for p in env.app.posts("/v1/runs")]
    assert sorted(posted) == sorted(row.request_ids)
    texts = [o[0] for o in outs]
    assert sum("Started 2 run(s)" in t for t in texts) == 1
    assert all(t.endswith(copy_text.CONTINUATION_ENDING) for t in texts)


def test_continue_admission_declined(env):
    key, cid = _pending_key(env)
    env.casa.settle_confirmation(cid, "declined")
    text = env.ok("continue_run", run_ref=f"admission:{cid}")
    assert copy_text.ADMISSION_DECLINED in text
    assert state.admission(key).status == "declined"
    assert env.app.posts("/v1/runs") == []


def test_continue_admission_still_pending(env):
    _, cid = _pending_key(env)
    text = env.ok("continue_run", run_ref=f"admission:{cid}")
    assert copy_text.ADMISSION_STILL_PENDING in text


def test_direct_admission_finished(env):
    runs = admitted_run(env, n=2)
    con = state._connect()
    (status,) = con.execute("SELECT status FROM admissions").fetchone()
    con.close()
    assert status == "submitted" and len(runs) == 2


def test_start_runs_retry_same_request_id(env):
    """The first POST fails; a later continuation resubmits with the SAME
    request id (the app deduplicates), and no run is created twice."""
    env.app.fail_next[("POST", "/v1/runs")] = 1
    text = env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                       "sites": [{"site_id": "s1", "access": "account"}],
                                       "task_text": "t", "result_expectation": ""}])
    assert text == copy_text.SUBMIT_PARTIAL.format(done=0, total=1)
    con = state._connect()
    (key,) = con.execute("SELECT request_key FROM admissions").fetchone()
    con.execute("UPDATE admissions SET claimed_at = claimed_at - 200")
    con.close()
    text = env.ok("continue_run", run_ref=f"request:{key}")
    assert "Started 1 run(s)" in text
    ids = [p.body["request_id"] for p in env.app.posts("/v1/runs")]
    assert len(ids) == 2 and ids[0] == ids[1]
    assert len(env.app.runs) == 1


def test_partial_submission(env):
    """Casa admits three; the second POST fails: all three intents exist, the
    first is attached, and `intent:<second>` later posts it once with its
    original request id."""
    env.app.fail_creates = {2}
    text = env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                       "sites": [{"site_id": "s1", "access": "account"}],
                                       "task_text": f"t{i}", "result_expectation": ""}
                                      for i in range(3)])
    assert text == copy_text.SUBMIT_PARTIAL.format(done=1, total=3)
    con = state._connect()
    rows = con.execute("SELECT request_id, run_id FROM intents ORDER BY position").fetchall()
    con.close()
    assert len(rows) == 3 and rows[0][1] is not None and rows[1][1] is None
    second = rows[1][0]
    before = len(env.app.posts("/v1/runs"))
    out = env.ok("continue_run", run_ref=f"intent:{second}")
    assert out.startswith("Run run")
    after = env.app.posts("/v1/runs")[before:]
    assert [p.body["request_id"] for p in after] == [second]
    assert state.intent(second).run_id is not None


# -- continue_run ----------------------------------------------------------------------

def test_continue_run_carries_the_silence_marker(env, casa_optional):
    (run,) = admitted_run(env)
    for ref in (run, "nope:1", "admission:conf-" + "0" * 32, "intent:missing", "x" * 80):
        text = env.ok("continue_run", run_ref=ref)
        assert text.endswith(copy_text.CONTINUATION_ENDING)
    assert "<silent/>" in copy_text.CONTINUATION_ENDING
    if casa_optional is not None:
        from conftest import casa_eval
        got = casa_eval(casa_optional, "import output_boundary\n"
                        "print(json.dumps(output_boundary.SILENCE_SENTINEL))")
        assert got in copy_text.CONTINUATION_ENDING


def test_continue_terminal_run(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "completed")
    state._connect().execute("DELETE FROM leases").connection.close()
    env.app.calls.clear()
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.ALREADY_FINISHED.format(state="completed") in text
    assert not state.lease_live(run, time.time())
    assert all(c.method == "GET" for c in env.app.calls)


def test_continue_run_shows_task_and_events(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    env.app.add_event(run, "action", handle="h9", origin="https://bank.example",
                      text_masked="Payment form send")
    text = env.ok("continue_run", run_ref=run)
    assert "Download invoice 0." in text and "handle h9" in text


def test_binding_enforced(env):
    (run,) = admitted_run(env)
    env.casa.requester["binding_ref"] = BIND_B
    env.app.calls.clear()
    for name, args in (("run_observe", {"run_id": run}),
                       ("run_act", {"run_id": run, "kind": "click", "handle": "e1",
                                    "generation": 1}),
                       ("run_wait", {"run_id": run, "max_wait_s": 1}),
                       ("continue_run", {"run_ref": run})):
        text = env.ok(name, **args)
        assert copy_text.NOT_YOUR_RUN in text
    env.casa.requester["requester_kind"] = "specialist"     # a specialist's call cancels none
    assert copy_text.NOT_YOUR_RUN in env.ok("run_cancel", run_id=run)
    assert env.app.calls == []


@pytest.mark.parametrize("st", ["running", "waiting_approval", "waiting_live"])
def test_their_request_cancels_any_of_their_runs(env, st):
    """Asked from a new conversation to stop a sign-in waiting on a
    live view they never opened, the operator is not told the run belongs to another request. Their
    own request may cancel any of their runs, a waiting view's included; only one they are in waits
    for their Done."""
    (run,) = admitted_run(env)
    env.app.set_state(run, st)
    env.casa.requester["binding_ref"] = BIND_B
    assert env.ok("run_cancel", run_id=run).startswith(copy_text.CANCELLED)
    assert env.app.runs[run]["state"] == "cancelled" and len(env.app.posts("/cancel")) == 1


def test_unknown_run_refused(env):
    env.app.calls.clear()
    assert env.ok("run_observe", run_id="ghost") == copy_text.UNKNOWN_RUN
    assert env.app.calls == []


@pytest.mark.parametrize("tool, args", [
    ("run_observe", {}), ("run_search", {"query": "x"}),
    ("run_fetch", {"url": "https://a.example/"}),
    ("run_act", {"kind": "click", "handle": "e1", "generation": 1}),
    ("run_wait", {"max_wait_s": 1}), ("run_download", {"handle": "e1"}),
    ("run_cancel", {}), ("run_finish", {
        "summary": "s", "file_ids": [], "source_ids": [], "expectation_status": "matched"}),
])
def test_no_run_call_after_failed_hello(env, tool, args):
    (run,) = admitted_run(env)
    for patch in ({"protocol_version": 2}, {"configured": False}):
        env.app.hello.update(patch)
        env.app.calls.clear()
        env.ok(tool, run_id=run, **args)
        assert env.app.paths() == ["/v1/hello"], (tool, env.app.paths())
        env.app.hello.update(protocol_version=1, configured=True)


def test_observation_render_has_no_raw_targets(env):
    (run,) = admitted_run(env)
    env.app.observation = {"page_handle": "p7", "generation": 3, "text": "Invoices",
                           "elements": [{"handle": "e5", "role": "button",
                                         "label_masked": "Download",
                                         "href": "https://raw.invalid/path?q=1",
                                         "selector": "#dl"}],
                           "source_id": "src1", "url": "https://raw.invalid/x"}
    text = env.ok("run_observe", run_id=run)
    assert text.splitlines()[0] == "page p7 generation 3 source src1"
    assert '[e5] button "Download"' in text
    assert "raw.invalid" not in text and "#dl" not in text


def test_turn_bound_any_tool(env, monkeypatch):
    import continuation
    (run,) = admitted_run(env)
    monkeypatch.setattr(continuation, "clock",
                        lambda: continuation._STARTED + continuation.TURN_BOUND_S + 1)
    env.app.calls.clear()
    env.casa.calls.clear()
    for name, args in (("run_observe", {}), ("run_act", {"kind": "click", "handle": "e1",
                                                         "generation": 1}),
                       ("run_wait", {"max_wait_s": 5}), ("run_search", {"query": "q"}),
                       ("run_download", {"handle": "e1"})):
        assert env.ok(name, run_id=run, **args) == copy_text.END_TURN_BOUND
    # no app call; its one call to Casa queues the next turn, once
    assert env.app.calls == []
    assert [(b["run_ref"], b["reason"]) for _, b in env.casa.calls] == [(run, "sweep")]


def test_the_turn_bound_queues_only_a_run_still_going(env, monkeypatch):
    """At the turn bound the run is queued for the next turn rather than left for the sweep's
    minute. A run that
    ended, or waits for a sign-in code, or whose answer the operator has, needs no turn; a queue Casa
    refuses leaves the run to the sweep, and the turn still ends."""
    import continuation
    late = lambda: continuation._STARTED + continuation.TURN_BOUND_S + 1  # noqa: E731
    continues = lambda: [c for c in env.casa.calls if c[0].endswith("/continue")]  # noqa: E731
    (run,) = admitted_run(env)
    monkeypatch.setattr(continuation, "clock", late)
    env.casa.calls.clear()

    state.mark_terminal(run)
    assert env.ok("run_observe", run_id=run) == copy_text.END_TURN_BOUND
    assert continues() == []

    monkeypatch.setattr(continuation, "clock", time.monotonic)
    (run,) = admitted_run(env)
    state.put_code_hold(run, "s1", "ctoken", "req", "operator_resident", time.time())
    monkeypatch.setattr(continuation, "clock", late)
    assert env.ok("run_observe", run_id=run) == copy_text.END_TURN_BOUND
    assert continues() == []

    monkeypatch.setattr(continuation, "clock", time.monotonic)
    (run,) = admitted_run(env)
    monkeypatch.setattr(continuation, "clock", late)
    monkeypatch.setattr(continuation.casa_broker, "request_continuation",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("no")))
    assert env.ok("run_observe", run_id=run) == copy_text.END_TURN_BOUND


def test_a_call_cut_by_the_bound_inside_the_client_queues_too(env, monkeypatch):
    import app_client
    import continuation
    (run,) = admitted_run(env)
    client = app_client.compatible_client()
    env.casa.calls.clear()
    monkeypatch.setattr(continuation, "clock",
                        lambda: continuation._STARTED + continuation.TURN_BOUND_S + 1)
    with pytest.raises(app_client.AppError):
        client.request("GET", f"/v1/runs/{run}")
    assert [b["run_ref"] for p, b in env.casa.calls if p.endswith("/continue")] == [run]


_DRIVING = (("run_observe", {}), ("run_look", {}), ("run_read", {}),
            ("run_act", {"kind": "click", "handle": "e1", "generation": 1}),
            ("run_wait", {"max_wait_s": 1}), ("run_search", {"query": "q"}),
            ("run_fetch", {"url": "https://a.example/"}),
            ("run_download", {"handle": "e1"}))


def test_step_bound_ends_driving_but_not_finishing(env):
    """A turn cut by the role's max_turns loses its answer and leaves its run going, so the
    plugin stops the driving first. Past STEP_BOUND calls of
    the turn, the driving tools stop with the wrap-up sentence and reach the app no more, while
    finishing and the sources still run."""
    import continuation
    import tooling
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.sources[run] = [{"source_id": "src0", "url": "https://a.example/1",
                             "title_masked": "One"}]
    left = continuation.STEP_BOUND - tooling.calls_made
    texts = [env.call("run_observe", run_id=run)[0] for _ in range(left)]
    assert copy_text.END_TURN_STEPS not in texts
    assert tooling.calls_made == continuation.STEP_BOUND
    assert len([c for c in env.app.calls if c.path == f"/v1/runs/{run}/observe"]) == left
    env.app.calls.clear()
    for name, args in _DRIVING:
        assert env.ok(name, run_id=run, **args) == copy_text.END_TURN_STEPS, name
    assert env.app.calls == []
    out = json.loads(env.ok("show_sources", run_id=run, answer="One page says one thing.",
                            source_ids=["src0"], expectation_status="mismatch"))
    assert out["sources"]                   # v0.1.27: the same call finishes the run
    assert env.app.runs[run]["state"] == "completed"
    assert env.app.runs[run]["finish"]["expectation_status"] == "mismatch"


def test_step_bound_counts_every_call_of_the_turn(env):
    """A download followed by its delivery is two calls, not one. Every call takes a model
    turn, so every call counts: download and
    delivery pairs reach the bound, and the next download is refused; the last file still goes
    out and the run still finishes."""
    import continuation
    import tooling
    (run,) = admitted_run(env)
    env.app.observation["elements"] = [{"handle": "e1", "role": "link", "label_masked": "PDF"}]
    env.ok("run_observe", run_id=run)
    delivered = 0
    for _ in range(continuation.STEP_BOUND):      # a bound that never comes fails, not hangs
        got = env.ok("run_download", run_id=run, handle="e1")
        if got == copy_text.END_TURN_STEPS:
            break
        fid = got.rsplit(" as file ", 1)[1].rstrip(".")
        json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption="A file"))
        delivered += 1
    # start_runs and the observation, then a pair per file; the next download is past the bound
    assert delivered == (continuation.STEP_BOUND - 2) // 2 == 199
    assert tooling.calls_made == continuation.STEP_BOUND + 1
    assert len([d for d in env.casa.deposits if d["slot"] == "file"]) == delivered
    env.ok("run_finish", run_id=run, summary="Files.", file_ids=[], source_ids=[],
           expectation_status="mismatch")


def test_a_refused_argument_counts_too(env):
    """A call refused for its arguments took a model turn as well."""
    import continuation
    import tooling
    (run,) = admitted_run(env)
    tooling.calls_made = continuation.STEP_BOUND - 1
    text, is_error = env.call("run_wait", run_id=run, max_wait_s=0)
    assert is_error and tooling.calls_made == continuation.STEP_BOUND
    assert env.ok("run_search", run_id=run, query="q") == copy_text.END_TURN_STEPS


def test_the_turn_bound_comes_before_the_step_bound(env, monkeypatch):
    """Past both bounds the turn ends (no finishing at the turn bound)."""
    import continuation
    import tooling
    (run,) = admitted_run(env)
    monkeypatch.setattr(tooling, "calls_made", continuation.STEP_BOUND + 5)
    monkeypatch.setattr(continuation, "clock",
                        lambda: continuation._STARTED + continuation.TURN_BOUND_S + 1)
    assert env.ok("run_observe", run_id=run) == copy_text.END_TURN_BOUND


def test_step_bound_leaves_turns_for_the_answer():
    """The bound sits well below the role's max_turns: the wrap-up (run_finish, sources, a
    file, the reply) and the tool searches of the turn fit in what is left."""
    import continuation
    import yaml
    role = yaml.safe_load((Path(__file__).resolve().parent.parent / "role" / "role.yaml")
                          .read_text("utf-8"))
    assert role["tools"]["max_turns"] - continuation.STEP_BOUND >= 15
    assert continuation.STEP_BOUND >= 40          # a page-by-page reading still fits a turn


def test_the_wrap_up_no_longer_says_answer_with_what_you_have():
    """A turn's end loses nothing now: the one conversation carries on, so the wrap-up never
    tells the turn to cut the job short."""
    assert "answer with what you have" not in copy_text.END_TURN_STEPS
    assert "End your turn" in copy_text.END_TURN_STEPS


def test_a_run_past_the_loop_bound_is_told_to_finish(env):
    """The count is the run's, kept in state, so it crosses turns (a fresh process has a fresh
    `calls_made` and the same count). Past 2,000 calls driving is refused before any app call;
    finishing and a progress line still run."""
    import continuation
    import tooling
    (run,) = admitted_run(env)
    with state._tx() as con:
        con.execute("INSERT INTO run_calls (run_id, n) VALUES (?, ?)",
                    (run, continuation.LOOP_BOUND - 1))
    tooling.calls_made = 0                        # as in a fresh turn
    assert env.ok("run_observe", run_id=run) != copy_text.FINISH_NOW     # the 2,000th
    assert state.count_call(run) == continuation.LOOP_BOUND + 1          # this one is the 2,001st
    env.app.calls.clear()
    for name, args in _DRIVING:
        assert env.ok(name, run_id=run, **args) == copy_text.FINISH_NOW, name
    assert env.app.calls == []
    assert copy_text.FINISH_NOW == ("This run has used 2,000 tool calls. Finish it now with "
                                    "run_finish: say what is done and what is left.")
    out = env.ok("progress", run_id=run, text="Still working.")
    assert copy_text.FINISH_NOW not in out
    env.ok("run_finish", run_id=run, summary="Half done.", file_ids=[], source_ids=[],
           expectation_status="mismatch")
    assert env.app.runs[run]["state"] == "completed"


def test_a_call_with_no_run_leaves_the_count_alone(env):
    import tooling
    (run,) = admitted_run(env)
    env.ok("run_observe", run_id=run)
    before = state.count_call(run)
    env.ok("browser_status")
    assert tooling.run_calls == 0
    assert state.count_call(run) == before + 1


# -- the picture beside the packet ---------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
PNG64 = base64.b64encode(PNG).decode("ascii")


def test_relay_sends_the_apps_picture_with_the_packet(env):
    """The packet is untouched and the picture travels beside it, on both of the check's attempts."""
    run = _judged(env, judgement_picture=PNG)
    env.casa.judge_script = ["check_unavailable", GO]
    assert _click(env, run).startswith("Done")
    first, second = env.casa.judgements()
    assert first["picture"] == second["picture"] == PNG64
    assert first["packet"] == second["packet"]
    assert "picture" not in json.loads(first["packet"])


@pytest.mark.parametrize("got", [None, "internal", b"GIF89a not a png"])
def test_no_picture_or_a_failed_fetch_relays_as_text(env, got):
    run = _judged(env, judgement_picture=got)
    env.casa.judge_script = [GO]
    assert _click(env, run).startswith("Done")
    (asked,) = env.casa.judgements()
    assert "picture" not in asked


def test_a_rejudge_fetches_no_picture(env):
    """Only kind `action` carries one: no call to the app at all for any other kind."""
    import judge
    for kind in ("rejudge", "sign_in", "login_item", "signin"):
        assert judge._picture(None, "r1", {"id": "j1", "kind": kind, "packet": "{}"}) is None


def test_an_app_started_judgement_fetches_no_picture(env):
    (run,) = admitted_run(env)
    env.app.add_app_judgement(run, "login_item", {"kind": "login_item", "host": "a.example",
                                                  "candidates": []})
    env.app.calls.clear()
    env.ok("run_observe", run_id=run)
    assert not [c for c in env.app.calls if c.path.endswith("/picture")]


# -- run_look --------------------------------------------------------------------------------------

def _rpc_call(name, **args):
    import browser_server
    return browser_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})["result"]


def test_run_look_answers_an_image_block(env):
    (run,) = admitted_run(env)
    env.app.look_picture = PNG64
    got = _rpc_call("run_look", run_id=run, handle="e1")
    assert got["isError"] is False
    assert got["content"] == [{"type": "text", "text": copy_text.LOOK_SHOWN},
                              {"type": "image", "data": PNG64, "mimeType": "image/png"}]
    assert env.app.look_bodies == [{"handle": "e1"}]
    # the next call carries no picture of its own
    assert [b["type"] for b in _rpc_call("run_observe", run_id=run)["content"]] == ["text"]


def test_run_look_says_when_there_is_no_picture(env):
    (run,) = admitted_run(env)
    got = _rpc_call("run_look", run_id=run)
    assert got["content"] == [{"type": "text", "text": copy_text.LOOK_NONE}]
    assert env.app.look_bodies == [{}]


def test_a_picture_that_is_not_a_png_never_reaches_the_conversation(env):
    """A broken image block would make the API refuse every later turn of the conversation."""
    (run,) = admitted_run(env)
    for bad in ("not base64!", base64.b64encode(b"GIF89a").decode("ascii"), 5):
        env.app.look_picture = bad
        got = _rpc_call("run_look", run_id=run)
        assert [b["type"] for b in got["content"]] == ["text"]
        assert copy_text.UNEXPECTED in got["content"][0]["text"]


def test_a_stop_during_a_look_returns_no_picture(env, monkeypatch):
    import tooling
    (run,) = admitted_run(env)
    env.app.look_picture = PNG64
    monkeypatch.setattr(tooling, "_stopped", lambda arguments: True)
    got = _rpc_call("run_look", run_id=run)
    assert got["content"] == [{"type": "text", "text": copy_text.STOPPED}]


def test_a_turn_of_only_looks_and_reads_is_not_a_stall(env):
    """A turn in which nothing happens on a run ends it; reading and looking are something."""
    (run,) = admitted_run(env)
    since = time.time() - 1
    env.ok("run_look", run_id=run)
    assert state.touched_since(run, since)
    with state._tx() as con:
        con.execute("DELETE FROM touches")
    env.ok("run_read", run_id=run)
    assert state.touched_since(run, since)


def test_past_the_turn_bound_the_new_tools_end_the_turn(env, monkeypatch):
    import continuation
    (run,) = admitted_run(env)
    monkeypatch.setattr(continuation, "clock",
                        lambda: continuation._STARTED + continuation.TURN_BOUND_S + 1)
    env.app.calls.clear()
    for name in ("run_look", "run_read"):
        assert env.ok(name, run_id=run) == copy_text.END_TURN_BOUND
    assert env.app.calls == []


# -- run_fetch --------------------------------------------------------------------------------------

_FILE = {"file_id": "f1", "run_id": None, "requester_id": "r", "safe_name": "a.pdf",
         "mime": "application/pdf", "size": 9, "sha256": "0" * 64, "source_handle": None}


def test_run_fetch_gives_the_window_its_file_and_source_and_keeps_the_file(env):
    import state
    (run,) = admitted_run(env)
    env.app.read_source = "x" * 45_000
    lines = env.ok("run_fetch", run_id=run, url="https://a.example/p").split("\n")
    file_id = lines[0].split()[1]
    assert lines[0] == (f"file {file_id} (text/html, 45000 bytes) from "
                        "https://a.example/answered source src1")
    assert lines[1] == "x" * 30_000
    assert lines[2] == "[offset 0, next_offset 30000, complete false, masked false]"
    assert lines[3] == copy_text.FETCH_MORE.format(file_id=file_id, next_offset=30000)
    assert state.file_run(file_id) == run
    assert env.app.fetch_bodies == [{"url": "https://a.example/p"}]


def test_run_fetch_passes_fields_and_needs_no_page(env):
    (run,) = admitted_run(env)
    env.app.read_source = '{"a": 1}'
    text = env.ok("run_fetch", run_id=run, url="https://a.example/j", fields=["a", "b.c"])
    assert env.app.fetch_bodies == [{"url": "https://a.example/j", "fields": ["a", "b.c"]}]
    assert text.splitlines()[1] == '{"a": 1}'
    assert text.splitlines()[-1] == copy_text.READ_WHOLE
    assert not env.app.posts("/observe") and not env.app.posts("/search")


def test_run_fetch_says_when_fields_are_cut_or_do_not_apply(env):
    (run,) = admitted_run(env)
    rec = dict(_FILE, run_id=run, mime="application/json")
    env.app.fetch_reply = {"outcome": "done", "generation": 3, "file": rec,
                           "url": "https://a.example/j", "text": "{",
                           "next_offset": None, "complete": False, "masked": False}
    text = env.ok("run_fetch", run_id=run, url="https://a.example/j", fields=["a"])
    assert text.splitlines()[-1] == copy_text.FETCH_FIELDS_CUT
    env.app.fetch_reply = dict(env.app.fetch_reply, not_json=True, text="plain words", complete=True)
    text = env.ok("run_fetch", run_id=run, url="https://a.example/j", fields=["a"])
    assert copy_text.FETCH_NOT_JSON in text.splitlines() and "plain words" in text


def test_run_fetch_of_what_is_not_text_gives_its_id_and_type(env):
    import state
    (run,) = admitted_run(env)
    env.app.fetch_reply = {"outcome": "done", "generation": 3, "file": dict(_FILE, run_id=run),
                           "url": "https://a.example/a.pdf", "text": None, "next_offset": None, "complete": False, "masked": False}
    text = env.ok("run_fetch", run_id=run, url="https://a.example/a.pdf")
    assert text == "\n".join(["file f1 (application/pdf, 9 bytes) from https://a.example/a.pdf",
                              copy_text.FETCH_NOT_TEXT])
    assert state.file_run("f1") == run


@pytest.mark.parametrize("reason", sorted(copy_text.FETCH_NOT_RUN))
def test_a_fetch_not_run_says_what_to_do(env, reason):
    (run,) = admitted_run(env)
    env.app.fetch_reply = {"outcome": "not_run", "generation": 3, "reason": reason}
    assert env.ok("run_fetch", run_id=run, url="https://a.example/p") == (
        copy_text.FETCH_NOT_RUN[reason])


@pytest.mark.parametrize("status", [403, 404, 503, 200])
def test_a_status_says_what_the_site_answered_and_what_to_do(env, status):
    (run,) = admitted_run(env)
    env.app.fetch_reply = {"outcome": "not_run", "generation": 3, "reason": f"http_{status}"}
    text = env.ok("run_fetch", run_id=run, url="https://a.example/p")
    assert text == copy_text.FETCH_HTTP.format(status=status)
    assert f"answered {status} " in text and "run_search with exactly that address" in text
    assert "search for the page" in text


def test_a_fetch_reply_for_another_run_is_refused(env):
    (run,) = admitted_run(env)
    env.app.fetch_reply = {"outcome": "done", "generation": 3, "file": dict(_FILE, run_id="other"),
                           "url": "https://a.example/p", "text": None, "next_offset": None, "complete": False, "masked": False}
    assert "(bad_response)" in env.ok("run_fetch", run_id=run, url="https://a.example/p")


# -- run_read and long results -----------------------------------------------------------------------

def _read_status(text):
    return text.splitlines()[-2:]


def test_run_read_prints_offset_next_offset_complete_masked(env):
    (run,) = admitted_run(env)
    env.app.read_source, env.app.read_masked = "x" * 45_000, True
    first = env.ok("run_read", run_id=run)
    assert first.startswith("x" * 30_000 + "\n")
    assert _read_status(first) == [
        "[offset 0, next_offset 30000, complete false, masked true]",
        copy_text.READ_MORE.format(next_offset=30000)]
    second = env.ok("run_read", run_id=run, offset=30000)
    assert _read_status(second) == ["[offset 30000, next_offset None, complete true, masked true]",
                                    copy_text.READ_WHOLE]
    assert env.app.read_bodies == [{}, {"offset": 30000}]


@pytest.mark.parametrize("size", [20_000, 60_000, 400_000])
def test_read_windows_join_to_the_whole(env, size):
    """Whatever the length, the windows join to the whole text."""
    (run,) = admitted_run(env)
    env.app.read_source = "".join(f"line {i}\n" for i in range(size))[:size]
    got, offset = [], None
    while True:
        args = {} if offset is None else {"offset": offset}
        lines = env.ok("run_read", run_id=run, **args).split("\n")
        got.append("\n".join(lines[:-2]))
        if lines[-1] == copy_text.READ_WHOLE:
            break
        offset = int(lines[-1].rsplit(" ", 1)[1].rstrip("."))
    assert "".join(got) == env.app.read_source


def test_incomplete_read_is_said_plainly(env):
    """A cut never reads as complete: the last window of a page past what the app can read says
    the page may hold more, and never that he has read it all."""
    (run,) = admitted_run(env)
    env.app.read_source, env.app.read_whole = "short", False
    text = env.ok("run_read", run_id=run)
    assert _read_status(text) == ["[offset 0, next_offset None, complete false, masked false]",
                                  copy_text.READ_PARTIAL]
    assert copy_text.READ_WHOLE not in text


def test_run_read_refuses_handle_and_file_together(env):
    (run,) = admitted_run(env)
    env.app.calls.clear()
    assert env.ok("run_read", run_id=run, handle="e1", file_id="f1") == copy_text.READ_ONE_THING
    assert env.app.read_bodies == []
    assert not [c for c in env.app.calls if c.path.endswith("/read")]


def test_run_read_passes_a_handle_or_a_file(env):
    (run,) = admitted_run(env)
    env.ok("run_read", run_id=run, handle="e1")
    env.ok("run_read", run_id=run, file_id="f1", offset=7)
    assert env.app.read_bodies == [{"handle": "e1"}, {"file_id": "f1", "offset": 7}]


def test_a_result_over_48000_is_cut_with_a_line_naming_run_read(env, monkeypatch):
    import tooling
    tool = {"description": "", "schema": {"type": "object", "properties": {}},
            "fn": lambda a: "z" * (tooling.MAX_RESULT_CHARS + 1), "slot": None, "ending": None,
            "receipt": False}
    monkeypatch.setitem(tooling.TOOLS, "t_over", tool)
    text = tooling.call("t_over", {})[0]
    assert len(text) == tooling.MAX_RESULT_CHARS and text.endswith(copy_text.RESULT_CUT)
    assert "run_read" in copy_text.RESULT_CUT
    # one at the limit is whole
    tool = dict(tool, fn=lambda a: "z" * tooling.MAX_RESULT_CHARS)
    monkeypatch.setitem(tooling.TOOLS, "t_whole", tool)
    assert tooling.call("t_whole", {})[0] == "z" * tooling.MAX_RESULT_CHARS


def test_a_cut_keeps_the_notes_and_the_envelope(monkeypatch):
    """A delivering tool still answers its envelope, and a note code wanted is never cut off."""
    import tooling

    def long(args, delivery):
        tooling.note("A note code wanted.")
        return "y" * 60_000
    monkeypatch.setitem(tooling.TOOLS, "t_long", {
        "description": "", "schema": {"type": "object", "properties": {}}, "fn": long,
        "slot": "notice", "ending": None, "receipt": False})
    out = json.loads(tooling.call("t_long", {})[0])
    assert out["notice"] is None and out["note"].endswith(
        copy_text.RESULT_CUT + "\nA note code wanted.")


def test_an_observation_cut_at_20000_says_so(env):
    (run,) = admitted_run(env)
    assert copy_text.TEXT_CUT not in env.ok("run_observe", run_id=run)
    env.app.observation["text_cut"] = True
    lines = env.ok("run_observe", run_id=run).split("\n")
    assert copy_text.TEXT_CUT in lines and "run_read" in copy_text.TEXT_CUT
    assert lines.index(copy_text.TEXT_CUT) < lines.index('[e1] link "Invoices"')


# -- a page reply is short on any site -----------------------------------------------------------

def _long_page(env):
    """A long page's shape: lines of text, and fields, dropdowns, links and page-made controls."""
    (run,) = admitted_run(env)
    els = [{"handle": f"n{i}", "role": "link", "label_masked": f"Nav {i}"} for i in range(100)]
    els += [{"handle": "pick", "role": "combobox", "label_masked": "Size", "value_masked": "S"}]
    els += [{"handle": f"o{i}", "role": "option", "label_masked": f"Size {i}"} for i in range(30)]
    els += [{"handle": f"c{i}", "role": "clickable", "label_masked": f"Tile {i}"}
            for i in range(100)]
    els += [{"handle": f"r{i}", "role": "link", "label_masked": f"Result {i}"} for i in range(100)]
    els += [{"handle": "go", "role": "button", "label_masked": "Send it"}]
    env.app.observation.update(text="\n".join(f"Line {i} of the page" for i in range(2000)),
                               elements=els)
    return run, els


def test_a_long_page_reply_is_short_and_says_how_to_get_the_rest(env):
    run, els = _long_page(env)
    text = env.ok("run_observe", run_id=run)
    assert len(text) < 6_500
    lines = text.split("\n")
    assert lines[1] == "Line 0 of the page" and "Line 1999 of the page" not in text
    assert "run_read" in next(line for line in lines if line.startswith("[The top"))
    # what holds a value or presses something comes first, wherever it stands on the page
    assert '[go] button "Send it"' in lines and '[o0] option "Size 0"' in lines
    assert f'[pick] combobox "Size" value "S"{copy_text.OPTIONS_LEFT.format(n=22)}' in lines
    assert '[r99] link "Result 99"' not in lines
    last = lines[-1]
    assert last.startswith(f"[{len([x for x in lines if x.startswith('[') and '] ' in x])} of "
                           f"{len(els)} controls listed") and "all_controls" in last


def test_any_control_is_one_call_away(env):
    """Trimming may never cost a control: every handle is in the all_controls reply."""
    run, els = _long_page(env)
    every = env.ok("run_observe", run_id=run, all_controls=True)
    for el in els:
        assert f'[{el["handle"]}] ' in every
    assert "Line 0 of the page" not in every


def test_every_handle_fits_however_long_the_labels(env):
    """Every handle fits in the reply, however long the labels: the generic result cut never
    drops one."""
    (run,) = admitted_run(env)
    els = [{"handle": f"b{i}", "role": "button", "label_masked": "Same long label " * 16}
           for i in range(400)]
    env.app.observation.update(text="x", elements=els)
    for args in ({"all_controls": True}, {"find": "same long"}):
        every = env.ok("run_observe", run_id=run, **args)
        assert sum(f'[{el["handle"]}] ' in every for el in els) == 400
        assert every.endswith(copy_text.CONTROLS_CUT)
    # the route that notice names: a narrower list holds the whole label
    els[7]["label_masked"] += "the last words"
    found = env.ok("run_observe", run_id=run, find="the last words")
    assert els[7]["label_masked"].replace('"', "'") in found


def test_find_lists_the_controls_that_hold_the_words(env):
    run, els = _long_page(env)
    found = env.ok("run_observe", run_id=run, find="result 50")
    assert '[r50] link "Result 50"' in found and "[r48] " in found and "[r52] " in found
    assert "[r47] " not in found and "[n1] " not in found and "Line 0" not in found
    # an option brings its dropdown; a dropdown brings its options
    found = env.ok("run_observe", run_id=run, find="Size 29")
    assert "[pick] " in found and "[o29] " in found and "[o10] " not in found
    assert all(f"[o{i}] " in env.ok("run_observe", run_id=run, find="size") for i in range(30))
    # a value counts, and no pattern is read
    env.app.observation["elements"][0]["value_masked"] = "abc.def"
    assert "[n0] " in env.ok("run_observe", run_id=run, find="ABC.DEF")
    assert env.ok("run_observe", run_id=run, find="abc.*def").endswith(copy_text.CONTROLS_NONE)


def test_find_asks_the_app_to_search_the_whole_page(env):
    """The words go in the request: the search is the app's, past what it can list."""
    run, _ = _long_page(env)
    env.ok("run_observe", run_id=run, find="Result 50")
    assert env.app.observe_bodies[-1] == {"sign_in": True, "find": "Result 50"}
    env.ok("run_observe", run_id=run, all_controls=True)
    assert env.app.observe_bodies[-1] == {"sign_in": True}
    env.ok("run_observe", run_id=run)
    assert env.app.observe_bodies[-1] == {"sign_in": True}


def test_only_the_apps_answer_says_what_is_found(env):
    """A label holding the words in this reply but not found by the app (masked there) is not
    found here, and the reverse: one the app found by what it cut from the label is."""
    (run,) = admitted_run(env)
    els = [{"handle": "a", "role": "button", "label_masked": "Words here", "found": False},
           {"handle": "b", "role": "button", "label_masked": "Other", "found": True}]
    reply = {**env.app.observation, "elements": els, "find": {"whole": True, "left": 0}}
    import tools_run
    import wire
    assert tools_run._found_controls(wire.parse_observation(reply)["elements"]) == {0, 1}
    far = [{"handle": f"n{i}", "role": "button", "label_masked": f"N {i}"} for i in range(8)]
    far[6]["found"] = True
    assert tools_run._found_controls(wire.parse_observation(
        {**env.app.observation, "elements": far})["elements"]) == {4, 5, 6, 7}


def test_no_control_holds_the_words_only_after_a_whole_search(env):
    run, _ = _long_page(env)
    assert env.ok("run_observe", run_id=run, find="nothing holds this").endswith(copy_text.CONTROLS_NONE)
    env.app.find_answer = {"whole": False, "left": 0}
    text = env.ok("run_observe", run_id=run, find="nothing holds this")
    assert text.endswith(copy_text.CONTROLS_NONE_PART)
    assert "No visible control" not in text and "could not reach all" in text


def test_a_search_says_what_it_could_not_reach_or_list(env):
    run, _ = _long_page(env)
    env.app.find_answer = {"whole": False, "left": 7}
    found = env.ok("run_observe", run_id=run, find="result 50")
    assert '[r50] link "Result 50"' in found
    assert found.endswith(copy_text.FIND_MORE.format(n=7) + "\n" + copy_text.FIND_PART)
    env.app.find_answer = {"whole": True, "left": 0}
    assert "\n[" not in env.ok("run_observe", run_id=run, find="send it").split('"Send it"')[1]


def test_all_controls_states_the_counts_on_a_capped_page(env):
    (run,) = admitted_run(env)
    env.app.observation.update(text="x", controls_left=1760, elements=[
        {"handle": f"b{i}", "role": "button", "label_masked": "Same long label " * 16}
        for i in range(400)])
    every = env.ok("run_observe", run_id=run, all_controls=True)
    assert copy_text.CONTROLS_CAPPED.format(listed=400, total=2160) in every
    assert "with find reaches any" in every
    assert sum(f"[b{i}] " in every for i in range(400)) == 400
    import tooling
    assert every.endswith(copy_text.CONTROLS_CUT) and len(every) <= tooling.MAX_RESULT_CHARS
    # the lines' share of the limit rounds down: some count leaves no slack for the line of counts
    for count in range(399, 359, -1):
        env.app.observation["elements"] = env.app.observation["elements"][:count]
        every = env.ok("run_observe", run_id=run, all_controls=True)
        assert every.endswith(copy_text.CONTROLS_CUT) and len(every) <= tooling.MAX_RESULT_CHARS, count


def test_all_controls_says_nothing_of_a_cap_when_all_fit(env):
    run, els = _long_page(env)
    every = env.ok("run_observe", run_id=run, all_controls=True)
    assert "of the page's" not in every and len(every.split("\n")) == len(els) + 1


def test_a_page_reply_counts_what_the_app_could_not_list(env):
    (run,) = admitted_run(env)
    env.app.observation.update(text="x", controls_left=30, elements=[
        {"handle": "a", "role": "button", "label_masked": "One"}])
    last = env.ok("run_observe", run_id=run).split("\n")[-1]
    assert last.startswith("[1 of 31 controls listed; not listed: elsewhere on the page 30.")
    assert "anywhere on the page" in last


def test_find_after_a_sign_in_look_still_searches(env):
    """The verdict's reply carries a page searched for nothing: it is asked again, so nothing is
    called found, or not found, on a list that was never searched."""
    run, _ = _long_page(env)
    env.app.sign_in_look = True
    found = env.ok("run_observe", run_id=run, find="result 50")
    assert '[r50] link "Result 50"' in found
    assert env.app.observe_bodies[-2:] == [{"sign_in": True, "find": "result 50"},
                                           {"find": "result 50"}]


def test_the_observe_tool_does_not_claim_every_control():
    import tooling
    import tools_run  # noqa: F401 — registers the tools
    described = tooling.TOOLS["run_observe"]["description"]
    assert "every control" not in described and "whole page" in described


@pytest.mark.parametrize("position,sentence", [
    ({"top": 1920, "end": 44280, "moved": True}, copy_text.SCROLL_MOVED),
    ({"top": 44280, "end": 44280, "moved": False}, copy_text.SCROLL_STILL),
])
def test_a_scroll_reply_says_where_the_page_is(env, position, sentence):
    (run,) = admitted_run(env)
    env.app.scroll_position = position
    text = env.ok("run_act", run_id=run, kind="scroll", handle="p1", generation=1,
                  direction="down", amount="page")
    assert text.endswith(sentence.format(top=position["top"], end=position["end"]))
    # observing replaces handles and starts the scroll over: the reply must not send him there first
    assert text.startswith("Done.") and "repeat this same call" in text
    assert "before the next action" not in text


@pytest.mark.parametrize("position", [
    {"top": "1", "end": 2, "moved": True}, {"top": 1, "end": None, "moved": True},
    {"top": 1, "end": 2}, {"top": 1, "end": 2, "moved": "yes"}, "far down"])
def test_a_scroll_position_of_the_wrong_shape_is_refused(position):
    import wire
    from app_client import AppError
    with pytest.raises(AppError):
        wire.parse_step({"outcome": "done", "generation": 1, "position": position})


@pytest.mark.parametrize("bad", [{"find": "yes"}, {"find": {"left": 1}}, {"find": {"whole": 1}},
                                 {"controls_left": "many"}, {"controls_left": -1},
                                 {"find": {"whole": True, "left": "x"}}])
def test_a_searchs_answer_of_the_wrong_shape_is_refused(bad):
    import wire
    from app_client import AppError
    with pytest.raises(AppError):
        wire.parse_observation({"page_handle": "p1", "generation": 1, "text": "", "elements": [], **bad})


def test_a_short_page_reply_stays_whole(env):
    (run,) = admitted_run(env)
    text = env.ok("run_observe", run_id=run)
    assert "[The top" not in text and "controls listed" not in text


def test_a_cut_at_the_apps_limit_says_or_more(env):
    run, _ = _long_page(env)
    env.app.observation["text_cut"] = True
    total = len(env.app.observation["text"])
    assert f"of {total} or more characters" in env.ok("run_observe", run_id=run)


def test_run_read_length_takes_less_and_the_window_goes_on(env):
    (run,) = admitted_run(env)
    env.app.read_source = "abcdefghij" * 10
    text = env.ok("run_read", run_id=run, offset=20, length=15)
    window, status, ending = text.split("\n")
    assert window == ("abcdefghij" * 10)[20:35]
    assert "next_offset 35" in status and "complete false" in status
    assert ending == copy_text.READ_MORE.format(next_offset=35)
    # a length past the end changes nothing
    assert "complete true" in env.ok("run_read", run_id=run, offset=90, length=30)


def test_run_read_find_gives_each_place_with_its_offset_across_windows(env):
    (run,) = admitted_run(env)
    body = "x" * 29_990 + "Needle One" + "y" * 40_000 + "needle TWO" + "z" * 50
    env.app.read_source = body
    text = env.ok("run_read", run_id=run, find="NEEDLE")
    lines = text.split("\n")
    assert lines[0] == copy_text.READ_FOUND.format(n=2)
    assert lines[1].startswith("[offset 29990] ") and "Needle One" in lines[1]
    assert lines[2].startswith(f"[offset {body.index('needle TWO')}] ")
    assert len(text) < 1000
    # a plain string, no pattern; from offset on; nothing found says so
    assert env.ok("run_read", run_id=run, find="ne.dle") == copy_text.READ_NOT_FOUND
    assert env.ok("run_read", run_id=run, find="needle", offset=60_000).split("\n")[1].startswith(
        f"[offset {body.index('needle TWO')}] ")
    env.app.read_source = "needle " * 50
    text = env.ok("run_read", run_id=run, find="needle")
    assert text.startswith(copy_text.READ_FOUND.format(n=50))
    assert copy_text.READ_FOUND_MORE.format(shown=20) in text


# -- run_act: long text and the plan -------------------------------------------------------------

def _type(env, run, text, **extra):
    return env.ok("run_act", run_id=run, kind="type", handle="e1", generation=1, text=text,
                  **extra)


def test_run_act_text_bounds_match_the_app(env):
    (run,) = admitted_run(env)
    env.app.calls.clear()
    assert _type(env, run, "a" * 2001) == copy_text.TEXT_ONE_LINE_LONG
    assert not [c for c in env.app.calls if c.path.endswith("/action")]
    assert _type(env, run, "a" * 2000).startswith("Done")
    env.app.observation["generation"] = 1
    long_list = ("1x Card\n" * 4000)[:32_000]
    assert _type(env, run, long_list).startswith("Done")
    assert env.app.posts("/action")[-1].body["text"] == long_list
    text, is_error = env.call("run_act", run_id=run, kind="type", handle="e1", generation=2,
                              text=long_list + "x")
    assert is_error


def test_run_act_sends_the_plan_in_the_action_body(env):
    (run,) = admitted_run(env)
    env.ok("run_act", run_id=run, kind="click", handle="e1", generation=1,
           plan="Create the list.\nAdd 1x Card.")
    assert env.app.posts("/action")[-1].body["plan"] == ("Create the list.\n"
                                                                     "Add 1x Card.")
    env.ok("run_act", run_id=run, kind="click", handle="e1", generation=2)
    assert "plan" not in env.app.posts("/action")[-1].body


def test_run_act_sends_ask_first_only_when_true(env):
    """A tick whose label commits money goes to the check (ask_first); an ordinary one runs as
    today, the member absent (the app's optional member)."""
    (run,) = admitted_run(env)
    env.ok("run_act", run_id=run, kind="click", handle="e1", generation=1, ask_first=True)
    assert env.app.posts("/action")[-1].body["ask_first"] is True
    env.ok("run_act", run_id=run, kind="click", handle="e1", generation=2, ask_first=False)
    assert "ask_first" not in env.app.posts("/action")[-1].body
    env.ok("run_act", run_id=run, kind="click", handle="e1", generation=3)
    assert "ask_first" not in env.app.posts("/action")[-1].body


@pytest.mark.parametrize("plan", [
    "(" * 6001,                    # one line longer than a message once escaped (12,002)
    "日" * 11_000,                 # one line past a rich message's prepared bytes
    "tab\there",                                           # the app refuses this
    " \n ",                                                # nothing to show
])
def test_a_plan_that_cannot_go_on_a_card_is_refused_before_any_app_call(env, plan):
    (run,) = admitted_run(env)
    env.app.calls.clear()
    got = env.ok("run_act", run_id=run, kind="click", handle="e1", generation=1, plan=plan)
    assert got == copy_text.PLAN_TOO_LONG
    assert env.app.calls == []


def test_continue_run_prints_what_he_approved(env):
    (run,) = admitted_run(env)
    assert "approved" not in env.ok("continue_run", run_ref=run).lower()
    env.app.approved[run] = "Add 1x Card."
    assert copy_text.APPROVED_PLAN.format(plan="Add 1x Card.") in env.ok("continue_run",
                                                                         run_ref=run)


def test_run_wait_waiting_states(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "waiting_approval", "q1")
    state.mint_tap("q_not_now", {"run_id": run, "question_id": "q1"}, "card-x")
    env.casa.calls.clear()
    text = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert copy_text.END_TURN_WAITING in text and copy_text.POST_CARD_FIRST not in text
    assert not [p for p, _ in env.casa.calls if p.endswith("/continue")]
    state.void_group("card-x")
    text = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert copy_text.POST_CARD_FIRST in text


def test_run_wait_bounded(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    t0 = time.monotonic()
    env.ok("run_wait", run_id=run, max_wait_s=3)
    assert time.monotonic() - t0 <= 3 + 2


def test_run_wait_returns_on_event(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    env.app.add_event(run, "action", handle="hb1", text_masked="Send form")
    t0 = time.monotonic()
    text = env.ok("run_wait", run_id=run, max_wait_s=30)
    assert time.monotonic() - t0 < 5
    assert "handle hb1" in text


def test_a_cancel_after_the_view_lapsed_is_the_only_telling(env):
    """The app ended the run (an unopened view's hour), the sweep owed its
    telling, then the operator asked to cancel it: the cancel's answer is the telling; no continuation
    tells it again."""
    import continuation
    (run,) = admitted_run(env)
    env.app.set_state(run, "waiting_live")
    con = state._connect()
    con.execute("DELETE FROM leases")
    con.close()
    env.app.set_state(run, "cancelled")
    assert [r["run_ref"] for r in continuation.sweep()] == [run]      # owed
    env.casa.requester["binding_ref"] = BIND_B
    env.ok("run_cancel", run_id=run)
    con = state._connect()
    con.execute("DELETE FROM leases")
    con.close()
    assert sum(run in [r["run_ref"] for r in continuation.sweep()] for _ in range(5)) == 0


def test_cancel_refused_in_live(env):
    """A view the operator is in ends at their Done; one waiting for them is never finished under it."""
    (run,) = admitted_run(env)
    for st in ("waiting_live", "live"):
        env.app.set_state(run, st)
        if st == "live":
            assert env.ok("run_cancel", run_id=run) == copy_text.LIVE_IS_OPERATORS
        out = json.loads(env.ok("run_finish", run_id=run, summary="s", file_ids=[],
                                source_ids=[], expectation_status="matched"))
        assert out == {"notice": None, "note": copy_text.LIVE_IS_OPERATORS}
    assert env.app.posts("/cancel") == [] and env.app.posts("/finish") == []


def test_capability_envelope(env, monkeypatch):
    out = json.loads(env.ok("browser_status"))
    assert out["notice"] is None and "Casa Browser 0.1.0: ready." in out["note"]
    env.app.notices = [{"seq": 1, "text_masked": "Login for Utility Co changed."},
                       {"seq": 2, "text_masked": "Run run9 was interrupted."}]
    out = json.loads(env.ok("browser_status"))
    assert out["notice"].startswith("casa-cap-")
    assert env.casa.deposits[-1]["key"] == "notices-1-2"
    assert "Login for Utility Co changed." not in out["note"]
    # an error after the deposit still returns the reference
    import deliveries
    env.app.notices.append({"seq": 3, "text_masked": "Another."})
    env.casa.delivery[out["notice"]] = {"status": "delivered", "repeat": False}

    def boom(*a, **k):
        from app_client import AppError
        raise AppError("unreachable")
    monkeypatch.setattr(deliveries.state, "put_delivery", boom)
    out = json.loads(env.ok("browser_status"))
    assert out["notice"].startswith("casa-cap-") and copy_text.UNREACHABLE in out["note"]


def test_notices_cursor_follows_delivery_status(env):
    env.app.notices = [{"seq": 1, "text_masked": "One."}, {"seq": 2, "text_masked": "Two."}]
    first = json.loads(env.ok("browser_status"))["notice"]
    env.casa.delivery[first] = {"status": "withheld", "repeat": False}
    second = json.loads(env.ok("browser_status"))["notice"]
    assert second and env.casa.deposits[-1]["key"] == "notices-1-2"
    assert state.notice_cursor() == 0
    env.casa.delivery[second] = {"status": "delivered", "repeat": False}
    third = json.loads(env.ok("browser_status"))
    assert third["notice"] is None and state.notice_cursor() == 2
    assert len(env.casa.deposits) == 2


def test_old_notices_pass_over_and_repeats_collapse(env):
    """Notices older than a day are not posted; a repeated text is one line with its
    count; a page of only old notices is passed over and the next fetched in the same call."""
    now = time.time()
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3 * 86400))
    young = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
    interrupted = "A browser run was interrupted and was not resumed."
    env.app.notices = ([{"seq": i, "text_masked": interrupted, "at": old} for i in range(1, 8)]
                       + [{"seq": 8, "text_masked": "Old test site notice.", "at": old}])
    out = json.loads(env.ok("browser_status"))
    assert out["notice"] is None and state.notice_cursor() == 8 and env.casa.deposits == []
    env.app.notices += [{"seq": 9, "text_masked": interrupted, "at": young},
                        {"seq": 10, "text_masked": "Saved the login for Utility Co", "at": young},
                        {"seq": 11, "text_masked": interrupted, "at": young}]
    out = json.loads(env.ok("browser_status"))
    dep = env.casa.deposits[-1]
    assert dep["key"] == "notices-9-11"
    assert dep["value"] == ("Casa Browser:\n- " + interrupted + copy_text.NOTICE_REPEATS.format(n=2)
                            + "\n- Saved the login for Utility Co")


def test_a_young_notice_behind_a_page_of_old_ones_is_posted(env):
    """The app returns at most a page of notices; a page of only old ones is passed
    over and the next page is fetched in the same call."""
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 86400))
    young = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    env.app.notices = ([{"seq": i, "text_masked": f"Old {i}.", "at": old} for i in range(1, 6)]
                       + [{"seq": 6, "text_masked": "Fresh.", "at": young}])
    env.app.notices_page = 2
    out = json.loads(env.ok("browser_status"))
    assert out["notice"] and env.casa.deposits[-1]["value"] == "Casa Browser:\n- Fresh."


def test_status_lists_this_bindings_runs(env):
    """A request's own runs are listed as its own; from another of the operator's requests they are listed
    as their runs from other requests (their request may cancel them); a specialist sees none."""
    (run,) = admitted_run(env)
    note = json.loads(env.ok("browser_status"))["note"]
    assert f"- {run}: queued" in note and "Utility Co (s1): ready" in note
    assert "other requests" not in note
    env.casa.requester["binding_ref"] = BIND_B
    note = json.loads(env.ok("browser_status"))["note"]
    assert "Runs for this request" not in note
    assert f"The operator's runs from other requests:\n- {run}: queued (Download invoice 0.)" in note
    env.casa.requester["requester_kind"] = "specialist"
    note = json.loads(env.ok("browser_status"))["note"]
    assert run not in note


def test_archive_foreign_file_refused(env):
    run_a, run_b = admitted_run(env, n=2)
    env.ok("run_download", run_id=run_a, handle="e1")
    fid = state._connect().execute("SELECT file_id FROM files").fetchone()[0]
    env.app.calls.clear()
    text = env.ok("run_archive", run_id=run_b, file_ids=[fid])
    assert text == copy_text.FOREIGN_FILE.format(file_id=fid)
    assert env.app.calls == []
    assert env.ok("run_archive", run_id=run_a, file_ids=[fid]).startswith("Zipped into")


def test_finish_queues_next_run(env):
    r1, r2, r3 = admitted_run(env, n=3)
    def queued():
        return [b["run_ref"] for p, b in env.casa.calls if p.endswith("/continue")]
    out = json.loads(env.ok("run_finish", run_id=r1, summary="done", file_ids=[],
                            source_ids=[], expectation_status="matched"))
    assert out["notice"] is None and "Finished" in out["note"]
    assert queued() == [r2]
    assert env.app.runs[r1]["finish"] == {"summary_masked": "done", "file_ids": [],
                                          "source_ids": [], "expectation_status": "matched"}
    env.ok("run_cancel", run_id=r2)
    assert queued() == [r2, r3]
    env.ok("run_finish", run_id=r3, summary="d", file_ids=[], source_ids=[],
           expectation_status="mismatch")
    assert queued() == [r2, r3]


def test_finish_foreign_file_refused(env):
    run_a, run_b = admitted_run(env, n=2)
    env.ok("run_download", run_id=run_a, handle="e1")
    fid = state._connect().execute("SELECT file_id FROM files").fetchone()[0]
    out = json.loads(env.ok("run_finish", run_id=run_b, summary="s", file_ids=[fid],
                            source_ids=[], expectation_status="matched"))
    assert out == {"notice": None, "note": copy_text.FOREIGN_FILE.format(file_id=fid)}
    assert env.app.posts("/finish") == []


def test_records_scope_from_the_requester(env):
    env.casa.requester["requester_id"] = "specialist:finance"
    env.app.records = [{"requester_id": "specialist:finance", "run_id": "r1",
                        "summary_masked": "Got March.", "sites": [{"site_id": "s1"}],
                        "changes": [], "ended_at": "2026-10-19T10:00:00Z"},
                       {"requester_id": "assistant", "run_id": "r2",
                        "summary_masked": "Other.", "sites": [], "changes": [],
                        "ended_at": "2026-10-19T10:00:00Z"}]
    text = env.ok("run_records", since_days=7)
    assert "Got March." in text and "Other." not in text
    q = [c for c in env.app.calls if c.path == "/v1/records"][0].query
    assert "requester_id=specialist%3Afinance" in q or "requester_id=specialist:finance" in q


@pytest.mark.parametrize("payload", [[{"run_id": "r"}], {"runs": "x"}, {"items": []},
                                     {"runs": [{"run_id": "r", "request_id": "q",
                                                "state": "completed", "pending_question": None}]}])
def test_active_runs_wrapper(env, payload):
    (run,) = admitted_run(env)
    env.app.overrides[("GET", "/v1/runs")] = (200, payload, "application/json")
    text = env.ok("continue_run", run_ref=run)
    assert "(bad_response)" in text


def test_app_400_unsupported(env):
    (run,) = admitted_run(env)
    env.app.overrides[("POST", f"/v1/runs/{run}/search")] = (
        400, {"error": "unsupported"}, "application/json")
    text = env.ok("run_search", run_id=run, query="x")
    assert text == copy_text.BY_CODE["unsupported"] + " (unsupported)"
    assert len(env.app.posts("/search")) == 1


def test_scroll_members(env):
    (run,) = admitted_run(env)
    env.ok("run_act", run_id=run, kind="scroll", handle="p1", generation=1,
           direction="down", amount="page")
    body = env.app.posts("/action")[-1].body
    assert (body["direction"], body["amount"]) == ("down", "page")
    env.app.calls.clear()
    for args in ({"kind": "scroll"}, {"kind": "scroll", "direction": "up"},
                 {"kind": "click", "direction": "up", "amount": "step"},
                 {"kind": "click", "amount": "step"}):
        assert env.ok("run_act", run_id=run, handle="p1", generation=1,
                      **args) == copy_text.SCROLL_MEMBERS
    assert env.app.calls == []


def test_stale_handle(env):
    (run,) = admitted_run(env)
    assert env.ok("run_act", run_id=run, kind="click", handle="e1",
                  generation=99) == copy_text.PAGE_CHANGED


def test_a_select_refusal_says_what_a_choice_is(env):
    """A select without choice_handle is refused with a sentence that says what a choice is,
    so an option's text is not passed as the handle and read as a changed page."""
    (run,) = admitted_run(env)
    said = env.ok("run_act", run_id=run, kind="select", handle="e1", generation=1, text="Table")
    assert said == copy_text.SELECT_NEEDS_CHOICE and "option lines" in said
    assert "changed" not in copy_text.PAGE_CHANGED and "handle" in copy_text.PAGE_CHANGED


def test_file_too_large(env):
    (run,) = admitted_run(env)
    env.ok("run_download", run_id=run, handle="e1")
    fid = state._connect().execute("SELECT file_id FROM files").fetchone()[0]
    for path, tool, args in ((f"/v1/runs/{run}/download", "run_download", {"handle": "e2"}),
                             (f"/v1/runs/{run}/archive", "run_archive", {"file_ids": [fid]})):
        env.app.overrides[("POST", path)] = (413, {"error": "file_too_large"},
                                             "application/json")
        env.app.calls.clear()
        text = env.ok(tool, run_id=run, **args)
        assert text == copy_text.FILE_TOO_LARGE + " (file_too_large)"
        assert len([c for c in env.app.calls if c.path == path]) == 1


def test_download_refusals_say_what_happened(env):
    """Casa Browser as built: `blocked` on a download is a fetch that failed."""
    (run,) = admitted_run(env)
    path = f"/v1/runs/{run}/download"
    for status, code, sentence in ((403, "blocked", copy_text.DOWNLOAD_BLOCKED),):
        env.app.overrides[("POST", path)] = (status, {"error": code}, "application/json")
        env.app.calls.clear()
        text = env.ok("run_download", run_id=run, handle="e2")
        assert text == f"{sentence} ({code})"
        assert len([c for c in env.app.calls if c.path == path]) == 1
    assert "approv" not in copy_text.DOWNLOAD_BLOCKED


# -- notices, argument errors and the binding ----------------------------------------------

def test_notice_batch_kept_until_delivered(env):
    """An unsettled notice post is offered again with the SAME key and body,
    even when newer notices arrived meanwhile; newer ones go out only after
    it was delivered."""
    env.app.notices = [{"seq": 1, "text_masked": "One."}]
    first = json.loads(env.ok("browser_status"))["notice"]
    body = env.casa.deposits[-1]["value"]
    env.app.notices.append({"seq": 2, "text_masked": "Two."})
    for status in ("unknown", "withheld"):
        env.casa.delivery[first] = {"status": status, "repeat": False}
        first = json.loads(env.ok("browser_status"))["notice"]
        assert env.casa.deposits[-1]["key"] == "notices-1-1"
        assert env.casa.deposits[-1]["value"] == body
    env.casa.delivery[first] = {"status": "delivered", "repeat": False}
    json.loads(env.ok("browser_status"))
    assert env.casa.deposits[-1]["key"] == "notices-2-2"
    assert state.notice_cursor() == 1


def test_continue_run_argument_error_keeps_ending(env):
    import tooling
    import browser_server  # noqa: F401
    for args in ({"run_ref": "run/1"}, {"run_ref": "run1", "extra": 1}, {}):
        text, is_error = tooling.call("continue_run", args)
        assert is_error and text.endswith(copy_text.CONTINUATION_ENDING)


def test_finish_checks_binding_before_any_casa_call(env):
    (run,) = admitted_run(env)
    state.put_delivery("casa-cap-" + "e" * 32, "notice", None, None, 1, None)
    env.casa.requester["binding_ref"] = BIND_B
    env.casa.calls.clear()
    env.app.calls.clear()
    out = json.loads(env.ok("run_finish", run_id=run, summary="s", file_ids=[], source_ids=[],
                            expectation_status="matched"))
    assert out == {"notice": None, "note": copy_text.NOT_YOUR_RUN}
    assert [p for p, _ in env.casa.calls] == ["/internal/broker/requester"]
    assert env.app.calls == []


# -- notices, the result contract and the turn bound ---------------------------------------

def test_notice_in_at_most_one_batch(env):
    """A batch formed from a stale fetch never repeats a notice an earlier
    batch covered: batches cover strictly increasing sequence ranges."""
    one = [{"seq": 1, "text_masked": "One."}]
    both = one + [{"seq": 2, "text_masked": "Two."}]
    b1 = state.form_notice_batch(one, "Casa Browser:")
    assert (b1.key, b1.first_seq, b1.last_seq) == ("notices-1-1", 1, 1)
    # an open batch is offered again whatever was fetched since
    assert state.form_notice_batch(both, "Casa Browser:") == b1
    state.put_delivery("casa-cap-" + "d" * 32, "notice", None, None, 1, None)
    state.settle_delivery("casa-cap-" + "d" * 32, "delivered")
    b2 = state.form_notice_batch(both, "Casa Browser:")     # a stale fetch from cursor 0
    assert (b2.key, b2.first_seq, b2.last_seq) == ("notices-2-2", 2, 2)
    assert "One." not in b2.body
    state.put_delivery("casa-cap-" + "f" * 32, "notice", None, None, 2, None)
    state.settle_delivery("casa-cap-" + "f" * 32, "delivered")
    assert state.form_notice_batch(both, "Casa Browser:") is None


_FORM = r"""
import sys, json
sys.path.insert(0, {server!r})
import state
b = state.form_notice_batch({notices!r}, "Casa Browser:")
print(json.dumps(b and b.key))
"""


def test_notice_batch_formed_once_across_processes(env):
    notices = [{"seq": 1, "text_masked": "One."}, {"seq": 2, "text_masked": "Two."}]
    procs = [subprocess.Popen([sys.executable, "-c", _FORM.format(
        server=str(SERVER), notices=notices[:k])], stdout=subprocess.PIPE, text=True,
        env=dict(os.environ), stderr=subprocess.PIPE) for k in (1, 2)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), [e[-2000:] for _, e in outs]
    keys = {json.loads(o) for o, _ in outs}
    assert len(keys) == 1
    con = state._connect()
    assert con.execute("SELECT COUNT(*) FROM notice_batches").fetchone()[0] == 1
    con.close()


_RPC = r"""
import sys, json
sys.path.insert(0, {server!r})
import browser_server
for name, args in {cases!r}:
    r = browser_server.handle({{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {{"name": name, "arguments": args}}}})
    print(json.dumps(r["result"]))
"""


def test_every_result_path_keeps_the_contract(env):
    """Argument errors of every kind, through the JSON-RPC dispatcher: a
    delivering tool still answers its envelope, and continue_run still ends
    with its silence line."""
    cases = [("browser_status", {"extra": 1}), ("browser_status", []),
             ("run_finish", {}), ("run_finish", "x"),
             ("continue_run", []), ("continue_run", {"run_ref": "a/b"}),
             ("continue_run", None)]
    out = subprocess.run([sys.executable, "-c", _RPC.format(server=str(SERVER), cases=cases)],
                         capture_output=True, text=True, env=dict(os.environ), timeout=60)
    results = [json.loads(line) for line in out.stdout.splitlines()]
    assert len(results) == len(cases)
    for (name, args), res in zip(cases, results):
        text = res["content"][0]["text"]
        if name == "continue_run":
            assert text.endswith(copy_text.CONTINUATION_ENDING), (args, text)
        else:
            env_ = json.loads(text)
            assert env_["notice"] is None and isinstance(env_["note"], str), (args, text)
        assert res["isError"] is True, (name, args)


def test_event_origin_is_a_host_only(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    env.app.add_event(run, "action", origin="https://bank.example/private?q=canary")
    env.app.add_event(run, "action", origin="https://pay.example:8443")
    env.app.add_event(run, "action", origin="javascript:alert(1)")
    text = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert "canary" not in text and "/private" not in text and "javascript" not in text
    assert "at pay.example" in text
    assert "://" not in text


def test_start_runs_turn_bound(env, monkeypatch):
    import continuation
    monkeypatch.setattr(continuation, "clock",
                        lambda: continuation._STARTED + continuation.TURN_BOUND_S + 1)
    env.app.calls.clear()
    env.casa.calls.clear()
    text = env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                       "sites": [{"site_id": "s1", "access": "account"}],
                                       "task_text": "t", "result_expectation": ""}])
    assert text == copy_text.END_TURN_BOUND
    assert env.app.calls == [] and env.casa.calls == []
    con = state._connect()
    assert con.execute("SELECT COUNT(*) FROM admissions").fetchone()[0] == 0
    con.close()


# -- unknown arguments, the hello, the bound and rendering ---------------------------------

def test_unknown_argument_names_never_echoed(env):
    canary = "https://example.invalid/private?token=private-canary"
    for name, args in (("continue_run", {canary: 1}), ("browser_status", {canary: 1}),
                       ("run_observe", {"run_id": "r1", canary: 1}),
                       ("start_runs", {"runs": [{canary: 1}]})):
        text, is_error = env.call(name, **args)
        assert is_error and "private-canary" not in text and "://" not in text


def test_status_uses_the_one_checked_hello(env):
    """browser_status acts only on the hello it checked: an app whose reply
    changes between two hellos cannot slip an unchecked one in."""
    env.app.hello_queue = [dict(env.app.hello),
                           dict(env.app.hello, protocol_version=2, app_version="2.0.0")]
    env.app.calls.clear()
    note = json.loads(env.ok("browser_status"))["note"]
    hellos = [c for c in env.app.calls if c.path == "/v1/hello"]
    assert len(hellos) == 1 and "2.0.0" not in note


def test_no_app_call_past_the_bound_any_path(env, monkeypatch):
    """Once the turn bound passes, the app client itself refuses every
    request — whatever tool or code path makes it."""
    import continuation
    import app_client
    (run,) = admitted_run(env)
    client = app_client.compatible_client()
    monkeypatch.setattr(continuation, "clock",
                        lambda: continuation._STARTED + continuation.TURN_BOUND_S + 1)
    env.app.calls.clear()
    with pytest.raises(app_client.AppError) as e:
        client.request("GET", f"/v1/runs/{run}")
    assert e.value.code == "turn_bound"
    assert env.app.calls == []


def test_run_wait_stops_at_the_bound(env, monkeypatch):
    """A wait that would cross the turn bound returns before it, and makes no
    request after it."""
    import continuation
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    start = continuation.time.monotonic()
    monkeypatch.setattr(continuation, "_STARTED",
                        start - (continuation.TURN_BOUND_S - 3))   # 3 s left in the turn
    t0 = continuation.time.monotonic()
    text = env.ok("run_wait", run_id=run, max_wait_s=30)
    assert continuation.time.monotonic() - t0 < 5
    assert continuation.turn_elapsed() < continuation.TURN_BOUND_S + 1
    assert text.startswith(f"Run {run}: running.") or text == copy_text.END_TURN_BOUND


def test_continue_run_shows_the_newest_steps_past_the_listings_cap(env):
    """The app lists 200 steps a fetch from the oldest; continue_run pages to the end and
    shows the newest steps, however many the run has."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    for i in range(605):
        env.app.add_event(run, "action", text_masked=f"Step {i + 1}.")
    env.app.event_page = 200
    text = env.ok("continue_run", run_ref=run)
    assert "Step 605." in text and "Step 586." in text and "Step 585." not in text
    assert "Step 200." not in text
    # a later wake starts near the end rather than paging from the first step again
    env.app.add_event(run, "action", text_masked="Step 606.")
    env.app.calls.clear()
    assert "Step 606." in env.ok("continue_run", run_ref=run)
    assert len([c for c in env.app.calls if c.path.endswith("/events")]) <= 3


def test_run_wait_waits_when_nothing_is_new_and_shows_only_the_newest(env):
    """With nothing new, run_wait waits instead of returning the same steps again at once,
    and it shows only the newest."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    for i in range(450):
        env.app.add_event(run, "action", text_masked=f"Step {i + 1}.")
    env.app.event_page = 200
    env.ok("continue_run", run_ref=run)
    t0 = time.monotonic()
    text = env.ok("run_wait", run_id=run, max_wait_s=3)
    assert "Step" not in text and time.monotonic() - t0 >= 2     # it waited, and repeated nothing
    # new steps past another cap: only the newest, and how many more there were
    for i in range(450, 700):
        env.app.add_event(run, "action", text_masked=f"Step {i + 1}.")
    text = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert "Step 700." in text and "Step 681." in text and "Step 680." not in text
    assert copy_text.EARLIER_STEPS.format(n=230) in text
    assert "Step" not in env.ok("run_wait", run_id=run, max_wait_s=1)


def test_action_reply_rendered(env):
    (run,) = admitted_run(env)
    text = env.ok("run_act", run_id=run, kind="click", handle="e1", generation=1)
    assert text == copy_text.DONE_ACTION.format(outcome="done", generation=2)
    assert env.ok("run_act", run_id=run, kind="click", handle="e1",
                  generation=1) == copy_text.PAGE_CHANGED       # generation moved on


def test_run_age_from_rfc3339(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    env.app.runs[run]["created_at"] -= 25 * 60
    text = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert "Running for 25 min." in text


# -- the check of a step: the plugin's relay (action model) -----------------------

JUDGE = "/internal/broker/action-judgement"
GO = {"verdict": "go", "category": "reading", "reason": "It opens the invoice list."}


def _judged(env, **app):
    """An admitted run whose actions, downloads and searches the app judges."""
    (run,) = admitted_run(env)
    env.app.judge = True
    for k, v in app.items():
        setattr(env.app, k, v)
    env.app.calls.clear()
    env.casa.calls.clear()
    return run


def _verdict_posts(env, run):
    return [c for c in env.app.calls
            if c.method == "POST" and c.path.startswith(f"/v1/control/runs/{run}/judgements/")]


def _click(env, run, **extra):
    return env.ok("run_act", run_id=run, kind="click", handle="e1",
                  generation=env.app.observation["generation"], **extra)


def test_relay_posts_casas_answer_untouched(env):
    run = _judged(env)
    text = _click(env, run)
    assert text == copy_text.DONE_ACTION.format(outcome="done", generation=2)
    (asked,) = env.casa.judgements()
    (post,) = _verdict_posts(env, run)
    # the packet reached Casa as the app's own string, the spec digest is the run's own
    jid = post.path.rsplit("/", 1)[1]
    assert asked["kind"] == "action"
    binding = state.run_binding(run)
    assert asked["spec_digest"] == binding.spec["admission"]["admitted_spec_digest"]
    import hashlib
    sha = hashlib.sha256(asked["packet"].encode("utf-8")).hexdigest()
    # the app accepted the digest of ITS packet: the string was relayed untouched
    assert env.app.responses[(run, jid)]["outcome"] == "done"
    assert post.body == {"packet_sha256": sha, "verdict": GO, "latency_ms": 4321}
    assert env.app.dispatches == [(run, jid)]
    assert len(env.app.posts("/action")) == 1          # the app dispatch is never re-sent


def test_relayed_packet_is_the_apps_text(env):
    run = _judged(env)
    sent = []
    real = env.app._freeze

    def spy(*a, **k):
        r = real(*a, **k)
        sent.append(r["judgement"]["packet"])
        return r
    env.app._freeze = spy
    _click(env, run)
    assert [b["packet"] for b in env.casa.judgements()] == sent
    assert "März €12,50" in sent[0]                    # non-ASCII text, byte for byte


def test_enter_sent_only_when_true(env):
    run = _judged(env, judge=False)
    gen = env.app.observation["generation"]
    env.ok("run_act", run_id=run, kind="type", handle="e1", generation=gen, text="kettle",
           enter=True)
    assert env.app.posts("/action")[-1].body["enter"] is True
    gen = env.app.observation["generation"]
    env.ok("run_act", run_id=run, kind="type", handle="e1", generation=gen, text="kettle")
    env.ok("run_act", run_id=run, kind="type", handle="e1", generation=gen + 1,
           text="kettle", enter=False)
    env.ok("run_act", run_id=run, kind="click", handle="e1", generation=gen + 2,
           enter=False)
    assert len(env.app.posts("/action")) == 4
    assert all("enter" not in p.body for p in env.app.posts("/action")[1:])


@pytest.mark.parametrize("args, sentence", [
    ({"kind": "click", "enter": True}, copy_text.ENTER_ONLY_TYPE),
    ({"kind": "type", "text": "tab\there"}, copy_text.TEXT_PLAIN),
    ({"kind": "type", "text": "one\ntwo\tthree"}, copy_text.TEXT_PLAIN),
    ({"kind": "type", "text": "line\u2028sep"}, copy_text.TEXT_PLAIN),
    ({"kind": "type", "text": "c1\x85"}, copy_text.TEXT_PLAIN),
    ({"kind": "type", "text": "esc\x1b"}, copy_text.TEXT_PLAIN),
])
def test_text_is_plain_and_only_typing_takes_enter(env, args, sentence):
    run = _judged(env)
    assert env.ok("run_act", run_id=run, handle="e1", generation=1, **args) == sentence
    assert env.app.calls == [] and env.casa.calls == []


@pytest.mark.parametrize("text", ["Hallo,\n\nDank je.\nJan", "one\r\ntwo", "one\r"])
def test_line_breaks_reach_the_app_untouched(env, text):
    """A line break is the app's to place (only into a multi-line field, as text):
    Fido sends it as given; the app answers where it may not go."""
    (run,) = admitted_run(env)
    gen = env.app.observation["generation"]
    env.app.overrides[("POST", f"/v1/runs/{run}/action")] = (
        200, {"outcome": "done", "generation": gen + 1, "typed": "shown"}, "application/json")
    out = env.ok("run_act", run_id=run, kind="type", handle="e1", generation=gen, text=text)
    assert out.startswith("Done (done).")
    (post,) = env.app.posts("/action")
    assert post.body["text"] == text and "enter" not in post.body


def test_one_retry_on_check_unavailable(env):
    run = _judged(env)
    env.casa.judge_script = ["check_unavailable", GO]
    assert _click(env, run) == copy_text.DONE_ACTION.format(outcome="done", generation=2)
    assert len(env.casa.judgements()) == 2
    (post,) = _verdict_posts(env, run)
    assert post.body["verdict"] == GO


def test_one_retry_on_a_timeout(env, monkeypatch):
    import casa_broker
    monkeypatch.setattr(casa_broker, "JUDGEMENT_TIMEOUT_S", 0.3)
    run = _judged(env)
    env.casa.judge_script = ["timeout", GO]
    assert _click(env, run) == copy_text.DONE_ACTION.format(outcome="done", generation=2)
    assert len(env.casa.judgements()) == 2
    (post,) = _verdict_posts(env, run)
    assert post.body["verdict"] == GO


@pytest.mark.parametrize("failures", [
    ("check_unavailable", "check_unavailable"), ("timeout", "check_unavailable"),
    ("bad_packet", "timeout"), ("malformed", "malformed"), ("check_unavailable", "malformed"),
])
def test_no_verdict_only_after_both_calls_fail(env, monkeypatch, failures):
    import casa_broker
    monkeypatch.setattr(casa_broker, "JUDGEMENT_TIMEOUT_S", 0.3)
    run = _judged(env)
    env.casa.judge_script = list(failures)
    assert _click(env, run) == copy_text.STEP_CHECK_UNAVAILABLE
    assert len(env.casa.judgements()) == 2                   # exactly one retry
    posts = _verdict_posts(env, run)
    assert [p.body for p in posts] == [{"no_verdict": "check_unavailable"}]
    assert env.app.dispatches == []


def test_never_a_verdict_of_the_plugins_own(env, monkeypatch):
    """Whatever Casa does, every verdict the app receives is one Casa sent, untouched;
    without one the app gets only the no-verdict body."""
    import casa_broker
    monkeypatch.setattr(casa_broker, "JUDGEMENT_TIMEOUT_S", 0.3)
    run = _judged(env)
    sent = [GO, {"verdict": "look_first", "category": "reading", "reason": "r",
                 "look_first": "Open the March row first."}]
    for script in (["check_unavailable", sent[0]], ["timeout", "timeout"],
                   ["malformed", sent[1]], ["bad_packet", "malformed"]):
        env.casa.judge_script = list(script)
        _click(env, run)
    bodies = [p.body for p in _verdict_posts(env, run)]
    assert [b.get("verdict") for b in bodies] == [sent[0], None, sent[1], None]
    for b in bodies:
        assert set(b) in ({"packet_sha256", "verdict", "latency_ms"}, {"no_verdict"})


def test_lost_reply_reposted_once_and_answered_from_the_store(env):
    run = _judged(env, lose_verdict_replies=1)
    assert _click(env, run) == copy_text.DONE_ACTION.format(outcome="done", generation=2)
    first, second = _verdict_posts(env, run)
    assert first.path == second.path and first.body == second.body
    assert len(env.app.dispatches) == 1                # applied once, replayed once
    assert len(env.casa.judgements()) == 1             # Casa is not asked again


def test_lost_reply_reposted_only_once(env):
    run = _judged(env, lose_verdict_replies=2)
    text = _click(env, run)
    assert text == copy_text.UNREACHABLE + " (unreachable)"
    assert len(_verdict_posts(env, run)) == 2
    assert len(env.app.dispatches) == 1 and len(env.casa.judgements()) == 1


def test_a_slow_go_is_waited_for(env, monkeypatch):
    """A verdict whose go takes longer than the client's own timeout is answered
    within the verdict's longer one: used once, never re-posted. (Scaled down: the
    client's 30 s is 0.3 s here, the verdict's bound 3 s, the go 1 s.)"""
    import app_client
    import judge
    monkeypatch.setattr(app_client, "DEFAULT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(judge, "VERDICT_TIMEOUT_S", 3.0)
    run = _judged(env, verdict_delay_s=1.0)
    assert _click(env, run) == copy_text.DONE_ACTION.format(outcome="done", generation=2)
    assert len(_verdict_posts(env, run)) == 1 and len(env.app.dispatches) == 1


def test_verdict_wait_never_passes_the_turn_bound(env, monkeypatch):
    import continuation
    import judge
    run = _judged(env)
    seen = []
    import app_client
    original = app_client.AppClient.request

    def spy(self, method, path, body=None, **kw):
        if "/judgements/" in path and method == "POST":     # a verdict (not the pending GET)
            seen.append(kw.get("timeout_s"))
        return original(self, method, path, body, **kw)
    monkeypatch.setattr(app_client.AppClient, "request", spy)
    monkeypatch.setattr(continuation, "turn_remaining", lambda: 42.0)
    _click(env, run)
    assert seen == [42.0]
    monkeypatch.setattr(continuation, "turn_remaining", lambda: 500.0)
    _click(env, run)
    assert seen == [42.0, judge.VERDICT_TIMEOUT_S] and judge.VERDICT_TIMEOUT_S == 200.0


def test_a_successor_is_relayed_again(env):
    run = _judged(env, go_results=["changed", "changed"])
    assert _click(env, run) == copy_text.DONE_ACTION.format(outcome="done", generation=2)
    posts = _verdict_posts(env, run)
    assert len(posts) == 3 and len({p.path for p in posts}) == 3
    assert len(env.casa.judgements()) == 3 and len(env.app.dispatches) == 1


def test_no_loop_past_three_judgements(env):
    run = _judged(env, go_results=["changed"] * 5)
    assert _click(env, run) == copy_text.PAGE_KEEPS_CHANGING
    assert len(env.casa.judgements()) == 3 and len(_verdict_posts(env, run)) == 3
    assert env.app.dispatches == []


def test_lost_reply_of_a_successor_replays_the_same_successor(env):
    """A lost reply that carried a successor: the re-post gets the same successor
    (the app's stored response), which is relayed once."""
    run = _judged(env, go_results=["changed"], lose_verdict_replies=1)
    assert _click(env, run) == copy_text.DONE_ACTION.format(outcome="done", generation=2)
    posts = _verdict_posts(env, run)
    assert [p.path for p in posts][:2] == [posts[0].path] * 2 and len(posts) == 3
    assert len(env.casa.judgements()) == 2 and len(env.app.dispatches) == 1


@pytest.mark.parametrize("verdict, go_result, sentence", [
    ({"verdict": "ask", "category": "payment", "reason": "It pays."}, None,
     copy_text.STEP_ASKED),
    ({"verdict": "go", "category": "account_change", "reason": "It saves."}, None,
     copy_text.STEP_ASKED_PLAN),
    ({"verdict": "look_first", "category": "reading", "reason": "r",
      "look_first": "Open the\nMarch row first."}, None,
     copy_text.STEP_LOOK_FIRST.format(note="Open the March row first.")),
    (GO, "moved", copy_text.STEP_MOVED),
    (GO, "blocked", copy_text.STEP_NOT_RUN.format(reason="blocked")),
    (GO, "unknown", copy_text.STEP_OUTCOME_UNKNOWN),
    (GO, "lapsed", copy_text.STEP_LAPSED.format(reason="changed")),
])
def test_every_outcome_has_its_sentence(env, verdict, go_result, sentence):
    run = _judged(env, go_results=[go_result] if go_result else [])
    env.casa.judge_script = [verdict]
    assert _click(env, run) == sentence


def test_asked_tells_fido_to_post_the_question(env):
    """An asked step names post_question (and show_screenshot with the question id
    when the question has a picture); run_wait and continue_run say the question has no card."""
    run = _judged(env)
    env.casa.judge_script = [{"verdict": "ask", "category": "payment", "reason": "It pays."}]
    assert _click(env, run) == copy_text.STEP_ASKED
    qid = env.app.runs[run]["pending_question"]
    assert env.app.runs[run]["state"] == "waiting_approval"
    text = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert copy_text.POST_CARD_FIRST in text and "post_question" in text
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.QUESTION_WAITING.format(question_id=qid) in text
    # a question with a picture: its screenshot first
    env.app.question_shot = "shot9"
    env.casa.judge_script = [{"verdict": "ask", "category": "payment", "reason": "It pays.",
                              "question": {"text": "Pay?", "confirm_label": "Pay",
                                           "extra_options": [], "screenshot": "required",
                                           "highlight": [], "text_incomplete": False}}]
    env.app.runs[run]["state"] = "running"
    qid2 = None
    text = _click(env, run)
    qid2 = env.app.runs[run]["pending_question"]
    assert text == copy_text.STEP_ASKED and qid2 != qid     # the card carries the picture
    # a waiting question with no card still asks for its card
    assert copy_text.POST_CARD_FIRST in env.ok("run_wait", run_id=run, max_wait_s=1)


def test_judged_download_keeps_the_file(env):
    run = _judged(env)
    text = env.ok("run_download", run_id=run, handle="e1")
    fid = state._connect().execute("SELECT file_id FROM files").fetchone()[0]
    assert text == copy_text.DOWNLOADED.format(safe_name="invoice-march.pdf",
                                               mime="application/pdf", size=13, file_id=fid)
    assert state.file_run(fid) == run and len(env.casa.judgements()) == 1


@pytest.mark.parametrize("reason, sentence", [
    ("blocked", copy_text.DOWNLOAD_BLOCKED)])
def test_judged_download_refusals_keep_their_sentence(env, reason, sentence):
    run = _judged(env, go_results=[reason])
    assert env.ok("run_download", run_id=run, handle="e1") == f"{sentence} ({reason})"


def test_judged_search_renders_the_observation(env):
    run = _judged(env)
    text = env.ok("run_search", run_id=run, query="kettle")
    assert text.startswith("page p1 generation 2") and '[e1] link "Invoices"' in text
    assert len(env.casa.judgements()) == 1


def test_a_signed_in_address_observes_after_its_check(env):
    """A verdict's `done` for an address on a signed-in site carries no page: the tool reads one,
    the way `run_observe` does, and the model gets the page, not a bare done."""
    run = _judged(env, search_is_signed_in_address=True)
    text = env.ok("run_search", run_id=run, query="https://a.example/invoices")
    assert text.startswith("page p1 generation 2") and '[e1] link "Invoices"' in text
    assert len(env.casa.judgements()) == 1 and len(env.app.dispatches) == 1
    assert [c.body for c in env.app.posts("/observe")] == [{"sign_in": True}]


def test_the_tool_says_an_address_opens_in_any_run():
    import tooling
    import tools_run  # noqa: F401 — registers the tools
    said = tooling.TOOLS["run_search"]["description"]
    assert "exactly one https address" in said and "any run" in said and "check" in said


@pytest.mark.parametrize("script, code", [(["wrong_sha"], "digest_mismatch")])
def test_typed_verdict_errors_are_not_reposted(env, script, code):
    run = _judged(env)
    env.casa.judge_script = script
    assert _click(env, run) == copy_text.BY_CODE[code] + f" ({code})"
    assert len(_verdict_posts(env, run)) == 1 and env.app.dispatches == []


def test_stale_judgement_is_said_plainly(env, monkeypatch):
    run = _judged(env)
    import judge
    real = judge.ask_check

    def superseded(judgement, digest, picture=None):
        env.app.pending.pop(run)                     # a newer command replaced it
        return real(judgement, digest, picture)
    monkeypatch.setattr(judge, "ask_check", superseded)
    text = _click(env, run)
    assert text == copy_text.BY_CODE["judgement_stale"] + " (judgement_stale)"
    assert len(_verdict_posts(env, run)) == 1


def test_no_check_past_the_turn_bound(env, monkeypatch):
    import continuation
    run = _judged(env)
    calls = []

    def bound(run_id=None):
        calls.append(run_id)
        return copy_text.END_TURN_BOUND if len(calls) > 1 else None
    monkeypatch.setattr(continuation, "check_turn_bound", bound)
    assert _click(env, run) == copy_text.END_TURN_BOUND
    assert env.casa.judgements() == [] and _verdict_posts(env, run) == []


def _schema_members(schema) -> set[str]:
    out = set()
    if isinstance(schema, dict):
        for key, value in schema.get("properties", {}).items():
            out.add(key)
            out |= _schema_members(value)
        out |= _schema_members(schema.get("items"))
    return out


def test_relay_never_takes_model_input(env):
    """No tool lets the model pass a verdict, a digest, a packet or a judgement:
    the schemas carry no such member, and one passed anyway is refused before
    any call."""
    import browser_server  # noqa: F401
    import tooling
    banned = ("verdict", "packet", "digest", "sha", "judgement")
    for name in ("run_act", "run_download", "run_search"):
        members = _schema_members(tooling.TOOLS[name]["schema"])
        assert not [m for m in members if any(b in m.lower() for b in banned)], name
        assert tooling.TOOLS[name]["schema"]["additionalProperties"] is False
    run = _judged(env)
    for member in ("verdict", "packet_sha256", "spec_digest", "judgement", "packet"):
        for name, args in (("run_act", {"kind": "click", "handle": "e1", "generation": 1}),
                           ("run_download", {"handle": "e1"}),
                           ("run_search", {"query": "q"})):
            text, is_error = env.call(name, run_id=run, **args, **{member: {"verdict": "go"}})
            assert is_error and copy_text.UNKNOWN_ARGUMENT in text
    assert env.app.calls == [] and env.casa.calls == []


def test_step_wire_matches_the_app(casa):
    """The step outcomes and the two new error codes, as Casa Browser builds them."""
    import subprocess
    import sys as _sys
    import app_client
    import wire
    opt = casa / "casa_browser" / "rootfs" / "opt"
    prog = (f"import sys, json\nsys.path.insert(0, {str(opt)!r})\n"
            "from casa_browser import protocol, gate\n"
            "print(json.dumps({'outcomes': list(protocol.STEP_OUTCOMES), "
            "'stale': protocol.ERROR_STATUS.get('judgement_stale'), "
            "'mismatch': protocol.ERROR_STATUS.get('digest_mismatch'), "
            "'note': gate.MAX_NOTE, 'codes': sorted(protocol.ERROR_STATUS)}))")
    out = subprocess.run([_sys.executable, "-I", "-c", prog], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["outcomes"] == list(wire.STEP_OUTCOMES)
    assert (got["stale"], got["mismatch"]) == (409, 409)
    assert got["note"] <= wire.MAX_NOTE
    assert set(got["codes"]) == app_client.APP_CODES


# -- wakes ---------------------------------------------------------------

def _continue(env, run):
    """A fresh continuation turn (a new plugin process: a new lease and turn identity)."""
    import tools_run
    state._connect().execute("DELETE FROM leases").connection.close()
    import continuation
    tools_run._WOKEN.clear()
    continuation.TURN_STARTED_AT = time.time()
    return env.ok("continue_run", run_ref=run)


def test_a_continuation_that_changed_nothing_ends_the_run_at_the_next_wake(env):
    """When the run stays running while Fido only retries a refused request, the sweep
    would wake a fresh turn every minute, each with its own report. A continuation finding
    that the previous one left the run running with nothing happening on it ends the run."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    first = _continue(env, run)                      # the sweep's first wake: drives as usual
    assert copy_text.STALLED not in first and env.app.runs[run]["state"] == "running"
    env.app.calls.clear()
    second = _continue(env, run)                     # nothing happened since
    assert copy_text.STALLED in second
    assert env.app.runs[run]["state"] == "cancelled"
    assert [c.path for c in env.app.calls if c.method == "POST"] == [f"/v1/runs/{run}/cancel"]
    assert state.run_binding(run) is not None and state.last_wake(run) is None


@pytest.mark.parametrize("step", ["act", "search", "new_event"])
def test_a_continuation_that_did_something_is_not_stopped(env, step):
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    _continue(env, run)
    if step == "act":
        env.ok("run_act", run_id=run, kind="click", handle="e1", generation=1)
    elif step == "search":
        env.ok("run_search", run_id=run, query="pizza form")
    else:
        env.app.add_event(run, "action", text_masked="Action: click.")
        env.ok("run_wait", run_id=run, max_wait_s=1)
    assert copy_text.STALLED not in _continue(env, run)
    assert env.app.runs[run]["state"] == "running"


def test_a_run_waiting_on_the_operator_is_never_stopped_for_stalling(env):
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    _continue(env, run)
    env.app.set_state(run, "waiting_live")
    _continue(env, run)
    env.app.set_state(run, "running")                 # Done pressed: the run carries on
    assert copy_text.STALLED not in _continue(env, run)
    assert env.app.runs[run]["state"] == "running"


def test_a_live_view_done_between_two_wakes_is_not_a_stall(env):
    """The continuation asks for a live view and ends; the sweep leaves the run
    alone while the link waits; the operator finishes and presses Done; the run is running
    again and the next wake must not end it (the app recorded live_requested and live_done)."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    _continue(env, run)
    env.ok("request_live_view", run_id=run, purpose="assist", reason="The form needs a person.")
    env.app.add_event(run, "live_done", outcome="running")   # controller.live_done
    env.app.set_state(run, "running")
    assert copy_text.STALLED not in _continue(env, run)
    assert env.app.runs[run]["state"] == "running"


def test_a_question_answered_between_two_wakes_is_not_a_stall(env):
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    _continue(env, run)
    env.app.add_event(run, "action", outcome="executed")     # the app records the operator's answer
    env.app.set_state(run, "running")                # answered; the run carries on
    assert copy_text.STALLED not in _continue(env, run)


def test_a_capped_step_listing_never_reads_as_a_stall(env):
    """The app lists at most 200 steps per fetch: a mark armed at a number read from a capped
    page sees later steps, never none."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    for _ in range(3):
        env.app.add_event(run, "action", text_masked="Action: click.")
    state.record_wake(run, time.time() - 60, 1)      # as if read from a capped first page
    assert copy_text.STALLED not in _continue(env, run)
    assert state.last_wake(run)[1] == 3


def test_more_than_a_page_of_steps_still_stalls(env):
    """The app lists 200 steps per fetch; the mark is armed at the
    run's real last step, however many pages, so a run with a long history that stops still
    ends."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    for _ in range(10_450):                         # more than 50 pages of 200
        env.app.add_event(run, "action", text_masked="Action: scroll.")
    env.app.event_page = 200
    _continue(env, run)
    assert state.last_wake(run)[1] == 10_450
    # Steps stamped in the arming turn's own second count for it once, and adding 10,450 of them
    # can cross a second, so which of these two wakes ends the run is not fixed.
    assert any(copy_text.STALLED in _continue(env, run) for _ in range(2))


@pytest.mark.parametrize("reply", ["stale_handle", "blocked"])
def test_a_step_refused_by_the_app_still_counts_as_sent(env, reply):
    """A reply that is an error can follow a step that changed the page
    (a search that navigated, then failed to read); a step sent is never a stall."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    _continue(env, run)
    env.app.overrides[("POST", f"/v1/runs/{run}/search")] = (
        409 if reply == "stale_handle" else 403, {"error": reply}, "application/json")
    env.call("run_search", run_id=run, query="pizza form")
    assert copy_text.STALLED not in _continue(env, run)


def test_a_keep_going_tap_between_two_wakes_is_not_a_stall(env):
    """The operator's Keep going tap records no step in the app."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    _continue(env, run)
    # a keep-going card already on screen at rollout (a new card's Keep going
    # only closes it, and reaches no one)
    env.call("apply_tap", t=state.mint_tap("keep_going", {"run_id": run}, "keep-legacy"))
    assert copy_text.STALLED not in _continue(env, run)
    assert env.app.runs[run]["state"] == "running"


def test_a_tap_while_a_wake_reads_the_steps_counts_for_the_next_wake(env, monkeypatch):
    """A tap racing a wake is never forgotten: it is a touch
    after the turn started, whenever it lands while that wake records itself."""
    import tools_run
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    real = tools_run.wire.parse_events
    reads = []

    def tap_meanwhile(reply):
        reads.append(1)
        if len(reads) == 2:                          # the wake's paging of the steps
            state.touch_run(run)                     # apply_tap on the run's Keep going card
        return real(reply)
    monkeypatch.setattr(tools_run.wire, "parse_events", tap_meanwhile)
    assert copy_text.STALLED not in _continue(env, run)
    monkeypatch.setattr(tools_run.wire, "parse_events", real)
    assert len(reads) >= 2
    assert copy_text.STALLED not in _continue(env, run)
    assert env.app.runs[run]["state"] == "running"


def test_progress_before_the_turns_first_continue_run_counts(env):
    """A first turn starts the run, searches, then calls continue_run; the
    search happened in that turn, so the next wake keeps the run."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    env.ok("run_search", run_id=run, query="pizza form")
    env.ok("continue_run", run_ref=run)              # same (first) turn
    assert copy_text.STALLED not in _continue(env, run)
    assert env.app.runs[run]["state"] == "running"

def test_a_cancel_that_does_not_reach_the_app_keeps_the_mark(env):
    """When the cancel fails (the app answers an error, or the turn bound stops the
    request), the mark is put back as it was, so the next wake ends the run."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    _continue(env, run)
    armed = state.last_wake(run)
    env.app.fail_next[("POST", f"/v1/runs/{run}/cancel")] = 1
    import continuation
    import tools_run
    tools_run._WOKEN.clear()                         # the next turn
    continuation.TURN_STARTED_AT = time.time()
    env.call("continue_run", run_ref=run)
    assert env.app.runs[run]["state"] == "running" and state.last_wake(run) == armed
    assert copy_text.STALLED in _continue(env, run)
    assert env.app.runs[run]["state"] == "cancelled"


def test_continue_run_called_twice_in_one_turn_is_not_a_stall(env):
    """A turn may call continue_run again; only a whole turn passing counts."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    _continue(env, run)
    env.ok("run_search", run_id=run, query="pizza form")
    _continue(env, run)                              # the next turn arms the mark
    for _ in range(2):                               # and calls continue_run again
        assert copy_text.STALLED not in env.ok("continue_run", run_ref=run)
    assert env.app.runs[run]["state"] == "running"
    assert copy_text.STALLED in _continue(env, run)  # a whole turn with nothing: ended


@pytest.mark.parametrize("progress", ["search", "keep_going"])
def test_progress_then_continue_run_again_in_the_same_turn_is_not_a_stall(env, progress):
    """A turn that made progress and then called continue_run again must not have
    re-armed the mark it cleared."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    _continue(env, run)
    if progress == "search":
        env.ok("run_search", run_id=run, query="pizza form")
    else:
        # a legacy keep-going card's tap
        env.call("apply_tap", t=state.mint_tap("keep_going", {"run_id": run}, "keep-legacy"))
    env.ok("continue_run", run_ref=run)              # the same turn, again
    assert copy_text.STALLED not in _continue(env, run)
    assert env.app.runs[run]["state"] == "running"


def test_an_operator_step_earlier_in_the_waking_turn_counts(env):
    """In one turn the run starts, a live view is asked for, the operator presses
    Done (seen through run_wait) and continue_run is called; those app steps happened in that
    turn, before its wake, so the next wake keeps the run."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    env.app.add_event(run, "live_requested", outcome="assist")   # recorded by the app
    env.app.add_event(run, "live_done", outcome="running")
    env.ok("continue_run", run_ref=run)              # the same (first) turn
    assert copy_text.STALLED not in _continue(env, run)
    assert env.app.runs[run]["state"] == "running"


def test_a_wake_pages_on_from_the_previous_wake(env):
    """With a long history, a wake pages from the previous wake's number, not
    from the top of the listing."""
    (run,) = admitted_run(env, mode="open_web", sites=[])
    env.app.set_state(run, "running")
    for _ in range(2_600):
        env.app.add_event(run, "action", text_masked="Action: scroll.")
    env.app.event_page = 200
    _continue(env, run)
    env.ok("run_search", run_id=run, query="pizza form")    # progress in that turn
    env.app.calls.clear()
    assert copy_text.STALLED not in _continue(env, run)
    gets = [c for c in env.app.calls if c.path.endswith("/events")]
    assert len(gets) <= 3                            # the top page and one read past the wake
    assert copy_text.STALLED in _continue(env, run)  # then idle: ended


# -- an interruption of a run Fido drives is told by Fido, not by a late notice ------

def test_an_interruption_notice_of_fidos_own_run_is_not_posted(env):
    (run,) = admitted_run(env)
    young = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    interrupted = "A browser run was interrupted and was not resumed."
    env.app.notices = [
        {"seq": 1, "kind": "run_interrupted", "run_id": run, "text_masked": interrupted,
         "at": young},
        {"seq": 2, "kind": "credential_unknown", "run_id": run, "site_id": "s1",
         "text_masked": "A password you changed may not be in the vault.", "at": young},
        {"seq": 3, "kind": "run_interrupted", "run_id": "run_" + "f" * 16,
         "text_masked": interrupted, "at": young}]
    out = json.loads(env.ok("browser_status"))
    assert out["notice"] and env.casa.deposits[-1]["value"] == (
        "Casa Browser:\n- A password you changed may not be in the vault.\n- " + interrupted)
    env.casa.delivery[out["notice"]] = {"status": "delivered", "repeat": False}
    env.app.notices = env.app.notices[:1]
    assert json.loads(env.ok("browser_status"))["notice"] is None


def test_only_its_own_interruption_notices_pass_over(env):
    (run,) = admitted_run(env)
    env.app.notices = [{"seq": 1, "kind": "run_interrupted", "run_id": run,
                        "text_masked": "A browser run was interrupted and was not resumed."}]
    out = json.loads(env.ok("browser_status"))
    assert out["notice"] is None and state.notice_cursor() == 1 and env.casa.deposits == []


def test_a_finished_run_reminds_fido_to_speak_to_the_operator(env):
    """The closing answer reaches the operator themselves through their assistant: "you",
    never "the operator"."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    out = json.loads(env.ok("run_finish", run_id=run, summary="s", file_ids=[], source_ids=[],
                            expectation_status="matched"))
    assert copy_text.SPEAK_TO_THE_OPERATOR in out["note"]


def test_a_finished_run_for_a_specialist_has_no_reminder(env):
    env.casa.requester["requester_kind"] = "specialist"
    env.casa.requester["chain"] = ["assistant", "finance", "browser"]
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    out = json.loads(env.ok("run_finish", run_id=run, summary="s", file_ids=[], source_ids=[],
                            expectation_status="matched"))
    assert copy_text.SPEAK_TO_THE_OPERATOR not in out["note"]


def test_a_confirmation_card_for_a_specialist_ends_a_first_turn_with_a_line(env):
    """Only the operator's assistant ends silently after the start card; a specialist
    that asked still hears what the run waits for."""
    env.casa.requester["requester_kind"] = "specialist"
    env.casa.requester["chain"] = ["assistant", "finance", "browser"]
    env.casa.admit_mode = "pending"
    text = env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                       "sites": [{"site_id": "s1", "access": "account"}],
                                       "task_text": "t", "result_expectation": ""}])
    assert text == f"{copy_text.CONFIRMATION_PENDING} {copy_text.CONFIRMATION_LINE}"
    assert "<silent/>" not in text


def test_observing_a_run_still_starting_never_says_the_setup_is_unfinished(env):
    """An observation right after the start card's Yes can meet a run still
    starting (the app's not_ready); Fido is not told "Casa Browser is still being set up".
    A run route's not_ready is about the run: wait, then go on."""
    (run,) = admitted_run(env)
    env.app.overrides[("POST", f"/v1/runs/{run}/observe")] = (
        503, {"error": "not_ready"}, "application/json")
    text = env.ok("run_observe", run_id=run)
    assert text == copy_text.RUN_NOT_READY + " (not_ready)"
    assert copy_text.NOT_READY not in text and "still being set up" not in text
    assert "run_wait" in text
    # the app also answers not_ready for a running run whose page it could not
    # read yet, so the sentence names no cause it cannot know
    assert "starting" not in text and "waits" not in text


# -- signing in off the start page (Casa Browser 0.1.7) ------------------------------------------

def test_run_observe_asks_for_the_look_and_renders_a_plain_page(env):
    """`run_observe` posts `{"sign_in": true}`; a page Casa Browser does not look at is today's
    observation, with no check and no notice line."""
    (run,) = admitted_run(env)
    env.casa.calls.clear()
    text = env.ok("run_observe", run_id=run)
    assert env.app.observe_bodies[-1] == {"sign_in": True}
    assert text.startswith("page p1 generation 1") and env.casa.judgements() == []


@pytest.mark.parametrize("verdict, notice", [
    ({"verdict": "sign_in", "reason": "the site's login form"}, True),
    ({"verdict": "not_sign_in", "reason": "a newsletter box"}, False)])
def test_a_look_relays_exactly_one_check_and_renders_its_page(env, verdict, notice):
    """A look's `sign_in` judgement in the observe reply is relayed by the plugin's code (one
    check, the packet untouched) and the verdict's reply is rendered: its page, with the sign-in
    step Casa Browser took as one line above it."""
    (run,) = admitted_run(env)
    env.app.sign_in_look = True
    env.casa.default_verdict = verdict
    env.casa.calls.clear()
    text = env.ok("run_observe", run_id=run)
    checks = env.casa.judgements()
    assert [c["kind"] for c in checks] == ["sign_in"]
    assert env.app.sign_in_verdicts == [verdict["verdict"]]
    lines = text.splitlines()
    if notice:
        assert lines[0] == "Casa Browser signed in to Site A by itself."
        assert lines[1].startswith("page p1 generation 1")
    else:
        assert lines[0].startswith("page p1 generation 1")
        assert "signed in" not in text


def test_a_look_without_a_check_posts_no_verdict(env):
    """One retry on a look: the check failing twice posts the no-verdict body; the page is rendered."""
    (run,) = admitted_run(env)
    env.app.sign_in_look = True
    env.casa.judge_script = ["check_unavailable", "check_unavailable"]
    text = env.ok("run_observe", run_id=run)
    assert env.app.sign_in_verdicts == [None]
    assert text.startswith("page p1 generation 1")


def test_the_sign_in_kind_is_relayed():
    import casa_broker
    import wire
    assert "sign_in" in wire.JUDGEMENT_KINDS and "sign_in" in casa_broker.JUDGEMENT_KINDS
    assert "sign_in" not in wire.APP_JUDGEMENT_KINDS        # never listed as pending


def test_start_tap_continuation_knows_the_task(env):
    """After the operator's Start, the continuation's task is only Casa's fixed
    "Continue browsing run admission:<id>." and holds none of their words, so the run it starts
    must be briefed with its task by `continue_run` itself, as a continuation of a run is."""
    env.casa.admit_mode = "pending"
    env.ok("start_runs", runs=[{"mode": "account", "task_kind": "sign in and read",
                                "sites": [{"site_id": "s1", "access": "account"}],
                                "task_text": "Sign in and read the secure area.",
                                "result_expectation": "The secure area text."},
                               {"mode": "open_web", "sites": [], "task_kind": "compare",
                                "task_text": "Compare kettles.", "result_expectation": ""}])
    con = state._connect()
    (cid,) = con.execute("SELECT confirmation_id FROM admissions").fetchone()
    con.close()
    env.casa.settle_confirmation(cid, "admitted")
    text = env.ok("continue_run", run_ref=f"admission:{cid}")
    first, _second = env.app.runs
    assert f"Drive the first ({first})" in text
    assert "Task (sign in and read): Sign in and read the secure area." in text
    assert "A correct result: The secure area text." in text
    assert "Compare kettles." not in text        # the run it drives, not the next one


# -- one question for the whole job: a plan can still replace a question not yet posted ------------

ACCOUNT_ASK = {"verdict": "ask", "category": "account_change", "reason": "It edits the entry.",
               "question": {"text": "Set this entry's minimum condition to Excellent?",
                            "confirm_label": "Set it", "extra_options": [],
                            "screenshot": "none", "highlight": [], "text_incomplete": False}}
PAYMENT_ASK = {**ACCOUNT_ASK, "category": "payment"}
PLAN_ASK = {**ACCOUNT_ASK, "question": {**ACCOUNT_ASK["question"],
                                        "text": "Add these 97 cards? Nothing is bought."}}


def _ask_step(env, run, verdict, **extra):
    env.app.judge = True
    env.casa.judge_script = [verdict]
    return env.ok("run_act", run_id=run, kind="click", handle="e1",
                  generation=env.app.observation["generation"], **extra)


def test_an_account_change_question_offers_the_plan_before_it_is_posted(env):
    (run,) = admitted_run(env)
    got = _ask_step(env, run, ACCOUNT_ASK)
    assert got == copy_text.STEP_ASKED_PLAN
    assert "do not post this question" in got and "plan" in got and "post_question" in got


@pytest.mark.parametrize("why", ["payment", "approved", "plan_given", "app_silent"])
def test_the_plain_question_stays_where_a_plan_does_not_apply(env, why):
    (run,) = admitted_run(env)
    if why == "approved":
        env.app.approved[run] = "Add 1x Card."
    if why == "app_silent":
        env.app.fail_question_view = True
    got = _ask_step(env, run, PAYMENT_ASK if why == "payment" else ACCOUNT_ASK,
                    **({"plan": "Add 1x Card."} if why == "plan_given" else {}))
    assert got == copy_text.STEP_ASKED


def test_repeating_the_step_with_a_plan_replaces_the_unposted_question(env):
    (run,) = admitted_run(env)
    _ask_step(env, run, ACCOUNT_ASK)
    first = env.app.runs[run]["pending_question"]
    posts = len(env.casa.judgements())
    got = _ask_step(env, run, PLAN_ASK, plan="Add 1x Card A.\nAdd 1x Card B.")
    assert got == copy_text.STEP_ASKED                  # judged afresh, and asked with the plan
    assert len(env.casa.judgements()) == posts + 1
    assert env.app.posts("/action")[-1].body["plan"] == "Add 1x Card A.\nAdd 1x Card B."
    second = env.app.runs[run]["pending_question"]
    assert second != first and env.app.questions[first]["state"] == "declined"
    assert env.app.questions[second]["view"]["plan"] == "Add 1x Card A.\nAdd 1x Card B."
    assert env.app.answers[-1]["choice"] == "not_now"
    assert env.app.answers[-1]["operator_ref"] == "fido:replaced-by-plan"
    assert env.app.runs[run]["state"] == "waiting_approval"
    assert "Asked the operator" in env.ok("post_question", run_id=run)


def test_a_refused_retry_leaves_no_question_stuck(env):
    (run,) = admitted_run(env)
    _ask_step(env, run, ACCOUNT_ASK)
    env.casa.judge_script = []
    env.app.stale_next_action = True
    got = env.ok("run_act", run_id=run, kind="click", handle="e1",
                 generation=env.app.observation["generation"], plan="Add 1x Card.")
    assert got == copy_text.PAGE_CHANGED
    assert env.app.runs[run]["state"] == "running"
    assert env.app.runs[run]["pending_question"] is None


@pytest.mark.parametrize("why", ["posted", "payment", "approved"])
def test_a_plan_never_withdraws_a_question_it_may_not_replace(env, why):
    """The operator already has it, or it is money, or a plan already stands: the question stays."""
    (run,) = admitted_run(env)
    _ask_step(env, run, PAYMENT_ASK if why == "payment" else ACCOUNT_ASK)
    qid = env.app.runs[run]["pending_question"]
    if why == "posted":
        env.ok("post_question", run_id=run)
    if why == "approved":
        env.app.approved[run] = "Add 1x Card."
    env.casa.judge_script = []
    env.app.answers.clear()
    got = env.ok("run_act", run_id=run, kind="click", handle="e1",
                 generation=env.app.observation["generation"], plan="Add 1x Card.")
    assert got.startswith(copy_text.RUN_NOT_READY)
    assert env.app.answers == []
    assert env.app.questions[qid]["state"] == "open"
    assert env.app.runs[run]["pending_question"] == qid

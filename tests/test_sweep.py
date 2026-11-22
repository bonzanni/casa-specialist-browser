"""The driver sweep: once a minute, which runs does nothing
drive? The app's state decides first; the predicate is mechanical."""
from __future__ import annotations

import json
import time

import pytest

import continuation
import copy_text
import state
from conftest import admitted_run, casa_eval

PJ_KEYS = ("setupTool", "resultContract", "provides_tools", "driverSweep")


ASK = {"verdict": "ask", "category": "payment", "reason": "It pays the invoice.",
       "question": {"text": "Pay €84.20 for the October invoice to Utility Co?",
                    "confirm_label": "Pay €84.20", "extra_options": [],
                    "screenshot": "required", "highlight": ["€84.20"],
                    "text_incomplete": False}}


def _asked(env, run=None) -> tuple[str, str]:
    """A run whose clicked step the check asked about; returns (run, question id)."""
    if run is None:
        (run,) = admitted_run(env)
    env.app.judge = True
    env.app.question_shot = "shot1"
    env.app.screenshots[(run, "shot1")] = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
    env.casa.judge_script = [ASK]
    env.ok("run_act", run_id=run, kind="click", handle="e1",
           generation=env.app.observation["generation"])
    qid = env.app.runs[run]["pending_question"]
    assert qid and env.app.runs[run]["state"] == "waiting_approval"
    return run, qid


def _card(env, run=None, deliver=True):
    """The run's question, posted as its card; delivered and settled unless `deliver` is
    False. Returns (run, question id, the tool's answer)."""
    run, qid = _asked(env, run)
    out = json.loads(env.ok("post_question", run_id=run))
    assert out["card"], out
    if deliver:
        env.casa.deliver(out["card"])
        env.ok("browser_status")              # a Fido call settles the delivery
    return run, qid, out


def drop_leases():
    con = state._connect()
    con.execute("DELETE FROM leases")
    con.close()


def swept(env) -> list[str]:
    env.casa.calls.clear()
    before = len(env.app.calls)
    out = json.loads(env.ok("runs_needing_driver"))
    assert env.casa.calls == []                                      # no Casa call
    assert all(c.method == "GET" for c in env.app.calls[before:])     # no run action
    assert set(out) <= {"runs", "sweep_again_s", "ended"}
    return [r["run_ref"] for r in out["runs"]]


def answer(env) -> dict:
    """The sweep tool's whole answer (entries may name their deposits, and the
    answer may ask for an extra pass)."""
    return json.loads(env.ok("runs_needing_driver"))


def entry(env, run) -> dict | None:
    return next((r for r in answer(env)["runs"] if r["run_ref"] == run), None)


def test_driver_sweep_declared(casa_optional):
    from conftest import ROOT
    pj = json.loads((ROOT / "plugins/browser/.claude-plugin/plugin.json").read_text())
    assert pj["casa"]["driverSweep"] == "runs_needing_driver"
    entry = pj["casa"]["resultContract"]["tools"]["runs_needing_driver"]
    assert entry == {"result": "safe"}
    assert "mcp__plugin_browser_browser__runs_needing_driver" in pj["casa"]["provides_tools"]
    import browser_server  # noqa: F401
    import tooling
    assert tooling.TOOLS["runs_needing_driver"]["schema"]["properties"] == {}
    if casa_optional is not None:
        got = casa_eval(casa_optional, f"""
import plugin_store
m = json.loads(open({str(ROOT / "plugins/browser/.claude-plugin/plugin.json")!r}).read())
print(json.dumps(plugin_store.manifest_driver_sweep(m)))
""")
        assert got == "runs_needing_driver"


def test_sweep_returns_bindings(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    drop_leases()
    out = json.loads(env.ok("runs_needing_driver"))
    assert out == {"runs": [{"run_ref": run,
                             "binding_ref": state.run_binding(run).binding_ref}]}


@pytest.mark.parametrize("app_state, leased, expected", [
    ("running", False, True), ("running", True, False),
    ("starting", False, True), ("finishing", False, True),
    ("waiting_live", False, False),          # no link was ever sent: none can be now
    ("completed", False, True),              # an ending nothing heard is told
])
def test_predicate_table(env, app_state, leased, expected):
    (run,) = admitted_run(env)
    env.app.set_state(run, app_state)
    if not leased:
        drop_leases()
    assert (run in swept(env)) is expected


def test_waiting_approval_rows(env):
    run, qid = _asked(env)
    drop_leases()
    assert run in swept(env)                  # a waiting question with no card
    out = json.loads(env.ok("post_question", run_id=run))
    drop_leases()
    assert run in swept(env)                  # posted, delivery not yet confirmed
    env.casa.deliver(out["card"])
    env.ok("browser_status")
    drop_leases()
    assert run not in swept(env)              # its card is open and delivered: the operator's
    env.app.set_state(run, "waiting_approval", None)
    assert run in swept(env)                  # nothing pending any more


def test_turn_ends_any_way(env):
    import subprocess
    import sys
    import os
    from conftest import SERVER
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    assert run not in swept(env)              # this process holds the lease
    child = subprocess.Popen([sys.executable, "-c",
                              "import sys; sys.path.insert(0, %r); import state; "
                              "state.take_lease(%r); print('ok', flush=True); "
                              "sys.stdin.readline()" % (str(SERVER), run)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ))
    assert child.stdout.readline().strip() == "ok"
    assert run not in swept(env)              # another live turn holds it
    child.kill()
    child.wait(30)
    assert run in swept(env)                  # killed: found again


def test_redundant_continuation_is_harmless(env):
    """A second continuation finds the current state and repeats nothing."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "completed")
    drop_leases()
    for _ in range(2):
        env.app.calls.clear()
        text = env.ok("continue_run", run_ref=run)
        assert copy_text.ALREADY_FINISHED.format(state="completed") in text
        assert all(c.method == "GET" for c in env.app.calls)


def test_tap_while_driving(env):
    run, qid, out = _card(env, deliver=False)
    env.ok("apply_tap", t=env.token(env.card(), "Not now"))       # the fast path queued
    queued = [b for p, b in env.casa.calls if p.endswith("/continue")]
    assert len(queued) == 1
    assert run not in swept(env)              # still leased by the driving turn


def test_partial_submission_recovered(env, monkeypatch):
    env.app.fail_creates = {2}
    env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                "sites": [{"site_id": "s1", "access": "account"}],
                                "task_text": f"t{i}", "result_expectation": ""}
                               for i in range(2)])
    drop_leases()
    missing = [r.binding.request_id for r in state.all_intents() if r.binding.run_id is None]
    assert len(missing) == 1
    later = time.time() + 121
    monkeypatch.setattr(continuation.time, "time", lambda: later)
    refs = swept(env)
    assert f"intent:{missing[0]}" in refs
    monkeypatch.setattr(continuation.time, "time", time.time)
    before = len(env.app.posts("/v1/runs"))
    env.ok("continue_run", run_ref=f"intent:{missing[0]}")
    assert len(env.app.posts("/v1/runs")) == before + 1
    env.ok("continue_run", run_ref=f"intent:{missing[0]}")
    assert len(env.app.posts("/v1/runs")) == before + 1          # posted exactly once


def test_batch_after_crash_before_mark_terminal(env):
    r1, r2 = admitted_run(env, n=2)
    env.app.set_state(r1, "completed")        # finished in the app, plugin never heard
    drop_leases()
    assert not state.is_terminal(r1)
    refs = swept(env)
    # the plugin never heard of r1's end, so its ending is owed to whoever asked
    assert state.is_terminal(r1) and r2 in refs and r1 in refs


def test_batch_next_run(env):
    r1, r2, r3 = admitted_run(env, n=3)
    env.app.set_state(r1, "completed")
    drop_leases()
    refs = swept(env)
    assert r2 in refs and r3 not in refs      # only the first queued run of the group
    env.app.set_state(r2, "running")
    refs = swept(env)
    assert r2 in refs and r3 not in refs


def test_live_never_swept_once_link_delivered(env, monkeypatch):
    import tools_ops
    monkeypatch.setattr(tools_ops.socket, "gethostname", lambda: "91d4d4c8-casa")
    (run,) = admitted_run(env)
    out = json.loads(env.ok("request_live_view", run_id=run, purpose="login", reason="code"))
    drop_leases()
    assert run in swept(env)                   # its link is not known delivered yet
    env.casa.deliver(out["live"])
    env.ok("continue_run", run_ref=run)        # a continuation settles the delivery
    drop_leases()
    for st in ("waiting_live", "live"):
        env.app.set_state(run, st)
        assert run not in swept(env)


def test_done_resumes_via_sweep(env, monkeypatch):
    import tools_ops
    monkeypatch.setattr(tools_ops.socket, "gethostname", lambda: "91d4d4c8-casa")
    (run,) = admitted_run(env)
    out = json.loads(env.ok("request_live_view", run_id=run, purpose="login", reason="code"))
    env.casa.deliver(out["live"])
    env.ok("continue_run", run_ref=run)
    drop_leases()
    env.app.set_state(run, "live")
    assert run not in swept(env)
    env.app.set_state(run, "running")          # the operator pressed Done
    assert run in swept(env)


def test_unanswerable_card_reasked(env, monkeypatch):
    run, qid, out = _card(env)
    drop_leases()
    later = time.time() + 3700
    monkeypatch.setattr(continuation.time, "time", lambda: later)
    assert run in swept(env)                    # its hour passed: no longer the operator's


def test_ingress_answer_resumes(env):
    run, qid, out = _card(env)
    drop_leases()
    assert run not in swept(env)
    env.app.questions[qid]["state"] = "executed"
    env.app.set_state(run, "running")           # answered on the ingress page
    assert run in swept(env)
    env.ok("continue_run", run_ref=run)
    assert state.open_card(run, time.time(), "question") is None       # retired


def test_keep_going_card_never_holds(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    env.ok("ask_keep_going", run_id=run, done_text="")
    drop_leases()
    assert run in swept(env)


def test_withheld_card_reasked(env):
    run, qid, out = _card(env, deliver=False)
    env.casa.deliver(out["card"], "withheld")
    env.ok("browser_status")                     # a Fido call settles the delivery
    drop_leases()
    assert run in swept(env)
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.QUESTION_WITHHELD in text


def test_admission_rows_swept(env, monkeypatch):
    env.casa.admit_mode = "pending"
    env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                "sites": [{"site_id": "s1", "access": "account"}],
                                "task_text": "t", "result_expectation": ""}])
    (adm,) = state.open_admissions()
    t0 = time.time()
    for offset, expect in ((60, False), (3600, False), (3901, True), (3960, False),
                           (3901 + 3601, True)):
        monkeypatch.setattr(continuation.time, "time", lambda o=offset: t0 + o)
        assert (f"request:{adm.request_key}" in swept(env)) is expect, offset


def test_requested_row_swept_and_recovered_once(env, monkeypatch):
    """Run admission's answer was lost (the row stays `requested`): the sweep returns
    the request; replaying it with the same key returns the original answer
    (no second card); the operator's tap on the ORIGINAL card then submits
    each run exactly once."""
    import casa_broker
    env.casa.admit_mode = "pending"
    real = casa_broker.run_admission
    monkeypatch.setattr(casa_broker, "run_admission",
                        lambda *a, **k: (real(*a, **k), (_ for _ in ()).throw(
                            casa_broker.BrokerFailed("broker_unreachable:Timeout")))[1])
    env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                "sites": [{"site_id": "s1", "access": "account"}],
                                "task_text": "t", "result_expectation": ""}])
    monkeypatch.setattr(casa_broker, "run_admission", real)
    (adm,) = state.open_admissions()
    assert adm.status == "requested"
    later = time.time() + 121
    monkeypatch.setattr(continuation.time, "time", lambda: later)
    assert f"request:{adm.request_key}" in swept(env)
    monkeypatch.setattr(continuation.time, "time", time.time)
    text = env.ok("continue_run", run_ref=f"request:{adm.request_key}")
    assert copy_text.CONFIRMATION_PENDING in text
    cids = {a["confirmation_id"] for a in env.casa.by_key.values()}
    assert len(cids) == 1                                          # one card only
    (cid,) = cids
    env.casa.settle_confirmation(cid, "admitted")
    env.ok("continue_run", run_ref=f"admission:{cid}")
    env.ok("continue_run", run_ref=f"admission:{cid}")
    assert len(env.app.posts("/v1/runs")) == 1


def test_unknown_file_stays_swept(env, monkeypatch):
    (run,) = admitted_run(env)
    fid = env.app.add_file(run, b"%PDF-1.4", "a.pdf", "application/pdf")
    state.put_file(fid, run, "a.pdf", "application/pdf", 8)
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.deliver(ref)                      # it landed …
    env.casa.delivery[ref] = {"status": "unknown", "repeat": False}   # … a restart forgot
    env.ok("browser_status")                   # settled unknown; the turn then dies
    env.app.set_state(run, "completed")
    drop_leases()
    later = time.time() + 121
    monkeypatch.setattr(continuation.time, "time", lambda: later)
    assert run in swept(env)
    monkeypatch.setattr(continuation.time, "time", time.time)
    ref2 = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.ok("browser_status")
    assert env.casa.delivery[ref2]["status"] == "delivered"
    assert len(env.casa.posted) == 1           # posted at most once
    env.ok("continue_run", run_ref=run)        # tells the ending nothing told
    monkeypatch.setattr(continuation.time, "time", lambda: later + 200)
    drop_leases()
    assert run not in swept(env)


def test_unsettled_live_link_swept(env, monkeypatch):
    import tools_ops
    monkeypatch.setattr(tools_ops.socket, "gethostname", lambda: "91d4d4c8-casa")
    (run,) = admitted_run(env)
    out = json.loads(env.ok("request_live_view", run_id=run, purpose="login", reason="code"))
    env.casa.deliver(out["live"], "withheld")
    drop_leases()
    assert run in swept(env)                    # still `pending` in plugin state
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.LIVE_UNSENT in text        # settled `withheld`: tell them where it waits
    drop_leases()
    assert run not in swept(env)                # told once: nothing wakes it again


def test_sweep_cap_and_unreachable_app(env, monkeypatch):
    env.casa.admit_mode = "pending"
    for i in range(3):
        env.ok("start_runs", runs=[{"mode": "account", "task_kind": "k",
                                    "sites": [{"site_id": "s1", "access": "account"}],
                                    "task_text": f"t{i}", "result_expectation": ""}])
    env.app.hello["protocol_version"] = 2      # the app cannot be read
    later = time.time() + 4000
    monkeypatch.setattr(continuation.time, "time", lambda: later)
    refs = swept(env)
    assert len(refs) == 3 and all(r.startswith("request:") for r in refs)


def test_unacknowledged_outcome_stays_swept(env, monkeypatch):
    """A final file outcome the app was never told keeps the run swept, even
    after it ended, until a continuation tells it."""
    (run,) = admitted_run(env)
    fid = env.app.add_file(run, b"%PDF-1.4", "a.pdf", "application/pdf")
    state.put_file(fid, run, "a.pdf", "application/pdf", 8)
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.deliver(ref, "withheld")
    env.app.fail_next[("POST", f"/v1/control/files/{fid}/delivery")] = 5
    env.ok("browser_status")                    # settles withheld; the report fails
    env.app.set_state(run, "completed")
    drop_leases()
    assert run in swept(env)
    env.app.fail_next = {}
    env.ok("continue_run", run_ref=run)          # tells the app
    assert env.app.deliveries[-1]["outcome"] == "withheld"
    drop_leases()
    assert run not in swept(env)


# -- a card holds a run only once Casa confirmed its delivery ------------------

def test_unconfirmed_card_does_not_hold(env):
    run, qid, out = _card(env, deliver=False)
    drop_leases()
    assert run in swept(env)                  # delivery unconfirmed: a turn must settle it
    env.casa.deliver(out["card"])
    env.ok("continue_run", run_ref=run)       # settles `delivered`
    drop_leases()
    assert run not in swept(env)


def test_withheld_without_a_later_call_is_found(env):
    run, qid, out = _card(env, deliver=False)
    env.casa.deliver(out["card"], "withheld")  # the turn ended before any delivery check
    drop_leases()
    assert run in swept(env)
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.QUESTION_WITHHELD in text


def test_card_killed_before_its_record(env):
    """A card group minted and deposited, its process killed before the
    delivery was recorded: it never counts as open."""
    import app_client
    import cards
    import tools_ops
    run, qid = _asked(env)
    q = tools_ops._question(app_client.compatible_client(), run, qid)
    cards.question_card(run, q, "q-orphan", confirm=True)     # minted, never recorded
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.QUESTION_OPEN not in text
    assert copy_text.QUESTION_WAITING in text
    drop_leases()
    assert run in swept(env)


def test_withheld_file_survives_a_killed_settlement(env, monkeypatch):
    """A delivery's `withheld` was saved, then the process died before any result
    named it: the run (ended or not) stays swept until a continuation names
    the file, with its ids, to the model."""
    (run,) = admitted_run(env)
    fid = env.app.add_file(run, b"%PDF-1.4", "a.pdf", "application/pdf")
    state.put_file(fid, run, "a.pdf", "application/pdf", 8)
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    state.settle_delivery(ref, "withheld")       # saved … and the process died here
    env.app.set_state(run, "completed")
    drop_leases()
    assert run in swept(env)
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.FILE_WITHHELD.format(name="a.pdf", run_id=run, file_id=fid) in text
    drop_leases()
    assert run not in swept(env)                  # named once: no endless loop
    # every later result for this binding still names it until it is sent again
    note = json.loads(env.ok("browser_status"))["note"]
    assert copy_text.FILE_WITHHELD.format(name="a.pdf", run_id=run, file_id=fid) in note


def test_a_live_view_opened_from_a_question_is_the_operators(env):
    """The operator opens a question's live view on Casa Browser's page (no link was
    sent): the sweep leaves the run alone, and a turn on it is told no link is owed; without
    the question the old rule stands."""
    (run,) = admitted_run(env)
    drop_leases()
    for st in ("waiting_live", "live"):
        env.app.set_state(run, st, "q1")
        assert run not in swept(env)
        out = env.ok("run_wait", run_id=run, max_wait_s=1)
        assert copy_text.SEND_LINK_FIRST not in out
    env.app.set_state(run, "waiting_live", None)
    assert run not in swept(env)
    assert copy_text.SEND_LINK_FIRST in env.ok("run_wait", run_id=run, max_wait_s=1)


def test_in_the_view_no_link_is_owed_even_after_its_question_expired(env):
    """A view opened from a question outlives the question's expiry; while the
    operator is in it nothing wakes Fido to send a link."""
    (run,) = admitted_run(env)
    drop_leases()
    env.app.set_state(run, "live", None)
    assert run not in swept(env)
    assert copy_text.LIVE_WAITING in env.ok("continue_run", run_ref=run)


def test_a_withheld_live_card_does_not_wake_a_waiting_view(env):
    """The tap_live path's re-posted card (qlive) that Casa
    withheld wakes nothing; a turn cannot send it again while the run waits for the view (the app takes a
    live request only from a running run), so nothing wakes it: the view waits for the operator on
    Casa Browser's page and its question's hour ends it."""
    (run,) = admitted_run(env)
    state.put_delivery("ref-qlive-1", "qlive", run, None, None, "q-grp")
    state.settle_delivery("ref-qlive-1", "withheld")
    drop_leases()
    env.app.set_state(run, "waiting_live", "q1")
    assert run not in swept(env)


# -- Casa confirms a hand-over's delivery; a live view is looked at again soon -------

def test_an_unconfirmed_card_names_its_deposit_for_casa(env):
    """The card was posted but this plugin has not asked Casa what became of it: the run is
    returned with the card's reference, which Casa (that delivered it) checks."""
    run, qid, out = _card(env, deliver=False)
    drop_leases()
    assert entry(env, run) == {"run_ref": run, "binding_ref": state.run_binding(run).binding_ref,
                               "unless_delivered": [out["card"]]}


@pytest.mark.parametrize("how", ["expired", "answered", "withheld", "other_question"])
def test_a_card_that_cannot_hold_names_nothing(env, monkeypatch, how):
    run, qid, out = _card(env, deliver=False)
    drop_leases()
    if how == "expired":
        later = time.time() + 3700
        monkeypatch.setattr(continuation.time, "time", lambda: later)
    elif how == "answered":
        env.ok("apply_tap", t=env.token(env.card(), "Not now"))
        drop_leases()
    elif how == "withheld":
        env.casa.deliver(out["card"], "withheld")
        env.ok("browser_status")              # settled: withheld
        drop_leases()
    else:
        env.app.runs[run]["pending_question"] = "q-newer"   # another question supersedes it
    e = entry(env, run)
    assert e is not None and "unless_delivered" not in e


def test_an_unconfirmed_live_link_names_its_deposit(env, monkeypatch):
    import tools_ops
    monkeypatch.setattr(tools_ops.socket, "gethostname", lambda: "91d4d4c8-casa")
    (run,) = admitted_run(env)
    out = json.loads(env.ok("request_live_view", run_id=run, purpose="assist",
                            reason="Set the time"))
    drop_leases()
    assert entry(env, run)["unless_delivered"] == [out["live"]]
    env.casa.deliver(out["live"], "withheld")
    env.ok("continue_run", run_ref=run)       # settled withheld: told once, never woken again
    drop_leases()
    assert entry(env, run) is None


def test_a_held_run_that_owes_a_file_is_woken_whatever_casa_delivered(env, monkeypatch):
    """A run may be held by its card and still owe a file outcome: the file need wins."""
    (run,) = admitted_run(env)
    fid = env.app.add_file(run, b"%PDF-1.4", "a.pdf", "application/pdf")
    state.put_file(fid, run, "a.pdf", "application/pdf", 8)
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.deliver(ref, "withheld")
    env.app.fail_next[("POST", f"/v1/control/files/{fid}/delivery")] = 5
    env.ok("browser_status")                  # settles withheld; the report fails
    _card(env, run, deliver=False)
    drop_leases()
    e = entry(env, run)
    assert e is not None and "unless_delivered" not in e


def test_an_open_live_view_asks_for_an_extra_pass(env, monkeypatch):
    """The operator's Done reaches no one: while they are in the view, the sweep asks Casa to look
    again in five seconds; Done (the run moves on) is then found within seconds."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "live")
    drop_leases()
    out = answer(env)
    assert out.get("sweep_again_s") == 5 and run not in [r["run_ref"] for r in out["runs"]]
    env.app.set_state(run, "running")         # Done
    out = answer(env)
    assert "sweep_again_s" not in out and run in [r["run_ref"] for r in out["runs"]]


@pytest.mark.parametrize("st", ["waiting_live", "running", "waiting_approval", "queued"])
def test_no_extra_pass_outside_an_open_live_view(env, st):
    (run,) = admitted_run(env)
    env.app.set_state(run, st)
    drop_leases()
    assert "sweep_again_s" not in answer(env)


def test_a_live_view_a_turn_waits_on_asks_for_no_extra_pass(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "live")            # this process (a turn) holds the lease
    assert "sweep_again_s" not in answer(env)


# -- a run the app ended while nothing drove it is told by Fido, once ---------------

def test_a_run_the_app_ended_is_told_until_a_continuation_tells_it(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    drop_leases()
    env.app.set_state(run, "interrupted")     # e.g. Casa Browser restarted
    assert run in swept(env)
    assert state.is_terminal(run)             # the group moves on, as before
    assert run in swept(env)                  # a lost continuation (Casa restarted) is re-found
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.ALREADY_FINISHED.format(state="interrupted") in text
    assert copy_text.ENDED_TELL in text
    drop_leases()
    assert run not in swept(env)
    again = env.ok("continue_run", run_ref=run)      # a redundant continuation
    assert copy_text.ENDED_TOLD in again and copy_text.ENDED_TELL not in again


def test_a_run_ended_under_a_driving_turn_is_left_to_that_turn(env):
    """While a turn drives the run its end is left to that turn; a turn that never read it
    (the app ended it during the turn) leaves it owed, told once."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "interrupted")     # this process (the turn) holds the lease
    assert run not in swept(env)
    assert not state.is_terminal(run)
    drop_leases()                             # that turn ended without reading it
    assert run in swept(env)
    assert copy_text.ENDED_TELL in env.ok("continue_run", run_ref=run)
    drop_leases()
    assert run not in swept(env)


def test_an_end_the_driving_turn_read_is_not_owed(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "cancelled")       # e.g. a live view's hour, during the turn
    assert run not in swept(env)              # leased: left to the turn
    text = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert copy_text.RUN_ENDED.format(state="cancelled") in text
    drop_leases()
    assert run not in swept(env)


def test_an_ending_a_turn_read_is_not_told_again_after_that_turn(env):
    """run_wait shows the turn its run interrupted; the turn starts a replacement, which then
    succeeds. The sweep, noticing the old run only after that turn has gone, must not owe its
    ending: a continuation told "interrupted" after the success."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "interrupted")
    text = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert copy_text.RUN_ENDED.format(state="interrupted") in text
    drop_leases()                             # that turn has gone
    assert run not in swept(env)
    assert copy_text.ENDED_TOLD in env.ok("continue_run", run_ref=run)


def test_an_interrupted_reply_to_a_turns_call_is_not_told_again(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "interrupted")
    env.app.overrides[("POST", f"/v1/runs/{run}/observe")] = (
        409, {"error": "interrupted"}, "application/json")
    text = env.ok("run_observe", run_id=run)
    assert "(interrupted)" in text
    drop_leases()
    assert run not in swept(env)


def test_an_owed_ending_a_later_turn_reads_is_owed_no_more(env):
    (run,) = admitted_run(env)
    drop_leases()
    env.app.set_state(run, "interrupted")
    assert run in swept(env)                  # owed: nothing drove it
    env.ok("run_wait", run_id=run, max_wait_s=1)   # a turn reads it (and tells it)
    drop_leases()
    assert run not in swept(env)


def test_an_owed_telling_lapses_after_an_hour(env, monkeypatch):
    (run,) = admitted_run(env)
    drop_leases()
    env.app.set_state(run, "interrupted")
    assert run in swept(env)
    later = time.time() + 3700
    monkeypatch.setattr(continuation.time, "time", lambda: later)
    assert run not in swept(env)


def test_a_run_fido_finished_is_never_told_again(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    env.ok("run_finish", run_id=run, summary="s", file_ids=[], source_ids=[],
           expectation_status="matched")
    drop_leases()
    assert run not in swept(env)
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.ENDED_TOLD in text


def test_a_run_held_by_its_delivered_card_asks_for_no_extra_pass(env):
    run, qid, out = _card(env)                # delivered and settled: the operator's
    drop_leases()
    out = answer(env)
    assert run not in [r["run_ref"] for r in out["runs"]] and "sweep_again_s" not in out


def test_an_ending_is_owed_in_the_same_commit_as_its_end(env, monkeypatch):
    """A sweep killed right after it recorded the end must still owe the telling."""
    (run,) = admitted_run(env)
    drop_leases()
    env.app.set_state(run, "interrupted")
    real = state.mark_terminal

    class Killed(BaseException):
        pass

    def killed_after_commit(*a, **k):
        real(*a, **k)
        raise Killed()

    monkeypatch.setattr(state, "mark_terminal", killed_after_commit)
    with pytest.raises(Killed):
        continuation.sweep()
    monkeypatch.setattr(state, "mark_terminal", real)
    assert state.is_terminal(run) and run in swept(env)
    assert copy_text.ENDED_TELL in env.ok("continue_run", run_ref=run)


# -- a live view nobody opens costs no turn while it waits ----------------

@pytest.mark.parametrize("outcome", ["unknown", "withheld", "delivered"])
def test_a_waiting_live_view_wakes_no_turn_until_it_ends(env, monkeypatch, outcome):
    """When the live link's outcome is `unknown` (a Casa restart forgot it), Casa's
    minute sweep must not wake a Fido turn for the run every minute; each turn would be told to send
    the view again, which the app refuses while the run waits for it. Once Fido has settled the
    link, a day of minute sweeps names the run zero times; when the app ends the run (an unopened
    view lapses after an hour) it is named once, that one turn tells the ending, and nothing
    names it after."""
    import tools_ops
    monkeypatch.setattr(tools_ops.socket, "gethostname", lambda: "91d4d4c8-casa")
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    out = json.loads(env.ok("request_live_view", run_id=run, purpose="login", reason="Sign in"))
    env.casa.deliver(out["live"], outcome)                # `unknown`: Casa restarted since
    text = env.ok("continue_run", run_ref=run)            # the one turn that settles the link
    assert state.latest_link(run).status == outcome
    assert (copy_text.LIVE_UNSENT if outcome == "withheld" else copy_text.LIVE_WAITING) in text
    assert env.app.posts("/live") == [env.app.posts("/live")[0]]   # no second live request
    drop_leases()
    named = 0
    for minute in range(24 * 60):
        named += run in swept(env)
    assert named == 0
    env.app.set_state(run, "cancelled")                    # the view's hour ended the run
    assert swept(env).count(run) == 1
    told = env.ok("continue_run", run_ref=run)
    assert copy_text.ENDED_TELL in told
    drop_leases()
    assert sum(run in swept(env) for _ in range(60)) == 0


# -- v0.1.28: a request that ended on its own is named, so Casa takes its card's Stop away -----

def _ended(env) -> list[dict]:
    return answer(env).get("ended", [])


def _request(run) -> dict:
    b = state.run_binding(run)
    return {"ref": f"request:{b.request_group}", "binding_ref": b.binding_ref}


def test_a_request_whose_runs_all_ended_is_named_ended(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    drop_leases()
    assert _ended(env) == []                          # still going: its card keeps its Stop
    env.app.set_state(run, "completed")
    assert _ended(env) == [_request(run)]             # the pass that sees the end names it
    assert _ended(env) == [_request(run)]             # and every pass of the day after


@pytest.mark.parametrize("end", ["completed", "failed", "cancelled"])
def test_any_end_the_app_made_is_named(env, end):
    (run,) = admitted_run(env)
    env.app.set_state(run, end)
    drop_leases()
    assert _ended(env) == [_request(run)]


def test_a_run_finished_in_its_own_turn_is_named(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "completed")               # the turn finished it in the app
    state.mark_terminal(run)                          # and recorded the end (lease still its)
    assert state.lease_live(run, time.time())
    assert _ended(env) == [_request(run)]


def test_a_request_with_a_run_still_going_is_not_named(env):
    a, b = admitted_run(env, n=2)
    env.app.set_state(a, "completed")
    env.app.set_state(b, "running")
    drop_leases()
    assert _ended(env) == []
    env.app.set_state(b, "completed")
    assert _ended(env) == [_request(a)]               # one request, named once
    assert _request(a) == _request(b)


def test_a_request_the_operator_stopped_is_not_named(env):
    (run,) = admitted_run(env)
    env.ok("stop_runs", ref=f"request:{state.run_binding(run).request_group}")
    env.app.set_state(run, "cancelled")
    drop_leases()
    assert _ended(env) == []                          # the operator's Stop marked its card


def test_a_request_older_than_a_day_is_not_named(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "completed")
    drop_leases()
    con = state._connect()
    con.execute("UPDATE admissions SET created_at = created_at - ?",
                (continuation.ENDED_TELL_S + 60,))
    con.commit()
    con.close()
    assert _ended(env) == []


def test_more_ended_requests_than_one_answer_holds_take_turns(env, monkeypatch):
    """More ended requests than one answer holds: naming the oldest every pass would leave a
    later card's Stop in place for good. Each pass names those named longest ago, so every one is named within a few passes."""
    monkeypatch.setattr(continuation, "MAX_RESULTS", 2)
    runs = [admitted_run(env)[0] for _ in range(3)]
    for run in runs:
        env.app.set_state(run, "completed")
    drop_leases()
    want = {_request(r)["ref"] for r in runs}
    first = [e["ref"] for e in _ended(env)]
    second = [e["ref"] for e in _ended(env)]
    assert len(first) == len(second) == 2
    assert set(first) | set(second) == want            # the third is named on the next pass


def test_a_run_attached_after_the_listing_is_not_taken_for_ended(env, monkeypatch):
    """A start_runs that creates and attaches a run after the sweep has read the app's
    listing leaves that run out of the listing; its absence is not its end, and the request is
    not named ended while the run goes on. Only a run attached
    before the listing is read can be found ended by its absence."""
    import wire
    a, b = admitted_run(env, n=2)
    env.app.set_state(a, "completed")
    env.app.set_state(b, "running")
    drop_leases()
    request_b = state.run_binding(b).request_id
    con = state._connect()
    con.execute("UPDATE intents SET run_id = NULL WHERE run_id = ?", (b,))
    con.commit()
    con.close()
    real = wire.parse_active_runs

    def _listing_then_attach(raw):
        listed = [r for r in real(raw) if r["run_id"] != b]     # read before b existed
        state.attach_run(request_b, b)                           # start_runs attaches it now
        return listed
    monkeypatch.setattr(wire, "parse_active_runs", _listing_then_attach)
    assert _ended(env) == []
    assert not state.is_terminal(b)
    monkeypatch.setattr(wire, "parse_active_runs", real)
    assert _ended(env) == []                                     # b is running: still going
    env.app.set_state(b, "completed")
    assert _ended(env) == [_request(a)]


def test_a_request_whose_rows_say_ended_while_the_app_runs_it_is_not_named(env):
    """Plugin state's end flags are written from inferences (here forced wrong); a wrong flag
    must not name a request ended while its run goes on. `ended` reads each run's own
    state from the app and names the request only when every one is terminal."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    state.mark_terminal(run)                          # a wrong end flag, however it came about
    assert _ended(env) == []
    env.app.set_state(run, "completed")
    assert _ended(env) == [_request(run)]


def test_a_run_the_app_was_read_to_have_ended_is_not_read_again(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "completed")
    drop_leases()
    assert _ended(env) == [_request(run)]
    before = len(env.app.calls)
    assert _ended(env) == [_request(run)]
    assert not any(c.path == f"/v1/runs/{run}" for c in env.app.calls[before:])


def test_a_failed_read_of_a_run_names_nothing_this_pass(env):
    (run,) = admitted_run(env)
    env.app.set_state(run, "completed")
    drop_leases()
    answer(env)                                       # the pass that marks it ended
    state._connect().execute("DELETE FROM app_ended").connection.commit()
    env.app.fail_next[("GET", f"/v1/runs/{run}")] = 1
    assert _ended(env) == []
    assert _ended(env) == [_request(run)]


def test_an_answered_run_attached_after_the_listing_is_not_taken_for_ended(env, monkeypatch):
    """retry_answers does not take an answered run's absence from a listing read before it
    was attached for its end, nor mark it heard (terminal) while it runs."""
    import wire
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    drop_leases()
    request_id = state.run_binding(run).request_id
    state.record_answer("casa-cap-" + "a" * 32, run, {"expectation_status": "met"}, ended=False)
    con = state._connect()
    con.execute("UPDATE intents SET run_id = NULL WHERE run_id = ?", (run,))
    con.commit()
    con.close()
    real = wire.parse_active_runs

    def _listing_then_attach(raw):
        listed = [r for r in real(raw) if r["run_id"] != run]
        state.attach_run(request_id, run)
        return listed
    monkeypatch.setattr(wire, "parse_active_runs", _listing_then_attach)
    assert _ended(env) == []
    assert not state.is_terminal(run)

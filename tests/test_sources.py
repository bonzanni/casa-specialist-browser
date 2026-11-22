"""show_sources: open-web sources reach the operator
as links whose destination is the recorded address byte for byte; no
address ever reaches a model."""
from __future__ import annotations

import json

import pytest

import copy_text
import tools_ops
from conftest import admitted_run, casa_eval

ODD = [
    "https://bank.example/a(b)/c%28d%29?q=%2F&x='y'!*;z@w#frag_1",
    "https://news.example/x&sol;y?a=1&amp;b=2",
    "https://xn--bcher-kva.example/path_with_underscore",
]
ANSWER = "The **best** fit is the first one; the second is close."


def _sources(env, run, urls, titles=None):
    titles = titles or [f"Title {i}" for i in range(len(urls))]
    env.app.sources[run] = [{"source_id": f"src{i}", "url": u, "title_masked": t}
                            for i, (u, t) in enumerate(zip(urls, titles))]


def _links(casa, body):
    spans = casa_eval(casa, f"""
from channels.tg_richtext import parse_markdown
display, spans = parse_markdown(json.loads({json.dumps(body)!r}))
print(json.dumps([[display[a:b], k] for a, b, k in spans]))
""")
    # the answer's own formatting (bold here) is the answer's; these tests read the links
    return [s for s in spans if s[1].startswith("link:")]


def _show(env, **args):
    """show_sources (v0.1.27: it also finishes the run, so it takes expectation_status)."""
    args.setdefault("expectation_status", "matched")
    return env.ok("show_sources", **args)


def _open_web(env):
    (run,) = admitted_run(env, mode="open_web", sites=[])
    return run


def test_sources_post_lines(env, casa_optional):
    run = _open_web(env)
    _sources(env, run, ODD)
    out = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src2", "src0", "src1"]))
    assert out["sources"].startswith("casa-cap-")
    body = env.casa.deposits[-1]["value"]
    answer, sources = body.split("\n\n")
    assert answer == ANSWER                         # v0.1.26: the answer first, as the operator reads it
    lines = sources.split("\n")
    assert lines[0] == "Sources:" and len(lines) == 4
    assert lines[1].startswith("1. Title 2 — ") and lines[2].startswith("2. Title 0 — ")
    if casa_optional is not None:
        spans = _links(casa_optional, body)
        assert spans == [["xn--bcher-kva.example", "link:" + ODD[2]],
                         ["bank.example", "link:" + ODD[0]],
                         ["news.example", "link:" + ODD[1]]]


@pytest.mark.parametrize("url", ["https://a.example/[x]", "https://a.example/b\\c",
                                 "https://a.example/%zz", "https://a.example/é",
                                 "http://a.example/x", "https://", "javascript:alert(1)"])
def test_sources_unlinkable_cited_plain(env, url, casa_optional):
    """The answer is in the message, so a source that cannot be a link is cited there
    by its title and host, plain: never a link, never the address."""
    run = _open_web(env)
    _sources(env, run, [url])
    out = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))
    assert out["sources"].startswith("casa-cap-")
    body = env.casa.deposits[-1]["value"]
    line = body.split("\n")[-1]
    assert line.startswith("1. Title 0") and "://" not in line and "](" not in line
    if casa_optional is not None:
        assert [k for _, k in _links(casa_optional, body)] == []


def test_sources_none_of_this_run_sends_nothing(env):
    run = _open_web(env)
    _sources(env, run, ["https://a.example/1"])
    out = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["nope"]))
    assert out == {"sources": None, "note": copy_text.SOURCES_NONE}
    assert [d for d in env.casa.deposits if d["slot"] == "sources"] == []


def test_the_answer_and_its_sources_are_one_message(env):
    """Sources posted while the turn runs and an answer posted when it
    ends would arrive in the wrong order. One deposit carries both; the same answer and sources
    covers the answer and its sources. (The same call finishes the run, and a second call for
    the run sends nothing.)"""
    run = _open_web(env)
    _sources(env, run, ["https://a.example/1", "https://b.example/2"])
    weird = "Line one\x07 with a bell\nline two\t tabbed  "
    note = json.loads(_show(env, run_id=run, answer=weird,
                             source_ids=["src1", "src0"]))["note"]
    (dep,) = [d for d in env.casa.deposits if d["slot"] == "sources"]
    assert dep["value"].startswith("Line one  with a bell\nline two\t tabbed\n\nSources:\n1. ")
    assert note == copy_text.SOURCES_FINISHED.format(n=2, status="matched", after="",
                                                     ending=copy_text.HANDED_OVER_SILENT)
    import hashlib
    used = "src1,src0\0Line one  with a bell\nline two\t tabbed"
    assert dep["key"] == f"sources-{run}-" + hashlib.sha256(used.encode()).hexdigest()[:16]


def test_sources_title_cannot_link(env, casa_optional):
    run = _open_web(env)
    _sources(env, run, ["https://bank.example/p"],
             ["[bank.example](https://other.example) *now* `x`"])
    _show(env, run_id=run, answer=ANSWER, source_ids=["src0"])
    body = env.casa.deposits[-1]["value"]
    if casa_optional is not None:
        assert _links(casa_optional, body) == [["bank.example",
                                                "link:https://bank.example/p"]]


def test_sources_only_this_run(env):
    run_a, run_b = admitted_run(env, mode="open_web", sites=[], n=2)
    _sources(env, run_a, ["https://a.example/1"])
    out = json.loads(_show(env, run_id=run_b, answer=ANSWER, source_ids=["src0"]))
    assert out == {"sources": None, "note": copy_text.SOURCES_NONE}
    env.app.calls.clear()
    text, is_error = env.call("show_sources", expectation_status="matched", run_id=run_a, answer=ANSWER, source_ids=["../src0"])
    assert is_error and env.app.calls == []


def test_sources_specialist_requester(env):
    env.casa.requester.update(requester_id="specialist:finance", requester_kind="specialist")
    run = _open_web(env)
    _sources(env, run, ["https://a.example/1"])
    out = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))
    assert out == {"sources": None, "note": copy_text.SOURCES_FOR_SPECIALIST}
    assert [d for d in env.casa.deposits if d["slot"] == "sources"] == []


def test_sources_no_address_in_model_text(env, capfd):
    run = _open_web(env)
    _sources(env, run, ODD)
    texts = [_show(env, run_id=run, answer=ANSWER, source_ids=["src0", "src1", "nope"]),
             env.ok("run_observe", run_id=run)]
    blob = "\n".join(texts) + capfd.readouterr().err
    assert "://" not in blob
    for u in ODD:
        assert u not in blob
    assert "nope" in texts[0]                      # reported left out, by id


def test_sources_fit_casa(env, casa_optional):
    run = _open_web(env)
    url = "https://bank.example/" + "a(b)!*'%2F;" * 70
    url = url[:800]
    while url.endswith("%") or url[-2:-1] == "%":
        url = url[:-1]
    title = "T*i[t]l(e)_!" * 10
    _sources(env, run, [url] * 8, [title] * 8)
    long_answer = ("A full answer, " * 300)[:4000]              # the longest answer allowed
    out = json.loads(_show(env, run_id=run, answer=long_answer,
                            source_ids=[f"src{i}" for i in range(8)]))
    body = env.casa.deposits[-1]["value"]
    assert len(body) <= 12_000 and body.startswith(long_answer.strip())
    # the answer, a blank line, the header, then one line per source
    assert out["sources"] and ("Left out" in out["note"]) == (body.count("\n") - 2 < 8)
    if casa_optional is not None:
        ok = casa_eval(casa_optional, f"""
import result_broker
from channels import tg_rich
body = json.loads({json.dumps(body)!r})
composed = result_broker.compose_operator_message(body, "Fido")
print(json.dumps([result_broker._message_ok(body), len(tg_rich.split(composed)),
                  result_broker.MAX_MESSAGE_PAGES]))
""")
        assert ok[0] is True and ok[1] <= ok[2]


def test_sources_note_delivery_neutral(env):
    run = _open_web(env)
    _sources(env, run, ["https://a.example/1"])
    note = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))["note"]
    for word in ("posted", "delivered", "sent"):
        assert word not in note.lower()


def test_linkable_rule():
    assert tools_ops.linkable("https://a.example/%2F?x=1#f")
    for bad in ("https://a.example/%2", "https://a.example/%g0", "https://a.example/ x",
                "https://a.example/<x>", "ftp://a.example"):
        assert not tools_ops.linkable(bad)


def test_sources_note_says_the_message_is_the_whole_answer(env):
    """The reply no longer lists the same sources as the links:
    the message is the answer, and an id that is not this run's is named as left out."""
    run = _open_web(env)
    _sources(env, run, ["https://a.example/1", "https://a.example/[x]"])
    note = json.loads(_show(env, run_id=run, answer=ANSWER,
                             source_ids=["src0", "src1", "src9"]))["note"]
    assert note.count("it is your whole answer and lists the sources") == 1
    assert note.endswith(copy_text.SOURCES_LEFT_OUT.format(ids="src9"))


# -- v0.1.27: one show_sources call answers and finishes the run --------------------------

def _running(env, urls=("https://a.example/1",)):
    run = _open_web(env)
    env.app.set_state(run, "running")
    _sources(env, run, list(urls))
    return run


def _finish(env, run):
    return json.loads(env.ok("run_finish", run_id=run, summary="Answered.", file_ids=[],
                             source_ids=["src0"], expectation_status="matched"))["note"]


def _sources_deposits(env):
    return [d for d in env.casa.deposits if d["slot"] == "sources"]


def _unconfirmed_file(env, run):
    """A file of the run whose delivery Casa has lost (`unknown`): run_finish's precondition."""
    import state
    fid = env.app.add_file(run, b"%PDF-1.4 x", "notes.pdf", "application/pdf")
    state.put_file(fid, run, "notes.pdf", "application/pdf", 10)
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.delivery[ref] = {"status": "unknown", "repeat": False}
    return ref


def _needs_driver(env, run) -> bool:
    import state
    con = state._connect()
    con.execute("DELETE FROM leases")
    con.close()
    out = json.loads(env.ok("runs_needing_driver"))
    return run in [r["run_ref"] for r in out["runs"]]


def test_show_sources_finishes_the_run_it_answers(env):
    """show_sources's result ending the turn with the run still going would let
    the sweep wake it and each continuation post the answer again. The same call
    finishes the run (the answer as its summary, the sources it used) and ends silent."""
    run = _running(env, ["https://a.example/1", "https://b.example/2"])
    note = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src1", "nope"],
                            expectation_status="mismatch"))["note"]
    (fin,) = env.app.posts("/finish")
    assert fin.body == {"summary_masked": ANSWER, "file_ids": [], "source_ids": ["src1"],
                        "expectation_status": "mismatch"}
    assert env.app.runs[run]["state"] == "completed"
    assert note == (copy_text.SOURCES_FINISHED.format(n=1, status="mismatch", after="",
                                                      ending=copy_text.HANDED_OVER_SILENT)
                    + copy_text.SOURCES_LEFT_OUT.format(ids="nope"))
    assert "run_finish" not in note
    assert not _needs_driver(env, run)


def test_a_finish_past_the_turn_bound_still_happens(env, monkeypatch):
    """`show_sources` just before the turn bound and a finish just after it must not leave
    the run going for a continuation to answer again. Once the answer is deposited the finish is not held by the bound: one deposit, one finish."""
    import casa_broker
    import continuation
    run = _running(env)
    late = {"on": False}
    monkeypatch.setattr(continuation, "clock", lambda: continuation._STARTED + (
        continuation.TURN_BOUND_S + 1 if late["on"] else 0))
    real = casa_broker.deposit_message

    def deposit(*a, **k):
        ref = real(*a, **k)
        late["on"] = True                   # the clock passes the bound right after the post
        return ref
    monkeypatch.setattr(casa_broker, "deposit_message", deposit)
    out = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))
    note = out["note"]
    assert len(env.app.posts("/finish")) == 1 and len(_sources_deposits(env)) == 1
    assert env.app.runs[run]["state"] == "completed"
    assert note.endswith(copy_text.HANDED_OVER_SILENT)
    env.casa.deliver(out["sources"])
    note = json.loads(env.ok("run_finish", run_id=run, summary="s", file_ids=[], source_ids=[],
                             expectation_status="matched"))["note"]
    assert note.endswith(copy_text.HANDED_OVER_SILENT) and copy_text.SPEAK_TO_THE_OPERATOR not in note
    assert len(env.app.posts("/finish")) == 1


def test_show_sources_at_the_turn_bound_posts_nothing(env, monkeypatch):
    """The bound is still checked before the deposit: at the bound nothing is posted and
    nothing finished."""
    import continuation
    run = _running(env)
    monkeypatch.setattr(continuation, "clock",
                        lambda: continuation._STARTED + continuation.TURN_BOUND_S + 1)
    text, is_error = env.call("show_sources", run_id=run, answer=ANSWER, source_ids=["src0"],
                              expectation_status="matched")
    assert _sources_deposits(env) == [] and env.app.posts("/finish") == []


def test_a_withheld_answer_still_finished_its_run(env):
    """When Casa withholds the sources post, the run is still finished in the same call, so
    nothing is left to drive and no continuation deposits again: one deposit, one finish
    (Casa's own result tells Fido what to say)."""
    run = _running(env)
    ref = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))["sources"]
    env.casa.deliver(ref, "withheld")
    assert env.app.runs[run]["state"] == "completed"
    assert not _needs_driver(env, run)
    assert len(env.app.posts("/finish")) == 1 and len(_sources_deposits(env)) == 1


def test_a_file_not_yet_confirmed_leaves_the_finish_to_run_finish(env):
    """run_finish's precondition: a file whose delivery Casa has not confirmed is sent again
    first, so show_sources does not finish; it asks for run_finish, and run_finish, once the
    answer reached the operator, ends silent with no SPEAK_TO_THE_OPERATOR."""
    run = _running(env)
    file_ref = _unconfirmed_file(env, run)
    out = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))
    assert out["note"].startswith(copy_text.SOURCES_PREPARED.format(
        n=1, ending=copy_text.SOURCES_FINISH))
    assert env.app.posts("/finish") == [] and env.app.runs[run]["state"] == "running"
    env.casa.deliver(out["sources"])
    env.casa.deliver(file_ref)
    note = _finish(env, run)
    assert copy_text.SPEAK_TO_THE_OPERATOR not in note
    assert note.startswith(copy_text.FINISHED.format(status="matched")
                           + copy_text.ANSWERED_WITH_SOURCES + " "
                           + copy_text.HANDED_OVER_SILENT)
    assert len(env.app.posts("/finish")) == 1 and len(_sources_deposits(env)) == 1


def test_a_finish_the_app_refuses_asks_for_run_finish_not_the_answer(env):
    """The answer is deposited and the app does not take the finish: the result says the
    answer is theirs and asks for run_finish, never to send it again."""
    run = _running(env)
    env.app.fail_next[("POST", f"/v1/runs/{run}/finish")] = 1
    text, is_error = env.call("show_sources", run_id=run, answer=ANSWER, source_ids=["src0"],
                              expectation_status="matched")
    assert not is_error
    out = json.loads(text)
    assert out["sources"].startswith("casa-cap-") and len(_sources_deposits(env)) == 1
    assert out["note"] == copy_text.SOURCES_PREPARED.format(n=1, ending=copy_text.SOURCES_FINISH)
    assert env.app.runs[run]["state"] == "running"


def test_run_finish_without_an_answer_is_unchanged(env):
    """No show_sources: the finish asks for the answer, spoken to the operator, and names no silence."""
    run = _running(env)
    note = _finish(env, run)
    assert note.startswith(copy_text.FINISHED.format(status="matched") + copy_text.SPEAK_TO_THE_OPERATOR)
    assert copy_text.ANSWERED_WITH_SOURCES not in note and "<silent/>" not in note


def test_run_finish_after_a_withheld_answer_warns_once(env):
    """A file not yet confirmed, show_sources, Casa withholds the message (its replacement
    says to tell the operator), then the file goes out: run_finish must not end silent and drop
    that warning. Not confirmed delivered, the finish asks for one plain warning line
    and never the answer again; the answer is still posted once and the run finished once."""
    run = _running(env)
    file_ref = _unconfirmed_file(env, run)            # so show_sources leaves the finish
    ref = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))["sources"]
    env.casa.deliver(ref, "withheld")
    env.casa.deliver(file_ref)
    note = _finish(env, run)
    assert note.count(copy_text.ANSWER_UNCONFIRMED) == 1          # one warning line asked for
    assert "never repeat the answer" in copy_text.ANSWER_UNCONFIRMED
    assert copy_text.SPEAK_TO_THE_OPERATOR not in note and "<silent/>" not in note     # 0 repeats
    assert len(env.app.posts("/finish")) == 1 and len(_sources_deposits(env)) == 1
    assert len([d for d in env.casa.deposits if d["slot"] == "file"]) == 1


def test_run_finish_of_a_finished_run_whose_answer_was_withheld_warns(env):
    """The same once show_sources finished the run itself: its post withheld, a later run_finish
    asks for the warning line, not silence, and finishes nothing again."""
    run = _running(env)
    ref = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))["sources"]
    env.casa.deliver(ref, "withheld")
    note = _finish(env, run)
    assert note == (copy_text.ANSWERED_FINISHED + copy_text.ANSWERED_WITH_SOURCES + " "
                    + copy_text.ANSWER_UNCONFIRMED)
    assert len(env.app.posts("/finish")) == 1


def test_show_sources_after_run_finish_posts_once_and_ends_silent(env):
    """The 0.1.25 habit, run_finish first: show_sources on the ended run still posts the one
    message, finishes nothing again, and ends the turn silent."""
    run = _running(env)
    _finish(env, run)
    out = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))
    assert out["sources"].startswith("casa-cap-")
    assert out["note"] == copy_text.SOURCES_PREPARED.format(
        n=1, ending=copy_text.HANDED_OVER_SILENT)
    assert len(_sources_deposits(env)) == 1 and len(env.app.posts("/finish")) == 1
    import state
    assert state.answer_of(run).finished_at is not None      # nothing owed: it had ended


# -- v0.1.27: the finish is plugin code's, held by a durable record --------------

def _sweep(env) -> list[str]:
    """One sweep pass (Casa's, outside any turn): the run refs it lists for a driver."""
    import state
    con = state._connect()
    con.execute("DELETE FROM leases")
    con.close()
    return [r["run_ref"] for r in json.loads(env.ok("runs_needing_driver"))["runs"]]


def _past_bound(monkeypatch):
    import continuation
    monkeypatch.setattr(continuation, "clock",
                        lambda: continuation._STARTED + continuation.TURN_BOUND_S + 1)


def _fresh_process(monkeypatch):
    """The sweep runs in its own call, not in the turn that crossed the bound."""
    import continuation
    monkeypatch.setattr(continuation, "clock", lambda: continuation._STARTED)


def test_a_failed_finish_past_the_bound_is_finished_by_the_sweep(env, monkeypatch):
    """The deposit lands and the app refuses the finish; after the turn ends, the sweep
    retries it. No second answer may follow from a later turn: one deposit, the run finished
    by the retry, never listed for a driver."""
    run = _running(env)
    env.app.fail_next[("POST", f"/v1/runs/{run}/finish")] = 1
    ref = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))["sources"]
    env.casa.deliver(ref)
    assert env.app.runs[run]["state"] == "running" and len(env.app.posts("/finish")) == 1
    _past_bound(monkeypatch)
    _fresh_process(monkeypatch)
    listed = _sweep(env) + _sweep(env)
    assert run not in listed
    assert env.app.runs[run]["state"] == "completed"
    assert len(env.app.posts("/finish")) == 2             # the refused one, then the retry
    assert env.app.runs[run]["finish"] == {"summary_masked": ANSWER, "file_ids": [],
                                           "source_ids": ["src0"],
                                           "expectation_status": "matched"}
    assert len(_sources_deposits(env)) == 1


def test_a_failed_finish_and_a_withheld_post_are_finished_by_the_sweep(env, monkeypatch):
    """The finish fails and Casa withholds the post, so its result REPLACES
    show_sources's and Fido never reads the instruction to finish. The sweep finishes it: one
    deposit, one retry, no driver listing."""
    run = _running(env)
    env.app.fail_next[("POST", f"/v1/runs/{run}/finish")] = 1
    ref = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))["sources"]
    env.casa.deliver(ref, "withheld")                     # the model reads Casa's line only
    _fresh_process(monkeypatch)
    assert run not in _sweep(env) + _sweep(env)
    assert env.app.runs[run]["state"] == "completed"
    assert len(env.app.posts("/finish")) == 2 and len(_sources_deposits(env)) == 1


def test_an_answered_run_with_a_file_is_woken_for_the_file_only(env):
    """A run whose file is not settled is woken for it, and only for it: the
    briefing says the operator already has the answer, a second show_sources sends nothing, and once the
    file settles run_finish ends it silent."""
    import state
    run = _running(env)
    file_ref = _unconfirmed_file(env, run)
    con = state._connect()
    con.execute("UPDATE deliveries SET deposited_at = deposited_at - 200 WHERE reference = ?",
                (file_ref,))
    con.close()
    ref = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))["sources"]
    env.casa.deliver(ref)
    assert _sweep(env) == [run] and env.app.posts("/finish") == []   # the retry waits
    brief = env.ok("continue_run", run_ref=run)
    assert copy_text.ANSWERED_BRIEF in brief and "Download invoice" not in brief
    again = json.loads(_show(env, run_id=run, answer="The same, reworded.",
                             source_ids=["src0"]))
    assert again["sources"] is None and again["note"].startswith(copy_text.SOURCES_ALREADY)
    assert "again" in copy_text.SOURCES_ALREADY and "never send it" in copy_text.SOURCES_ALREADY
    assert len(_sources_deposits(env)) == 1
    env.casa.deliver(file_ref)                  # the file settles; Fido's job ends with run_finish
    note = _finish(env, run)
    assert copy_text.SPEAK_TO_THE_OPERATOR not in note and note.endswith(copy_text.HANDED_OVER_SILENT)
    assert run not in _sweep(env)
    assert env.app.runs[run]["state"] == "completed" and len(env.app.posts("/finish")) == 1


def test_a_stop_while_answered_is_never_finished_by_the_sweep(env):
    """The operator's Stop wins: a sealed request's answered run is not finished by the retry, even while
    the app has not taken the Stop's cancel."""
    import state
    run = _running(env)
    env.app.fail_next[("POST", f"/v1/runs/{run}/finish")] = 1
    _show(env, run_id=run, answer=ANSWER, source_ids=["src0"])
    env.app.fail_next[("POST", "/v1/requests/cancel")] = 3          # the cancel does not land
    key = state.run_binding(run).request_group
    json.loads(env.ok("stop_runs", ref=f"request:{key}"))
    assert run not in _sweep(env)
    assert len(env.app.posts("/finish")) == 1                       # the refused one only
    assert env.app.runs[run]["state"] == "running"
    assert state.answer_of(run).finished_at is None


def test_the_answered_record_ends_with_the_run(env):
    """The record's finish is owed until the run ends, by whatever path (here run_cancel)."""
    import state
    run = _running(env)
    env.app.fail_next[("POST", f"/v1/runs/{run}/finish")] = 1
    _show(env, run_id=run, answer=ANSWER, source_ids=["src0"])
    assert state.answer_of(run).finished_at is None
    env.ok("run_cancel", run_id=run)
    assert state.answer_of(run).finished_at is not None
    _sweep(env)
    assert len(env.app.posts("/finish")) == 1


def _owed() -> list[str]:
    import state
    con = state._connect()
    try:
        return [r[0] for r in con.execute("SELECT run_id FROM ended_owed")]
    finally:
        con.close()


def test_an_answered_run_the_app_ended_is_owed_nothing(env):
    """The finish failed and the app then ended the run itself: the sweep records it ended
    (nothing more to finish) and owes no telling, since the operator has its answer."""
    import state
    run = _running(env)
    env.app.fail_next[("POST", f"/v1/runs/{run}/finish")] = 1
    _show(env, run_id=run, answer=ANSWER, source_ids=["src0"])
    env.app.set_state(run, "failed")
    assert run not in _sweep(env)
    assert state.answer_of(run).finished_at is not None and run not in _owed()
    assert len(env.app.posts("/finish")) == 1


def test_a_continuation_of_an_ended_answered_run_tells_nothing(env):
    """continue_run on an answered run that has ended marks the record ended and says the
    ending was told: the operator has its answer."""
    import state
    run = _running(env)
    env.app.fail_next[("POST", f"/v1/runs/{run}/finish")] = 1
    _show(env, run_id=run, answer=ANSWER, source_ids=["src0"])
    env.app.set_state(run, "failed")
    text = env.ok("continue_run", run_ref=run)
    assert copy_text.ENDED_TOLD in text and copy_text.ENDED_TELL not in text
    assert state.answer_of(run).finished_at is not None


def test_an_answered_run_with_an_owed_ending_is_not_woken(env):
    """The app ended the run while nothing drove it (a telling owed), then show_sources posted
    its answer on the ended run: the sweep does not wake it to tell an ending the operator does not need."""
    import state
    run = _running(env)
    env.app.set_state(run, "completed")
    _sweep(env)
    assert run in _owed()
    out = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))
    assert out["note"] == copy_text.SOURCES_PREPARED.format(
        n=1, ending=copy_text.HANDED_OVER_SILENT)
    assert state.answer_of(run).finished_at is not None
    assert run not in _sweep(env)


def test_run_finish_of_an_answered_run_at_the_bound_finishes_it(env, monkeypatch):
    """The answered run left to run_finish (its file) is finished by that one call even past
    the turn bound, and ends silent."""
    run = _running(env)
    file_ref = _unconfirmed_file(env, run)
    ref = json.loads(_show(env, run_id=run, answer=ANSWER, source_ids=["src0"]))["sources"]
    env.casa.deliver(ref)
    env.casa.deliver(file_ref)
    _past_bound(monkeypatch)
    note = _finish(env, run)
    assert env.app.runs[run]["state"] == "completed" and len(env.app.posts("/finish")) == 1
    assert note.endswith(copy_text.HANDED_OVER_SILENT) and copy_text.SPEAK_TO_THE_OPERATOR not in note

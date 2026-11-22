"""Logins (action model).

Casa Browser starts two judgements itself: `login_item` (a site with no linked 1Password item:
which of the vault's items signs in to it) and `signin` (after a live-view sign-in: did it
work). The plugin's own code relays each one at the start of the next run tool
(`_prelude`) — Casa's check, then the app's verdict route — and the tool's result carries one
fixed sentence for its outcome. The login question is posted by `post_question` as fixed copy
(the browser's host, the vault's whole titles): [Yes] [No], or the titles and [None of these].
"""
from __future__ import annotations

import json

import cards
import copy_text
from conftest import admitted_run

LOGIN_ITEM = {"kind": "login_item", "site": {"name": "Cloud Co", "sign_in_host": "cloudco.example",
                                             "registered_origins": ["https://cloudco.example"]},
              "items": [{"index": 0, "title": "Cloud Co", "website_hosts": ["cloudco.example"]}],
              "items_truncated": False}
SIGNIN = {"kind": "signin", "site": {"name": "Cloud Co", "origins": ["https://cloudco.example"]},
          "after_attempt": [], "at_done": {"blocks": [{"n": 1, "text": "Log out"}]}}


def _login_item(env, titles=("Cloud Co",), verdict=None, host="www.cloudco.example"):
    (run,) = admitted_run(env)
    env.app.runs[run]["state"] = "running"
    env.app.add_app_judgement(run, "login_item", LOGIN_ITEM, host=host, titles=list(titles))
    env.casa.judge_script = [verdict or {"items": list(range(len(titles))), "reason": "fits"}]
    return run


def _labels(card):
    return [b["label"] for b in card["buttons"]]


def _tap(env, card, label):
    return json.loads(env.ok("apply_tap", t=env.token(card, label)))["receipt"]


def test_prelude_relays_an_app_judgement_by_code_and_says_the_outcome(env):
    """Any run tool relays it first: Casa's check gets the app's packet untouched, the app gets
    Casa's verdict untouched, and the result says to post the question."""
    run = _login_item(env)
    out = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert copy_text.LOGIN_ASKED.format(run_id=run) in out
    (call,) = env.casa.judgements()
    assert call["kind"] == "login_item" and json.loads(call["packet"]) == LOGIN_ITEM
    assert call["spec_digest"] is None
    assert env.app.runs[run]["state"] == "waiting_approval"
    assert env.app.app_pending[run] == []
    env.ok("run_wait", run_id=run, max_wait_s=1)
    assert len(env.casa.judgements()) == 1                   # relayed once


def test_one_candidate_card_is_yes_no_with_the_host_and_title(env):
    run = _login_item(env, titles=("Cloud Co *personal*",))
    out = json.loads(env.ok("post_question", run_id=run))
    assert out["note"] == copy_text.QUESTION_POSTED.format(ending=copy_text.HANDED_OVER_SILENT) and copy_text.LOGIN_ASKED[:20] not in out["note"]
    card = env.card()
    assert card["text"] == copy_text.LOGIN_ONE.format(host=cards.md("www.cloudco.example"),
                                                      title=cards.md("Cloud Co *personal*"))
    assert "**www\\.cloudco\\.example**" in card["text"]
    assert _labels(card) == ["Yes", "No"]
    assert all(b["call"]["tool"] == "apply_tap" for b in card["buttons"])
    assert _tap(env, card, "Yes") == copy_text.R_LINKED.format(label=cards.md("Cloud Co *personal*"))
    assert env.app.answers[-1]["choice"] == {"item": 0}
    assert [b["reason"] for p, b in env.casa.calls if p.endswith("/continue")] == ["operator_answer"]


def test_several_candidates_card_lists_titles_and_none_of_these(env):
    long_a = "Cloud Co — the company account used for the GPU pods (finance)"
    long_b = "Cloud Co — the company account used for the GPU pods (research)"
    run = _login_item(env, titles=(long_a, long_b, "Cloud Co"))
    env.ok("post_question", run_id=run)
    card = env.card()
    assert card["text"].startswith(copy_text.LOGIN_MANY.format(host=cards.md("www.cloudco.example")))
    assert cards.md(long_a) in card["text"] and cards.md(long_b) in card["text"]   # whole titles
    assert _labels(card) == [cards.button_label(long_a), cards.button_label(long_b), "Cloud Co",
                             "None of these"]
    assert _tap(env, card, "None of these") == copy_text.R_NO_ITEM
    assert env.app.answers[-1]["choice"] == "not_now"
    assert _tap(env, card, cards.button_label(long_b)) == copy_text.R_NO_ITEM   # one answer


def test_no_candidate_says_live_view(env):
    run = _login_item(env, verdict={"items": [], "reason": "none"})
    out = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert copy_text.LOGIN_NONE in out and env.app.runs[run]["state"] == "running"


def test_a_failed_check_is_no_verdict_after_one_retry(env):
    run = _login_item(env)
    env.casa.judge_script = ["check_unavailable", "check_unavailable"]
    out = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert len(env.casa.judgements()) == 2 and copy_text.LOGIN_NONE in out


def test_signin_outcomes_say_saved_or_kept_once(env):
    (run,) = admitted_run(env)
    env.app.add_app_judgement(run, "signin", SIGNIN, site="Cloud Co")
    env.casa.judge_script = [{"verdict": "signed_in", "evidence_blocks": [1], "reason": "x"}]
    out = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert copy_text.LOGIN_SAVED.format(site="Cloud Co") in out
    env.app.add_app_judgement(run, "signin", SIGNIN, site="Cloud Co", kept=True)
    env.casa.judge_script = [{"verdict": "signed_in", "evidence_blocks": [1], "reason": "x"}]
    assert copy_text.LOGIN_KEPT.format(site="Cloud Co") in env.ok("run_wait", run_id=run,
                                                                 max_wait_s=1)
    env.app.add_app_judgement(run, "signin", SIGNIN)
    env.casa.judge_script = [{"verdict": "unsure", "evidence_blocks": [], "reason": "x"}]
    out = env.ok("run_wait", run_id=run, max_wait_s=1)
    assert "saved" not in out.lower() and "1Password" not in out


def test_a_login_item_waits_while_a_question_is_open(env):
    """The app serves a login_item only while the run is running: nothing is relayed (no
    check is spent) while another question waits."""
    (run,) = admitted_run(env)
    env.app.runs[run]["state"] = "waiting_approval"
    env.app.add_app_judgement(run, "login_item", LOGIN_ITEM, host="cloudco.example", titles=["Cloud Co"])
    env.ok("run_wait", run_id=run, max_wait_s=1)
    assert env.casa.judgements() == []

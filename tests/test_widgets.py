"""Fields that are not plain text, and a hand-over that is one message.

* An observation names a field's kind (a closed set) so Fido gives the value in the browser's
  own format; a kind outside the set is a bad response.
* A typing step that did not run says why (read-only, the format, Enter after a value field);
  one whose field does not show the text says so — one bit, never the value.
* A result picture's caption says what it is; the reply says the outcome, once.
"""
from __future__ import annotations

import json

import pytest

import copy_text
from conftest import admitted_run


def test_an_observation_names_a_fields_kind(env):
    (run,) = admitted_run(env)
    env.app.observation = {"page_handle": "p1", "generation": 1, "text": "Order",
                           "elements": [{"handle": "e1", "role": "textbox",
                                         "label_masked": "Pickup time", "kind": "time"},
                                        {"handle": "e2", "role": "clickable",
                                         "label_masked": "09:30"}],
                           "source_id": None}
    text = env.ok("run_observe", run_id=run)
    assert '[e1] textbox (time) "Pickup time"' in text and '[e2] clickable "09:30"' in text
    env.app.observation["elements"][0]["kind"] = "password"     # not a kind: a bad response
    text, is_error = env.call("run_observe", run_id=run)
    assert copy_text.UNEXPECTED in text and "Pickup" not in text


def test_an_observation_names_a_multi_line_field(env):
    """A real textarea is shown as multi-line, the one field that takes line breaks."""
    (run,) = admitted_run(env)
    env.app.observation = {"page_handle": "p1", "generation": 1, "text": "Contact",
                           "elements": [{"handle": "e1", "role": "textbox",
                                         "label_masked": "Message", "kind": "multi-line"}],
                           "source_id": None}
    assert '[e1] textbox (multi-line) "Message"' in env.ok("run_observe", run_id=run)


@pytest.mark.parametrize("reason", ["not_typable", "value_format", "no_enter", "one_line"])
def test_a_typing_step_that_did_not_run_says_why(env, reason):
    (run,) = admitted_run(env)
    gen = env.app.observation["generation"]
    env.app.overrides[("POST", f"/v1/runs/{run}/action")] = (
        200, {"outcome": "not_run", "generation": gen, "reason": reason}, "application/json")
    out = env.ok("run_act", run_id=run, kind="type", handle="e1", generation=gen, text="19:30")
    assert out == copy_text.STEP_NOT_RUN_BY_REASON[reason]


@pytest.mark.parametrize("reason, hint", [("not_visible", "hidden"), ("disabled", "disabled"),
                                          ("no_size", "no space")])
def test_a_refused_target_says_why_and_what_helps(env, reason, hint):
    (run,) = admitted_run(env)
    gen = env.app.observation["generation"]
    env.app.overrides[("POST", f"/v1/runs/{run}/action")] = (
        200, {"outcome": "not_run", "generation": gen, "reason": reason}, "application/json")
    out = env.ok("run_act", run_id=run, kind="click", handle="e1", generation=gen)
    assert out == copy_text.STEP_NOT_RUN_BY_REASON[reason] and hint in out
    assert "\n" not in out and len(out) < 200


@pytest.mark.parametrize("typed,said", [("shown", False), ("not_shown", True), (None, False)])
def test_a_field_that_does_not_show_the_text_is_said_once(env, typed, said):
    (run,) = admitted_run(env)
    gen = env.app.observation["generation"]
    reply = {"outcome": "done", "generation": gen + 1, **({"typed": typed} if typed else {})}
    env.app.overrides[("POST", f"/v1/runs/{run}/action")] = (200, reply, "application/json")
    out = env.ok("run_act", run_id=run, kind="type", handle="e1", generation=gen, text="Jan")
    assert out.startswith("Done (done).")
    assert (copy_text.TYPED_NOT_SHOWN in out) is said and "Jan" not in out


def test_a_result_picture_names_itself_and_the_reply_says_the_outcome_once():
    """The picture's caption says what it is, never
    the outcome; the reply is always the one outcome line — no silence to authorise."""
    import pathlib
    import browser_server  # noqa: F401 — registers every tool
    import tooling
    desc = tooling.TOOLS["show_screenshot"]["description"]
    assert "what the picture is" in desc and "never the outcome" in desc
    assert "one short plain line with the outcome" in copy_text.SCREENSHOT_SENT
    doctrine = (pathlib.Path(__file__).resolve().parents[1] / "role" / "doctrine.md").read_text()
    assert "never the outcome" in doctrine and "the outcome, once" in doctrine

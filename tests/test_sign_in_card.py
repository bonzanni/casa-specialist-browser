"""Signing in to a site that is not set up takes one operator tap.

`ask_sign_in(run_id, site_name)` asks Casa Browser for the sign-in card of the public page the run
is on. The plugin's code relays the vault check (no model in the relay) and posts the card. Every
word on the card is the APP's view (the browser's host, the label, the one fitting item's title);
the tool has no host, address or item argument. [Yes] registers and signs in; [No] does nothing.
"""
from __future__ import annotations

import json

import cards
import copy_text
import tooling
import wire
from conftest import admitted_run


def _running(env):
    (run,) = admitted_run(env)
    env.app.runs[run]["state"] = "running"
    return run


def _labels(card):
    return [b["label"] for b in card["buttons"]]


def _tap(env, card, label):
    return json.loads(env.ok("apply_tap", t=env.token(card, label)))["receipt"]


def test_the_tool_takes_no_host_address_or_item():
    import tools_ops  # noqa: F401
    schema = tooling.TOOLS["ask_sign_in"]["schema"]
    assert set(schema["properties"]) == {"run_id", "site_name"}
    assert schema.get("additionalProperties") is False


def test_the_card_shows_the_apps_host_never_the_tools_name(env):
    run = _running(env)
    env.app.sign_in_host = "login.cloudco.example"
    out = json.loads(env.ok("ask_sign_in", run_id=run, site_name="accounts.google.com"))
    assert out["note"] == copy_text.SIGN_IN_ASKED.format(ending=copy_text.HANDED_OVER_SILENT)
    assert env.app.sign_in_names == ["accounts.google.com"]
    card = env.card()
    assert card["text"] == "\n".join([
        copy_text.SITE_PLAIN.format(host=cards.md("login.cloudco.example"), name="Cloud Co"),
        copy_text.SITE_SAVES])
    assert "google" not in card["text"]
    assert _labels(card) == ["Yes", "No"]
    assert _tap(env, card, "Yes") == copy_text.R_SIGNING_IN.format(
        label=cards.md("login.cloudco.example"))
    assert env.app.answers[-1]["choice"] == "confirm"
    assert [b["reason"] for p, b in env.casa.calls if p.endswith("/continue")] == ["operator_answer"]


def test_one_fitting_item_is_named_on_the_same_card(env):
    run = _running(env)
    env.app.sign_in_titles = ["Cloud Co *personal*", "Other"]
    env.casa.judge_script = [{"items": [0], "reason": "same host"}]
    env.ok("ask_sign_in", run_id=run, site_name="Cloud Co")
    (call,) = env.casa.judgements()
    assert call["kind"] == "login_item"                       # relayed by code, untouched
    card = env.card()
    assert card["text"].startswith(copy_text.SITE_ONE.format(
        host=cards.md("login.cloudco.example"), name="Cloud Co",
        title=cards.md("Cloud Co *personal*")))
    assert _labels(card) == ["Yes", "No"]


def test_several_items_or_a_failed_check_name_none(env):
    run = _running(env)
    env.app.sign_in_titles = ["A", "B"]
    env.casa.judge_script = ["check_unavailable", "check_unavailable"]
    env.ok("ask_sign_in", run_id=run, site_name="Cloud Co")
    card = env.card()
    assert "1Password" not in card["text"] and _labels(card) == ["Yes", "No"]


def test_no_does_nothing_and_says_so(env):
    run = _running(env)
    env.ok("ask_sign_in", run_id=run, site_name="Cloud Co")
    assert _tap(env, env.card(), "No") == copy_text.R_SITE_DECLINED
    assert env.app.answers[-1]["choice"] == "not_now"


def test_refusals_post_no_card(env):
    run = _running(env)
    for outcome, sentence in (("not_configured", copy_text.SIGN_IN_NOT_CONFIGURED),
                              ("account_site", copy_text.SIGN_IN_ACCOUNT_SITE),
                              ("not_https", copy_text.SIGN_IN_NOT_HTTPS),
                              ("page_changed", copy_text.SIGN_IN_PAGE_CHANGED)):
        env.app.sign_in_refusal = {"outcome": outcome}
        out = json.loads(env.ok("ask_sign_in", run_id=run, site_name="X"))
        assert out == {"card": None, "note": sentence}, out
    env.app.sign_in_refusal = {"outcome": "registered_site", "site": "Bank *A*"}
    out = json.loads(env.ok("ask_sign_in", run_id=run, site_name="X"))
    assert out == {"card": None, "note": copy_text.SIGN_IN_REGISTERED.format(
        site=cards.md("Bank *A*", 80))}


def test_the_parser_keeps_the_site_fields_and_refuses_a_confirm(plugin_data):
    base = {"question_id": "q1", "run_id": "r1", "revision": 1, "digest": "a" * 64,
            "state": "open"}
    view = {"kind": "site_signin", "host": "login.cloudco.example:8443", "name": "Cloud Co",
            "item": None, "text": "Sign in?", "options": [], "confirm_label": "",
            "extra_options": [], "screenshot_id": None, "confirm_offered": False,
            "live_only_copy": None}
    got = wire.parse_question({**base, "view": view})["view"]
    assert (got["kind"], got["host"], got["name"], got["item"]) == \
        ("site_signin", "login.cloudco.example:8443", "Cloud Co", None)
    card = cards.question_card("r1", {**base, "view": got}, "g1", confirm=False)
    assert [b["label"] for b in card["buttons"]] == ["Yes", "No"]
    import pytest
    for bad in ({"confirm_offered": True, "confirm_label": "Go"}, {"options": ["x"]},
                {"host": ""}):
        with pytest.raises(Exception):
            wire.parse_question({**base, "view": {**view, **bad}})


def test_a_sign_in_page_that_did_not_open_is_said_on_the_receipt(env):
    """The site is saved but its sign-in page did not open: the receipt says so."""
    run = _running(env)
    env.app.sign_in_answer = "not_opened"
    env.ok("ask_sign_in", run_id=run, site_name="Cloud Co")
    assert _tap(env, env.card(), "Yes") == copy_text.R_SITE_NOT_OPENED.format(
        label=cards.md("login.cloudco.example"))

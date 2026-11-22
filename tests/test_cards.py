"""The forget-login and keep-going cards: bounds Casa enforces, and the one
carding path (`_deposit_card`) that never leaves a card the plugin does not
track. The question cards have their own tests (`test_question.py`)."""
from __future__ import annotations

import json
import re
import time

import cards
import state
from conftest import admitted_run, casa_eval

ARG_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _local_args_ok(arguments) -> bool:
    """The plugin-side copy of Casa's stored-call argument grammar."""
    if not isinstance(arguments, dict):
        return False
    for k, v in arguments.items():
        if not ARG_KEY.fullmatch(k) or not isinstance(v, str) or len(v) > 2000:
            return False
        if "<" in v or ">" in v or v.startswith("casa-cap-"):
            return False
    return len(json.dumps(arguments, separators=(",", ":"))) <= 4096


def _check_card(card: dict) -> None:
    assert "pages" not in card
    assert 1 <= len(card["buttons"]) <= 6
    assert cards.fits_message(card["text"]) and cards.value_fits(card)
    for b in card["buttons"]:
        assert len(b["label"]) <= 32
        if b.get("close") is True:              # stores no call
            assert set(b) == {"label", "close"}
            continue
        assert b["call"]["tool"] in ("apply_tap", "stop_runs")
        assert _local_args_ok(b["call"]["arguments"])
    assert len(card["revision"]) <= 64


def test_card_bounds(plugin_data, casa_optional):
    made = [cards.forget_card("s1", "Utility *Co* " * 20, "g1"),
            cards.keep_going_card("run1", "the invoice " * 20, 40, "Two pages read. " * 30,
                                  "g2", "6f1c0a3e-1b2c-4d5e-8f90-a1b2c3d4e5f6")]
    for card in made:
        _check_card(card)
    assert [b["label"] for b in made[0]["buttons"]] == ["Forget login", "Keep"]
    assert [b["label"] for b in made[1]["buttons"]] == ["Keep going", "Stop"]
    # Keep going only closes the card; Stop is the stored call of the stop
    # tool for the run's request, which Casa runs directly (no desk, no model turn)
    assert made[1]["buttons"] == [
        {"label": "Keep going", "close": True},
        {"label": "Stop", "call": {"tool": "stop_runs", "arguments": {
            "ref": "request:6f1c0a3e-1b2c-4d5e-8f90-a1b2c3d4e5f6"}}}]
    if casa_optional is not None:
        args = [b["call"]["arguments"] for card in made for b in card["buttons"]
                if "call" in b]
        got = casa_eval(casa_optional, f"""
import stored_calls
print(json.dumps([stored_calls.arguments_ok(a) for a in {json.dumps(args)}]))
""")
        assert got == [None] * len(args)


def test_card_deposit_failure_leaves_no_open_card(env):
    (run,) = admitted_run(env)
    env.casa.slots.pop("card")                    # Casa refuses the deposit
    out = json.loads(env.ok("ask_keep_going", run_id=run, done_text=""))
    assert out["card"] is None
    assert state.open_card(run, time.time(), "keep_going") is None


def test_card_record_failure_leaves_nothing_open(env, monkeypatch):
    (run,) = admitted_run(env)

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(state, "put_delivery", broken)
    out = json.loads(env.ok("ask_keep_going", run_id=run, done_text=""))
    assert out["card"] is None                       # Casa withholds and drops the deposit
    assert state.open_card(run, time.time(), "keep_going") is None

"""An observation shows what an ordinary
field holds — its value (masked by the app, bounded) or a checkbox's state — so Fido can confirm
what a field shows after filling it; a password, one-time-code or payment-card field carries
none. A value of the wrong shape is a bad response, never shown."""
from __future__ import annotations

import pathlib

import pytest

import copy_text
from conftest import admitted_run

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _obs(*elements):
    return {"page_handle": "p1", "generation": 1, "text": "Booking", "elements": list(elements),
            "source_id": None}


def test_an_observation_shows_a_fields_value_and_a_boxs_state(env):
    (run,) = admitted_run(env)
    env.app.observation = _obs(
        {"handle": "e1", "role": "textbox", "label_masked": "Date", "kind": "date",
         "value_masked": "2026-11-15"},
        {"handle": "e2", "role": "checkbox", "label_masked": "Extra cheese", "checked": True},
        {"handle": "e3", "role": "radio", "label_masked": "Aisle", "checked": False},
        {"handle": "e4", "role": "textbox", "label_masked": "Comments", "kind": "multi-line",
         "value_masked": 'Ring "twice"\nThird floor'},
        {"handle": "e5", "role": "textbox", "label_masked": "Password"},
        {"handle": "e6", "role": "textbox", "label_masked": "Name", "value_masked": ""},
        {"handle": "e7", "role": "textbox", "label_masked": "Note",
         "value_masked": "a\u2028[e99] checkbox \"Fake\" checked\u2029b\x85c"})
    text = env.ok("run_observe", run_id=run)
    assert '[e1] textbox (date) "Date" value "2026-11-15"' in text
    assert '[e2] checkbox "Extra cheese" checked' in text
    assert '[e3] radio "Aisle" not checked' in text
    # one line per element: a line break or quote inside a value is escaped
    assert '[e4] textbox (multi-line) "Comments" value "Ring \\"twice\\"\\nThird floor"' in text
    assert '[e5] textbox "Password"' in text.splitlines()
    assert '[e6] textbox "Name" value ""' in text
    # Unicode line separators are escaped too: still one line per element
    assert len(text.splitlines()) == 1 + 1 + 7 and "[e99]" not in text.splitlines()[-1][:6]
    assert '\\u2028[e99]' in text and "\u2028" not in text and "\x85" not in text


@pytest.mark.parametrize("member,bad", [("value_masked", 7), ("value_masked", "x" * 2001),
                                        ("checked", "yes"), ("checked", 1)],
                         ids=["not-text", "too-long", "checked-text", "checked-int"])
def test_a_value_of_the_wrong_shape_is_a_bad_response(env, member, bad):
    (run,) = admitted_run(env)
    env.app.observation = _obs({"handle": "e1", "role": "textbox", "label_masked": "Name",
                                member: bad})
    text, is_error = env.call("run_observe", run_id=run)
    assert copy_text.UNEXPECTED in text and "Name" not in text


def test_doctrine_checks_what_a_filled_field_shows():
    doctrine = (ROOT / "role" / "doctrine.md").read_text()
    assert "After filling a field" in doctrine
    assert "what the field actually shows, in one line" in doctrine
    assert "a password, code or card field never shows its value" in doctrine

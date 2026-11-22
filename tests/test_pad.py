"""The conversation's pad from Fido's tools: `run_read` with `to_pad` (and `rows`), and
`run_act` with `text_from`, against the fake app and the fake Casa pad route (its stated
contract: write/append `{lines, bytes}`, read `{text, next}` in 30,000-character windows, a
refusal `{error, message}`)."""
from __future__ import annotations

import pytest

import copy_text
from conftest import admitted_run

PAD = "/internal/broker/pad"


def _pad_calls(env):
    return [b for p, b in env.casa.calls if p == PAD]


def _actions(env):
    return [c for c in env.app.calls if c.path.endswith("/action")]


def test_to_pad_writes_the_whole_text_and_tells_only_counts(env):
    (run,) = admitted_run(env)
    env.app.read_source = "".join(f"line {i}\n" for i in range(9000)) + "## not a section"
    env.app.read_masked = True
    text = env.ok("run_read", run_id=run, to_pad="page")
    assert env.casa.pad_sections["page"].endswith("line 8999\n ## not a section")
    assert env.casa.pad_sections["page"].replace("\n ## ", "\n## ") == env.app.read_source
    assert text == copy_text.PAD_WRITTEN.format(chars=len(env.app.read_source), lines=9001,
                                                section='"page"', start=0, masked="true")
    assert "line 1" not in text
    assert [b["offset"] for b in env.app.read_bodies] == list(range(0, len(env.app.read_source), 30_000))
    (write,) = _pad_calls(env)
    assert write["op"] == "write" and write["client"] == env.casa.client
    # a write replaces the section: reading again does not pile up
    env.ok("run_read", run_id=run, to_pad="page")
    assert env.casa.pad_sections["page"].count("line 8999") == 1


def test_to_pad_reads_rows_a_field_or_a_file_from_an_offset(env):
    (run,) = admitted_run(env)
    env.app.rows_source = '{"text": "Line 1", "fields": [["Qty", "3"]], "press": ["Save"]}'
    env.ok("run_read", run_id=run, rows=True, to_pad="rows")
    assert env.casa.pad_sections["rows"] == env.app.rows_source
    env.app.read_source = "abcdef"
    env.ok("run_read", run_id=run, handle="e1", offset=2, to_pad="value")
    assert env.casa.pad_sections["value"] == "cdef"
    assert env.app.read_bodies == [{"rows": True, "offset": 0}, {"handle": "e1", "offset": 2}]


def test_rows_without_to_pad_is_a_window_like_any_read(env):
    (run,) = admitted_run(env)
    env.app.rows_source = '{"text": "Line 1", "fields": [], "press": []}'
    assert env.ok("run_read", run_id=run, rows=True).startswith(env.app.rows_source + "\n")
    assert env.app.read_bodies == [{"rows": True}]


def test_to_pad_past_the_pads_cap_writes_what_fits_and_says_where_it_goes_on(env):
    (run,) = admitted_run(env)
    env.app.read_source = ("x" * 99 + "\n") * 12_000              # 1,200,000 characters
    text = env.ok("run_read", run_id=run, to_pad="a")
    kept = len(env.casa.pad_sections["a"])
    assert kept % 100 == 0 and kept <= 1024 * 1024
    assert copy_text.PAD_MORE.format(next_offset=kept) in text
    env.ok("run_read", run_id=run, offset=kept, to_pad="b")
    b = env.casa.pad_sections["b"]          # the whole pad is capped: "b" holds what room is left
    assert b and env.casa.pad_sections["a"] + b == env.app.read_source[:kept + len(b)]


def test_to_pad_into_a_pad_holding_other_sections_writes_what_room_is_left(env):
    """Casa's cap is the whole pad's: with another section already there, a page past the room
    left is written in part, cut at a line, and the reply says where it goes on."""
    (run,) = admitted_run(env)
    env.casa.pad_sections["notes"] = "n" * 600_000
    env.app.read_source = ("x" * 99 + "\n") * 12_000              # 1,200,000 characters
    text = env.ok("run_read", run_id=run, to_pad="page")
    kept = len(env.casa.pad_sections["page"])
    assert 0 < kept < 1024 * 1024 - 600_000 and kept % 100 == 0
    assert copy_text.PAD_MORE.format(next_offset=kept) in text
    assert env.casa.pad_sections["page"] == env.app.read_source[:kept]


def test_to_pad_into_a_nearly_full_pad_still_writes_a_short_part(env):
    """A pad with only a few hundred bytes left still takes a short first part of a long
    unbroken read, and the reply says where it goes on."""
    (run,) = admitted_run(env)
    env.casa.pad_sections["notes"] = "n" * (1024 * 1024 - 400)
    env.app.read_source = "x" * 1_200_000                         # no line to cut at
    text = env.ok("run_read", run_id=run, to_pad="page")
    kept = len(env.casa.pad_sections["page"])
    assert 0 < kept < 400
    assert copy_text.PAD_MORE.format(next_offset=kept) in text


def test_rows_and_to_pad_combinations_refused_before_any_call(env):
    (run,) = admitted_run(env)
    env.app.calls.clear()
    assert env.ok("run_read", run_id=run, rows=True, handle="e1") == copy_text.READ_ONE_THING
    assert env.ok("run_read", run_id=run, rows=True, file_id="f1") == copy_text.READ_ONE_THING
    assert env.ok("run_read", run_id=run, to_pad="a", find="x") == copy_text.PAD_READ_ALONE
    assert env.ok("run_read", run_id=run, to_pad="a", length=5) == copy_text.PAD_READ_ALONE
    assert env.app.read_bodies == [] and _pad_calls(env) == []


def test_a_pad_refusal_is_casas_code_never_its_message(env):
    (run,) = admitted_run(env)
    env.app.read_source = "abc"
    env.casa.pad_cap = 1
    text = env.ok("run_read", run_id=run, to_pad="a")
    assert text == copy_text.CASA_FAILED.format(code="too_large")
    assert "The pad is full" not in text


def test_text_from_types_the_section_through_the_same_checks(env):
    (run,) = admitted_run(env)
    paste = "".join(f"1x Item {i}\n" for i in range(2500)) + "1x Last item"
    env.casa.pad_sections["paste"] = paste
    text = env.ok("run_act", run_id=run, kind="type", handle="e1", generation=1,
                  text_from="paste")
    assert text.startswith("Done") and "Item 7" not in text
    assert env.app.posts("/action")[-1].body["text"] == paste
    assert [b["op"] for b in _pad_calls(env)] == ["read", "read"]   # two windows


@pytest.mark.parametrize("section,want", [
    ("x\n" * 16_001, "too_long"), ("a" * 2001, copy_text.TEXT_ONE_LINE_LONG),
    ("a\tb", copy_text.TEXT_PLAIN)], ids=["over-32000", "one-line-over-2000", "a-tab"])
def test_text_from_refuses_what_typed_text_may_not_hold(env, section, want):
    (run,) = admitted_run(env)
    env.casa.pad_sections["s"] = section
    env.app.calls.clear()
    text = env.ok("run_act", run_id=run, kind="type", handle="e1", generation=1, text_from="s")
    if want == "too_long":
        want = copy_text.TEXT_FROM_TOO_LONG.format(section='"s"')
    assert text == want
    assert _actions(env) == []


def test_text_from_of_exactly_32000_characters_is_typed(env):
    (run,) = admitted_run(env)
    env.casa.pad_sections["s"] = ("x" * 9 + "\n") * 3_200
    assert env.ok("run_act", run_id=run, kind="type", handle="e1", generation=1,
                  text_from="s").startswith("Done")
    assert len(env.app.posts("/action")[-1].body["text"]) == 32_000


def test_text_from_goes_alone_with_type_and_a_missing_section_is_casas_code(env):
    (run,) = admitted_run(env)
    env.app.calls.clear()
    assert env.ok("run_act", run_id=run, kind="type", handle="e1", generation=1, text="a",
                  text_from="s") == copy_text.TEXT_FROM_ALONE
    assert env.ok("run_act", run_id=run, kind="click", handle="e1", generation=1,
                  text_from="s") == copy_text.TEXT_FROM_ALONE
    assert _pad_calls(env) == []
    assert env.ok("run_act", run_id=run, kind="type", handle="e1", generation=1,
                  text_from="s") == copy_text.CASA_FAILED.format(code="no_section")
    assert _actions(env) == []

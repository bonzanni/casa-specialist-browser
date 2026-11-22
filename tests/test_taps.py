"""The tap tools: a token applies exactly the answer its button showed, once,
and no token ever appears in a model-visible result."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import copy_text
import state
from conftest import SERVER, admitted_run

def _forget_card(env, site_id="s2"):
    env.ok("ask_forget_login", site_id=site_id)
    return env.card()


def _continues(env):
    return [(b["binding_ref"], b["run_ref"], b["reason"]) for p, b in env.casa.calls
            if p.endswith("/continue")]


def test_no_token_in_any_tool_result(env):
    (run,) = admitted_run(env)
    texts, tokens = [], []
    texts.append(env.ok("ask_keep_going", run_id=run, done_text="Two pages read."))
    # the keep-going card carries no token at all (Close, and a stored
    # stop_runs call naming the request)
    assert not any("t" in b.get("call", {}).get("arguments", {})
                   for b in env.card()["buttons"])
    texts.append(env.ok("ask_forget_login", site_id="s1"))
    card = env.card()
    tokens += [b["call"]["arguments"]["t"] for b in card["buttons"]]
    texts.append(env.ok("apply_tap", t=env.token(card, "Keep")))
    blob = "\n".join(texts)
    for t in tokens:
        assert t not in blob
        assert hashlib.sha256(t.encode()).hexdigest() not in blob
    # tokens travel only inside card values
    for d in env.casa.deposits:
        if d["slot"] != "card":
            assert not any(t in json.dumps(d) for t in tokens)


def test_sibling_buttons_void(env):
    card = _forget_card(env)
    env.ok("apply_tap", t=env.token(card, "Keep"))
    env.app.calls.clear()
    for label in ("Forget login", "Keep"):
        out = json.loads(env.ok("apply_tap", t=env.token(card, label)))
        assert copy_text.UNKNOWN_TAP in json.dumps(out)
    assert env.app.forgets == [] and env.app.calls == []


def test_unknown_or_forged_tap(env):
    card = _forget_card(env)
    env.ok("apply_tap", t=env.token(card, "Keep"))
    env.app.calls.clear()
    env.casa.calls.clear()
    fresh = _forget_card(env)
    env.app.calls.clear()
    env.casa.calls.clear()
    for tool, t in (("apply_tap", "x" * 43), ("apply_tap", env.token(card, "Forget login")),
                    ("tap_live", "garbage"), ("tap_live", env.token(fresh, "Forget login"))):
        out = env.ok(tool, t=t)
        assert copy_text.UNKNOWN_TAP in out
    assert env.app.calls == [] and env.casa.calls == [] and env.app.forgets == []


_TAP = r"""
import sys, json
sys.path.insert(0, {server!r})
import browser_server, tooling
print(json.dumps(tooling.call("apply_tap", {{"t": {token!r}}})))
"""


def test_two_process_race(env):
    card = _forget_card(env)
    token = env.token(card, "Forget login")
    env.casa.calls.clear()
    procs = [subprocess.Popen([sys.executable, "-c", _TAP.format(server=str(SERVER),
                                                                 token=token)],
                              stdout=subprocess.PIPE, text=True, env=dict(os.environ))
             for _ in range(2)]
    outs = [p.communicate(timeout=60)[0] for p in procs]
    assert len(env.app.forgets) == 1
    assert sum(copy_text.UNKNOWN_TAP in o for o in outs) == 1


def test_forget_tap(env):
    env.ok("ask_forget_login", site_id="s2")
    card = env.card()
    assert [b["label"] for b in card["buttons"]] == ["Forget login", "Keep"]
    receipt = json.loads(env.ok("apply_tap", t=env.token(card, "Forget login")))
    assert receipt == {"receipt": copy_text.RECEIPT_FORGOT.format(site="Bank Example")}
    assert [f["site_id"] for f in env.app.forgets] == ["s2"]
    env.ok("ask_forget_login", site_id="s2")
    keep = env.card()
    env.ok("apply_tap", t=env.token(keep, "Keep"))
    assert len(env.app.forgets) == 1


def test_keep_going_and_stop(env):
    """[Keep going] closes the card (no call reaches Fido); [Stop] stores the
    call of `stop_runs` for the run's request (Casa runs it directly)."""
    r1, r2 = admitted_run(env, n=2)
    env.app.set_state(r1, "running")
    env.ok("ask_keep_going", run_id=r1, done_text="")
    card = env.card()
    assert "Fido keeps going; tap Stop to end it." in card["text"]
    key = state.run_binding(r1).request_group
    assert card["buttons"] == [
        {"label": copy_text.KEEP_GOING, "close": True},
        {"label": copy_text.STOP, "call": {"tool": "stop_runs",
                                           "arguments": {"ref": f"request:{key}"}}}]


def _legacy_keep_going(run_id: str) -> tuple[str, str]:
    """A keep-going card already on screen at rollout: its two apply_tap tokens."""
    group = "keep-legacy"
    return (state.mint_tap("keep_going", {"run_id": run_id}, group),
            state.mint_tap("stop", {"run_id": run_id}, group))


def test_a_legacy_keep_going_card_still_works(env):
    """`apply_tap`'s keep_going and stop kinds stay for cards already on
    screen (they keep the old path until they expire within the hour)."""
    r1, r2 = admitted_run(env, n=2)
    env.app.set_state(r1, "running")
    keep, _ = _legacy_keep_going(r1)
    env.app.calls.clear()
    env.casa.calls.clear()
    assert json.loads(env.ok("apply_tap", t=keep)) == {"receipt": copy_text.RECEIPT_KEEP_GOING}
    assert env.app.calls == [] and _continues(env) == []
    state.mint_tap("keep_going", {"run_id": r1}, "keep-legacy-2")
    stop = state.mint_tap("stop", {"run_id": r1}, "keep-legacy-2")
    env.casa.calls.clear()
    assert json.loads(env.ok("apply_tap", t=stop)) == {"receipt": copy_text.RECEIPT_STOPPED}
    assert len(env.app.posts("/cancel")) == 1 and env.app.runs[r1]["state"] == "cancelled"
    assert [c[1:] for c in _continues(env)] == [(r2, "next_run")]


def test_show_screenshot_bytes_never_returned(env):
    (run,) = admitted_run(env)
    canary = b"\x89PNG\r\n\x1a\n" + b"CANARY-PIXELS" * 10
    env.app.screenshots[(run, "shot1")] = canary
    env.app.add_event(run, "action", screenshot_id="shot1")
    out = json.loads(env.ok("show_screenshot", run_id=run, screenshot_id="shot1",
                            caption="Order accepted"))
    assert out["file"].startswith("casa-cap-") and "CANARY" not in json.dumps(out)
    dep = env.casa.deposits[-1]
    assert dep["kind"] == "photo" and dep["caption"] == "Order accepted"   # never "step N"
    staged = [p for p in env.out.iterdir()]
    assert len(staged) == 1 and staged[0].read_bytes() == canary
    assert dep["value"] == str(staged[0]) and staged[0].name.startswith("fido-")
    assert oct(staged[0].stat().st_mode & 0o777) == "0o640"
    env.app.screenshots[(run, "shot2")] = b"not a png"
    out = json.loads(env.ok("show_screenshot", run_id=run, screenshot_id="shot2",
                            caption="Order accepted"))
    assert out == {"file": None, "note": copy_text.NOT_A_PNG}


def test_show_screenshot_of_the_page_now(env):
    """After a confirmed order, the page it led to, with Fido's one line as the caption."""
    (run,) = admitted_run(env)
    env.app.set_state(run, "running")
    out = json.loads(env.ok("show_screenshot", run_id=run, caption="Order accepted, number 1234"))
    assert out["file"] and out["note"] == copy_text.SCREENSHOT_SENT
    dep = env.casa.deposits[-1]
    assert dep["kind"] == "photo" and dep["caption"] == "Order accepted, number 1234"
    assert open(dep["value"], "rb").read() == env.app.page_png
    env.app.set_state(run, "waiting_live")
    out = json.loads(env.ok("show_screenshot", run_id=run, caption="Order accepted"))
    assert out == {"file": None, "note": copy_text.SHOT_NOT_RUNNING}


def test_receipts_show_site_names_literally(env):
    env.app.sites.append({"site_id": "s9", "display_name": "ACME *Rewards* `Portal`",
                          "status": "ready"})
    env.ok("ask_forget_login", site_id="s9")
    r = json.loads(env.ok("apply_tap", t=env.token(env.card(), "Keep")))["receipt"]
    assert "\\*Rewards\\*" in r and "\\`Portal\\`" in r

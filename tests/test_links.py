"""Live-view and site-setup links: Casa posts the button; no grant, path or
token ever reaches the link, its caption or a model-visible result."""
from __future__ import annotations

import json
import re

import pytest

import casa_broker
import copy_text
import tools_ops
from conftest import ROOT, admitted_run


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(tools_ops.socket, "gethostname", lambda: "91d4d4c8-casa")


def test_live_link_carries_no_grant(env, host):
    (run,) = admitted_run(env)
    out = json.loads(env.ok("request_live_view", run_id=run, purpose="login",
                            reason="the bank wants a code"))
    assert out["live"].startswith("casa-cap-")
    assert out["note"] == copy_text.LIVE_SENT.format(ending=copy_text.HANDED_OVER_SILENT)
    assert "whole hand-over" in out["note"] and "<silent/>" in out["note"]
    dep = env.casa.deposits[-1]
    assert dep["slot"] == "live" and dep["label"] == copy_text.LIVE_LABEL
    grant_reply = env.app.posts("/live")[-1]
    assert grant_reply.body == {"purpose": "login"}
    blob = json.dumps(dep) + json.dumps(out)
    assert env.app.secret not in blob
    assert re.search(r"live/[0-9a-f]{16}", blob) is None        # operator_delivery_path
    assert re.search(r"\bg[0-9]+\b", blob) is None              # grant_id
    assert dep["value"] == ("https://my.home-assistant.io/redirect/supervisor_app/"
                            "?app=91d4d4c8_casa_browser")
    # one message from Fido, Done named once (by the caption, never the reason)
    # Casa heads the post with Fido's label; the caption leads with the reason
    assert dep["caption"] == ("The bank wants a code. Press Done on that page "
                              "when you have finished.")
    assert dep["caption"].count("Done") == 1
    assert "Fido" not in dep["caption"]
    from state import latest_link
    assert latest_link(run).reference == out["live"]
    assert env.app.runs[run]["state"] == "waiting_live"


@pytest.mark.parametrize("ha, expected", [
    ("https://ha.example.org", "https://ha.example.org/hassio/ingress/91d4d4c8_casa_browser"),
    ("https://ha.example.org:8123/", "https://ha.example.org:8123/hassio/ingress/"
                                     "91d4d4c8_casa_browser"),
    ("http://ha.example:8123", "https://my.home-assistant.io/redirect/supervisor_app/"
                             "?app=91d4d4c8_casa_browser"),
    ("", "https://my.home-assistant.io/redirect/supervisor_app/?app=91d4d4c8_casa_browser"),
    ("https://u:p" + "@ha.example", "https://my.home-assistant.io/redirect/supervisor_app/"
                               "?app=91d4d4c8_casa_browser"),
])
def test_live_link_url_forms(env, host, monkeypatch, ha, expected):
    monkeypatch.setenv("CASA_PLUGIN_BROWSER_HA_URL", ha)
    (run,) = admitted_run(env)
    env.ok("request_live_view", run_id=run, purpose="assist", reason="pick a date://www.x")
    dep = env.casa.deposits[-1]
    assert dep["value"] == expected
    for part in (dep["label"], dep["caption"]):
        assert "://" not in part and "www." not in part.lower() and "\n" not in part


def test_live_purpose_demo_is_gone(env, host):
    """The "show me once" view is deleted; only login and assist views are asked for."""
    (run,) = admitted_run(env)
    env.app.calls.clear()
    text, is_error = env.call("request_live_view", run_id=run, purpose="demo", reason="menu")
    assert is_error and copy_text.BAD_ARGUMENTS.format(name="purpose") in text
    assert env.app.posts("/live") == [] and env.casa.deposits == []


def test_live_refused_when_unsupported(env, host):
    (run,) = admitted_run(env)
    env.app.overrides[("POST", f"/v1/runs/{run}/live")] = (400, {"error": "unsupported"},
                                                            "application/json")
    out = json.loads(env.ok("request_live_view", run_id=run, purpose="login", reason="r"))
    assert out == {"live": None, "note": copy_text.BY_CODE["unsupported"] + " (unsupported)"}
    assert [d for d in env.casa.deposits if d["slot"] == "live"] == []


def test_live_unknown_host_asks_nothing_of_the_app(env, monkeypatch):
    monkeypatch.setattr(tools_ops.socket, "gethostname", lambda: "strange-host")
    (run,) = admitted_run(env)
    out = json.loads(env.ok("request_live_view", run_id=run, purpose="login", reason="r"))
    assert out["live"] is None and copy_text.UNKNOWN_HOST in out["note"]
    assert env.app.posts("/live") == []


def test_never_completes_live():
    """No plugin code path can end a live view: only the page's Done may."""
    src = "\n".join(p.read_text("utf-8")
                    for p in (ROOT / "plugins" / "browser" / "server").glob("*.py"))
    assert "live-complete" not in src and "live_complete" not in src


def test_setup_link_copy(env, host):
    out = json.loads(env.ok("site_setup_link", site_name="Bank Example"))
    dep = env.casa.deposits[-1]
    assert out["setup"] == dep["reference"]
    assert dep["label"] == copy_text.SETUP_LABEL
    # a site is no longer set up there (the sign-in card does it); the page is the
    # app's setup
    assert dep["caption"] == "Casa Browser's page for Bank Example."
    assert dep["value"].endswith("?app=91d4d4c8_casa_browser")


def test_a_live_view_for_a_specialist_ends_a_first_turn_with_a_line(env, host):
    """The operator's assistant is told by Casa that they have the message, so its
    turn ends silently; a specialist that asked still gets one line saying what the run waits
    for."""
    env.casa.requester["requester_kind"] = "specialist"
    env.casa.requester["chain"] = ["assistant", "finance", "browser"]
    (run,) = admitted_run(env)
    out = json.loads(env.ok("request_live_view", run_id=run, purpose="assist",
                            reason="Set the time"))
    assert out["note"] == copy_text.LIVE_SENT.format(ending=copy_text.HANDED_OVER_LINE)
    assert "<silent/>" not in out["note"]

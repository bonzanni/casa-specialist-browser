"""The exact result contract Casa enforces for every tool of the plugin."""
from __future__ import annotations

import json

from conftest import ROOT, casa_eval

PJ = ROOT / "plugins" / "browser" / ".claude-plugin" / "plugin.json"
CALLS = {"callsCasa": True}
SAFE = {"result": "safe", **CALLS}
NOTICE = {"result": "capability", "provides": ["notice"],
          "delivers": {"notice": "operator_message"}, **CALLS}

CARD = {"result": "capability", "provides": ["card"],
        "delivers": {"card": "operator_proposal"}, **CALLS}
FILE = {"result": "capability", "provides": ["file"], "delivers": {"file": "operator_file"},
        **CALLS}

EXPECTED = {
    "browser_status": NOTICE, "start_runs": SAFE, "continue_run": SAFE,
    "run_observe": SAFE, "run_act": SAFE, "run_search": SAFE, "run_fetch": SAFE, "run_wait": SAFE,
    "run_look": SAFE, "run_read": SAFE,
    "run_download": SAFE, "run_archive": SAFE, "run_records": SAFE,
    "run_finish": NOTICE, "run_cancel": SAFE,
    # action model: a site's emailed sign-in code
    "request_login_code": SAFE, "run_login_code": SAFE,
    "ask_forget_login": CARD, "ask_keep_going": CARD, "apply_tap": SAFE, "show_screenshot": FILE,
    # action model: the question card, and [Open live view], which posts the question
    # again as a new card carrying the link (a button may store a card tool, never a link
    # tool: Casa's `stored_calls._entry_ok`)
    "post_question": CARD, "tap_live": CARD,
    # the sign-in card for a site that is not set up
    "ask_sign_in": CARD,
    "request_live_view": {"result": "capability", "provides": ["live"],
                          "delivers": {"live": "operator_link"}, **CALLS},
    "site_setup_link": {"result": "capability", "provides": ["setup"],
                        "delivers": {"setup": "operator_link"}, **CALLS},
    "deliver_file": dict(FILE, filename=True), "handoff_files": SAFE,
    "progress": {"result": "capability", "provides": ["beat"],
                 "delivers": {"beat": "operator_message"}, **CALLS},
    "show_sources": {"result": "capability", "provides": ["sources"],
                     "delivers": {"sources": "operator_message"}, **CALLS},
    "runs_needing_driver": {"result": "safe"},
    # Casa calls it directly, with no session, so it calls nothing of Casa's
    "stop_runs": {"result": "safe"},
}


def test_contract_exact():
    contract = json.loads(PJ.read_text("utf-8"))["casa"]["resultContract"]
    assert contract == {"version": 1, "tools": EXPECTED}
    assert "setup_browser" not in contract["tools"]


def test_contract_slots_match_tools():
    import browser_server  # noqa: F401
    import tooling
    for name, entry in EXPECTED.items():
        slot = tooling.TOOLS[name]["slot"]
        assert slot == (entry["provides"][0] if entry["result"] == "capability" else None)


def test_contract_valid_in_casa(casa):
    got = casa_eval(casa, f"""
import plugin_store
m = json.loads(open({str(PJ)!r}).read())
c = plugin_store.manifest_result_contract(m)
keys = sorted(plugin_store._RC_ENTRY_KEYS)
print(json.dumps({{"tools": sorted(c["tools"]), "keys": keys,
                  "max_delivers": plugin_store._RC_MAX_DELIVERS,
                  "calls": sorted(n for n, e in c["tools"].items() if e.get("calls_casa"))}}))
""")
    assert got["tools"] == sorted(EXPECTED)
    assert got["calls"] == sorted(n for n in EXPECTED
                                  if n not in ("runs_needing_driver", "stop_runs"))
    for entry in EXPECTED.values():
        assert set(entry) <= set(got["keys"])
        assert len(entry.get("delivers", {})) <= got["max_delivers"]

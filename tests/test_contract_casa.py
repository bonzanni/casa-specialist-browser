"""Contract checks against the Casa revision `CASA_ROOT` names.

`test_casa_has_the_release_this_component_needs` runs first and fails loudly when that revision predates
the Casa release this component needs, instead of letting later manifest
checks fail obscurely (`callsCasa` and `casa.driverSweep` are unknown to an
older Casa, which refuses the component at install).
"""
from __future__ import annotations

import json

from conftest import casa_eval

NEEDS = ("the Casa release with plugin calls to Casa (callsCasa), the driver "
         "sweep (casa.driverSweep), session.long_runs, own_chat and the persona avatar")


def test_casa_has_the_release_this_component_needs(casa):
    got = casa_eval(casa, """
import plugin_store
from pathlib import Path
out = {}
m = {"name": "x", "casa": {"resultContract": {"version": 1, "tools": {
    "a": {"result": "safe", "callsCasa": True},
    "sweep": {"result": "safe"}}}, "driverSweep": "sweep"}}
try:
    c = plugin_store.manifest_result_contract(m)
    out["callsCasa"] = bool(c["tools"]["a"].get("calls_casa"))
except Exception as exc:
    out["callsCasa"] = False
try:
    out["driverSweep"] = plugin_store.manifest_driver_sweep(m) == "sweep"
except Exception:
    out["driverSweep"] = False
schema = json.loads((Path(plugin_store.__file__).parent
                     / "defaults/schema/role.v1.json").read_text())
out["long_runs"] = "long_runs" in schema["properties"]["session"]["properties"]
out["own_chat"] = "own_chat" in schema["properties"]
pm = json.loads((Path(plugin_store.__file__).parent
                 / "defaults/schema/persona-manifest.v1.json").read_text())
out["avatar"] = "avatar.jpg" in pm["properties"]["files"]["items"]["properties"]["path"]["enum"]
print(json.dumps(out))
""")
    assert got == {"callsCasa": True, "driverSweep": True, "long_runs": True,
                   "own_chat": True, "avatar": True}, (
        f"CASA_ROOT is a Casa revision without {NEEDS}: {got}. Pin "
        "tests/casa-pin.txt to a main commit that contains it.")


def test_a_picture_passes_casas_result_hook_unchanged(casa):
    """`run_look` is declared `safe`: Casa's real result hook leaves its result as it is, the
    image block included, and asks nothing of its shape."""
    from conftest import ROOT
    got = casa_eval(casa, f"""
import asyncio, plugin_grants as pg, plugin_store, result_broker as rb
m = json.loads(open({str(ROOT / "plugins/browser/.claude-plugin/plugin.json")!r}).read())
entry = plugin_store.manifest_result_contract(m)["tools"]["run_look"]
name = "mcp__plugin_browser_browser__run_look"
cmap = pg.ResultContractMap(
    tools={{name: pg.ToolContract(artifact_id="a", plugin_seg="browser", kind=entry["result"],
                                 provides=(), consumes={{}}, calls_casa=True)}},
    plugins={{"browser": pg.PluginContract(artifact_id="a", adopted=True,
                                          setup_tools=frozenset())}})
hook = rb.make_result_hook(cmap, client_id="c1")
response = [{{"type": "text", "text": "The page."}},
            {{"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}}]
out = asyncio.run(hook({{"hook_event_name": "PostToolUse", "tool_name": name, "tool_input": {{}},
                        "tool_response": response}}, "call-1", {{}}))
print(json.dumps({{"kind": entry["result"], "out": out}}))
""")
    assert got == {"kind": "safe", "out": {}}


def test_a_long_stop_listing_reaches_casa_whole(casa, monkeypatch):
    """Casa parses the stop listing itself: past the model's 48,000-character cut it stays JSON,
    and Casa's own reader finds every request."""
    import browser_server  # noqa: F401
    import tools_run
    import tooling
    requests = [{"ref": f"request:r{i}", "tasks": ["t" * 150] * 8, "state": "running"}
                for i in range(50)]
    monkeypatch.setattr(tools_run, "_listing", lambda: {"requests": requests})
    text, is_error = tooling.call("stop_runs", {})
    assert not is_error and len(text) > tooling.MAX_RESULT_CHARS
    got = casa_eval(casa, f"""
import plugin_stop
print(json.dumps(plugin_stop._parse(json.loads({json.dumps(json.dumps(text))}), {{}})))
""")
    assert not got.get("failed") and len(got["requests"]) == 50


def test_every_result_casa_parses_is_never_cut():
    """The tools Casa calls with no model, and the tap's receipt: a cut would break their JSON."""
    import browser_server  # noqa: F401
    import tooling
    from conftest import ROOT
    casa = json.loads((ROOT / "plugins/browser/.claude-plugin/plugin.json")
                      .read_text("utf-8"))["casa"]
    for name in (casa["driverSweep"], casa["stopTool"], "apply_tap"):
        assert tooling.TOOLS[name]["receipt"] is True, name

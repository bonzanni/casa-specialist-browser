"""The MCP server's surface: the tool list matches the manifest, arguments are
checked before any tool runs, and the MCP timeout cannot cut a run_wait."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

import copy_text
from conftest import ROOT

PLUGIN = ROOT / "plugins" / "browser"


def _manifest():
    return json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text("utf-8"))


def _rpc(*reqs, env=None):
    out = subprocess.run([sys.executable, str(PLUGIN / "server" / "browser_server.py")],
                         input="\n".join(json.dumps(r) for r in reqs) + "\n",
                         capture_output=True, text=True, timeout=60, env=env)
    return [json.loads(x) for x in out.stdout.splitlines()], out.stderr


def test_tools_list_matches_contract():
    resp, _ = _rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp[0]["result"]["tools"]}
    casa = _manifest()["casa"]
    assert names == set(casa["resultContract"]["tools"]) | {"setup_browser"}
    assert sorted(casa["provides_tools"]) == sorted(
        f"mcp__plugin_browser_browser__{n}" for n in names)
    for t in resp[0]["result"]["tools"]:
        schema = t["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
    # action model: the tool list is unchanged; run_act gains `enter`
    act = next(t for t in resp[0]["result"]["tools"] if t["name"] == "run_act")
    assert act["inputSchema"]["properties"]["enter"] == {"type": "boolean"}
    assert "enter" not in act["inputSchema"]["required"]


@pytest.mark.parametrize("extra", ["admission", "requester_id", "binding_ref", "url",
                                   "selector", "secret"])
def test_unknown_args_rejected(env, extra):
    env.app.calls.clear()
    for name, args in (("run_observe", {"run_id": "run1"}),
                       ("start_runs", {"runs": []}),
                       ("browser_status", {}),
                       ("continue_run", {"run_ref": "run1"})):
        text, is_error = env.call(name, **dict(args, **{extra: "x"}))
        assert is_error and copy_text.UNKNOWN_ARGUMENT in text and extra not in text
    assert env.app.calls == [] and env.casa.calls == []


@pytest.mark.parametrize("args", [{"run_id": "../x"}, {"run_id": "a" * 65}, {"run_id": 5},
                                  {"run_id": ""}, {}])
def test_ids_validated_before_call(env, args):
    env.app.calls.clear()
    text, is_error = env.call("run_observe", **args)
    assert is_error
    assert env.app.calls == [] and env.casa.calls == []


def test_mcp_timeout_cannot_cut_run_wait():
    """Measured on CLI 2.1.293: a per-server `timeout` is a SILENCE limit that
    aborts a call that is silent for longer; unset, a silent 400 s call
    completes. run_wait is silent for up to 55 s, so none is set (or one
    far above it)."""
    server = json.loads((PLUGIN / ".mcp.json").read_text("utf-8"))["mcpServers"]["browser"]
    assert "timeout" not in server or server["timeout"] >= 600_000
    import tools_run  # noqa: F401
    import tooling
    assert tooling.TOOLS["run_wait"]["schema"]["properties"]["max_wait_s"]["maximum"] <= 55


def test_server_logs_nothing_on_a_call(env):
    import os
    resp, stderr = _rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "browser_status", "arguments": {}}},
                        env=dict(os.environ))
    assert resp[0]["result"]["isError"] is False
    assert env.app.secret not in stderr and env.app.secret not in json.dumps(resp)


def test_setup_tool_is_error_when_not_paired(app_env):
    import browser_server  # noqa: F401
    import tooling
    text, is_error = tooling.call("setup_browser", {})
    assert is_error is True
    import copy_text
    assert text == copy_text.NO_WINDOW
    app_env.arm()
    text, is_error = tooling.call("setup_browser", {})
    assert is_error is False and text.startswith("Paired with Casa Browser")

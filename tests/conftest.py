"""Shared test setup.

The plugin's server modules are imported the way Casa runs them: with
`plugins/browser/server/` as the first entry of `sys.path`, so a test imports
`app_client`, `pairing` or `state` by their bare names.

Casa's own functions are consulted only in a SUBPROCESS (`casa_eval`), never
imported here: Casa's flat modules and the plugin's flat modules share one
namespace on `sys.path`, and a cross-check that imported both could compare
the plugin with itself without noticing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "plugins" / "browser" / "server"
SCRIPTS = ROOT / "scripts"
for p in (str(SERVER), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)


def casa_root() -> Path | None:
    """The Casa checkout `CASA_ROOT` names (its root, holding
    `casa/rootfs/opt/casa/`), or None when it is unset."""
    raw = os.environ.get("CASA_ROOT", "")
    if not raw:
        return None
    root = Path(raw).expanduser().resolve()
    if not (root / "casa" / "rootfs" / "opt" / "casa").is_dir():
        raise RuntimeError(f"CASA_ROOT={raw} is not a Casa checkout "
                           "(no casa/rootfs/opt/casa)")
    return root


@pytest.fixture
def casa():
    """The Casa checkout, or a skip WITH a reason (never a silent pass)."""
    root = casa_root()
    if root is None:
        pytest.skip("CASA_ROOT is unset: the cross-check against Casa's own "
                    "code did not run")
    return root


def casa_eval(root: Path, code: str, *, cwd: Path | None = None):
    """Run `code` in a fresh interpreter with Casa's code directory first on
    `sys.path` and return the JSON it prints as its last line."""
    casa_dir = root / "casa" / "rootfs" / "opt" / "casa"
    prog = f"import sys, json\nsys.path.insert(0, {str(casa_dir)!r})\n{code}"
    out = subprocess.run([sys.executable, "-I", "-c", prog], cwd=cwd or ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"Casa evaluation failed:\n{out.stderr[-3000:]}"
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture(autouse=True)
def _fresh_turn_steps():
    """Every test is a fresh turn for the step bound: its count lives in the plugin process
    (one per turn in Casa), which every test here shares."""
    module = sys.modules.get("tooling")
    if module is not None:
        module.calls_made = 0
    yield


@pytest.fixture
def fakeapp():
    from fakeapp import FakeApp
    app = FakeApp().start()
    yield app
    app.stop()


@pytest.fixture
def plugin_data(tmp_path, monkeypatch):
    """A fresh `$CLAUDE_PLUGIN_DATA`, as Casa gives each plugin."""
    data = tmp_path / "plugin-data"
    data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    return data


@pytest.fixture
def app_env(fakeapp, plugin_data, monkeypatch):
    """The plugin pointed at the fake app through the documented override."""
    monkeypatch.setenv("CASA_PLUGIN_BROWSER_APP_URL", fakeapp.url)
    return fakeapp


def closed_port() -> int:
    """A local TCP port nothing listens on."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def broker(monkeypatch):
    from fakebroker import FakeBroker
    b = FakeBroker().start()
    for k, v in b.env().items():
        monkeypatch.setenv(k, v)
    yield b
    b.stop()


@pytest.fixture
def env(app_env, broker, plugin_data, monkeypatch, tmp_path):
    """A paired plugin, the fake app, the fake broker, an outbox and a fresh
    turn."""
    import pairing
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    monkeypatch.setenv("CASA_PLUGIN_OUTBOX_DIR", str(outbox))
    import continuation
    import tools_run
    pairing.store_secret(app_env.pair())
    monkeypatch.setattr(continuation, "clock", continuation.time.monotonic)
    monkeypatch.setattr(continuation, "_STARTED", continuation.time.monotonic())
    tools_run._cursor.clear()
    tools_run._WOKEN.clear()
    monkeypatch.setattr(continuation, "TURN_STARTED_AT", continuation.time.time())

    class Env:
        app = app_env
        casa = broker
        data = plugin_data
        out = outbox

        @staticmethod
        def card(index: int = -1) -> dict:
            """The card value of a proposal deposit (tokens included)."""
            import json as _json
            props = [d for d in broker.deposits if d["slot"] == "card"]
            return _json.loads(props[index]["value"])

        @staticmethod
        def token(card: dict, label: str) -> str:
            return next(b["call"]["arguments"]["t"] for b in card["buttons"]
                        if b["label"] == label)

        @staticmethod
        def call(name, **args):
            import browser_server  # noqa: F401 — registers every tool
            import tooling
            return tooling.call(name, args)

        @staticmethod
        def ok(name, **args):
            text, is_error = Env.call(name, **args)
            assert not is_error, text
            return text

    return Env


def admitted_run(env, *, mode="account", sites=None, n=1, binding=None):
    """Start `n` admitted runs through start_runs; returns their run ids."""
    import state
    sites = sites if sites is not None else [{"site_id": "s1", "access": "account"}]
    runs = [{"mode": mode, "sites": sites, "task_kind": "invoice",
             "task_text": f"Download invoice {i}.", "result_expectation": "A PDF."}
            for i in range(n)]
    before = set(env.app.runs)
    env.ok("start_runs", runs=runs)
    created = [r for r in env.app.runs if r not in before]
    assert len(created) == n
    return created


@pytest.fixture
def casa_optional():
    """The Casa checkout when `CASA_ROOT` is set, else None (the test runs
    its own checks either way)."""
    return casa_root()

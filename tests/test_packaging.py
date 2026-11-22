"""The component as Casa installs it: manifests, digests, persona, plugin.

Casa refuses a component whose pinned digests do not match the tree it
fetched, so every digest is checked against the TRACKED tree (what a tag
ships), and, when `CASA_ROOT` names a Casa checkout, against Casa's own
functions run on that same tree.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import ROOT, casa_eval

PLUGIN = ROOT / "plugins" / "browser"
DIGESTS = ROOT / "scripts" / "digests.py"
NOREPLY = "3899230+bonzanni@users.noreply.github.com"


def _manifest() -> dict:
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def _git(*args: str, cwd: Path = ROOT, binary: bool = False):
    out = subprocess.run(["git", "-C", str(cwd), *args], check=True,
                         capture_output=True)
    return out.stdout if binary else out.stdout.decode("utf-8")


def tracked_copy(dest: Path) -> Path:
    """Materialise the git index into `dest` (modes and symlinks kept) and
    make `dest` its own repository with everything staged, so
    `digests.py --root dest` reads exactly what was copied."""
    raw = _git("ls-files", "-sz", binary=True)
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.decode("utf-8").partition("\t")
        mode = meta.split()[0]
        blob = _git("show", f":{name}", binary=True)
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            target.symlink_to(blob.decode("utf-8"))
            continue
        target.write_bytes(blob)
        target.chmod(0o755 if mode == "100755" else 0o644)
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    return dest


def run_digests(root: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(DIGESTS), "--root", str(root), *flags],
                          capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------

def test_manifest_shape():
    m = _manifest()
    assert set(m) == {"api_version", "component_id", "version", "default_persona",
                      "dependencies", "checksum"}
    assert m["api_version"] == "casa.specialist-component/v1"
    assert m["component_id"] == "casa/browser"
    assert m["version"] == "0.1.35"
    assert m["default_persona"]["ref"] == "casa/fido@0.1.35"
    deps = m["dependencies"]
    assert [d["kind"] for d in deps] == ["persona", "plugin/implementation"]
    persona, plugin = deps
    assert persona["identifier"] == "casa/fido@0.1.35"
    assert set(persona) == {"kind", "identifier", "digest"}
    assert persona["digest"] == m["default_persona"]["checksum"]
    assert plugin["identifier"] == "browser"
    assert plugin["source"] == {"type": "bundled", "path": "plugins/browser"}
    assert set(plugin) == {"kind", "identifier", "digest", "source"}
    assert not any(d["kind"] == "corpus/data" for d in deps)
    for value in (m["checksum"], persona["digest"], plugin["digest"]):
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", value)


def test_digests_current(tmp_path):
    ok = run_digests(ROOT, "--check")
    assert ok.returncode == 0, ok.stdout + ok.stderr

    cases = {
        "role/doctrine.md": {"manifest.json checksum"},
        "persona/pack/persona.md": {"persona/manifest.json",
                                    "manifest.json default_persona.checksum",
                                    "manifest.json persona digest"},
        "plugins/browser/server/browser_server.py": {"manifest.json plugin digest"},
    }
    every = set().union(*cases.values())
    for i, (path, expected) in enumerate(cases.items()):
        tree = tracked_copy(tmp_path / f"t{i}")
        assert run_digests(tree, "--check").returncode == 0
        target = tree / path
        target.write_bytes(target.read_bytes() + b"x")
        subprocess.run(["git", "-C", str(tree), "add", path], check=True)
        bad = run_digests(tree, "--check")
        assert bad.returncode == 1, path
        named = {n for n in every if n in bad.stdout}
        assert named == expected, (path, bad.stdout)
        # --write repairs exactly what --check named
        assert run_digests(tree, "--write").returncode == 0
        subprocess.run(["git", "-C", str(tree), "add", "-A"], check=True)
        assert run_digests(tree, "--check").returncode == 0, path


def test_digests_ignore_untracked_and_bytecode(tmp_path):
    """What ships is the index: an untracked file or an interpreter cache in
    the plugin directory changes no digest (Casa strips caches before it
    hashes, and an untracked file is not in a tag)."""
    tree = tracked_copy(tmp_path / "t")
    (tree / "plugins/browser/server/__pycache__").mkdir()
    (tree / "plugins/browser/server/__pycache__/x.cpython-311.pyc").write_bytes(b"\0")
    (tree / "plugins/browser/server/scratch.txt").write_text("not tracked\n")
    assert run_digests(tree, "--check").returncode == 0


def test_digest_functions_frame_exec_bit_and_symlink(tmp_path):
    import digests
    a = tmp_path / "a"
    (a / "server").mkdir(parents=True)
    (a / "server" / "x.py").write_text("print(1)\n")
    before = digests.plugin_digest(a)
    (a / "server" / "x.py").chmod(0o755)
    assert digests.plugin_digest(a) != before
    (a / "server" / "x.py").chmod(0o644)
    assert digests.plugin_digest(a) == before
    (a / ".casa-artifact.json").write_text("{}")
    assert digests.plugin_digest(a) == before
    (a / "link").symlink_to("server/x.py")
    assert digests.plugin_digest(a) != before


# A persona avatar is optional; the shipped pack has none. These tests add one to a copy.
FAKE_JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4 + b"\r\n \t\xff\xd9"


def _pack_with_avatar(tmp_path):
    pack = tmp_path / "pack"
    shutil.copytree(ROOT / "persona/pack", pack)
    (pack / "avatar.jpg").write_bytes(FAKE_JPEG)
    return pack


def test_avatar_row_is_its_raw_bytes(tmp_path):
    """The avatar is checksummed as it is: never decoded or canonicalised as text."""
    import hashlib
    import digests
    pack = _pack_with_avatar(tmp_path)
    m = digests.persona_manifest(pack)
    assert [r["path"] for r in m["files"]] == ["avatar.jpg", "persona.md", "persona.yaml"]
    assert m["files"][0]["checksum"] == "sha256:" + hashlib.sha256(FAKE_JPEG).hexdigest()
    assert m["checksum"] != digests.persona_manifest(ROOT / "persona/pack")["checksum"]


def test_avatar_digest_matches_casa(casa, tmp_path):
    """Casa's own persona loader accepts the manifest digests.py writes for a pack with an
    avatar, and computes the same checksum."""
    import digests
    pack = _pack_with_avatar(tmp_path)
    ours = digests.persona_manifest(pack)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(ours, indent=2) + "\n", encoding="utf-8")
    got = casa_eval(casa, f"""
from pathlib import Path
import persona_pack
p = persona_pack.load_persona_pack(Path({str(pack)!r}), Path({str(manifest)!r}))
print(json.dumps({{"checksum": p.checksum, "avatar": p.avatar_path.name}}))
""")
    assert got == {"checksum": ours["checksum"], "avatar": "avatar.jpg"}


def test_digests_match_casa(casa, tmp_path):
    import digests
    tree = tracked_copy(tmp_path / "t")
    ours = {
        "plugin": digests.plugin_digest(tree / "plugins/browser"),
        "persona": digests.persona_manifest(tree / "persona/pack")["checksum"],
        "component": digests.component_checksum(tree),
    }
    theirs = casa_eval(casa, f"""
from pathlib import Path
import plugin_store, persona_pack, specialist_component
tree = Path({str(tree)!r})
plugin = tree / "plugins/browser"
plugin_store.strip_bytecode_derivatives(plugin)
pack = persona_pack.load_persona_pack(tree / "persona/pack", tree / "persona/manifest.json")
files = {{n: (tree / n).read_bytes() for n in
         ("role/role.yaml", "role/doctrine.md", "config-schema.json")}}
print(json.dumps({{
    "plugin": "sha256:" + plugin_store.content_checksum(plugin),
    "persona": pack.checksum,
    "component": specialist_component.compute_component_checksum(files),
}}))
""")
    assert ours == theirs
    m = _manifest()
    assert m["dependencies"][1]["digest"] == theirs["plugin"]
    assert m["default_persona"]["checksum"] == theirs["persona"]
    assert m["checksum"] == theirs["component"]


def test_component_loads_in_casa(casa, tmp_path):
    """Casa's own component loader (schema, role artifact, persona binding,
    checksum) accepts the component as the index holds it."""
    tree = tracked_copy(tmp_path / "t")
    got = casa_eval(casa, f"""
from pathlib import Path
import specialist_component
tree = Path({str(tree)!r})
c = specialist_component.load_specialist_component(tree, tree / "manifest.json")
print(json.dumps({{"id": c.component_id, "slug": c.slug}}))
""")
    assert got == {"id": "casa/browser", "slug": "browser"}


def test_persona_shape():
    pack = ROOT / "persona" / "pack"
    persona = yaml.safe_load((pack / "persona.yaml").read_text(encoding="utf-8"))
    assert persona["api_version"] == "casa.persona/v1"
    assert persona["id"] == "casa/fido"
    assert persona["version"] == "0.1.35"
    assert persona["identity"]["display_name"] == "Fido"
    assert persona["relationship_posture"] == "professional"
    assert persona["traits"] == {"warmth": 3, "formality": 2, "candor": 5,
                                 "attunement": 4, "curiosity": 4, "levity": 2,
                                 "social_energy": 2, "optimism": 3}
    assert sorted(p.name for p in pack.iterdir()) == ["avatar.jpg", "persona.md", "persona.yaml"]
    modes = {}
    for entry in _git("ls-files", "-s", "persona/pack").splitlines():
        meta, _, name = entry.partition("\t")
        modes[name] = meta.split()[0]
    assert modes == {"persona/pack/avatar.jpg": "100644",
                     "persona/pack/persona.md": "100644",
                     "persona/pack/persona.yaml": "100644"}
    md = (pack / "persona.md").read_text(encoding="utf-8")
    # Casa counts the whole level-1 section, its "## Negative space" included
    core = md.split("# Core\n", 1)[1].split("\n# ", 1)[0].strip()
    assert 300 <= len(core) <= 500, len(core)
    assert "## Negative space" in md


def test_persona_loads_in_casa(casa):
    got = casa_eval(casa, f"""
from pathlib import Path
import persona_pack
root = Path({str(ROOT)!r})
p = persona_pack.load_persona_pack(root / "persona/pack", root / "persona/manifest.json")
print(json.dumps({{"id": p.persona_id, "version": p.version,
                  "name": p.identity["display_name"]}}))
""")
    assert got == {"id": "casa/fido", "version": "0.1.35", "name": "Fido"}


def test_plugin_manifest_shape():
    pj = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert pj["name"] == "browser"
    assert pj["version"] == "0.1.0"
    casa = pj["casa"]
    # driverSweep and stopTool each come with their tool.
    assert set(casa) == {"setupTool", "resultContract", "provides_tools", "driverSweep",
                         "stopTool"}
    assert casa["driverSweep"] == "runs_needing_driver"
    assert casa["stopTool"] == "stop_runs"
    assert "jobs" not in casa and "triggers" not in casa
    assert casa["setupTool"] == "setup_browser"
    assert casa["resultContract"]["version"] == 1
    contract_tools = set(casa["resultContract"]["tools"])
    assert "setup_browser" not in contract_tools
    assert sorted(casa["provides_tools"]) == sorted(
        f"mcp__plugin_browser_browser__{t}" for t in contract_tools | {"setup_browser"})

    mcp = json.loads((PLUGIN / ".mcp.json").read_text("utf-8"))
    assert list(mcp) == ["mcpServers"]
    assert list(mcp["mcpServers"]) == ["browser"]
    server = mcp["mcpServers"]["browser"]
    assert server.get("type", "stdio") == "stdio"
    assert server["command"] == "python3"
    assert server["args"] == ["${CLAUDE_PLUGIN_ROOT}/server/browser_server.py"]
    assert server["env"] == {
        "CASA_PLUGIN_BROWSER_APP_URL": "${CASA_PLUGIN_BROWSER_APP_URL:-}",
        "CASA_PLUGIN_BROWSER_HA_URL": "${CASA_PLUGIN_BROWSER_HA_URL:-}",
    }
    # no bare ${VAR}: a missing value never withholds the plugin
    text = (PLUGIN / ".mcp.json").read_text("utf-8")
    for ref in re.findall(r"\$\{([^}]*)\}", text):
        assert ref == "CLAUDE_PLUGIN_ROOT" or ref.endswith(":-"), ref


def test_plugin_manifest_valid_in_casa(casa, tmp_path):
    """Casa's manifest validator and result-contract parser accept the
    plugin as the index holds it."""
    tree = tracked_copy(tmp_path / "t")
    got = casa_eval(casa, f"""
from pathlib import Path
import plugin_store
plugin = Path({str(tree)!r}) / "plugins/browser"
m = plugin_store.validate_manifest(plugin, "browser")
print(json.dumps({{"setup": plugin_store.manifest_setup_tool(m),
                  "contract": sorted((plugin_store.manifest_result_contract(m) or {{}}).keys())}}))
""")
    assert got["setup"] == "setup_browser"


def test_config_schema():
    assert json.loads((ROOT / "config-schema.json").read_text("utf-8")) == {
        "required": [], "secret_names": []}


def test_server_skeleton_speaks_mcp():
    """The bundled server answers initialize and tools/list over stdio, and
    lists no tool the manifest does not declare."""
    pj = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    declared = {t.rsplit("__", 1)[1] for t in pj["casa"]["provides_tools"]}
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}}]
    out = subprocess.run(
        [sys.executable, str(PLUGIN / "server" / "browser_server.py")],
        input="\n".join(json.dumps(r) for r in reqs) + "\n",
        capture_output=True, text=True, timeout=30)
    lines = [json.loads(x) for x in out.stdout.splitlines()]
    assert [x["id"] for x in lines] == [1, 2, 3]
    assert lines[0]["result"]["protocolVersion"] == "2024-11-05"
    assert lines[0]["result"]["serverInfo"]["name"] == "browser"
    assert {t["name"] for t in lines[1]["result"]["tools"]} <= declared
    assert lines[2]["result"]["isError"] is True
    assert "nope" not in lines[2]["result"]["content"][0]["text"]


# --- publication hygiene -----------------------------------------------------

# Built from parts so this file does not contain what it forbids.
_FORBIDDEN = ["design" + ".md", "plan" + "-", "spi" + "ke"]
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)")
_HOSTS = {"github.com", "api.github.com", "my.home-assistant.io",
          "home-assistant.io", "www.home-assistant.io", "code.claude.com"}


def test_no_private_material():
    names = [n for n in _git("ls-files", "-z").split("\0") if n]
    assert names
    for name in names:
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        low = text.lower()
        for bad in _FORBIDDEN:
            assert bad not in low, f"{name} contains {bad!r}"
        for addr in _EMAIL_RE.findall(text):
            assert addr in (NOREPLY, "git@github.com"), f"{name}: {addr}"
        for host in _URL_RE.findall(text):
            if "." not in host or re.fullmatch(r"[0-9.]+", host):
                continue            # a container host name or an address literal
            assert host.lower() in _HOSTS or host.endswith((".example", ".invalid", ".test",
                                                           "example.org", "example.com")) \
                or host in ("localhost", "127.0.0.1"), f"{name}: {host}"
    ignore = (ROOT / ".gitignore").read_text("utf-8").splitlines()
    assert "docs/" in ignore and ".claude/" in ignore and ".superpowers/" in ignore


def test_server_reports_the_installed_revision(tmp_path):
    """The serverInfo version carries the revision Casa recorded in the installed copy's
    `.casa-artifact.json`: the Claude CLI logs it at each connection, which is how a deploy
    proves the code a turn ran (a turn can run an older copy than the one installed)."""
    import browser_server
    assert browser_server.server_version(str(tmp_path)) == "0.1.0"
    (tmp_path / ".casa-artifact.json").write_text(json.dumps(
        {"revision": "git:227c5f63b8501d5f42e0a4b3a3c5cee745a3e751"}), "utf-8")
    assert browser_server.server_version(str(tmp_path)) == "0.1.0+227c5f63b850"
    copy = tmp_path / "copy"
    shutil.copytree(PLUGIN, copy, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(tmp_path / ".casa-artifact.json", copy / ".casa-artifact.json")
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}}]
    out = subprocess.run([sys.executable, str(copy / "server" / "browser_server.py")],
                         input=json.dumps(reqs[0]) + "\n", capture_output=True, text=True,
                         timeout=30)
    info = json.loads(out.stdout.splitlines()[0])["result"]["serverInfo"]
    assert info == {"name": "browser", "version": "0.1.0+227c5f63b850"}

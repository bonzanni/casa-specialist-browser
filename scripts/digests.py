#!/usr/bin/env python3
"""Compute and check the digests Casa verifies when it installs this component.

Three values, each a copy of Casa's own definition:

* the bundled plugin's digest (`manifest.json` → the `plugin/implementation`
  dependency) — Casa's `plugin_store.content_checksum` over `plugins/browser/`
  after interpreter caches are stripped: one length-framed line per entry,
  `len:rel\\0type\\0execbit\\0payload`, sorted by path, `.casa-artifact.json`
  excluded;
* the persona manifest (`persona/manifest.json`, and the persona checksum in
  `manifest.json`) — Casa's `persona_pack`: one row per pack file over its
  canonical text (the avatar `avatar.jpg` over its raw bytes), and a SHA-256
  of the RFC 8785 form of the rows;
* the component checksum (`manifest.json` `checksum`) — Casa's
  `specialist_component.compute_component_checksum` over the raw bytes of
  `role/role.yaml`, `role/doctrine.md` and `config-schema.json`.

`--write` and `--check` read the git INDEX — what the next commit ships — never
untracked files: stage an edit before rewriting its digests. Standard library
only.

    python3 scripts/digests.py --write      # rewrite the stale values
    python3 scripts/digests.py --check      # exit 1 naming each stale value
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

PLUGIN_DIR = "plugins/browser"
PERSONA_DIR = "persona/pack"
PERSONA_MANIFEST = "persona/manifest.json"
# The persona avatar: checksummed over its raw bytes, as Casa's `persona_pack` does.
BINARY_PERSONA_FILES = frozenset({"avatar.jpg"})
COMPONENT_FILES = ("role/role.yaml", "role/doctrine.md", "config-schema.json")
METADATA_FILENAME = ".casa-artifact.json"


# --- canonical forms ----------------------------------------------------------

def canonical_json(value) -> bytes:
    """RFC 8785 (JCS) for the documents hashed here: objects with ASCII keys,
    strings and booleans. For exactly that input, sorted separator-tight
    `json.dumps` with `ensure_ascii=False` IS the JCS form; anything else
    (a float, a non-ASCII key, another type) is refused rather than guessed."""
    def check(v):
        if isinstance(v, dict):
            for k, item in v.items():
                if not isinstance(k, str) or not k.isascii():
                    raise ValueError("non-ASCII or non-string key")
                check(item)
        elif isinstance(v, list):
            for item in v:
                check(item)
        elif v is None or isinstance(v, (str, bool)):
            return
        elif isinstance(v, int):
            return
        else:
            raise ValueError(f"unsupported value type {type(v).__name__}")
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def checksum_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_text(text: str) -> str:
    """Casa's `canonical_bytes.canonical_text`: NFC, CRLF/CR → LF, trailing
    spaces and tabs stripped per line, exactly one final newline."""
    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in normalized.split("\n")]
    return "\n".join(lines).rstrip("\n") + "\n"


# --- the three digests -------------------------------------------------------

def _entry_line(rel: str, etype: str, exec_bit: int, payload: str) -> bytes:
    body = f"{rel}\x00{etype}\x00{exec_bit}\x00{payload}".encode("utf-8")
    return str(len(body)).encode("ascii") + b":" + body


def _is_bytecode(rel: str) -> bool:
    return "__pycache__" in rel.split("/") or rel.endswith(".pyc")


def plugin_digest(tree: Path) -> str:
    """`sha256:` + Casa's `content_checksum` of `tree` once its interpreter
    caches are stripped (computed without them; the tree is not modified)."""
    tree = Path(tree)
    lines: list[bytes] = []
    for p in sorted(tree.rglob("*")):
        rel = p.relative_to(tree).as_posix()
        if rel == METADATA_FILENAME or _is_bytecode(rel):
            continue
        st = p.lstat()
        exec_bit = 1 if (st.st_mode & stat.S_IXUSR) else 0
        if stat.S_ISLNK(st.st_mode):
            lines.append(_entry_line(rel, "l", 0, os.readlink(p)))
        elif stat.S_ISDIR(st.st_mode):
            lines.append(_entry_line(rel, "d", 0, ""))
        elif stat.S_ISREG(st.st_mode):
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            lines.append(_entry_line(rel, "f", exec_bit, digest))
        else:
            raise ValueError(f"special file in plugin tree: {rel}")
    return "sha256:" + hashlib.sha256(b"".join(lines)).hexdigest()


def persona_manifest(pack: Path) -> dict:
    """The `persona/manifest.json` Casa's persona loader expects for `pack`."""
    rows = []
    for path in sorted(Path(pack).iterdir(), key=lambda q: q.name):
        if path.name in BINARY_PERSONA_FILES:
            data = path.read_bytes()  # raw bytes, never decoded
        else:
            data = canonical_text(path.read_text(encoding="utf-8")).encode("utf-8")
        rows.append({"path": path.name, "type": "file", "executable": False,
                     "checksum": checksum_bytes(data)})
    payload = {"api_version": "casa.persona.manifest/v1", "files": rows}
    return {**payload, "checksum": checksum_bytes(canonical_json(payload))}


def component_checksum(root: Path) -> str:
    files = {name: (Path(root) / name).read_bytes() for name in COMPONENT_FILES}
    rows = [{"path": n, "checksum": checksum_bytes(files[n])} for n in sorted(files)]
    return checksum_bytes(canonical_json(
        {"api_version": "casa.specialist-component.manifest/v1", "files": rows}))


# --- the tracked tree -----------------------------------------------------------

def materialise_index(repo: Path, dest: Path) -> None:
    """Write every file of `repo`'s git index into `dest`, with its git mode
    (executable bit, symlink) — the tree a tag of the next commit ships."""
    raw = subprocess.run(["git", "-C", str(repo), "ls-files", "-sz"], check=True,
                         capture_output=True).stdout
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.decode("utf-8").partition("\t")
        mode, obj = meta.split()[:2]
        if mode == "160000":
            raise ValueError(f"submodule in the tracked tree: {name}")
        blob = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", obj],
                              check=True, capture_output=True).stdout
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            target.symlink_to(blob.decode("utf-8"))
            continue
        target.write_bytes(blob)
        target.chmod(0o755 if mode == "100755" else 0o644)


def expected_values(tree: Path) -> dict:
    persona = persona_manifest(tree / PERSONA_DIR)
    return {"persona_manifest": persona,
            "persona": persona["checksum"],
            "plugin": plugin_digest(tree / PLUGIN_DIR),
            "component": component_checksum(tree)}


def stale_values(tree: Path) -> list[str]:
    """The name of every value in `tree` that does not match its content."""
    want = expected_values(tree)
    manifest = json.loads((tree / "manifest.json").read_text(encoding="utf-8"))
    names = []
    try:
        persona_file = json.loads((tree / PERSONA_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        persona_file = None
    if persona_file != want["persona_manifest"]:
        names.append(PERSONA_MANIFEST)
    if manifest.get("checksum") != want["component"]:
        names.append("manifest.json checksum")
    if (manifest.get("default_persona") or {}).get("checksum") != want["persona"]:
        names.append("manifest.json default_persona.checksum")
    for dep in manifest.get("dependencies") or []:
        if dep.get("kind") == "persona" and dep.get("digest") != want["persona"]:
            names.append("manifest.json persona digest")
        if (dep.get("kind") == "plugin/implementation"
                and dep.get("digest") != want["plugin"]):
            names.append("manifest.json plugin digest")
    return names


def write_values(root: Path, want: dict) -> None:
    (root / PERSONA_MANIFEST).write_text(
        json.dumps(want["persona_manifest"], indent=2) + "\n", encoding="utf-8")
    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["default_persona"]["checksum"] = want["persona"]
    for dep in manifest["dependencies"]:
        if dep["kind"] == "persona":
            dep["digest"] = want["persona"]
        elif dep["kind"] == "plugin/implementation":
            dep["digest"] = want["plugin"]
    manifest["checksum"] = want["component"]
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = ap.parse_args(argv)
    root = args.root.resolve()
    with tempfile.TemporaryDirectory() as td:
        tree = Path(td) / "tree"
        materialise_index(root, tree)
        stale = stale_values(tree)
        if args.check:
            for name in stale:
                print(f"stale: {name}")
            if stale:
                print("run: python3 scripts/digests.py --write (after staging your edits)")
            return 1 if stale else 0
        write_values(root, expected_values(tree))
        for name in stale:
            print(f"rewrote: {name}")
        return 0


if __name__ == "__main__":
    sys.exit(main())

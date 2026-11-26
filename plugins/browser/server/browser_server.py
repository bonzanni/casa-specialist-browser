"""Fido's `browser` MCP server: newline-delimited JSON-RPC 2.0 over stdio.

Dispatch only. Every tool lives in its own module and registers itself in
`tooling.TOOLS`; `tooling.call` checks its arguments before it runs.

Nothing a caller sent, and no exception text, is echoed into a response:
an unknown tool, bad arguments or a failing tool each answer a fixed
sentence, so no secret, address or token can travel back through an error.
Standard library only (Casa runs plugin servers with the container's python3).
"""
from __future__ import annotations

import json
import logging
import os
import sys

import casa_broker
import copy_text
import pairing
import tooling
import tools_ops  # noqa: F401 — registers the cards, taps and screenshots
import tools_run  # noqa: F401 — registers the run tools
from tooling import ToolError, obj, tool

SERVER_NAME = "browser"
SERVER_VERSION = "0.1.0"


def server_version(root: str | None = None) -> str:
    """The version this server reports, followed by the revision Casa recorded when it
    installed this copy (`.casa-artifact.json` beside `server/`): the Claude CLI logs it
    at every connection, so a deploy can prove which code a turn actually ran."""
    path = os.path.join(root or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".casa-artifact.json")
    try:
        with open(path, encoding="utf-8") as fh:
            revision = json.load(fh).get("revision")
    except (OSError, ValueError, AttributeError):
        return SERVER_VERSION
    if not isinstance(revision, str) or not revision:
        return SERVER_VERSION
    return f"{SERVER_VERSION}+{revision.removeprefix('git:')[:12]}"
DEFAULT_PROTOCOL = "2024-11-05"
#: where a `tools/call` request's metadata names the call
CALL_ID_KEY = "claudecode/toolUseId"
MAX_CALL_ID = 128

#: name -> {"description", "schema", "fn", "slot"}
TOOLS = tooling.TOOLS


@tool("setup_browser",
      "Pair Fido with Casa Browser (the operator first presses Pair with Casa on the Casa "
      "Browser page). Safe to run again: it does nothing when already paired.", obj())
def setup_browser(args: dict) -> str:
    text, paired = pairing.setup_outcome()
    if not paired:
        # Casa counts only a non-error setup result as setup done
        raise ToolError(text)
    return text


def _tool_defs() -> list[dict]:
    return [{"name": name, "description": spec["description"],
             "inputSchema": spec["schema"]} for name, spec in TOOLS.items()]


def _call_id(params: dict) -> str | None:
    """The call's id from the request's metadata: a non-empty string of at most
    `MAX_CALL_ID` characters, else None (Casa's own calls send no metadata)."""
    meta = params.get("_meta")
    value = meta.get(CALL_ID_KEY) if isinstance(meta, dict) else None
    return value if isinstance(value, str) and 0 < len(value) <= MAX_CALL_ID else None


def _call(params: dict) -> tuple[str, bool]:
    name = params.get("name")
    if not isinstance(name, str):
        return copy_text.UNKNOWN_TOOL, True
    # kept for this call only: the loop handles one call at a time
    casa_broker.CALL_ID = _call_id(params)
    try:
        return tooling.call(name, params.get("arguments"))
    finally:
        casa_broker.CALL_ID = None


def handle(req) -> dict | None:
    if not isinstance(req, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request"}}
    has_id = "id" in req
    rid = req.get("id")
    method = req.get("method")
    if not isinstance(method, str):
        return {"jsonrpc": "2.0", "id": rid if has_id else None,
                "error": {"code": -32600, "message": "invalid request"}}
    if req.get("jsonrpc") != "2.0":
        if not has_id:
            return None
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32600, "message": "invalid jsonrpc version"}}
    params = req.get("params")
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        if not has_id:
            return None
        wanted = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": wanted if isinstance(wanted, str) and wanted
            else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": server_version()}}}
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        if not has_id:
            return None
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _tool_defs()}}
    if method == "tools/call":
        text, is_error = _call(params)
        if not has_id:
            return None
        content = [{"type": "text", "text": text}]
        image = tooling.take_image()
        if image is not None:
            content.append({"type": "image", "data": image, "mimeType": "image/png"})
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": content, "isError": is_error}}
    if not has_id:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": "unknown method"}}


def _reject_constant(name: str):
    raise ValueError("non-standard JSON constant")


def main() -> None:
    # stderr only, code words only: no module logs a value, a body or a secret
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(name)s: %(message)s")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                resp = handle(req)
            except Exception:  # noqa: BLE001 — never crash the stdio loop
                resp = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32603, "message": "internal error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

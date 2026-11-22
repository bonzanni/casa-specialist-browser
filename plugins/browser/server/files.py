"""Files on their way to whoever asked.

* **To the operator** (the requester is the operator's assistant): through
  Casa's plugin outbox as an `operator_file` of the kind Casa's media policy
  accepts — PDF → `document`, PNG/JPEG → `photo`, UTF-8 plain text (MT940
  included) → `text`, a zip → `zip`; anything else is zipped by the app
  first. Casa's caps are binary MB (`media_policies._MB`).
* **To a requesting specialist**: through Casa's handoff folder
  (`casa_handoff.publish`, 25 MB per file).

A file is staged under a fresh random name `fido-<16 hex>.<ext>` by writing a
`.part` file and linking it into place (never replacing anything), mode
0640; Casa claims it at delivery.
"""
from __future__ import annotations

import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import NamedTuple

OUTBOX_ENV = "CASA_PLUGIN_OUTBOX_DIR"
DEFAULT_OUTBOX = "/data/plugin-outbox"

_MB = 1024 * 1024
#: Casa's media policy caps (`media_policies.py`), checked against Casa in tests
CAPS = {"document": 20 * _MB, "photo": 10 * _MB, "zip": 20 * _MB, "text": 5 * _MB}
EXTENSIONS = {"document": (".pdf",), "photo": (".jpg", ".jpeg", ".png"), "zip": (".zip",),
              "text": (".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml", ".sta",
                       ".940")}
#: the largest file Telegram takes from Casa at all (its 20 MB document cap)
TELEGRAM_MAX = 20 * _MB
GROUP_CAP = 19_000_000
HANDOFF_MAX = 25_000_000
_TEXT_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_NAME_RE = re.compile(r"^[^/\\\x00-\x1f]{1,200}$")
_DEFAULT_EXT = {"document": ".pdf", "photo": ".png", "zip": ".zip", "text": ".txt"}


class Delivery(NamedTuple):
    kind: str
    filename: str


class FileMeta(NamedTuple):
    file_id: str
    size: int


NAME_MAX_BYTES = 255          # Casa's `_FILENAME_MAX_BYTES`


def _fit(stem: str, ext: str) -> str:
    """`stem + ext` within Casa's 255 UTF-8 bytes, the stem shortened."""
    while stem and len((stem + ext).encode("utf-8")) > NAME_MAX_BYTES:
        stem = stem[:-1]
    return (stem or "file") + ext


def _named(name: str, kind: str, ext: str | None = None) -> str:
    """`name` ending in an extension Casa accepts for `kind`: its own when it
    is one, else the kind's (or `ext`) appended (`statement.mt940` →
    `statement.mt940.txt`); always within Casa's filename byte limit."""
    base = name if _NAME_RE.fullmatch(name or "") else "file"
    own = os.path.splitext(base)[1]
    if ext is None and own.lower() in EXTENSIONS[kind]:
        return _fit(base[:-len(own)], own)
    if ext is not None and own.lower() == ext:
        return _fit(base[:-len(own)], own)
    return _fit(base, ext or _DEFAULT_EXT[kind])


def kind_for(safe_name: str, mime: str, head: bytes) -> Delivery | None:
    """The Casa media kind for a file by its content (`head` must be the
    whole file for text), or None when it must be zipped first."""
    if head.startswith(b"%PDF-"):
        return Delivery("document", _named(safe_name, "document"))
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return Delivery("photo", _named(safe_name, "photo", ".png"))
    if head.startswith(b"\xff\xd8\xff"):
        own = os.path.splitext(safe_name)[1].lower()
        return Delivery("photo", _named(safe_name, "photo",
                                        own if own in (".jpg", ".jpeg") else ".jpg"))
    if (head.startswith(b"PK\x03\x04") and len(head) >= 30) or (
            head.startswith(b"PK\x05\x06") and len(head) >= 22):
        # a zip, or an empty one, past Casa's fixed-header floors (30 / 22 bytes)
        return Delivery("zip", _named(safe_name, "zip"))
    if head:
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if _TEXT_FORBIDDEN.search(text) is None:
            return Delivery("text", _named(safe_name, "text"))
    return None


def plan_groups(files: list[FileMeta], cap: int = GROUP_CAP) -> list[list[str]]:
    """Files in order, greedily grouped so each group's sizes sum to at most
    `cap`; a file larger than `cap` is a group of its own (whoever sends it
    reports it too large). Nothing is dropped."""
    groups: list[list[str]] = []
    current: list[str] = []
    total = 0
    for f in files:
        if current and total + f.size > cap:
            groups.append(current)
            current, total = [], 0
        current.append(f.file_id)
        total += f.size
    if current:
        groups.append(current)
    return groups


def outbox_dir() -> Path:
    return Path(os.environ.get(OUTBOX_ENV) or DEFAULT_OUTBOX)


def stage(data: bytes, ext: str) -> Path:
    d = outbox_dir()
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".fido-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o640)
        for _ in range(8):
            path = d / f"fido-{secrets.token_hex(8)}.{ext}"
            try:
                os.link(tmp, path)
            except FileExistsError:
                continue
            return path
        raise OSError("no free outbox name")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def megabytes(size: int) -> str:
    return f"{size / _MB:.1f}"

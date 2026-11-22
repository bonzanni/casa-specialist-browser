"""Where Casa Browser is, and the pairing secret Fido holds for it.

The address is derived from Casa's own container host name: a Home Assistant
app's host name is its slug with `_` replaced by `-`, and Casa Browser sits
beside Casa in the same app repository. `CASA_PLUGIN_BROWSER_APP_URL`, when
set, overrides it for a non-standard install.

The secret is claimed by plugin CODE (`setup_browser`), never by a model,
and lives only in `$CLAUDE_PLUGIN_DATA/pairing.json` (directory 0700, file
0600). No sentence, log line or error carries it.
"""
from __future__ import annotations

import json
import os
import re
import socket
import tempfile
from pathlib import Path
from typing import NamedTuple

import copy_text
import wire
from app_client import (API_PORT, PROTOCOL_VERSION, AppClient, AppError, check_compatible,
                        hello)

CALLER_LABEL = "Casa (Fido plugin)"
PAIRING_FILE = "pairing.json"
ENV_APP_URL = "CASA_PLUGIN_BROWSER_APP_URL"
ENV_DATA = "CLAUDE_PLUGIN_DATA"
CLAIM_PATH = "/v1/pair/claim"

_SECRET_RE = re.compile(r"^[\x21-\x7e]{1,512}$")
_HOST_RE = re.compile(r"^([a-z0-9]+)[_-](casa|casa-test)$")


class Sibling(NamedTuple):
    host: str
    app_slug: str


def sibling(hostname: str) -> Sibling:
    """Casa Browser's host and app slug beside the Casa whose host name (or
    slug) is `hostname`: `<prefix>[-_]casa` → `<prefix>-casa-browser` /
    `<prefix>_casa_browser`; `<prefix>[-_]casa-test` →
    `<prefix>-casa-browser-test` / `<prefix>_casa-browser-test`. Anything
    else is `unknown_host`."""
    m = _HOST_RE.fullmatch(hostname or "")
    if m is None:
        raise AppError("unknown_host")
    prefix, name = m.groups()
    if name == "casa":
        return Sibling(f"{prefix}-casa-browser", f"{prefix}_casa_browser")
    return Sibling(f"{prefix}-casa-browser-test", f"{prefix}_casa-browser-test")


def app_base_url() -> str:
    override = os.environ.get(ENV_APP_URL, "")
    if override:
        from app_client import _origin
        scheme, host, port = _origin(override)
        if any(ch.isspace() for ch in override):
            raise AppError("unknown_host")
        netloc = f"[{host}]" if ":" in host else host
        return f"{scheme}://{netloc}" + (f":{port}" if port is not None else "")
    return f"http://{sibling(socket.gethostname()).host}:{API_PORT}"


def _data_dir() -> Path | None:
    raw = os.environ.get(ENV_DATA, "")
    return Path(raw) if raw else None


def load_secret() -> str | None:
    data = _data_dir()
    if data is None:
        return None
    try:
        with open(data / PAIRING_FILE, "rb") as fh:
            raw = fh.read(4096)
        obj = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    secret = obj.get("secret") if isinstance(obj, dict) else None
    if isinstance(secret, str) and _SECRET_RE.fullmatch(secret):
        return secret
    return None


def store_secret(secret: str) -> None:
    """Write the secret atomically: a fresh 0600 file in the 0700 data
    directory, fsynced, then renamed over `pairing.json` (a symlink there is
    replaced, never followed)."""
    if not isinstance(secret, str) or not _SECRET_RE.fullmatch(secret):
        raise AppError("bad_response")
    data = _data_dir()
    if data is None:
        raise AppError("no_data_dir")
    try:
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(data, 0o700)
        fd, tmp = tempfile.mkstemp(prefix=".pairing-", dir=data)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(json.dumps({"secret": secret}).encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, data / PAIRING_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        dfd = os.open(data, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        raise AppError("no_data_dir") from None


def _paired_outcome(client: AppClient) -> tuple[str, bool]:
    """Hello with the secret: the PAIRED sentence, and `True` when the app can
    be used as it is; otherwise followed by the compatibility sentence, and
    `False` (paired, but the setup is not done)."""
    h = hello(client)
    text = copy_text.PAIRED.format(app_version=h.app_version or "")
    try:
        check_compatible(h)
    except AppError as err:
        return text + " " + copy_text.BY_CODE[err.code], False
    return text, True


def setup_outcome() -> tuple[str, bool]:
    """The setup tool's body: `(sentence, paired)`. `paired` is False whenever
    Fido does not hold a secret the app accepts (no window, app absent, an
    unreadable answer), so the caller can report the setup as not done.

    Order: a stored secret the app accepts ⇒ paired, no claim (the window is
    left for whoever armed it). Otherwise claim; the new secret replaces the
    stored one only once the claim succeeded and was stored.
    """
    try:
        base = app_base_url()
    except AppError as err:
        return copy_text.BY_CODE[err.code], False
    if _data_dir() is None:
        return copy_text.NO_DATA_DIR, False
    secret = load_secret()
    if secret is not None:
        try:
            return _paired_outcome(AppClient(base, secret))
        except AppError as err:
            if err.code != "unauthorized":
                return copy_text.BY_CODE.get(err.code, copy_text.UNEXPECTED), False
    try:
        answer = AppClient(base, None).request(
            "POST", CLAIM_PATH, {"caller_label": CALLER_LABEL}, control=True)
    except AppError as err:
        if err.code == "unreachable":
            return copy_text.UNREACHABLE, False
        if wire.claim_had_no_window(err):
            return copy_text.NO_WINDOW, False
        return copy_text.BY_CODE.get(err.code, copy_text.UNEXPECTED), False
    try:
        new, version = wire.claim_reply(answer)
    except AppError:
        return copy_text.UNEXPECTED, False
    if version != PROTOCOL_VERSION:
        # The window is spent either way; a secret for an app this plugin
        # cannot speak to is not kept.
        return (copy_text.APP_OLDER if version < PROTOCOL_VERSION
                else copy_text.APP_NEWER), False
    try:
        store_secret(new)
    except AppError as err:
        return copy_text.BY_CODE[err.code], False
    try:
        return _paired_outcome(AppClient(base, new))
    except AppError as err:
        return copy_text.BY_CODE.get(err.code, copy_text.UNEXPECTED), False


def setup_browser() -> str:
    return setup_outcome()[0]

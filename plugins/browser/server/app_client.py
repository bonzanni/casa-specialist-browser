"""Fido's typed client of Casa Browser's `/v1` API (protocol 1).

* Every request carries the pairing secret as `Authorization: Bearer`, never
  in a query string; every non-GET also carries `X-Casa-Browser-Protocol: 1`.
* A path is built by plugin code from validated parts and must match a
  narrow grammar; a control route (`/v1/control/…`, `/v1/pair/…`) is reachable
  only through `control=True`, which plugin code passes only on its own
  trusted paths, never for a path a model could shape.
* Errors are `AppError(code)`: one of the app's codes, or the plugin's own.
  An `AppError` never carries response text, a URL, a header or the secret;
  an answer that is not JSON, too large, or names an unknown code is
  `bad_response`.

Standard library only (`http.client`).
"""
from __future__ import annotations

import http.client
import json
import re
import urllib.parse
from typing import NamedTuple

import copy_text
import wire

PROTOCOL_VERSION = 1
PROTOCOL_HEADER = "X-Casa-Browser-Protocol"
API_PORT = 8098

#: The largest JSON reply Fido reads; a larger one is refused as `too_large`. A step reply
#: carrying a judgement fits: Casa's check takes a packet of at most 512 KiB (a
#: re-judgement carries two packets and a difference), which JSON-escaped as a string stays far below this.
MAX_JSON_BYTES = 4 * 1024 * 1024
#: a request's timeout unless the client or the call names another
DEFAULT_TIMEOUT_S = 30.0
MAX_ERROR_BYTES = 64 * 1024
#: The largest file the plugin ever fetches whole: above every Casa delivery
#: cap (20 MB document/zip, 25 MB handoff), so a deliverable file always fits.
MAX_RAW_BYTES = 32 * 1024 * 1024
_CHUNK = 64 * 1024

#: Casa Browser's error codes (protocol 1; one HTTP status per code)
APP_CODES = frozenset({
    "unauthorized", "protocol_mismatch", "not_ready", "stale_handle", "blocked",
    "unsupported", "interrupted", "file_too_large", "conflict", "not_found", "internal",
    "judgement_stale", "digest_mismatch",
})
PLUGIN_CODES = frozenset({
    "not_paired", "unreachable", "bad_response", "unknown_host", "app_newer",
    "app_older", "no_data_dir", "bad_request", "turn_bound", "too_large", "not_set_up",
})
ERROR_CODES = APP_CODES | PLUGIN_CODES

_SEGMENT = r"[A-Za-z0-9_-]+"
_QVALUE = r"[A-Za-z0-9_.:+%-]*"
_PATH_RE = re.compile(
    rf"^/v1(?:/{_SEGMENT})+(?:\?{_SEGMENT}={_QVALUE}(?:&{_SEGMENT}={_QVALUE})*)?$")
_SECRET_RE = re.compile(r"^[\x21-\x7e]{1,512}$")
_APP_VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{1,32}$")


class AppError(Exception):
    """A typed failure. `code` is one of `ERROR_CODES`; `status` is the HTTP
    status when the app answered, else None. Neither carries the app's text."""

    def __init__(self, code: str, status: int | None = None):
        if code not in ERROR_CODES:
            code = "bad_response"
        super().__init__(code)
        self.code = code
        self.status = status


class Hello(NamedTuple):
    protocol_version: int
    app_version: str
    configured: bool
    needs_locale: bool


def _is_control(path: str) -> bool:
    parts = path.split("?", 1)[0].split("/")
    return len(parts) > 2 and parts[2].lower() in ("control", "pair")


def _origin(base_url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(base_url)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in ("", "/")):
        raise AppError("unknown_host")
    try:
        port = parts.port
    except ValueError:
        raise AppError("unknown_host") from None
    return parts.scheme, parts.hostname, port


#: whether this turn has run its time (set by `continuation`; a client made
#: by `compatible_client` refuses every request once it is true)
turn_over = lambda: False  # noqa: E731


class AppClient:
    def __init__(self, base_url: str, secret: str | None, timeout_s: float | None = None):
        self.base_url = base_url
        self.secret = secret
        self.timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
        self.bounded = False
        self.hello: "Hello | None" = None

    def request(self, method: str, path: str, body: dict | None = None, *,
                control: bool = False, raw: bool = False, timeout_s: float | None = None):
        """One request; `timeout_s`, when given, replaces the client's timeout for
        this request only."""
        if method not in ("GET", "POST") or not isinstance(path, str) \
                or not _PATH_RE.fullmatch(path) or _is_control(path) != bool(control):
            raise AppError("bad_request")
        if self.bounded and turn_over():
            raise AppError("turn_bound")
        if self.secret is not None and not _SECRET_RE.fullmatch(self.secret):
            raise AppError("not_paired")
        scheme, host, port = _origin(self.base_url)
        headers = {"Accept": "application/octet-stream" if raw else "application/json"}
        if self.secret is not None:
            headers["Authorization"] = f"Bearer {self.secret}"
        payload = None
        if method != "GET":
            headers[PROTOCOL_HEADER] = str(PROTOCOL_VERSION)
            payload = json.dumps({} if body is None else body,
                                 separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = cls(host, port, timeout=self.timeout_s if timeout_s is None else timeout_s)
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            if 200 <= status < 300:
                limit = MAX_RAW_BYTES if raw else MAX_JSON_BYTES
            else:
                limit = MAX_ERROR_BYTES
            chunks, size = [], 0
            while size <= limit:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
        except Exception:  # noqa: BLE001 — the class or text could name the host
            raise AppError("unreachable") from None
        finally:
            conn.close()
        if len(data) > limit:
            raise AppError("too_large" if 200 <= status < 300 else "bad_response", status)
        if not 200 <= status < 300:
            code = wire.error_code(data)
            if code in APP_CODES:
                raise AppError(code, status)
            raise AppError("bad_response", status)
        if raw:
            return data
        try:
            answer = wire.loads(data)
        except (ValueError, RecursionError):
            raise AppError("bad_response", status) from None
        if not isinstance(answer, dict):
            raise AppError("bad_response", status)
        return answer


def hello(client: AppClient) -> Hello:
    """`GET /v1/hello`. For protocol 1 every member is checked; an app on
    another protocol is reported by its version alone (its other members may
    be shaped differently), with `configured`/`needs_locale` False and an
    empty version string when its own does not fit."""
    raw = client.request("GET", "/v1/hello")
    pv = wire.protocol_version(raw)
    if not isinstance(pv, int) or isinstance(pv, bool):
        raise AppError("bad_response")
    version = raw.get("app_version")
    if pv != PROTOCOL_VERSION:
        ok = isinstance(version, str) and _APP_VERSION_RE.fullmatch(version)
        return Hello(pv, version if ok else "", False, False)
    configured, needs_locale = raw.get("configured"), raw.get("needs_locale")
    if (not isinstance(version, str) or not _APP_VERSION_RE.fullmatch(version)
            or not isinstance(configured, bool) or not isinstance(needs_locale, bool)):
        raise AppError("bad_response")
    return Hello(pv, version, configured, needs_locale)


def check_compatible(h: Hello) -> None:
    if h.protocol_version < PROTOCOL_VERSION:
        raise AppError("app_older")
    if h.protocol_version > PROTOCOL_VERSION:
        raise AppError("app_newer")
    if not h.configured or h.needs_locale:
        # its own code; a run route's `not_ready` is about that run, not the setup
        raise AppError("not_set_up")


def require_compatible(client: AppClient) -> Hello:
    h = hello(client)
    check_compatible(h)
    return h


def client_from_env() -> AppClient:
    """The client for this Casa's Casa Browser: the override address when set,
    else the sibling of this container's host name; the stored secret, else
    `not_paired`."""
    import pairing
    base = pairing.app_base_url()
    secret = pairing.load_secret()
    if secret is None:
        raise AppError("not_paired")
    return AppClient(base, secret)


def compatible_client(*, bounded: bool = True) -> AppClient:
    """What every tool that acts calls first: no app call is made unless hello
    succeeded and the app is compatible and ready. The client keeps that one
    checked hello (`client.hello`) and refuses every request once the turn
    bound has passed (v0.1.27: unless `bounded=False`, for the finish of a run
    whose answer is already posted)."""
    client = client_from_env()
    client.bounded = bounded
    client.hello = require_compatible(client)
    return client


def sentence_for(err: AppError) -> str:
    return copy_text.BY_CODE.get(err.code, copy_text.UNEXPECTED)

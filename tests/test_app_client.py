"""The typed client of Casa Browser's /v1 API: protocol header, bearer,
typed errors that carry no response text, and the compatibility check that
precedes every run call."""
from __future__ import annotations

import json
import socket
import time

import pytest

import app_client
import copy_text
from app_client import AppClient, AppError
from conftest import closed_port

CANARY_URL = "https://canary.invalid/secret-path?token=abc"
CANARY_TOKEN = "tok_CANARY_0123456789abcdef"


def test_constants():
    assert app_client.PROTOCOL_VERSION == 1
    assert app_client.PROTOCOL_HEADER == "X-Casa-Browser-Protocol"
    assert app_client.API_PORT == 8098


def test_mutations_carry_protocol_header(fakeapp):
    secret = fakeapp.pair()
    c = AppClient(fakeapp.url, secret)
    c.request("GET", "/v1/hello")
    for path, control in (("/v1/runs", False), ("/v1/runs/run1/observe", False),
                          ("/v1/control/files/f1/delivery", True)):
        try:
            c.request("POST", path, {"a": 1}, control=control)
        except AppError as err:
            assert err.code != "protocol_mismatch"
    posts = [x for x in fakeapp.calls if x.method == "POST"]
    assert len(posts) == 3
    for call in posts:
        assert call.headers.get("X-Casa-Browser-Protocol") == "1"
    for call in fakeapp.calls:
        assert call.headers.get("Authorization") == f"Bearer {secret}"
        assert secret not in call.query


def test_hello_needs_no_header_but_bearer(fakeapp):
    secret = fakeapp.pair()
    h = app_client.hello(AppClient(fakeapp.url, secret))
    assert h == app_client.Hello(1, "0.1.0", True, False)
    assert fakeapp.calls[-1].headers.get("X-Casa-Browser-Protocol") is None
    with pytest.raises(AppError) as e:
        app_client.hello(AppClient(fakeapp.url, "wrong"))
    assert e.value.code == "unauthorized"


@pytest.mark.parametrize("patch, code, sentence", [
    ({"protocol_version": 0}, "app_older", copy_text.APP_OLDER),
    ({"protocol_version": 2}, "app_newer", copy_text.APP_NEWER),
    ({"configured": False}, "not_set_up", copy_text.NOT_READY),
    ({"needs_locale": True}, "not_set_up", copy_text.NOT_READY),
])
def test_hello_mismatch_messages(fakeapp, patch, code, sentence):
    secret = fakeapp.pair()
    fakeapp.hello.update(patch)
    with pytest.raises(AppError) as e:
        app_client.require_compatible(AppClient(fakeapp.url, secret))
    assert e.value.code == code
    assert app_client.sentence_for(e.value) == sentence


def test_hello_unauthorized_is_pair_again(fakeapp):
    fakeapp.pair()
    with pytest.raises(AppError) as e:
        app_client.require_compatible(AppClient(fakeapp.url, "stale-secret"))
    assert e.value.code == "unauthorized"
    assert app_client.sentence_for(e.value) == copy_text.PAIR_AGAIN


def test_newer_app_judged_on_version_before_shape(fakeapp):
    """A newer app may reshape hello; the version decides first."""
    secret = fakeapp.pair()
    fakeapp.hello = {"protocol_version": 2, "something": "else"}
    with pytest.raises(AppError) as e:
        app_client.require_compatible(AppClient(fakeapp.url, secret))
    assert e.value.code == "app_newer"


@pytest.mark.parametrize("hello", [
    {"protocol_version": True, "app_version": "x", "configured": True, "needs_locale": False},
    {"protocol_version": 1, "app_version": 5, "configured": True, "needs_locale": False},
    {"protocol_version": 1, "app_version": "x", "configured": 1, "needs_locale": False},
    {"protocol_version": 1, "app_version": "x", "configured": True},
    {"protocol_version": "1", "app_version": "x", "configured": True, "needs_locale": False},
])
def test_hello_malformed_is_bad_response(fakeapp, hello):
    secret = fakeapp.pair()
    fakeapp.hello = hello
    with pytest.raises(AppError) as e:
        app_client.require_compatible(AppClient(fakeapp.url, secret))
    assert e.value.code == "bad_response"


def test_no_run_call_after_failed_hello(app_env, plugin_data):
    """`compatible_client`, the gate every run tool passes first: a failed
    hello means no further request reaches the app."""
    import pairing
    pairing.store_secret(app_env.pair())
    for patch in ({"protocol_version": 2}, {"configured": False}):
        app_env.hello.update(patch)
        app_env.calls.clear()
        with pytest.raises(AppError):
            app_client.compatible_client()
        assert app_env.paths() == ["/v1/hello"]
        app_env.hello.update(protocol_version=1, configured=True)
    app_env.secret = "rotated"
    app_env.calls.clear()
    with pytest.raises(AppError) as e:
        app_client.compatible_client()
    assert e.value.code == "unauthorized"
    assert app_env.paths() == ["/v1/hello"]


def test_compatible_client_not_paired(app_env):
    app_env.calls.clear()
    with pytest.raises(AppError) as e:
        app_client.compatible_client()
    assert e.value.code == "not_paired"
    assert app_client.sentence_for(e.value) == copy_text.NOT_PAIRED
    assert app_env.calls == []


@pytest.mark.parametrize("status, body, ctype", [
    (400, {"error": "unsupported", "detail": CANARY_URL, "token": CANARY_TOKEN}, ""),
    (500, f"<html>{CANARY_URL} {CANARY_TOKEN}</html>", "text/html"),
    (400, {"error": f"{CANARY_TOKEN}"}, ""),
    (401, {"error": "unauthorized", "hint": CANARY_URL}, ""),
    (200, f"not json {CANARY_TOKEN}", "text/plain"),
    (302, {"location": CANARY_URL}, ""),
])
def test_errors_never_echo_response(fakeapp, status, body, ctype):
    secret = fakeapp.pair()
    fakeapp.overrides[("POST", "/v1/runs/run1/observe")] = (status, body, ctype or "application/json")
    with pytest.raises(AppError) as e:
        AppClient(fakeapp.url, secret).request("POST", "/v1/runs/run1/observe", {})
    err = e.value
    text = " ".join([str(err), repr(err), str(err.args), app_client.sentence_for(err)])
    for canary in (CANARY_URL, CANARY_TOKEN, "canary", secret, fakeapp.url):
        assert canary not in text
    assert err.code in app_client.ERROR_CODES


def test_known_code_and_status_kept(fakeapp):
    secret = fakeapp.pair()
    fakeapp.overrides[("POST", "/v1/control/proposals/b1/decision")] = (
        409, {"error": "stale_handle"}, "application/json")
    with pytest.raises(AppError) as e:
        AppClient(fakeapp.url, secret).request(
            "POST", "/v1/control/proposals/b1/decision", {}, control=True)
    assert (e.value.code, e.value.status) == ("stale_handle", 409)


def test_oversized_response_is_too_large(fakeapp):
    """A success reply over the read limit is `too_large` (its own sentence); an
    error reply over the error limit stays `bad_response`."""
    secret = fakeapp.pair()
    big = {"error": "x" * (app_client.MAX_JSON_BYTES + 10)}
    fakeapp.overrides[("GET", "/v1/sites")] = (200, big, "application/json")
    with pytest.raises(AppError) as e:
        AppClient(fakeapp.url, secret).request("GET", "/v1/sites")
    assert e.value.code == "too_large"
    fakeapp.overrides[("GET", "/v1/sites")] = (
        500, {"error": "x" * (app_client.MAX_ERROR_BYTES + 10)}, "application/json")
    with pytest.raises(AppError) as e:
        AppClient(fakeapp.url, secret).request("GET", "/v1/sites")
    assert e.value.code == "bad_response"


def test_raw_returns_bytes(fakeapp):
    secret = fakeapp.pair()
    fakeapp.screenshots[("run1", "s1")] = b"\x89PNG\r\n\x1a\n"
    data = AppClient(fakeapp.url, secret).request(
        "GET", "/v1/control/runs/run1/screenshots/s1", control=True, raw=True)
    assert data.startswith(b"\x89PNG")


def test_control_routes_need_the_control_flag(fakeapp):
    """A control route is reachable only from a code path that says so; an
    ordinary request cannot be steered to one, and control=True cannot be
    aimed at an ordinary route."""
    secret = fakeapp.pair()
    c = AppClient(fakeapp.url, secret)
    for path in ("/v1/control/sites/s1/forget", "/v1/pair/claim",
                 "/v1/runs/../control/sites/s1/forget", "/v1/Control/x"):
        with pytest.raises(AppError) as e:
            c.request("POST", path, {})
        assert e.value.code in ("unsupported", "bad_request")
    with pytest.raises(AppError):
        c.request("POST", "/v1/runs", {}, control=True)
    assert fakeapp.calls == []


@pytest.mark.parametrize("path", ["/v1/runs/a b", "/v1/runs/x\r\nX: y", "v1/hello",
                                  "/v1/runs/%2e%2e", "http://x.invalid/v1/hello", "/v1/runs/é"])
def test_path_must_be_plain(fakeapp, path):
    c = AppClient(fakeapp.url, fakeapp.pair())
    with pytest.raises(AppError):
        c.request("GET", path)
    assert fakeapp.calls == []


def test_unreachable_closed_port():
    t0 = time.monotonic()
    with pytest.raises(AppError) as e:
        AppClient(f"http://127.0.0.1:{closed_port()}", "s", timeout_s=2).request(
            "GET", "/v1/hello")
    assert e.value.code == "unreachable"
    assert app_client.sentence_for(e.value) == copy_text.UNREACHABLE
    assert time.monotonic() - t0 < 2.5


def test_unreachable_silent_server():
    """A peer that accepts and never answers is unreachable within the timeout."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        port = srv.getsockname()[1]
        t0 = time.monotonic()
        with pytest.raises(AppError) as e:
            AppClient(f"http://127.0.0.1:{port}", "s", timeout_s=0.5).request(
                "GET", "/v1/hello")
        assert e.value.code == "unreachable"
        assert time.monotonic() - t0 < 3
    finally:
        srv.close()


def test_secret_with_control_characters_never_sent(fakeapp):
    with pytest.raises(AppError):
        AppClient(fakeapp.url, "abc\r\nX-Evil: 1").request("GET", "/v1/hello")
    assert fakeapp.calls == []


def test_every_code_has_a_sentence():
    for code in app_client.ERROR_CODES:
        s = app_client.sentence_for(AppError(code))
        assert isinstance(s, str) and s and "{" not in s


@pytest.mark.parametrize("code, status", [("conflict", 409), ("not_found", 404),
                                          ("internal", 500), ("blocked", 403)])
def test_settled_codes_keep_their_meaning(fakeapp, code, status):
    """The app's typed codes (one status per code) arrive as themselves."""
    secret = fakeapp.pair()
    fakeapp.overrides[("POST", "/v1/runs/run1/observe")] = (status, {"error": code},
                                                           "application/json")
    with pytest.raises(AppError) as e:
        AppClient(fakeapp.url, secret).request("POST", "/v1/runs/run1/observe", {})
    assert (e.value.code, e.value.status) == (code, status)
    assert copy_text.BY_CODE[code]


def test_rfc3339_times():
    import wire
    assert wire.epoch("2026-10-20T12:00:00Z") == 1792497600.0
    assert wire.epoch("2026-10-20T14:00:00+02:00") == 1792497600.0
    assert wire.epoch("2026-10-20T12:00:00") is None          # no zone: not a time we trust
    assert wire.epoch("yesterday") is None and wire.epoch(True) is None
    assert wire.parse_run({"run_id": "r1", "state": "running",
                           "created_at": "2026-10-20T12:00:00Z"})["created_at"] == 1792497600.0


@pytest.mark.parametrize("method, path", [("GET", "/v1/nothing/here"),
                                          ("GET", "/v1/runs/run1/observe"),
                                          ("POST", "/v1/sites")])
def test_wrong_method_or_unknown_path_is_not_found(fakeapp, method, path):
    """Casa Browser answers a wrong method or an unknown path 404 `not_found`
    the client reports that code, not `bad_response`."""
    secret = fakeapp.pair()
    with pytest.raises(AppError) as e:
        AppClient(fakeapp.url, secret).request(method, path, {} if method == "POST" else None)
    assert (e.value.code, e.value.status) == ("not_found", 404)


@pytest.mark.parametrize("method, path", [("GET", "/v1/nothing/here"),
                                          ("GET", "/v1/runs/run1/observe"),
                                          ("POST", "/v1/sites")])
@pytest.mark.parametrize("secret", [None, "revoked-secret"])
def test_route_failures_are_authenticated_first(fakeapp, method, path, secret):
    """Like the real app: without a valid secret every route — known, unknown
    or with the wrong method — answers unauthorized before anything else."""
    fakeapp.pair()
    with pytest.raises(AppError) as e:
        AppClient(fakeapp.url, secret).request(method, path, {} if method == "POST" else None)
    assert (e.value.code, e.value.status) == ("unauthorized", 401)


def test_protocol_gate_before_route_failure(fakeapp):
    """Like the real app: a POST without the protocol header answers
    protocol_mismatch, whether or not its route exists (in this order:
    bearer, protocol header, then not_found)."""
    import http.client
    secret = fakeapp.pair()
    for path in ("/v1/sites", "/v1/nothing", "/v1/runs/x/observe"):
        conn = http.client.HTTPConnection("127.0.0.1", fakeapp.port, timeout=10)
        conn.request("POST", path, body=b"{}", headers={
            "Authorization": f"Bearer {secret}", "Content-Type": "application/json"})
        resp = conn.getresponse()
        assert (resp.status, json.loads(resp.read())) == (400, {"error": "protocol_mismatch"})
        conn.close()

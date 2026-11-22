"""The app's address, the pairing claim and the secret store."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

import app_client
import copy_text
import pairing
from app_client import AppError
from conftest import ROOT, SERVER, closed_port


@pytest.mark.parametrize("hostname, host, slug", [
    ("91d4d4c8-casa", "91d4d4c8-casa-browser", "91d4d4c8_casa_browser"),
    ("91d4d4c8_casa", "91d4d4c8-casa-browser", "91d4d4c8_casa_browser"),
    ("local-casa-test", "local-casa-browser-test", "local_casa-browser-test"),
    ("local_casa-test", "local-casa-browser-test", "local_casa-browser-test"),
    ("local-casa", "local-casa-browser", "local_casa_browser"),
])
def test_sibling_table(hostname, host, slug):
    assert pairing.sibling(hostname) == pairing.Sibling(host, slug)


@pytest.mark.parametrize("hostname", [
    "casa", "91d4d4c8-other", "a-casa-x", "", "-casa", "_casa", "x-casa-test-2",
    "91d4d4c8-casa.local", "Local-Casa", "a b-casa", "x-casa_test", "x-casa-browser",
])
def test_sibling_unknown_host(hostname):
    with pytest.raises(AppError) as e:
        pairing.sibling(hostname)
    assert e.value.code == "unknown_host"
    assert app_client.sentence_for(e.value) == copy_text.UNKNOWN_HOST


def test_base_url_from_hostname(monkeypatch):
    monkeypatch.delenv("CASA_PLUGIN_BROWSER_APP_URL", raising=False)
    monkeypatch.setattr(pairing.socket, "gethostname", lambda: "91d4d4c8-casa")
    assert pairing.app_base_url() == "http://91d4d4c8-casa-browser:8098"
    monkeypatch.setenv("CASA_PLUGIN_BROWSER_APP_URL", "")
    assert pairing.app_base_url() == "http://91d4d4c8-casa-browser:8098"


@pytest.mark.parametrize("value, expected", [
    ("http://10.0.0.5:8098", "http://10.0.0.5:8098"),
    ("http://browser.example:9000/", "http://browser.example:9000"),
    ("https://browser.example", "https://browser.example"),
])
def test_app_url_override(monkeypatch, value, expected):
    monkeypatch.setattr(pairing.socket, "gethostname", lambda: "unrelated")
    monkeypatch.setenv("CASA_PLUGIN_BROWSER_APP_URL", value)
    assert pairing.app_base_url() == expected


@pytest.mark.parametrize("value", ["ftp://x", "http://", "http://u:p@host", "host:8098",
                                   "http://host/path?q=1", "http://host#f", "http://a b"])
def test_app_url_override_refused(monkeypatch, value):
    monkeypatch.setenv("CASA_PLUGIN_BROWSER_APP_URL", value)
    with pytest.raises(AppError) as e:
        pairing.app_base_url()
    assert e.value.code == "unknown_host"


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_setup_claims_and_stores(app_env, plugin_data):
    app_env.arm()
    text, paired = pairing.setup_outcome()
    assert paired is True
    assert text == copy_text.PAIRED.format(app_version="0.1.0")
    claims = [c for c in app_env.calls if c.path == "/v1/pair/claim"]
    assert len(claims) == 1
    assert claims[0].body == {"caller_label": "Casa (Fido plugin)"}
    assert "Authorization" not in claims[0].headers
    assert claims[0].headers.get("X-Casa-Browser-Protocol") == "1"
    secret = app_env.secret
    assert secret not in text
    stored = plugin_data / "pairing.json"
    assert _mode(stored) == 0o600 and _mode(plugin_data) == 0o700
    assert pairing.load_secret() == secret
    # the claim was verified with the new secret
    assert app_env.calls[-1].path == "/v1/hello"
    assert app_env.calls[-1].headers["Authorization"] == f"Bearer {secret}"
    assert [p.name for p in plugin_data.iterdir()] == ["pairing.json"]


def test_setup_browser_returns_the_sentence(app_env):
    app_env.arm()
    assert pairing.setup_browser() == copy_text.PAIRED.format(app_version="0.1.0")


def test_setup_no_window(app_env, plugin_data):
    text, paired = pairing.setup_outcome()
    assert (text, paired) == (copy_text.NO_WINDOW, False)
    assert not (plugin_data / "pairing.json").exists()


def test_setup_no_window_keeps_existing_secret(app_env, plugin_data):
    pairing.store_secret("old-secret-value")
    app_env.secret = "app-rotated"          # the stored one is now rejected
    text, paired = pairing.setup_outcome()
    assert (text, paired) == (copy_text.NO_WINDOW, False)
    assert pairing.load_secret() == "old-secret-value"


def test_setup_app_absent(plugin_data, monkeypatch):
    monkeypatch.setenv("CASA_PLUGIN_BROWSER_APP_URL", f"http://127.0.0.1:{closed_port()}")
    text, paired = pairing.setup_outcome()
    assert (text, paired) == (copy_text.UNREACHABLE, False)
    assert not (plugin_data / "pairing.json").exists()


def test_setup_idempotent(app_env, plugin_data):
    pairing.store_secret(app_env.pair())
    app_env.arm()
    app_env.calls.clear()
    text, paired = pairing.setup_outcome()
    assert (text, paired) == (copy_text.PAIRED.format(app_version="0.1.0"), True)
    assert app_env.paths() == ["/v1/hello"]
    assert app_env.armed is True            # the window was left for someone else


def test_setup_replaces_rejected_secret(app_env, plugin_data):
    pairing.store_secret("stale-secret")
    app_env.pair("current-other")
    app_env.arm()
    text, paired = pairing.setup_outcome()
    assert paired is True
    assert pairing.load_secret() == app_env.secret != "stale-secret"
    assert app_env.paths() == ["/v1/hello", "/v1/pair/claim", "/v1/hello"]


def test_setup_paired_but_incompatible(app_env):
    app_env.arm()
    app_env.hello["protocol_version"] = 2
    text, paired = pairing.setup_outcome()
    # paired, but not usable: the setup is not reported done
    assert paired is False
    assert text == copy_text.PAIRED.format(app_version="0.1.0") + " " + copy_text.APP_NEWER


def test_setup_claim_answer_malformed(app_env, plugin_data):
    app_env.overrides[("POST", "/v1/pair/claim")] = (
        200, {"secret": "has space", "protocol_version": 1}, "application/json")
    text, paired = pairing.setup_outcome()
    assert paired is False
    assert text == copy_text.UNEXPECTED
    assert not (plugin_data / "pairing.json").exists()


def test_setup_without_plugin_data(app_env, monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    app_env.arm()
    text, paired = pairing.setup_outcome()
    assert (text, paired) == (copy_text.NO_DATA_DIR, False)
    assert app_env.armed is True            # nothing claimed that could not be kept


def test_load_secret_rejects_garbage(plugin_data):
    for content in ("", "{", "[]", '{"secret": 5}', '{"secret": "a b"}',
                    '{"secret": "x\\r\\n"}', json.dumps({"secret": "x" * 1000})):
        (plugin_data / "pairing.json").write_text(content)
        assert pairing.load_secret() is None


def test_store_secret_atomic_and_tightens(plugin_data):
    plugin_data.chmod(0o755)
    pairing.store_secret("first-secret")
    pairing.store_secret("second-secret")
    assert pairing.load_secret() == "second-secret"
    assert _mode(plugin_data) == 0o700
    assert _mode(plugin_data / "pairing.json") == 0o600
    assert sorted(p.name for p in plugin_data.iterdir()) == ["pairing.json"]


def test_store_secret_refuses_symlink(plugin_data, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (plugin_data / "pairing.json").symlink_to(target)
    pairing.store_secret("s3cret-value")
    assert target.read_text() == "{}"
    assert not (plugin_data / "pairing.json").is_symlink()


_SEQUENCE = r"""
import os, sys, logging
sys.path.insert(0, {server!r})
logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
import app_client, pairing
print(pairing.setup_browser())
c = app_client.compatible_client()
try:
    c.request("POST", "/v1/runs", {{"request_id": "r1"}})
except app_client.AppError as e:
    print(app_client.sentence_for(e))
try:
    c.request("POST", "/v1/runs/zzz/observe", {{}})
except app_client.AppError as e:
    print(app_client.sentence_for(e))
os.environ["CASA_PLUGIN_BROWSER_APP_URL"] = "http://127.0.0.1:{closed}"
try:
    app_client.compatible_client()
except app_client.AppError as e:
    print(app_client.sentence_for(e))
"""


def test_secret_absent_from_stderr(app_env, plugin_data):
    """A whole setup and run sequence in a fresh interpreter, with debug
    logging on: the secret reaches neither stdout nor stderr."""
    app_env.arm()
    env = dict(os.environ, CLAUDE_PLUGIN_DATA=str(plugin_data),
               CASA_PLUGIN_BROWSER_APP_URL=app_env.url)
    out = subprocess.run([sys.executable, "-c", _SEQUENCE.format(
        server=str(SERVER), closed=closed_port())],
        capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    secret = app_env.secret
    assert secret and len(secret) > 20
    assert secret not in out.stdout and secret not in out.stderr
    assert copy_text.PAIRED.format(app_version="0.1.0") in out.stdout
    assert copy_text.UNREACHABLE in out.stdout


def test_setup_does_not_claim_when_hello_fails_otherwise(app_env, plugin_data):
    """Only a rejected secret leads to a claim: an app that answers hello
    with something else keeps its armed window and Fido keeps its secret."""
    pairing.store_secret(app_env.pair())
    app_env.arm()
    app_env.overrides[("GET", "/v1/hello")] = (500, "oops", "text/plain")
    text, paired = pairing.setup_outcome()
    assert (text, paired) == (copy_text.UNEXPECTED, False)
    assert "/v1/pair/claim" not in app_env.paths()
    assert app_env.armed is True


@pytest.mark.parametrize("pv, sentence", [(2, copy_text.APP_NEWER), (0, copy_text.APP_OLDER),
                                          ("1", copy_text.UNEXPECTED),
                                          (True, copy_text.UNEXPECTED),
                                          (None, copy_text.UNEXPECTED)])
def test_setup_claim_protocol_checked(app_env, plugin_data, pv, sentence):
    """A claim reply is taken only from an app on protocol 1: any other or
    missing version stores nothing and the setup is not done."""
    pairing.store_secret("kept-secret")
    app_env.secret = "valid-secret"
    reply = {"secret": "valid-secret"}
    if pv is not None:
        reply["protocol_version"] = pv
    app_env.overrides[("POST", "/v1/pair/claim")] = (200, reply, "application/json")
    text, paired = pairing.setup_outcome()
    assert (text, paired) == (sentence, False)
    assert pairing.load_secret() == "kept-secret"

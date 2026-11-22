"""The plugin's pad client against Casa's own `POST /internal/broker/pad` handler, served by
Casa's code in a subprocess (Casa's modules never share this process's namespace): a call in
flight is recorded for the client and its pad registered, as `attach` and the call's admission
do. Skipped, with the reason, when `CASA_ROOT` is unset."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import pytest

import casa_broker
from casa_broker import BrokerFailed

SERVER = r"""
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from aiohttp import web
import operator_calls, scratchpad
sock, mode = sys.argv[2], sys.argv[3]
if mode == "in_call":
    operator_calls.CALLS.open(client_id="client-x", tool_use_id="tu-1", tool_name="run_act",
                              role="specialist:browser", plugin="browser", identity=None,
                              origin={"cid": "c1"})
scratchpad.PADS.register("client-x", "conv-1")
app = web.Application(client_max_size=operator_calls.INTERNAL_MAX_BODY_BYTES)
app.router.add_post("/internal/broker/pad", operator_calls.build_pad_handler())
async def main():
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.UnixSite(runner, sock).start()
    print("ready", flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
"""


@pytest.fixture
def casa_pad(casa, monkeypatch):
    def start(mode="in_call"):
        d = tempfile.mkdtemp(prefix="pad-")
        sock = os.path.join(d, "internal.sock")
        proc = subprocess.Popen([sys.executable, "-I", "-c", SERVER,
                                 str(casa / "casa" / "rootfs" / "opt" / "casa"), sock, mode],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(proc)
        line = proc.stdout.readline()
        assert line.strip() == "ready", proc.stderr.read()[-2000:]
        monkeypatch.setenv(casa_broker.ENV_SOCKET, sock)
        monkeypatch.setenv(casa_broker.ENV_CLIENT, "client-x")
    procs: list = []
    yield start
    for p in procs:
        p.kill()
        p.wait(10)


def test_the_plugin_client_reads_back_what_it_wrote_through_casas_route(casa_pad):
    casa_pad()
    text = "".join(f"line {i}\n" for i in range(9000)) + "## not a section"
    lines, size = casa_broker.pad_write("page", text)
    assert lines == 9001 and size == len(text.encode()) + 1          # the mark's one space
    got = casa_broker.pad_text("page", 200_000)
    assert got == text.replace("\n## ", "\n ## ")
    first, nxt = casa_broker.pad_read("page")
    assert len(first) == 30_000 and nxt == 30_000
    casa_broker.pad_write("page", "line A", append=True)
    assert casa_broker.pad_text("page", 200_000).endswith(" ## not a section\nline A")
    casa_broker.pad_write("page", "only")                             # a write replaces
    assert casa_broker.pad_text("page", 100) == "only"
    with pytest.raises(BrokerFailed) as err:
        casa_broker.pad_text("page", 3)
    assert err.value.code == "pad_too_long"


def test_casas_refusals_reach_the_plugin_as_their_codes(casa_pad):
    casa_pad()
    casa_broker.pad_write("page", "x")
    for call, code in ((lambda: casa_broker.pad_read("missing"), "no_section"),
                       (lambda: casa_broker.pad_write("", "x"), "bad_section"),
                       (lambda: casa_broker.pad_read("page", 2), "bad_offset")):
        with pytest.raises(BrokerFailed) as err:
            call()
        assert err.value.code == code


def test_without_a_call_in_flight_casa_answers_no_pad_at_all(casa_pad):
    casa_pad(mode="no_call")
    with pytest.raises(BrokerFailed) as err:
        casa_broker.pad_write("page", "x")
    assert err.value.code == "no_call_in_flight"

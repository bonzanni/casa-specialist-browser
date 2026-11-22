"""Files to whoever asked: kinds and caps, staging, delivery outcomes from
Casa, re-delivery under the same key, the handoff folder, and beats."""
from __future__ import annotations

import json
import os
import time

import pytest

import copy_text
import files
import state
from conftest import admitted_run, casa_eval

MB = 1024 * 1024


@pytest.mark.parametrize("name, head, expected", [
    ("inv.pdf", b"%PDF-1.7 x", ("document", "inv.pdf")),
    ("invoice", b"%PDF-1.7 x", ("document", "invoice.pdf")),
    ("shot.png", b"\x89PNG\r\n\x1a\nxx", ("photo", "shot.png")),
    ("shot.bin", b"\x89PNG\r\n\x1a\nxx", ("photo", "shot.bin.png")),
    ("p.jpeg", b"\xff\xd8\xff\xe0x", ("photo", "p.jpeg")),
    ("p.gif", b"\xff\xd8\xff\xe0x", ("photo", "p.gif.jpg")),
    ("st.sta", b":20:STATEMENT\n:25:NL01\n", ("text", "st.sta")),
    ("st.940", b":20:STATEMENT\n", ("text", "st.940")),
    ("st.mt940", b":20:STATEMENT\n", ("text", "st.mt940.txt")),
    ("data.csv", "a;b\nè;ü\n".encode(), ("text", "data.csv")),
    ("a.zip", b"PK\x03\x04" + b"\x00" * 30, ("zip", "a.zip")),
])
def test_kind_mapping(name, head, expected):
    assert tuple(files.kind_for(name, "", head)) == expected


@pytest.mark.parametrize("head", [b"\x00\x01binary", b"\xff\xfe\x00bad utf8", b"",
                                  b"text with a \x07 bell"])
def test_kind_none_means_zip(head):
    assert files.kind_for("x.bin", "", head) is None


def test_caps_match_casa(casa):
    got = casa_eval(casa, """
import media_policies
print(json.dumps({k: [media_policies.MEDIA_POLICIES[k].size_cap,
                      sorted(media_policies.MEDIA_POLICIES[k].extensions)]
                  for k in ("document", "photo", "zip", "text")}))
""")
    for kind, (cap, exts) in got.items():
        assert files.CAPS[kind] == cap
        assert sorted(files.EXTENSIONS[kind]) == exts


def test_groups_under_cap():
    metas = [files.FileMeta(f"f{i}", s * MB) for i, s in enumerate((12, 9, 15, 3))]
    groups = files.plan_groups(metas, cap=19 * MB)
    assert groups == [["f0"], ["f1"], ["f2", "f3"]]
    assert sorted(sum(groups, [])) == sorted(m.file_id for m in metas)
    sizes = {m.file_id: m.size for m in metas}
    assert all(sum(sizes[f] for f in g) <= 19 * MB for g in groups)


def test_stage_unique_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("CASA_PLUGIN_OUTBOX_DIR", str(tmp_path))
    paths = {files.stage(b"%PDF-x", "pdf") for _ in range(20)}
    assert len(paths) == 20
    for p in paths:
        assert p.parent == tmp_path and p.name.startswith("fido-") and p.suffix == ".pdf"
        assert os.stat(p).st_nlink == 1 and (os.stat(p).st_mode & 0o777) == 0o640
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".part")]


def _file(env, run, data=b"%PDF-1.4 x", name="invoice-march.pdf", mime="application/pdf"):
    fid = env.app.add_file(run, data, name, mime)
    state.put_file(fid, run, name, mime, len(data))
    return fid


def test_deposit_fields(env):
    (run,) = admitted_run(env)
    fid = _file(env, run)
    out = json.loads(env.ok("deliver_file", run_id=run, file_id=fid,
                            caption="March invoice\nfrom https://x.example"))
    dep = env.casa.deposits[-1]
    assert out["file"] == dep["reference"]
    assert dep["kind"] == "document" and dep["filename"] == "invoice-march.pdf"
    assert dep["key"] == f"file-{fid}"
    assert "\n" not in dep["caption"] and "://" not in dep["caption"]
    assert os.path.dirname(dep["value"]) == str(env.out)
    assert open(dep["value"], "rb").read() == b"%PDF-1.4 x"
    assert env.app.deliveries[-1] == {"file_id": fid, "requester_id": "assistant",
                                      "delivery_id": dep["reference"], "outcome": "deposited"}
    assert "%PDF" not in json.dumps(out)


def test_requester_kind_from_run_not_turn(env, tmp_path, monkeypatch):
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    monkeypatch.setenv("CASA_HANDOFF_DIR", str(handoff))
    import casa_handoff
    monkeypatch.setattr(casa_handoff, "root_dir", lambda: str(handoff))
    env.casa.requester.update(requester_id="specialist:finance", requester_kind="specialist")
    (run,) = admitted_run(env)
    fid = _file(env, run)
    env.casa.requester.update(requester_id="assistant", requester_kind="operator_resident")
    out = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))
    assert out == {"file": None, "note": copy_text.FILE_FOR_SPECIALIST}
    text = env.ok("handoff_files", run_id=run, file_ids=[fid])
    assert text.startswith("Handed off: invoice-march.pdf at ")
    published = list((handoff / "browser").glob("*/invoice-march.pdf"))
    assert len(published) == 1 and published[0].read_bytes() == b"%PDF-1.4 x"
    assert env.app.deliveries[-1]["outcome"] == "handed_off"
    assert env.app.deliveries[-1]["delivery_id"] == published[0].parent.name
    assert env.app.deliveries[-1]["requester_id"] == "specialist:finance"


def test_handoff_refused_for_operator_requester(env):
    (run,) = admitted_run(env)
    fid = _file(env, run)
    assert env.ok("handoff_files", run_id=run, file_ids=[fid]) == copy_text.FILE_FOR_OPERATOR


def test_too_large_reported(env):
    (run,) = admitted_run(env)
    big = b"%PDF-" + b"x" * (25 * MB)
    fid = _file(env, run, big)
    env.app.calls.clear()
    out = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))
    assert out["file"] is None and "too large to send in Telegram (25.0 MB)" in out["note"]
    assert not [c for c in env.app.calls if c.path.startswith("/v1/files/")]


def test_over_kind_cap_needs_zip(env):
    (run,) = admitted_run(env)
    fid = _file(env, run, b"\x89PNG\r\n\x1a\n" + b"x" * (11 * MB), "big.png", "image/png")
    out = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))
    assert out == {"file": None, "note": copy_text.FILE_NEEDS_ZIP.format(name="big.png")}
    fid = _file(env, run, b"\x00\x01binary", "x.bin", "application/octet-stream")
    out = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))
    assert out == {"file": None, "note": copy_text.FILE_NEEDS_ZIP.format(name="x.bin")}


def test_foreign_file_refused(env):
    run_a, run_b = admitted_run(env, n=2)
    fid = _file(env, run_a)
    env.app.calls.clear()
    env.casa.calls.clear()
    for tool, args in (("deliver_file", {"file_id": fid, "caption": ""}),
                       ("handoff_files", {"file_ids": [fid]})):
        text = env.ok(tool, run_id=run_b, **args)
        assert copy_text.FOREIGN_FILE.format(file_id=fid) in text
    assert env.app.calls == [] and env.casa.calls == []


def test_delivery_outcome_from_delivery_status(env):
    (run,) = admitted_run(env)
    fid = _file(env, run)
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.deliver(ref, "withheld")
    note = json.loads(env.ok("browser_status"))["note"]
    assert copy_text.FILE_WITHHELD.format(name="invoice-march.pdf", run_id=run,
                                          file_id=fid) in note
    assert env.app.deliveries[-1]["outcome"] == "withheld"
    fid2 = _file(env, run, name="b.pdf")
    ref2 = json.loads(env.ok("deliver_file", run_id=run, file_id=fid2, caption=""))["file"]
    env.casa.deliver(ref2)
    env.ok("browser_status")
    assert env.app.deliveries[-1] == {"file_id": fid2, "requester_id": "assistant",
                                      "delivery_id": ref2, "outcome": "delivered"}
    # reported once, never again
    n = len(env.app.deliveries)
    env.ok("browser_status")
    assert len(env.app.deliveries) == n


def test_no_model_supplied_outcome():
    import browser_server  # noqa: F401
    import tooling
    for name, spec in tooling.TOOLS.items():
        assert "outcome" not in json.dumps(spec["schema"]), name


def test_unknown_outcome_redelivers_once(env):
    """A delivery `unknown` asks for deliver_file; the re-deposit uses the same key,
    and Casa's delivered-key memory sends nothing when the first one had
    landed, once when it had not."""
    for landed in (True, False):
        (run,) = admitted_run(env)
        fid = _file(env, run, name=f"inv-{landed}.pdf")
        ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
        if landed:
            env.casa.deliver(ref)                    # it landed …
        env.casa.delivery[ref] = {"status": "unknown", "repeat": False}   # … then a restart
        before = len(env.casa.posted)
        text = env.ok("run_observe", run_id=run)
        assert copy_text.FILE_UNCONFIRMED.format(name=f"inv-{landed}.pdf", run_id=run,
                                                 file_id=fid) in text
        ref2 = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
        assert env.casa.deposits[-1]["key"] == f"file-{fid}"
        if not landed:
            env.casa.deliver(ref2)
        env.ok("browser_status")
        assert len(env.casa.posted) - before == (0 if landed else 1)
        assert state.file_delivery(run, fid).reference == ref2
        assert state.file_delivery(run, fid).status == "delivered"


def test_run_finish_refuses_unresolved_file(env):
    (run,) = admitted_run(env)
    fid = _file(env, run)
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.delivery[ref] = {"status": "unknown", "repeat": False}
    out = json.loads(env.ok("run_finish", run_id=run, summary="s", file_ids=[fid],
                            source_ids=[], expectation_status="matched"))
    assert copy_text.SEND_AGAIN_FIRST.format(name="invoice-march.pdf", run_id=run,
                                             file_id=fid) in out["note"]
    assert env.app.posts("/finish") == []
    # pending, but older than 120 s
    (run2,) = admitted_run(env)
    fid2 = _file(env, run2)
    ref2 = json.loads(env.ok("deliver_file", run_id=run2, file_id=fid2, caption=""))["file"]
    env.casa.delivery[ref2] = {"status": "pending", "repeat": False}
    con = state._connect()
    con.execute("UPDATE deliveries SET deposited_at = deposited_at - 200 WHERE reference = ?",
                (ref2,))
    con.close()
    out = json.loads(env.ok("run_finish", run_id=run2, summary="s", file_ids=[fid2],
                            source_ids=[], expectation_status="matched"))
    assert "Send invoice-march.pdf again first" in out["note"]
    env.casa.deliver(ref2)
    out = json.loads(env.ok("run_finish", run_id=run2, summary="s", file_ids=[fid2],
                            source_ids=[], expectation_status="matched"))
    assert "Finished" in out["note"]


def test_redelivery_replaces_attempt_across_restart(env):
    (run,) = admitted_run(env)
    fid = _file(env, run)
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.delivery[ref] = {"status": "unknown", "repeat": False}
    ref2 = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    import importlib
    importlib.reload(state)                      # a new process reads the same file
    assert state.file_delivery(run, fid).reference == ref2


def test_beat_rate(env):
    (run,) = admitted_run(env)
    out = json.loads(env.ok("progress", run_id=run, text="Reading page 2 of 5 [link](x)"))
    assert out["beat"] and env.casa.deposits[-1]["key"] == f"beat-{run}-1"
    assert "\\[link\\]" in env.casa.deposits[-1]["value"]
    out = json.loads(env.ok("progress", run_id=run, text="Again"))
    assert out == {"beat": None, "note": copy_text.BEAT_TOO_SOON}
    con = state._connect()
    con.execute("UPDATE beats SET at = at - 91")
    con.close()
    out = json.loads(env.ok("progress", run_id=run, text="Later"))
    assert out["beat"] and env.casa.deposits[-1]["key"] == f"beat-{run}-2"


def test_no_bytes_in_model_text(env, tmp_path, monkeypatch):
    (run,) = admitted_run(env)
    canary = b"%PDF-1.4 CANARY-BYTES"
    fid = _file(env, run, canary)
    texts = [env.ok("deliver_file", run_id=run, file_id=fid, caption="c"),
             env.ok("run_observe", run_id=run), json.loads(env.ok("browser_status"))["note"]]
    assert all("CANARY" not in t for t in texts)


def test_long_names_fit_casa_after_an_extension():
    name = "é" * 126                                   # 252 bytes, no extension
    got = files.kind_for(name, "", "plain text\n".encode())
    assert got.kind == "text" and got.filename.endswith(".txt")
    assert len(got.filename.encode("utf-8")) <= 255
    got = files.kind_for("é" * 200 + ".pdf", "", b"%PDF-1.4")
    assert len(got.filename.encode("utf-8")) <= 255 and got.filename.endswith(".pdf")


def test_empty_zip_is_a_zip():
    eocd = b"PK\x05\x06" + b"\x00" * 18
    assert tuple(files.kind_for("e.zip", "", eocd)) == ("zip", "e.zip")


def test_finish_requires_listed_files_delivered(env):
    (run,) = admitted_run(env)
    fid = _file(env, run)
    out = json.loads(env.ok("run_finish", run_id=run, summary="s", file_ids=[fid],
                            source_ids=[], expectation_status="matched"))
    assert copy_text.SEND_FILE_FIRST.format(name="invoice-march.pdf", run_id=run,
                                            file_id=fid) in out["note"]
    assert env.app.posts("/finish") == []
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.deliver(ref, "withheld")
    out = json.loads(env.ok("run_finish", run_id=run, summary="s", file_ids=[fid],
                            source_ids=[], expectation_status="matched"))
    assert env.app.posts("/finish") == []
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.deliver(ref)
    out = json.loads(env.ok("run_finish", run_id=run, summary="s", file_ids=[fid],
                            source_ids=[], expectation_status="matched"))
    assert "Finished" in out["note"]


def test_handoff_published_once_when_report_fails(env, tmp_path, monkeypatch):
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    import casa_handoff
    monkeypatch.setattr(casa_handoff, "root_dir", lambda: str(handoff))
    env.casa.requester.update(requester_id="specialist:finance", requester_kind="specialist")
    (run,) = admitted_run(env)
    fid = _file(env, run)
    env.app.fail_next[("POST", f"/v1/control/files/{fid}/delivery")] = 2
    first = env.ok("handoff_files", run_id=run, file_ids=[fid])
    second = env.ok("handoff_files", run_id=run, file_ids=[fid])
    assert len(list((handoff / "browser").glob("*/invoice-march.pdf"))) == 1
    third = env.ok("handoff_files", run_id=run, file_ids=[fid])
    assert env.app.deliveries[-1]["outcome"] == "handed_off"
    path = str(next((handoff / "browser").glob("*/invoice-march.pdf")))
    assert path in third
    out = json.loads(env.ok("run_finish", run_id=run, summary="s", file_ids=[fid],
                            source_ids=[], expectation_status="matched"))
    assert "Finished" in out["note"]


def test_recovery_sentences_name_the_ids(env):
    (run,) = admitted_run(env)
    fid = _file(env, run)
    ref = json.loads(env.ok("deliver_file", run_id=run, file_id=fid, caption=""))["file"]
    env.casa.delivery[ref] = {"status": "unknown", "repeat": False}
    text = env.ok("continue_run", run_ref=run)
    assert f"run {run}" in text and f"file {fid}" in text


@pytest.mark.parametrize("head", [b"PK\x03\x04rest", b"PK\x05\x06" + b"\x00" * 10,
                                  b"PK\x07\x08" + b"\x00" * 40])
def test_truncated_zip_is_not_a_zip(head):
    assert files.kind_for("a.zip", "", head) is None


def test_media_detection_matches_casa(casa):
    samples = {"pdf": b"%PDF-1.4", "png": b"\x89PNG\r\n\x1a\nxx", "jpg": b"\xff\xd8\xffx",
               "zip": b"PK\x03\x04" + b"\x00" * 30, "ezip": b"PK\x05\x06" + b"\x00" * 18,
               "short": b"PK\x03\x04rest", "text": "plain\r\ntext\t".encode(),
               "esc": b"ansi \x1b[0m"}
    ours = {k: (files.kind_for("x", "", v) or (None,))[0] for k, v in samples.items()}
    got = casa_eval(casa, f"""
import media_policies
samples = {{k: bytes.fromhex(v) for k, v in {json.dumps({k: v.hex() for k, v in samples.items()})}.items()}}
out = {{}}
for k, v in samples.items():
    out[k] = next((kind for kind in ("document", "photo", "zip", "text")
                   if media_policies.MEDIA_POLICIES[kind].accepts(v)), None)
print(json.dumps(out))
""")
    assert ours == got

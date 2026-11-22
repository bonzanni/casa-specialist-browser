"""The admission digest: RFC 8785 of every RunSpec member except
`admission`, SHA-256, `sha256:<hex>` — checked on shared vectors."""
from __future__ import annotations

import hashlib
import json

import pytest
import rfc8785

from conftest import ROOT
from tools_run import canonical_spec, spec_digest

VECTORS = json.loads((ROOT / "tests" / "vectors" / "spec_digest.json").read_text("utf-8"))


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_vectors(v):
    assert canonical_spec(v["members"]).decode("utf-8") == v["canonical"]
    assert rfc8785.dumps(v["members"]).decode("utf-8") == v["canonical"]
    assert spec_digest(v["members"]) == v["digest"]
    assert v["digest"] == "sha256:" + hashlib.sha256(v["canonical"].encode()).hexdigest()


def test_admission_member_excluded():
    v = VECTORS[0]["members"]
    assert spec_digest(dict(v, admission={"origin_ref": "x"})) == VECTORS[0]["digest"]


@pytest.mark.parametrize("bad", [{"a": 1.5}, {"é": 1}, {"a": {1, 2}}, {"a": 2 ** 60},
                                 {"a": [1.0]}, {"a": b"x"}])
def test_refuses_floats_and_non_ascii_keys(bad):
    with pytest.raises(ValueError):
        spec_digest(bad)

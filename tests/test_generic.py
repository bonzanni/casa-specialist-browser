"""Fido is general: nothing it ships names one site or product. A term here fails the build
wherever it appears in what Casa installs or the model reads; tests and fixtures are out of the
scan. Add a term when a job teaches one; never allow-list a file to pass."""
from __future__ import annotations

import re

import pytest

from conftest import ROOT

TERMS = ("cardmarket", "moxfield", "scryfall", "magic the gathering", "mtg", "secret lair")
SCANNED = ("plugins", "role", "persona", "scripts", "manifest.json", "config-schema.json",
           "README.md")


def _words(text: str) -> str:
    """Lower case, with every run of anything else one space, so `Magic: The Gathering` and
    `magic-the-gathering` are the one term, and `mtg` is not a part of a longer word."""
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "


def _files():
    for name in SCANNED:
        path = ROOT / name
        for f in sorted(path.rglob("*")) if path.is_dir() else [path]:
            if f.is_file() and "__pycache__" not in f.parts:
                yield f


def mentions(text: str) -> list[str]:
    words = _words(text)
    return [term for term in TERMS if f" {term} " in words]


@pytest.mark.parametrize("path", list(_files()), ids=lambda f: str(f.relative_to(ROOT)))
def test_no_shipped_file_names_a_site_or_product(path):
    text = path.read_bytes().decode("utf-8", "ignore")
    assert mentions(text) == [], f"{path.relative_to(ROOT)} is not general"


@pytest.mark.parametrize("text, found", [
    ("Open CARDMARKET now", ["cardmarket"]),
    ("It covers Magic: The Gathering cards", ["magic the gathering"]),
    ("an MTG deck", ["mtg"]),
    ("the Secret-Lair drop", ["secret lair"]),
    ("a smtg of its own, a mtgo id", []),
])
def test_the_scan_reads_case_and_punctuation_but_only_whole_words(text, found):
    assert mentions(text) == found

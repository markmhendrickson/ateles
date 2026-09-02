#!/usr/bin/env python3
"""Tests for the Neotoma-sourced spoken-language set.

The degraded paths carry most of the weight here. A filter that silently runs
on a built-in default while reporting healthy is the failure this module was
written to avoid, so every fallback asserts on ``source`` as well as on the
languages themselves.
"""

from __future__ import annotations

import json
import time

import pytest

import spoken_languages as sl
from hallucination_filter import screen_transcription

# The real locale_profile payload, as returned by Neotoma. Pinned here so a
# change to the entity's shape fails a test instead of silently degrading.
REAL_PAYLOAD = {
    "entities": [
        {
            "entity_id": "ent_ea9a413189860f872c6cc99a",
            "snapshot": {
                "profile_key": "default",
                "primary_jurisdiction": "Spain (EU)",
                "timezone": "Europe/Madrid",
                "currency": "EUR",
                "language": "English",
                "secondary_languages": ["Spanish", "Catalan"],
                "visibility": "private",
            },
        }
    ]
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "CACHE_PATH", tmp_path / "langs.json")


def _serve(monkeypatch, payload):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(payload))


def _fail(monkeypatch, exc=ConnectionError("no network")):
    import httpx

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(httpx, "post", boom)


def test_reads_the_operators_languages_from_locale_profile(monkeypatch):
    """locale_profile already carried this; nothing read it before."""
    _serve(monkeypatch, REAL_PAYLOAD)
    result = sl.spoken_languages()
    assert result.languages == ("en", "es", "ca")
    assert result.source == "neotoma"
    assert result.is_fresh


def test_catalan_is_present_because_the_operator_speaks_it(monkeypatch):
    """Regression guard for the specific error this correction fixes.

    Catalan was dropped on the evidence that the capture corpus contained no
    Catalan speech — confusing absence from a sample with absence from the
    operator's speech.
    """
    _serve(monkeypatch, REAL_PAYLOAD)
    assert "ca" in sl.spoken_languages().languages


def test_an_unreachable_profile_falls_back_to_the_last_good_read(monkeypatch):
    """A cached set beats both falling open and falling closed."""
    _serve(monkeypatch, REAL_PAYLOAD)
    assert sl.spoken_languages().source == "neotoma"

    _fail(monkeypatch)
    degraded = sl.spoken_languages()
    assert degraded.languages == ("en", "es", "ca")
    assert degraded.source == "cache"
    assert not degraded.is_fresh, "a cached read must not look fresh"


def test_with_no_cache_it_seeds_rather_than_failing_open_or_closed(monkeypatch):
    """Neither "every language" (fabrications pass) nor "English only"
    (real Spanish and Catalan get filtered)."""
    _fail(monkeypatch)
    seeded = sl.spoken_languages()
    assert seeded.source == "seed"
    assert seeded.languages == ("en", "es", "ca")
    assert not seeded.is_fresh


def test_the_set_is_never_empty(monkeypatch):
    """An empty set makes every non-ASCII letter foreign, which would filter
    the operator's own Spanish and Catalan."""
    for payload in (
        {"entities": []},
        {"entities": [{"snapshot": {"language": None, "secondary_languages": []}}]},
    ):
        _serve(monkeypatch, payload)
        assert sl.spoken_languages().languages, payload


def test_transcription_never_raises_on_a_neotoma_outage(monkeypatch):
    """This runs on a laptop that may have no Neotoma reachability at all.

    A hard dependency here would take live transcription down on an outage.
    """
    _fail(monkeypatch, RuntimeError("boom"))
    assert sl.spoken_languages().languages


def test_a_stale_cache_is_reported_as_stale(monkeypatch):
    """A caller that cannot tell a live read from a week-old cache cannot tell
    a healthy filter from a silently stale one."""
    sl.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    sl.CACHE_PATH.write_text(
        json.dumps(
            {
                "languages": ["en", "es", "ca"],
                "written_at": time.time() - (10 * 24 * 3600),
            }
        ),
        encoding="utf-8",
    )
    _fail(monkeypatch)
    assert "stale" in sl.spoken_languages().detail


def test_language_names_fold_to_the_codes_the_filter_uses(monkeypatch):
    """locale_profile stores names ("Spanish"); the tables are keyed by code."""
    _serve(
        monkeypatch,
        {
            "entities": [
                {
                    "snapshot": {
                        "language": "English",
                        "secondary_languages": ["Castilian", "Valencian"],
                    }
                }
            ]
        },
    )
    assert sl.spoken_languages().languages == ("en", "es", "ca")


def test_the_resolved_set_actually_drives_the_orthography_check(monkeypatch):
    """The point of the whole module: a set that resolves but is never applied
    would be another shipped-and-never-selected component."""
    _serve(monkeypatch, REAL_PAYLOAD)
    langs = sl.spoken_languages().languages

    catalan = screen_transcription(
        "Això és el català.", expected_language="en", plausible_languages=langs
    )
    assert not catalan.filtered, "real Catalan speech must survive"

    german = screen_transcription(
        "Möchtest du ein Feuer?", expected_language="en", plausible_languages=langs
    )
    assert german.filtered and german.reason == "foreign_diacritic"

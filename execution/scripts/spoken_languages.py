#!/usr/bin/env python3
"""The set of languages the operator actually SPEAKS, sourced from Neotoma.

Why this is not a constant
--------------------------
The orthography check in ``hallucination_filter`` decides whether a non-ASCII
letter is the operator code-switching or a fabricated turn. That decision is
operator-specific: it depends on which languages this particular person speaks,
which is exactly the class of configuration CLAUDE.md requires be read from
Neotoma at runtime rather than baked into code, so the swarm stays portable.

It is also the class of thing a code constant gets WRONG. The set shipped as
``(en, es, ca, fr, de, pt, it)`` — languages the operator can read — which let
German and Portuguese fabrications through. Narrowing it by hand to ``(en, es)``
then dropped Catalan, which the operator does speak, on the evidence that the
capture corpus contained none. Absence from a sample is not absence from
someone's speech. Both errors are the same error: a local guess about a fact
the operator had already recorded.

``locale_profile`` already carries it
-------------------------------------
The entity exists, is provisioned by ``ateles/provision.py``, and holds
``language: "English"`` with ``secondary_languages: ["Spanish", "Catalan"]``.
Nothing in the codebase read it before this module. Adding a new field or a new
entity would have duplicated a source of truth that was already correct.

Fallback, chosen deliberately
-----------------------------
This runs on the transcription path, which is a laptop process that may have no
Neotoma reachability at all. A hard dependency would take live transcription
down on an outage, so an unreachable profile must degrade rather than raise.

Both obvious degradations are wrong:

* Fall OPEN (every language plausible) and the orthography signal silently
  stops catching anything — the filter reports healthy while admitting every
  fabrication it exists to reject.
* Fall CLOSED (English only) and real Spanish and Catalan speech gets marked as
  fabrication — the false positives this filter has never once produced.

So the fallback is neither: it is the last successfully-loaded set, cached on
disk, and only if no cache has ever been written does it use a conservative
built-in seed. The seed is the union of the operator's known spoken languages,
which fails toward admitting real speech rather than filtering it, because a
missed fabrication is recoverable by eye and a filtered real turn is a
corrupted record. Every degraded path logs at WARNING and is visible in the
returned ``LanguageSet.source``, so a stale set is diagnosable instead of
looking like a fresh read.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

CACHE_PATH = Path(
    os.environ.get(
        "STREAM_TRANSCRIPT_LANGUAGE_CACHE",
        str(Path.home() / ".cache" / "ateles" / "spoken_languages.json"),
    )
)

# Only used when Neotoma is unreachable AND no cache has ever been written.
# Deliberately the operator's known spoken set rather than a bare ("en",):
# see the module docstring on why falling closed is the worse failure.
SEED_LANGUAGES: tuple[str, ...] = ("en", "es", "ca")

CACHE_TTL_SECONDS = int(
    os.environ.get("STREAM_TRANSCRIPT_LANGUAGE_CACHE_TTL", str(24 * 3600))
)

# locale_profile stores human-readable names ("Spanish"); the filter's
# orthography tables are keyed by ISO code. normalize_language in
# hallucination_filter handles codes and English names alike, so reuse it
# rather than keeping a second mapping that can drift.


@dataclass(frozen=True)
class LanguageSet:
    """A resolved language set plus WHERE it came from.

    ``source`` is not decoration: a caller that cannot tell a live read from a
    week-old cache cannot tell a healthy filter from a silently stale one.
    """

    languages: tuple[str, ...]
    source: str  # "neotoma" | "cache" | "seed"
    detail: str = ""

    @property
    def is_fresh(self) -> bool:
        return self.source == "neotoma"


def _auth_headers() -> dict[str, str]:
    if not NEOTOMA_BEARER_TOKEN:
        return {}
    return {"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"}


def _to_codes(values: list[str]) -> tuple[str, ...]:
    """Fold locale_profile's language names to ISO codes, order-preserving."""
    from hallucination_filter import normalize_language

    out: list[str] = []
    for v in values:
        code = normalize_language(v)
        if code and code not in out:
            out.append(code)
    return tuple(out)


def _read_cache() -> LanguageSet | None:
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        langs = tuple(raw["languages"])
        if not langs:
            return None
        age = time.time() - float(raw.get("written_at", 0))
        stale = " (stale)" if age > CACHE_TTL_SECONDS else ""
        return LanguageSet(
            langs, "cache", f"cached {age / 3600:.1f}h ago{stale}"
        )
    except Exception:
        return None


def _write_cache(languages: tuple[str, ...]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"languages": list(languages), "written_at": time.time()}),
            encoding="utf-8",
        )
    except OSError as exc:  # a read-only cache dir must not break transcription
        log.warning(f"could not cache spoken languages: {exc}")


def _fetch_from_neotoma(timeout: float) -> tuple[str, ...]:
    """Read locale_profile. Raises on any failure; the caller degrades."""
    import httpx

    # POST /entities/query is the canonical list route; /retrieve_entities is
    # the MCP TOOL name and 404s on the hosted instance (see agent_loader.py).
    resp = httpx.post(
        f"{NEOTOMA_BASE_URL}/entities/query",
        json={
            "entity_type": "locale_profile",
            "limit": 10,
            "include_snapshots": True,
        },
        headers=_auth_headers(),
        timeout=timeout,
    )
    resp.raise_for_status()
    entities = resp.json().get("entities", [])
    if not entities:
        raise LookupError("no locale_profile entity")

    snap = entities[0].get("snapshot") or {}
    if isinstance(snap.get("snapshot"), dict):  # tolerate one extra nesting
        snap = snap["snapshot"]

    names: list[str] = []
    primary = snap.get("language")
    if isinstance(primary, str) and primary.strip():
        names.append(primary)
    secondary = snap.get("secondary_languages") or []
    if isinstance(secondary, list):
        names.extend(s for s in secondary if isinstance(s, str) and s.strip())

    codes = _to_codes(names)
    if not codes:
        raise LookupError(f"locale_profile carried no usable language: {names!r}")
    return codes


def spoken_languages(timeout: float = 5.0) -> LanguageSet:
    """Resolve the operator's spoken languages, degrading rather than raising.

    Never raises. Never returns an empty set — an empty set would make every
    non-ASCII letter foreign and filter the operator's own speech.
    """
    try:
        codes = _fetch_from_neotoma(timeout)
    except Exception as exc:
        cached = _read_cache()
        if cached is not None:
            log.warning(
                f"locale_profile unreachable ({exc}); using cached spoken "
                f"languages {cached.languages} — {cached.detail}"
            )
            return cached
        log.warning(
            f"locale_profile unreachable ({exc}) and no cache written; using "
            f"seed {SEED_LANGUAGES}. The orthography check is running on a "
            "built-in default, not on operator configuration."
        )
        return LanguageSet(SEED_LANGUAGES, "seed", str(exc))

    _write_cache(codes)
    return LanguageSet(codes, "neotoma", "read from locale_profile")

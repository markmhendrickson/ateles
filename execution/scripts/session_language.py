"""Resolve the operator's spoken languages from Neotoma, for transcription.

Why this module exists
----------------------
`stream_transcript.py` opened its Realtime session with
``transcription: {"model": model}`` and NO language field. The API's own echo
confirms the consequence — it replies ``"language": null`` — so every session
ran full auto-detect.

Read the honest scope of this module before relying on it. Pinning the language
is NOT what stops the fabrications: measured against the live socket,
`gpt-4o-transcribe` with ``language: "en"`` still returns Japanese for Japanese
speech, identical to the unpinned run. The pin is an accuracy and latency hint
within a language, not a constraint on the output alphabet. The defence that
actually works is the INPUT GATE — audio that never reaches the decoder cannot
be described by it.

What this module is for, then, is making the session's expected languages
explicit and operator-sourced: the pin the API gets, and — the part that does
filter — the ``plausible`` set the output hallucination filter screens against,
so a trilingual operator code-switching is not flagged while a fabrication in a
fourth language still is.

The languages are an OPERATOR PREFERENCE, not a property of this pipeline, so
they are read from the `locale_profile` context entity at runtime rather than
hardcoded — the same rule the agent prompts follow (CLAUDE.md: "jurisdiction,
timezone, currency, language from `locale_profile`"). A forked swarm supplies
its own entity and this path follows it with no code change.

Degrading safely
----------------
Neotoma being unreachable must NOT take live transcription down, and must not
silently resurrect auto-detect either. `resolve_session_languages` always
returns a usable result and reports, in `source`, where it came from — the
caller announces that on the operator-visible channel. The fallback pin is the
explicit `STREAM_TRANSCRIPT_LANGUAGE` / `--language` value, never "auto".

The PIN and the PLAUSIBLE SET degrade differently, and conflating them was a
real defect. The pin is one code and falling back to the configured language is
right. The plausible set is not: collapsing it to that same single language
makes every non-ASCII letter foreign, so on an outage the operator's genuine
Spanish and Catalan — "El niño está aquí.", "Això és el català." — get marked
`foreign_diacritic`. Both obvious degradations are wrong:

* Fall OPEN (every language plausible) and the orthography signal silently
  stops catching anything — the filter reports healthy while admitting the
  fabrications it exists to reject.
* Fall CLOSED (the pin only) and real speech is recorded as fabrication — the
  false positives this filter has never once produced.

So the plausible set degrades to neither: it is the last successfully-read set,
cached on disk, and only if no cache has ever been written does it use a
conservative seed of the operator's known spoken languages. That errs toward
admitting real speech, because a missed fabrication is recoverable by eye while
a filtered real turn is a corrupted record. Every degraded path warns and is
visible in `source`, so a stale set is diagnosable rather than looking fresh.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from hallucination_filter import DEFAULT_PLAUSIBLE_LANGUAGES, normalize_language

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
LOCALE_PROFILE_KEY = os.environ.get("ATELES_LOCALE_PROFILE_KEY", "default")

# Last good read of the operator's spoken set, so an outage degrades to what
# was true yesterday rather than to a guess. Not a cache of the PIN — that
# falls back to the explicitly configured language, which needs no cache.
#
# Keyed by LOCALE_PROFILE_KEY: two profile keys on one host must not seed each
# other's degraded set, which would silently screen one operator's speech
# against another's languages.
CACHE_PATH = Path(
    os.environ.get(
        "STREAM_TRANSCRIPT_LANGUAGE_CACHE",
        str(
            Path.home()
            / ".cache"
            / "ateles"
            / f"spoken_languages.{LOCALE_PROFILE_KEY}.json"
        ),
    )
)
CACHE_TTL_SECONDS = int(
    os.environ.get("STREAM_TRANSCRIPT_LANGUAGE_CACHE_TTL", str(24 * 3600))
)

# Used only when Neotoma is unreachable AND no cache has ever been written.
# Deliberately the operator's known spoken set rather than a bare ("en",):
# see the module docstring on why falling closed is the worse failure. Shared
# with the filter's own default so the two cannot drift apart.
SEED_PLAUSIBLE_LANGUAGES: tuple[str, ...] = DEFAULT_PLAUSIBLE_LANGUAGES

# The hosted instance sits behind Cloudflare, which 1010-blocks urllib's
# default User-Agent. Naming ourselves is what makes the read work at all.
_USER_AGENT = "ateles-stream-transcript/1.0"

# Codes the Realtime API accepts for `transcription.language`, read back from
# the API's own error message when an invalid code is sent. Kept here so a
# preference naming a language the API cannot pin degrades to a reported
# blocker instead of a session that fails to open.
REALTIME_SUPPORTED_LANGUAGES = frozenset(
    """af ar az be bg bs ca cs cy da de el en es et fa fi fr gl he hi hr hu hy
    id is it iw ja kk kn ko lt lv mi mk mr ms ne nl no pl pt ro ru sk sl sr sv
    sw ta th tl tr uk ur vi zh""".split()
)


@dataclass(frozen=True)
class SessionLanguages:
    """The operator's spoken languages, resolved.

    ``primary`` is the single code sent to the Realtime API — see
    `stream_transcript.session_update_message` for why only one can be sent on
    the server-VAD path. ``plausible`` is the full set, which the output
    hallucination filter uses so genuine code-switching is not flagged.
    """

    primary: str
    plausible: tuple[str, ...]
    source: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _fetch_locale_profile(timeout: float) -> dict | None:
    """The `locale_profile` snapshot, or None when it cannot be read."""
    token = _bearer_token()
    if not token:
        return None
    body = json.dumps(
        {
            "entity_type": "locale_profile",
            "limit": 25,
            "include_snapshots": True,
        }
    ).encode()
    # POST /entities/query is the canonical list route; /retrieve_entities is
    # an MCP TOOL name and 404s on the hosted instance (agent_loader.py).
    req = urllib.request.Request(
        f"{NEOTOMA_BASE_URL}/entities/query",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data = json.load(resp)
    entities = data.get("entities") or []
    fallback = None
    for ent in entities:
        # /entities/query returns the field dict either flat under "snapshot"
        # or nested one level deeper; accept both.
        outer = ent.get("snapshot") or {}
        snap = outer.get("snapshot", outer) if isinstance(outer, dict) else {}
        if not isinstance(snap, dict):
            continue
        if snap.get("profile_key") == LOCALE_PROFILE_KEY:
            return snap
        if fallback is None:
            fallback = snap
    return fallback


def _bearer_token() -> str | None:
    token = os.environ.get("NEOTOMA_BEARER_TOKEN")
    if token:
        return token.strip()
    # Same dotenv the OpenAI key comes from, so an operator who set one has set
    # both. Never logged.
    name = "NEOTOMA_BEARER_TOKEN"
    path = os.path.join(os.path.expanduser("~"), ".config", "neotoma", ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _read_cached_plausible() -> tuple[tuple[str, ...], str] | None:
    """The last good spoken set and how old it is, or None if unusable."""
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        # Re-normalize rather than trusting the file: it is on disk, so a hand
        # edit or a cache written by an older shape could otherwise carry a
        # non-ISO code straight into `plausible`, where it silently matches no
        # orthography table and narrows the allowed set without saying so.
        #
        # `normalize_language` is deliberately permissive — it passes an
        # unrecognized code through so the filter can still compare labels — so
        # a second check against the codes this API can actually pin is what
        # rejects junk here.
        codes = tuple(
            code
            for code in (
                normalize_language(c)
                for c in raw["languages"]
                if isinstance(c, str)
            )
            if code and code in REALTIME_SUPPORTED_LANGUAGES
        )
        if not codes:
            return None
        age = time.time() - float(raw.get("written_at", 0))
        stale = ", STALE" if age > CACHE_TTL_SECONDS else ""
        return codes, f"cached {age / 3600:.1f}h ago{stale}"
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cached_plausible(codes: tuple[str, ...]) -> None:
    """Record a good read. A read-only cache dir must not break transcription."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"languages": list(codes), "written_at": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        pass


def languages_from_profile(snapshot: dict) -> list[str]:
    """ISO-639-1 codes from a locale_profile snapshot, primary first.

    The entity stores English NAMES ("English", "Spanish", "Catalan"), which
    `normalize_language` already folds to codes for the hallucination filter.
    Reusing it keeps one table rather than two that can drift apart.
    """
    raw = [snapshot.get("language")]
    secondary = snapshot.get("secondary_languages") or []
    if isinstance(secondary, str):
        secondary = [secondary]
    raw.extend(secondary)

    codes: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            continue
        code = normalize_language(value)
        if code and code not in codes:
            codes.append(code)
    return codes


def resolve_session_languages(
    fallback_language: str,
    *,
    override: str | None = None,
    timeout: float = 5.0,
) -> SessionLanguages:
    """The languages this session should be pinned and filtered against.

    Never raises and never returns "auto": an unreachable Neotoma degrades to
    ``fallback_language``, which is still a pin. Losing the pin is the defect
    this module exists to close, so the degraded path keeps one.
    """
    warnings: list[str] = []

    if override:
        codes = [c for c in (normalize_language(p) for p in override.split(",")) if c]
        if codes:
            return _build(codes, "explicit override", warnings)
        warnings.append(f"could not parse --languages {override!r}")

    try:
        snapshot = _fetch_locale_profile(timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        snapshot = None
        warnings.append(
            f"locale_profile unreadable ({type(exc).__name__}) — "
            "falling back to the configured language"
        )

    if snapshot is None:
        if not warnings:
            warnings.append(
                "no locale_profile entity found — falling back to the "
                "configured language"
            )
        return _degraded(fallback_language, warnings)

    codes = languages_from_profile(snapshot)
    if not codes:
        warnings.append(
            "locale_profile carries no usable language field — falling back "
            "to the configured language"
        )
        return _degraded(fallback_language, warnings)

    _write_cached_plausible(tuple(codes))
    return _build(codes, f"locale_profile:{LOCALE_PROFILE_KEY}", warnings)


def _degraded(fallback_language: str, warnings: list[str]) -> SessionLanguages:
    """The unreachable-Neotoma result: a pin, plus a plausible set that is NOT
    just that pin.

    Collapsing `plausible` to the single fallback code is what marks the
    operator's own Spanish and Catalan as `foreign_diacritic` during an outage.
    The pin still degrades to the configured language — one code is all the API
    takes — while the set the output filter screens against degrades to the
    last good read, else the seed.
    """
    pin = normalize_language(fallback_language) or "en"

    cached = _read_cached_plausible()
    if cached is not None:
        codes, detail = cached
        warnings.append(
            f"spoken languages are a cached read, not live ({detail}): "
            f"{'/'.join(codes)}"
        )
        source = "cache"
    else:
        codes = SEED_PLAUSIBLE_LANGUAGES
        warnings.append(
            "no cached spoken languages either — the output filter is running "
            f"on the built-in seed {'/'.join(codes)}, not on operator "
            "configuration"
        )
        source = "seed"

    # The pin must be inside the set it is screened against, or a monolingual
    # override plus a cached multilingual set would disagree about the pin.
    plausible = (pin,) + tuple(c for c in codes if c != pin)
    resolved = _build([pin], source, warnings)
    return SessionLanguages(
        primary=resolved.primary,
        plausible=plausible,
        source=source,
        warnings=resolved.warnings,
    )


def _build(codes: list[str], source: str, warnings: list[str]) -> SessionLanguages:
    primary = codes[0]
    if primary not in REALTIME_SUPPORTED_LANGUAGES:
        warnings.append(
            f"the Realtime API cannot pin {primary!r}; sending no language "
            "would restore auto-detect, so the session pins 'en' instead"
        )
        primary = "en"
    return SessionLanguages(
        primary=primary,
        plausible=tuple(codes),
        source=source,
        warnings=tuple(warnings),
    )

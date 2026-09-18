#!/usr/bin/env python3
"""resolve_ateles_identity.py — live Ateles identity resolution for interactive sessions.

Closes the asymmetry documented in ateles task ent_ebb8ecc95b19ca2ba5f0201c:
dispatched agents already resolve their agent_definition LIVE from Neotoma on
every dispatch (lib/daemon_runtime/agent_loader.py::AgentLoader.load()), and
degrade LOUDLY (a logged, flagged stub) on failure. The interactive session's
SessionStart hook (.claude/hooks/ateles-session-start.sh) instead `cat`(1)ed a
static on-disk mirror with no live fetch and no staleness signal — which is
how four rules sat in Neotoma undelivered for eleven days (the entity was
updated 2026-09-14; the mirror was last regenerated 2026-09-07).

This script is the interactive-session equivalent of AgentLoader.load(), and
it REUSES AgentLoader rather than re-implementing an HTTP-fetch-with-auth
(docs/foundation/principles.md's "extend the mechanism that already
generalizes" — CLAUDE.md's verification-discipline section names this pattern
directly: a parallel fetch mechanism is exactly what a from-scratch HTTP call
here would be, and this repo already has instances of that defect).

Three things the daemon path solves differently that an interactive shell
must solve for itself:

1. Token resolution. AgentLoader reads NEOTOMA_BEARER_TOKEN from the
   process environment, which a daemon's LaunchAgent plist supplies but an
   interactive terminal often does not. This script falls back to
   ~/.config/neotoma/.env using the SAME parsing approach as
   execution/scripts/render_agent_docs.py::_load_env, rather than inventing a
   new one. The token is never printed or logged.

2. Loud degradation with no log channel. A daemon fails into logs; this
   script's only channel is what it prints to stdout, which the SessionStart
   hook injects into the model's context. So "loud" means: on a failed live
   read, print the cached definition PLUS an explicit banner naming the
   failure and the cache's age. Cached text is never presented as current.

3. Fail-open. Any unexpected error must exit 0 having printed a marked
   fallback (cache, or the static mirror, each with a STALE IDENTITY banner)
   rather than raising — a session that cannot start is worse than one that
   starts on stale rules. Printing nothing is not fail-open: the calling hook
   would then inject the mirror as if it were current. The hook
   (ateles-session-start.sh) still banners that mirror itself when this
   script is missing or prints only whitespace, so a missing script cannot
   skip the marker.

Design basis: docs/foundation/principles.md invariant #6 ("Extend the
mechanism that already generalizes; do not build a parallel one" — AgentLoader
reuse, above) and invariant #7 ("Unknown stays distinct from a conclusion" —
a failed live read must resolve to a visibly-marked "stale" state, never
silently to "current", the same way a failed reachability check must resolve
to `unknown`, never to `clean` or `denied`-as-if-verified).

Cache: a dedicated file under .claude/.session_state/ (gitignored — see
.claude/.gitignore), NOT the git-tracked .claude/skills/ateles/SKILL.md. That
mirror is a byte-for-byte render_agent_docs.py output that
scripts/lint.sh (as of PR #1092, fix/agent-mirror-drift-binding) now checks
BINDINGLY against Neotoma. Overwriting it at session-start with a live fetch
would either (a) dirty the git working tree on every session, which the
lint check would then flag as drift even when Neotoma and the mirror agree in
substance, or (b) require duplicating render_agent_docs.py's exact rendering
logic to byte-match it, which is the "parallel mechanism" invariant #6 warns
against. Keeping this cache separate means the two checks — "is the
committed mirror faithful to Neotoma" (lint, point-in-time, CI) and "did this
session see a live or stale identity" (this script, per-session, interactive)
— stay independent and neither can silently paper over the other.

Resolution outcome (live / cached-with-age / failed-no-cache) is appended as
one JSON line to .claude/.session_state/ateles_identity_resolution.log so
"was this session running stale rules?" is answerable after the fact without
relying on the model having reported it faithfully in conversation.

Usage:
    execution/scripts/resolve_ateles_identity.py
        Prints the resolved identity markdown to stdout and exits 0.
        Never raises past main(); an unexpected error degrades through the
        same cache-then-mirror banner as a failed load, then exits 0.

Env:
    NEOTOMA_BASE_URL, NEOTOMA_BEARER_TOKEN — read directly if set; otherwise
    read from ~/.config/neotoma/.env (same file render_agent_docs.py uses).
    ATELES_IDENTITY_CACHE_MAX_AGE_HOURS — informational only; the banner
    always states the age, this just changes wording at very stale ages.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / ".claude" / ".session_state"
CACHE_FILE = CACHE_DIR / "ateles_identity_cache.md"
LOG_FILE = CACHE_DIR / "ateles_identity_resolution.log"
FALLBACK_MIRROR = REPO_ROOT / ".claude" / "skills" / "ateles" / "SKILL.md"

# The ateles agent_definition entity (CLAUDE.md "Key entity IDs" table).
ATELES_ENTITY_ID = "ent_706f1432822b4a9d9d71c127"


def _load_env() -> None:
    """Populate NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN in os.environ if unset.

    Same fallback file and parsing approach as
    execution/scripts/render_agent_docs.py::_load_env, so the two scripts
    agree on where an interactive shell's Neotoma credentials live. Mutates
    os.environ (rather than returning values) because lib/daemon_runtime/
    agent_loader.py reads NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN as MODULE
    CONSTANTS at import time — they must be set before that import happens.
    """
    base_url = os.environ.get("NEOTOMA_BASE_URL", "")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    env_path = Path.home() / ".config" / "neotoma" / ".env"
    if (not base_url or not token) and env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if key == "NEOTOMA_BASE_URL" and not base_url:
                    base_url = value
                elif key == "NEOTOMA_BEARER_TOKEN" and not token:
                    token = value
        except OSError:
            pass  # fail open — proceed with whatever was already in the environment
    if base_url:
        os.environ["NEOTOMA_BASE_URL"] = base_url
    if token:
        os.environ["NEOTOMA_BEARER_TOKEN"] = token
    # AgentLoader's own priority order tries an explicit
    # "<PREFIX>_AGENT_DEFINITION_ID" env var before falling back to a name
    # search. Set it so we always hit the exact entity by id (avoids a
    # name-search false match and saves a round trip).
    os.environ.setdefault("ATELES_AGENT_DEFINITION_ID", ATELES_ENTITY_ID)


def _age_str(age_seconds: float) -> str:
    if age_seconds < 3600:
        return f"{int(age_seconds // 60)} minute(s)"
    if age_seconds < 86400:
        return f"{age_seconds / 3600:.1f} hour(s)"
    return f"{age_seconds / 86400:.1f} day(s)"


def _log_outcome(outcome: str, detail: str) -> None:
    """Append one JSON line recording how identity was resolved this session.

    Best-effort: a logging failure must never affect the exit code or the
    printed identity text. This is the auditable record for "was this
    session running stale rules?" — CACHE_FILE alone only proves a cache
    existed, not which state a given session actually saw.
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,  # live | warned | cached | failed_no_cache | refused | error
            "detail": detail,
        }
        with LOG_FILE.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _write_cache(markdown: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(markdown)
    except OSError:
        pass  # best-effort; a failed cache write must not fail the live path


def _read_cache() -> "tuple[str, float] | None":
    """Return (markdown, age_seconds) or None if no cache exists / unreadable."""
    try:
        if not CACHE_FILE.exists():
            return None
        markdown = CACHE_FILE.read_text()
        age = time.time() - CACHE_FILE.stat().st_mtime
        return markdown, age
    except OSError:
        return None


def _staleness_banner(age_seconds: float, reason: str) -> str:
    cached_at = datetime.fromtimestamp(
        time.time() - age_seconds, tz=timezone.utc
    ).isoformat(timespec="seconds")
    return (
        "> **STALE IDENTITY — Neotoma unreachable.** Showing a cached copy of "
        f"the `ateles` agent_definition from **{cached_at}** "
        f"(**{_age_str(age_seconds)} old**). Reason: {reason}. Rules added or "
        "changed in Neotoma since that timestamp are NOT reflected below. This "
        "is a degraded fallback, not the current definition — do not treat it "
        "as authoritative for anything time-sensitive (recent standing-rule "
        "changes, renamed agents, current entity ids).\n"
    )


def _fallback_static_mirror_banner(reason: str) -> str:
    return (
        "> **STALE IDENTITY — Neotoma unreachable and no session cache exists.** "
        f"Reason: {reason}. Falling back to the git-tracked "
        f"`.claude/skills/ateles/SKILL.md` mirror, which is regenerated from "
        "Neotoma periodically (see `execution/scripts/render_agent_docs.py`) "
        "and may itself be out of date relative to the live entity. Treat "
        "anything time-sensitive in it as unverified.\n"
    )


def _refused_identity_banner(reason: str) -> str:
    """First line contains REFUSED IDENTITY so the hook can stop before adopt."""
    return (
        "> **REFUSED IDENTITY — agent_definition status forbids adoption.** "
        f"{reason}. The live prompt was not cached and is not printed. "
        "Do not adopt this identity for the session.\n"
    )


def _status_warn_prefix(reason: str) -> str:
    return f"> **STATUS WARNING.** {reason}\n"


def _degrade_to_cache_or_mirror(reason: str) -> "tuple[str, str]":
    """Cache, else static mirror, each prefixed with a STALE IDENTITY banner.

    Shared by resolve()'s failed-load path and main()'s unexpected-exception
    path so an uncaught error cannot print nothing and exit 0. An OSError
    reading the mirror still returns the banner (non-empty) so the hook does
    not treat that failure as "no output" and cat the mirror unmarked.
    """
    cached = _read_cache()
    if cached is not None:
        markdown, age = cached
        return _staleness_banner(age, reason) + "\n" + markdown, "cached"

    banner = _fallback_static_mirror_banner(reason)
    if FALLBACK_MIRROR.exists():
        try:
            static_markdown = FALLBACK_MIRROR.read_text()
        except OSError:
            return banner, "failed_no_cache"
        if static_markdown:
            return banner + "\n" + static_markdown, "failed_no_cache"
    return "", "failed_no_cache"


def _emit(markdown: str) -> None:
    if not markdown:
        return
    sys.stdout.write(markdown)
    if not markdown.endswith("\n"):
        sys.stdout.write("\n")


def resolve() -> "tuple[str, str]":
    """Return (markdown_to_print, outcome).

    outcome is one of "live" | "warned" | "cached" | "failed_no_cache" |
    "refused". "error" is logged only by main() when resolve() itself raises.
    """
    _load_env()

    # Imported after _load_env() populates os.environ, because agent_loader
    # reads NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN as module-level constants
    # at import time.
    lib_dir = REPO_ROOT / "lib" / "daemon_runtime"
    sys.path.insert(0, str(lib_dir))
    from agent_loader import AgentLoader, StatusAction, evaluate_status  # type: ignore  # noqa: E402

    loader = AgentLoader("ateles")
    agent_def = loader.load()

    if not agent_def.is_stub and agent_def.prompt_markdown:
        # evaluate_status, not enforce_status_or_exit: the latter sys.exit(1)s
        # on REFUSE, SystemExit is not an Exception, and the hook's `|| true`
        # would then cat the mirror with no banner.
        action, reason = evaluate_status(agent_def.status)
        if action is StatusAction.REFUSE:
            return _refused_identity_banner(reason), "refused"
        if action is StatusAction.WARN:
            _write_cache(agent_def.prompt_markdown)
            return _status_warn_prefix(reason) + "\n" + agent_def.prompt_markdown, "warned"
        _write_cache(agent_def.prompt_markdown)
        return agent_def.prompt_markdown, "live"

    # Live load failed (or returned an empty prompt, which AgentLoader never
    # does on a genuine success — see AgentDefinition docstring). Fall back to
    # the cache, announcing staleness rather than silently substituting it.
    reason = agent_def.load_error or "unknown load failure"
    return _degrade_to_cache_or_mirror(reason)


def main() -> int:
    try:
        markdown, outcome = resolve()
    except Exception as exc:  # noqa: BLE001 - fail-open is the whole point
        _log_outcome("error", f"{type(exc).__name__}: {exc}")
        markdown, _degraded = _degrade_to_cache_or_mirror(
            f"resolver raised {type(exc).__name__}: {exc}"
        )
        _emit(markdown)
        return 0

    detail = ""
    if outcome == "cached":
        cached = _read_cache()
        detail = f"cache_age_seconds={cached[1]:.0f}" if cached else ""
    elif outcome == "refused":
        detail = "status refused; prompt not cached"
    _log_outcome(outcome, detail)
    _emit(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())

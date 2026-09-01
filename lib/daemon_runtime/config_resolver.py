"""
lib/daemon_runtime/config_resolver.py — resolve daemon CONFIG from Neotoma
`daemon_configuration` entities, with a local cache and a loud failure mode.

Why this exists
---------------
Every configuration failure the swarm has suffered was an env var that was
absent, stale, or pointed at the wrong thing — and in each case the wrong
configuration was INDISTINGUISHABLE from the right one:

  * NEOTOMA_SSE_SUBSCRIPTION_ID_APIS missing from the Apis plist → Apis consumed
    zero task events for 88 days. sse_client logged one warning and returned.
  * NEOTOMA_BEARER_TOKEN absent as a repo secret → lanius-stale-issues.yml
    failed every scheduled run for 12+ weeks, exiting in 0.08s.
  * A Fly app var silently resolved to a CLIENT's app; a deploy moved the wrong
    instance behind a green workflow.

An env var has no provenance, no history, and nothing to query. A
`daemon_configuration` entity has all three. This module is the read path.

SECRETS ARE NOT STORED HERE. A config entity may NAME the secret it needs
(``token_secret_name: "NEOTOMA_BEARER_TOKEN"``); the VALUE stays in SOPS/1Password
and is read from the environment. That indirection is the point: a named-but-
absent secret becomes a *detectable, queryable* condition instead of a silent
empty string.

Resolution order (first hit wins)
---------------------------------
  1. **Environment variable** — an explicit operator override, and the escape
     hatch when Neotoma is unavailable AND no cache exists. Always wins, so this
     module can never make a working daemon worse.
  2. **Neotoma** ``daemon_configuration`` entity for this daemon (authoritative).
  3. **Local cache** — last-known-good, written on every successful fetch.
  4. **Declared default** on the ConfigSpec, if the spec permits one.
  5. **Loud failure** — ``ConfigResolutionError``, or a ``MISSING`` sentinel the
     caller must handle. Never a silent empty string.

Degradation posture (deliberate)
--------------------------------
Neotoma has served 19-60s reads and intermittent 502s. A daemon that cannot
START because its config store is degraded is a strictly worse failure than one
running slightly stale config — a stale subscription id still delivers events;
a daemon that refused to boot delivers nothing. So:

  * the fetch is **time-boxed** (CONFIG_FETCH_TIMEOUT_S, default 5s) — a slow
    Neotoma costs seconds at startup, never a hang;
  * on timeout/error we fall through to the **cache** and log at WARNING with
    the cache's age, so running-on-stale-config is visible rather than assumed;
  * we only fail hard when a value is resolvable from **nowhere** — which is the
    case that was previously silent.

Stdlib + httpx only; safe to import at daemon startup.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("daemon_runtime.config_resolver")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# Time-box the fetch. Neotoma has served 19-60s reads; a daemon must not hang on
# startup waiting for config it can read from cache instead.
CONFIG_FETCH_TIMEOUT_S = float(os.environ.get("ATELES_CONFIG_FETCH_TIMEOUT_S", "5"))

# Last-known-good cache. Survives a Neotoma outage and a machine reboot.
CONFIG_CACHE_DIR = Path(
    os.environ.get(
        "ATELES_CONFIG_CACHE_DIR", str(Path.home() / ".cache" / "ateles" / "config")
    )
).expanduser()

# Age past which a cache hit is reported as notably stale (informational only —
# we still USE it; refusing to boot on a stale cache is the worse failure).
CACHE_STALE_WARN_SECONDS = float(
    os.environ.get("ATELES_CONFIG_CACHE_STALE_WARN_S", str(7 * 24 * 3600))
)


class ConfigResolutionError(RuntimeError):
    """Raised when a required config value resolves from nowhere.

    This is the loud failure. It names the daemon, the key, every source that
    was tried, and the exact remedy — so the condition can never again look
    like normal operation.
    """


@dataclass(frozen=True)
class ConfigSpec:
    """Declares one configuration value a daemon needs.

    key:          logical name, e.g. "sse_subscription_id"
    env_var:      environment variable consulted first (operator override)
    required:     if True, unresolvable → ConfigResolutionError
    default:      used only when not required and nothing else resolved
    secret_name:  names an env var holding a SECRET this config points at.
                  The value is never stored in Neotoma; we only verify presence
                  so a missing secret is detectable instead of silently empty.
    remedy:       operator-facing instruction, printed on failure.
    """

    key: str
    env_var: str | None = None
    required: bool = True
    default: Any = None
    secret_name: str | None = None
    remedy: str = ""


@dataclass
class ResolvedConfig:
    """The outcome of resolving one daemon's config specs."""

    daemon: str
    values: dict[str, Any] = field(default_factory=dict)
    # key -> "env" | "neotoma" | "cache" | "default"
    sources: dict[str, str] = field(default_factory=dict)
    entity_id: str | None = None
    cache_age_seconds: float | None = None
    degraded: bool = False  # True when Neotoma was unreachable/slow
    missing_secrets: list[str] = field(default_factory=list)

    def get(self, key: str, fallback: Any = None) -> Any:
        return self.values.get(key, fallback)

    def source_of(self, key: str) -> str:
        return self.sources.get(key, "unresolved")

    def provenance_line(self) -> str:
        """One log line naming where every value came from.

        Printed at startup so 'which config is this daemon actually running'
        is answerable from the log alone — the question that took 88 days.
        """
        parts = [f"{k}<-{self.sources.get(k, '?')}" for k in sorted(self.values)]
        suffix = ""
        if self.degraded:
            age = (
                f", cache_age={self.cache_age_seconds / 3600:.1f}h"
                if self.cache_age_seconds is not None
                else ""
            )
            suffix = f" [DEGRADED: Neotoma unreachable{age}]"
        if self.entity_id:
            suffix += f" [entity={self.entity_id}]"
        return f"[{self.daemon}] config: {', '.join(parts) or '(none)'}{suffix}"


def _cache_path(daemon: str) -> Path:
    return CONFIG_CACHE_DIR / f"{daemon}.json"


def _read_cache(daemon: str) -> tuple[dict, float | None]:
    """Return (payload, age_seconds) from the last-known-good cache."""
    path = _cache_path(daemon)
    try:
        if not path.exists():
            return {}, None
        raw = json.loads(path.read_text())
        written = float(raw.get("_cached_at", 0))
        age = time.time() - written if written else None
        return raw.get("values", {}) or {}, age
    except Exception as exc:  # noqa: BLE001 — cache must never crash startup
        log.warning(f"[{daemon}] config cache unreadable at {path}: {exc}")
        return {}, None


def _write_cache(daemon: str, values: dict, entity_id: str | None) -> None:
    """Persist last-known-good config. Never raises."""
    try:
        CONFIG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "_cached_at": time.time(),
            "_entity_id": entity_id,
            "values": values,
        }
        path = _cache_path(daemon)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(path)  # atomic — a torn cache file is worse than none
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[{daemon}] could not write config cache: {exc}")


def _fetch_from_neotoma(daemon: str) -> tuple[dict, str | None]:
    """Fetch the `daemon_configuration` entity for this daemon.

    Returns (config_values, entity_id). Time-boxed; returns ({}, None) on any
    failure so the caller can fall through to cache. Never raises.
    """
    headers = {}
    if NEOTOMA_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {NEOTOMA_BEARER_TOKEN}"

    url = f"{NEOTOMA_BASE_URL}/entities"
    params = {
        "entity_type": "daemon_configuration",
        "search": daemon,
        "include_snapshots": "true",
        "limit": "10",
    }
    try:
        resp = httpx.get(
            url, headers=headers, params=params, timeout=CONFIG_FETCH_TIMEOUT_S
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — degraded Neotoma must not block boot
        log.warning(
            f"[{daemon}] could not fetch daemon_configuration from Neotoma "
            f"({type(exc).__name__}: {exc}) — falling back to cache/env"
        )
        return {}, None

    entities = data.get("entities") or data.get("results") or []
    for ent in entities:
        snap = ent.get("snapshot") or ent
        # `search` is fuzzy; require an exact daemon-name match so a partial hit
        # can never hand one daemon another daemon's configuration. That exact
        # class of mistake pointed a deploy at a client's app.
        if str(snap.get("daemon_name", "")).strip().lower() != daemon.strip().lower():
            continue
        values = snap.get("config") or {}
        if not isinstance(values, dict):
            log.warning(f"[{daemon}] daemon_configuration.config is not an object")
            values = {}
        return values, ent.get("entity_id") or ent.get("id")

    log.warning(
        f"[{daemon}] no daemon_configuration entity with daemon_name=={daemon!r} "
        f"(searched {len(entities)} candidate(s))"
    )
    return {}, None


def resolve(
    daemon: str,
    specs: list[ConfigSpec],
    *,
    allow_neotoma: bool = True,
) -> ResolvedConfig:
    """Resolve every spec for `daemon`. Raises ConfigResolutionError if a
    required value resolves from nowhere.

    Env always wins, so adopting this can never break a daemon that works today.
    """
    result = ResolvedConfig(daemon=daemon)

    remote: dict = {}
    entity_id: str | None = None
    if allow_neotoma:
        remote, entity_id = _fetch_from_neotoma(daemon)

    if remote:
        _write_cache(daemon, remote, entity_id)
        result.entity_id = entity_id
        cached, cache_age = {}, None
    else:
        cached, cache_age = _read_cache(daemon)
        result.cache_age_seconds = cache_age
        result.degraded = allow_neotoma
        if cached and cache_age is not None and cache_age > CACHE_STALE_WARN_SECONDS:
            log.warning(
                f"[{daemon}] using config cache written "
                f"{cache_age / 86400:.1f} days ago — Neotoma unreachable and this "
                f"config may be stale"
            )

    failures: list[str] = []

    for spec in specs:
        value = None
        source = None

        # 1. env override — always wins
        if spec.env_var:
            env_val = os.environ.get(spec.env_var, "")
            # launchd plists carry __PLACEHOLDER__ markers; those are not values.
            if env_val and not (
                env_val.startswith("__") and env_val.endswith("__")
            ):
                value, source = env_val, "env"

        # 2. Neotoma, 3. cache
        if value is None and spec.key in remote:
            value, source = remote[spec.key], "neotoma"
        if value is None and spec.key in cached:
            value, source = cached[spec.key], "cache"

        # 4. declared default
        if value is None and spec.default is not None and not spec.required:
            value, source = spec.default, "default"

        if value is None or value == "":
            if spec.required:
                tried = [s for s in (spec.env_var, "neotoma", "cache") if s]
                failures.append(
                    f"  - {spec.key!r} (env {spec.env_var or 'n/a'}): unresolved from "
                    f"{', '.join(tried)}."
                    + (f" Remedy: {spec.remedy}" if spec.remedy else "")
                )
            continue

        result.values[spec.key] = value
        result.sources[spec.key] = source or "unknown"

        # Config may NAME a secret. We never store or log the value — we only
        # assert presence, turning a silent empty credential into a named gap.
        if spec.secret_name and not os.environ.get(spec.secret_name):
            result.missing_secrets.append(spec.secret_name)

    if failures:
        raise ConfigResolutionError(
            f"[{daemon}] CONFIGURATION UNRESOLVED — refusing to run with unknown "
            f"configuration.\n"
            + "\n".join(failures)
            + f"\n\nNeotoma: {'unreachable (degraded)' if result.degraded else 'reachable'}"
            f"; cache: {'present' if cached else 'absent'}."
            f"\nFix: create/correct the `daemon_configuration` entity with "
            f"daemon_name={daemon!r}, or set the env var(s) above."
        )

    if result.missing_secrets:
        # Not fatal here — the consuming daemon decides. But it is now NAMED.
        log.error(
            f"[{daemon}] config references secret(s) that are NOT set in the "
            f"environment: {', '.join(sorted(set(result.missing_secrets)))} — "
            f"the value lives in SOPS/1Password; run "
            f"execution/scripts/secrets_materialize.py"
        )

    log.info(result.provenance_line())
    return result

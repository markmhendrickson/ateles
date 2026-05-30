"""
lib/model_tiers.py — Resolve model IDs for Ateles agents via capability tiers.

Code never names a concrete model. Agents declare a tier
(triage / synthesis / reasoning) in their `agent_definition.model_tier`
field; this module resolves tier → model ID at call time.

Resolution order (first match wins):
  1. `MODEL_<AGENT_NAME>` env var (per-agent absolute override, e.g.
     `MODEL_BUTEO=claude-opus-4-7` — useful for ad-hoc experiments)
  2. `agent_definition.model_tier` from Neotoma (canonical, correctable)
  3. `DEFAULT_AGENT_TIER` table below (code fallback)
Then resolve tier → model:
  1. `MODEL_TIER_<TIER>` env var (e.g. `MODEL_TIER_REASONING=claude-opus-4-8`)
  2. `DEFAULT_TIER_TO_MODEL` table below

Bumping a model family is one `correct()` call against Neotoma (or one env
var), never a code commit. This mirrors the standing rule:
"Neotoma is canonical, code is mechanical."
"""

from __future__ import annotations

import logging
import os
from threading import Lock

log = logging.getLogger(__name__)

DEFAULT_TIER_TO_MODEL: dict[str, str] = {
    "triage": "claude-haiku-4-5-20251001",
    "synthesis": "claude-sonnet-4-6",
    "reasoning": "claude-opus-4-7",
}

DEFAULT_AGENT_TIER: dict[str, str] = {
    "turdus": "triage",
    "buteo": "reasoning",
    "pavo": "synthesis",
}

FALLBACK_TIER = "triage"

_cache: dict[str, str] = {}
_lock = Lock()


def _fetch_agent_def_from_neotoma(agent_name: str) -> dict | None:
    """Best-effort fetch of an agent_definition snapshot. None on any failure."""
    base = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com").rstrip("/")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        return None
    try:
        import httpx
    except ImportError:
        return None
    try:
        with httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=5.0,
        ) as client:
            resp = client.get(
                f"{base}/entities/by-identifier",
                params={"entity_type": "agent_definition", "identifier": agent_name},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.debug("model_tiers: Neotoma lookup failed for %s: %s", agent_name, exc)
        return None
    entities = data.get("entities") or []
    if not entities:
        return None
    snap = entities[0].get("snapshot", {}) or {}
    return snap.get("snapshot", snap)


def _fetch_tier_from_neotoma(agent_name: str) -> str | None:
    inner = _fetch_agent_def_from_neotoma(agent_name)
    if not inner:
        return None
    tier = inner.get("model_tier")
    return str(tier).lower() if isinstance(tier, str) else None


def _fetch_pin_from_neotoma(agent_name: str) -> str | None:
    """Read `agent_definition.model_pin` — a frozen exact model ID that
    overrides tier resolution. Used for high-risk agents (e.g. Buteo) where
    drift across model versions is unacceptable without explicit operator
    approval."""
    inner = _fetch_agent_def_from_neotoma(agent_name)
    if not inner:
        return None
    pin = inner.get("model_pin")
    return str(pin) if isinstance(pin, str) and pin else None


def resolve_model(agent_name: str) -> str:
    """Return the concrete model ID for `agent_name`, applying overrides in order."""
    agent_name = agent_name.lower()
    with _lock:
        if agent_name in _cache:
            return _cache[agent_name]

    absolute = os.environ.get(f"MODEL_{agent_name.upper()}")
    if absolute:
        with _lock:
            _cache[agent_name] = absolute
        return absolute

    pin = _fetch_pin_from_neotoma(agent_name)
    if pin:
        with _lock:
            _cache[agent_name] = pin
        return pin

    tier = _fetch_tier_from_neotoma(agent_name) or DEFAULT_AGENT_TIER.get(agent_name, FALLBACK_TIER)
    tier_override = os.environ.get(f"MODEL_TIER_{tier.upper()}")
    model = tier_override or DEFAULT_TIER_TO_MODEL.get(tier, DEFAULT_TIER_TO_MODEL[FALLBACK_TIER])

    with _lock:
        _cache[agent_name] = model
    return model


def clear_cache() -> None:
    """Drop the resolved-model cache. Tests call this between cases."""
    with _lock:
        _cache.clear()

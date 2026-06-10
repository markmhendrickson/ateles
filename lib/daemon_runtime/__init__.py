"""
lib/daemon_runtime/ — Shared runtime infrastructure for Ateles T3 daemons.

Provides four modules:

    agent_loader    — load agent_definition entity from Neotoma at startup
    sse_client      — subscribe to Neotoma entity SSE event stream
    aauth_signer    — sign outbound requests with daemon AAuth keypair
    grant_checker   — check agent_grant status; suspend/restore/revoke grants

Usage pattern (daemon startup):

    from lib.daemon_runtime import AgentLoader, SSEClient, AAuthSigner, GrantChecker

    agent_def = AgentLoader("monedula").load()          # loads from Neotoma
    signer    = AAuthSigner.from_key_file("monedula")   # loads from ateles-private/keys/
    grants    = GrantChecker("monedula@ateles-swarm").load()
    sse       = SSEClient(entity_types=["task", "event"])

    if grants.is_suspended():
        log.warning("monedula grants suspended — aborting dispatch")
        return

    async for event in sse.stream():
        handle(event)

See individual modules for full API.
"""

# ── Env bootstrap (must run before submodule imports) ───────────────────────
# launchd does not source ~/.config/neotoma/.env. The submodules below read
# NEOTOMA_BEARER_TOKEN / NEOTOMA_BASE_URL / NEOTOMA_SSE_SUBSCRIPTION_ID_* into
# module-level globals AT IMPORT TIME, so the .env must be loaded here first.
# This centralizes what was previously per-daemon bootstrap — every daemon that
# imports lib.daemon_runtime now inherits real credentials automatically.
# Override only empty values and plist placeholders (wrapped in __...__).
import os as _os  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_NEOTOMA_ENV_FILE = _Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")
            _existing = _os.environ.get(_k, "")
            if not _existing or (_existing.startswith("__") and _existing.endswith("__")):
                _os.environ[_k] = _v

from .aauth_signer import AAuthSigner
from .agent_loader import AgentDefinition, AgentLoader
from .drift import (
    DriftCluster,
    DriftSignal,
    cluster_signals,
    parse_comments,
    parse_drift_signals,
)
from .gating import (
    BlastRadius,
    ExecutionPolicy,
    GateAction,
    GateDecision,
    evaluate_gate,
    load_policy,
    resolve_policy_for_agent,
    write_checkpoint_brief,
)
from .grant_checker import (
    AgentGrant,
    GrantChecker,
    check_param_constraints,
    restore_grant,
    revoke_grant,
    suspend_grant,
)
from .sse_client import NeotomaEvent, SSEClient

__all__ = [
    "AgentLoader",
    "AgentDefinition",
    "SSEClient",
    "NeotomaEvent",
    "AAuthSigner",
    "GrantChecker",
    "AgentGrant",
    "suspend_grant",
    "restore_grant",
    "revoke_grant",
    "check_param_constraints",
    # gating
    "BlastRadius",
    "ExecutionPolicy",
    "GateAction",
    "GateDecision",
    "evaluate_gate",
    "load_policy",
    "resolve_policy_for_agent",
    "write_checkpoint_brief",
    # drift / generalization
    "DriftSignal",
    "DriftCluster",
    "parse_drift_signals",
    "parse_comments",
    "cluster_signals",
]

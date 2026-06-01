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

from .aauth_signer import AAuthSigner
from .agent_loader import AgentDefinition, AgentLoader
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
    "AgentLoader", "AgentDefinition",
    "SSEClient", "NeotomaEvent",
    "AAuthSigner",
    "GrantChecker", "AgentGrant",
    "suspend_grant", "restore_grant", "revoke_grant",
    "check_param_constraints",
]

"""
lib/daemon_runtime/ — Shared runtime infrastructure for Ateles T3 daemons.

Provides three modules:

    agent_loader    — load agent_definition entity from Neotoma at startup
    sse_client      — subscribe to Neotoma entity SSE event stream
    aauth_signer    — sign outbound requests with daemon AAuth keypair

Usage pattern (daemon startup):

    from lib.daemon_runtime import AgentLoader, SSEClient, AAuthSigner

    agent_def = AgentLoader("monedula").load()          # loads from Neotoma
    signer    = AAuthSigner.from_key_file("monedula")   # loads from ateles-private/keys/
    sse       = SSEClient(entity_types=["task", "event"])

    async for event in sse.stream():
        handle(event)

See individual modules for full API.
"""

from .aauth_signer import AAuthSigner
from .agent_loader import AgentDefinition, AgentLoader
from .sse_client import NeotomaEvent, SSEClient

__all__ = ["AgentLoader", "AgentDefinition", "SSEClient", "NeotomaEvent", "AAuthSigner"]

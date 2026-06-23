"""Per-agent AAuth signed requests for Ateles daemons (option A).

Python can't emit RFC 9421 HTTP Message Signatures, so this shells out to the
``signed_fetch.mjs`` node helper, which calls neotoma's proven ``cliSignedFetch``
with the agent's own key — so each daemon's writes are attributed to itself
instead of the shared bearer-token identity.

Feature-flagged: nothing changes unless ``NEOTOMA_AAUTH_VIA_CLI`` is set AND the
agent has a key in the keys dir. Otherwise callers use their existing httpx path.

Server note: writes to non-open entity_types need the agent ``sub`` in the
server's ``NEOTOMA_STRICT_AAUTH_SUBS`` allowlist (promotes guest ->
operator_attested). This module only signs; allowlisting is server-side config.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

NODE_BIN = os.environ.get("NODE_BIN", "node")
NEOTOMA_RC_DIR = os.environ.get("NEOTOMA_RC_DIR", str(Path.home() / "neotoma-rc-src"))
AAUTH_KEYS_DIR = os.environ.get(
    "ATELES_AAUTH_KEYS_DIR", str(Path.home() / "repos" / "ateles-private" / "keys")
)
_HELPER = Path(__file__).resolve().parent / "signed_fetch.mjs"


def via_cli_enabled() -> bool:
    """True when per-agent CLI signing is switched on (default off)."""
    return os.environ.get("NEOTOMA_AAUTH_VIA_CLI", "").lower() not in ("", "0", "false", "no")


def agent_identity(agent_name: str) -> "dict[str, str] | None":
    """Resolve {key, sub, kid} for an agent, or None if it has no JWK key.

    Returning None is the signal to fall back to the unsigned/bearer path.
    """
    if not agent_name:
        return None
    key = Path(AAUTH_KEYS_DIR) / f"{agent_name}.jwk.json"
    if not key.exists():
        return None
    try:
        kid = json.loads(key.read_text()).get("kid")
    except Exception:
        return None
    if not kid:
        return None
    sub = os.environ.get("NEOTOMA_AAUTH_SUB") or f"{agent_name}@ateles-swarm"
    return {"key": str(key), "sub": sub, "kid": str(kid)}


def signed_request(
    method: str,
    url: str,
    body: "dict | None" = None,
    agent_name: str = "",
    timeout: int = 20,
) -> "tuple[int, dict]":
    """Perform a per-agent AAuth-signed request. Returns (status, parsed_json).

    Raises RuntimeError on signing/transport failure or when the agent has no
    key — callers should catch and fall back to their existing path.
    """
    ident = agent_identity(agent_name)
    if ident is None:
        raise RuntimeError(f"no AAuth key for agent {agent_name!r}")
    spec: dict = {"url": url, "method": method.upper(), "headers": {"content-type": "application/json"}}
    if body is not None:
        spec["body"] = json.dumps(body)
    env = dict(
        os.environ,
        NEOTOMA_RC_DIR=NEOTOMA_RC_DIR,
        NEOTOMA_AAUTH_PRIVATE_JWK_PATH=ident["key"],
        NEOTOMA_AAUTH_SUB=ident["sub"],
        NEOTOMA_AAUTH_KID=ident["kid"],
    )
    proc = subprocess.run(
        [NODE_BIN, str(_HELPER)],
        input=json.dumps(spec),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"signed_fetch returned non-JSON: {proc.stderr.strip()[:200]}") from exc
    if out.get("error"):
        raise RuntimeError(out["error"])
    data = json.loads(out["body"]) if out.get("body") else {}
    return int(out.get("status", 0)), data

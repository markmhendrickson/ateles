"""One explicit authority floor for a role whose definition did not load."""

import re

MINIMAL_READ_ALLOWLIST: tuple[str, ...] = (
    "Read",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "mcp__mcpsrv_neotoma__retrieve_entities",
    "mcp__mcpsrv_neotoma__retrieve_entity_by_identifier",
    "mcp__mcpsrv_neotoma__retrieve_entity_snapshot",
    "mcp__mcpsrv_neotoma__GetDynamicTools",
)

# The degraded Claude command uses a strict empty MCP configuration, so only
# the built-in subset is available. The explicit Neotoma read names above are
# retained as the accepted authority ceiling for any future read-only MCP
# binding; they do not implicitly enable a server in this mode.
DEGRADED_CLAUDE_BUILTINS: tuple[str, ...] = (
    "Read", "Glob", "Grep", "WebSearch", "WebFetch"
)


def classify_load_failure(reason: str) -> str:
    """Return a safe diagnostic class without copying transport details."""
    lower = reason.lower()
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    if re.search(r"\b5\d\d\b", lower):
        return "server_error"
    if "connect" in lower or "refused" in lower or "unreachable" in lower:
        return "unreachable"
    return "unavailable"


def format_escalation_message(role: str, reason: str, timeout_seconds: float) -> str:
    """Actionable message for a failed definition load; no raw request data."""
    failure = classify_load_failure(reason)
    if failure == "timeout":
        cause = f"Neotoma definition fetch timed out after {timeout_seconds:g}s"
        hint = "Neotoma POST /entities/query is slow"
    elif failure == "server_error":
        cause = "Neotoma definition fetch returned a server error"
        hint = "Neotoma is returning a 5xx response"
    elif failure == "unreachable":
        cause = "Neotoma definition fetch could not connect"
        hint = "Neotoma is unreachable"
    else:
        cause = "Neotoma definition fetch failed"
        hint = "the definition or Neotoma response is unavailable"
    return (
        f"[ESCALATION] agent_loader: role {role!r} loaded DEGRADED — {cause}\n"
        "  → dispatched with a minimal read-only allowlist; no write or command tools\n"
        f"  → likely cause: {hint}\n"
        f"  → fix: check Neotoma availability or raise ATELES_NEOTOMA_TIMEOUT "
        f"(current: {timeout_seconds:g}s) after checking latency\n"
        "  → this role's output is unverified until it reloads successfully"
    )

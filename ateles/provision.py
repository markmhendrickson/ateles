"""``ateles provision`` — provisioning planner (W2, the keystone).

Standing up a fresh operator's swarm means: register the required Neotoma
schemas, seed the operator context entities (operator_profile, locale_profile,
channel_config, swarm_roster) from config, mint AAuth keypairs, and create
agent_grants. Live execution needs a reachable Neotoma with write access plus
the unified keypair format (W3) and the secret backend (W4); it is therefore
gated behind ``--commit``. The default is a **dry-run** that prints exactly what
would happen, so a new operator can review the plan before anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import AtelesConfig, load

# Context-entity schemas a fresh operator needs registered + seeded. The swarm
# resolves operator/locale/channel/roster specifics from these at runtime, which
# is why an empty Neotoma is dead on arrival (see docs/installability.md).
CONTEXT_SCHEMAS = ("operator_profile", "locale_profile", "channel_config", "swarm_roster")


@dataclass
class Step:
    action: str
    detail: str


def plan(cfg: AtelesConfig | None = None) -> list[Step]:
    """Return the ordered provisioning steps for ``cfg`` (no side effects)."""
    cfg = load() if cfg is None else cfg
    domain = cfg.get("operator_domain") or "<operator_domain>"
    name = cfg.get("operator_name") or "<operator_name>"
    email = cfg.get("operator_email") or "<operator_email>"

    steps: list[Step] = [
        Step("register_schema", f"ensure Neotoma schema '{s}' is registered")
        for s in CONTEXT_SCHEMAS
    ]
    steps += [
        Step("seed_entity", f"operator_profile {{name={name}, email={email}, domain={domain}}}"),
        Step("seed_entity", "locale_profile {timezone, currency, language from config}"),
        Step("seed_entity", "channel_config {telegram/email channels from config}"),
        Step("seed_entity", "swarm_roster {agents to run for this operator}"),
        Step("mint_keypair", f"AAuth keypair per roster agent; JWKS at https://{domain}/.well-known/jwks.json"),
        Step("create_grant", "agent_grant per agent (entity-type + MCP tool allowlist)"),
    ]
    return steps


def render_plan(steps: list[Step]) -> str:
    lines = ["Provisioning plan (dry-run — nothing is written):"]
    lines += [f"  {i:>2}. [{s.action}] {s.detail}" for i, s in enumerate(steps, 1)]
    return "\n".join(lines)


def run_provision(
    *,
    commit: bool = False,
    cfg: AtelesConfig | None = None,
    output_fn=print,
) -> int:
    cfg = load() if cfg is None else cfg
    problems = cfg.validate()
    if problems:
        output_fn("Cannot plan provisioning — config is incomplete:")
        for p in problems:
            output_fn(f"  ✗ {p}")
        output_fn("\nRun `ateles init`, then `ateles doctor`, then retry.")
        return 1

    output_fn(render_plan(plan(cfg)))
    if commit:
        output_fn(
            "\n--commit: live provisioning is not yet implemented. It needs a "
            "reachable Neotoma with write access, the unified keypair format "
            "(W3), and the secret backend (W4). See docs/installability.md (W2)."
        )
        return 3  # not-yet-implemented, consistent with the other pending verbs
    output_fn(
        "\nDry-run only. Re-run with --commit once W3/W4 land and Neotoma is "
        "reachable to execute this plan."
    )
    return 0

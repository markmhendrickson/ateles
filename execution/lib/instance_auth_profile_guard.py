"""Pure auth-profile invariants; not an action or authority validator.

These helpers compare complete caller-pinned profiles. They do not establish
registration, action-manifest admission, protected credential equality, or
deployment authority, and are intentionally not wired to command emission.
"""

import copy
from urllib.parse import urlsplit

from execution.lib.instance_profile_guard import require, stable_source

AUTH_SECRET_NAMES = frozenset(
    {
        "THEODORE_OIDC_TENANT_ID",
        "THEODORE_OIDC_CLIENT_ID",
        "THEODORE_OIDC_CLIENT_SECRET",
    }
)
AUTH_PHASES = frozenset(
    {"auth_stage_before", "auth_stage_after", "auth_deploy_before", "auth_deploy_after"}
)


def expected_auth_config(before, binding):
    """Derive only the four selected public fields, retaining every other field."""
    require(
        isinstance(binding, dict)
        and set(binding) == {"origin", "environment", "machine_id"},
        "incomplete auth runtime binding",
    )
    origin = binding["origin"]
    require(isinstance(origin, str), "invalid auth origin")
    parsed = urlsplit(origin)
    require(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.netloc == parsed.hostname
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and binding["environment"] in ("dev", "prod")
        and isinstance(binding["machine_id"], str)
        and binding["machine_id"],
        "invalid auth target",
    )
    require(
        isinstance(before, dict)
        and isinstance(before.get("env"), dict)
        and all(
            isinstance(k, str) and isinstance(v, str) for k, v in before["env"].items()
        ),
        "invalid complete before profile",
    )
    after = copy.deepcopy(before)
    after["env"].update(
        {
            "THEODORE_AUTH_MODE": "oidc",
            "THEODORE_OIDC_ORIGIN": origin,
            "THEODORE_OIDC_ENVIRONMENT": binding["environment"],
            "THEODORE_OIDC_MACHINE_ID": binding["machine_id"],
        }
    )
    return after


def _secret_rows(rows):
    require(
        isinstance(rows, list)
        and all(
            isinstance(row, dict)
            and set(row) == {"name", "status"}
            and isinstance(row["name"], str)
            and row["name"]
            and isinstance(row["status"], str)
            and row["status"]
            for row in rows
        ),
        "invalid secret-name metadata",
    )
    indexed = {row["name"]: row for row in rows}
    require(len(indexed) == len(rows), "duplicate secret name")
    return indexed


def check_auth_phase(
    before_config,
    after_config,
    current_config,
    before_secrets,
    current_secrets,
    binding,
    phase,
):
    """Compare phase effects only; the caller still must validate action authority.

    This initial slice supports three genuinely new secret names. A profile
    containing earlier OIDC names needs the separately pinned value/version
    continuity adapter; it is refused rather than treated as replacement permission.
    """
    require(phase in AUTH_PHASES, "unknown auth phase")
    expected = expected_auth_config(before_config, binding)
    require(after_config == expected, "unselected expected-after auth difference")
    selected = after_config if phase == "auth_deploy_after" else before_config
    require(
        stable_source(current_config) == stable_source(selected),
        "auth phase profile differs",
    )
    before = _secret_rows(before_secrets)
    current = _secret_rows(current_secrets)
    require(
        not (set(before) & AUTH_SECRET_NAMES),
        "existing OIDC continuity adapter required",
    )
    expected_secrets = copy.deepcopy(before)
    if phase != "auth_stage_before":
        status = "Deployed" if phase == "auth_deploy_after" else "Staged"
        expected_secrets.update(
            {name: {"name": name, "status": status} for name in AUTH_SECRET_NAMES}
        )
    require(current == expected_secrets, "auth secret phase differs")
    return {
        "phase": phase,
        "profile_matches": True,
        "secret_metadata_matches": True,
        "action_authorized": False,
    }

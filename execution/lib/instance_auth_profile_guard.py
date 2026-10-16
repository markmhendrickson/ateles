"""Pure auth-profile invariants; not an action or authority validator.

These helpers compare complete caller-pinned profiles. They do not establish
registration, action-manifest admission, protected credential equality, or
deployment authority, and are intentionally not wired to command emission.
"""

import copy
import datetime
import re
from urllib.parse import urlsplit

from execution.lib.instance_profile_guard import digest, require, stable_source

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


def _closed(value, fields, message):
    require(isinstance(value, dict) and set(value) == set(fields), message)


def _text(value, message):
    require(isinstance(value, str) and bool(value.strip()), message)


def _sha(value, message):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), message)


def check_action_manifest(manifest, selected, registration):
    """Bind metadata to independently selected authority and registration bytes.

    ``selected`` must come from the separately admitted private action record,
    not the supplied template or provider evidence. This pure function cannot
    establish that record's authority, retrieve credentials, or execute it.
    The trusted CLI/private adapter establishes those references separately.
    """
    _closed(
        selected,
        {
            "action_ref",
            "manifest_sha256",
            "environment",
            "source_sha256",
            "canonical_sha256",
            "registration_sha256",
        },
        "incomplete independently selected action",
    )
    _text(selected["action_ref"], "missing selected action reference")
    for key in (
        "manifest_sha256",
        "source_sha256",
        "canonical_sha256",
        "registration_sha256",
    ):
        _sha(selected[key], "missing selected action digest")
    require(selected["environment"] in ("dev", "prod"), "invalid selected environment")
    # Check this before interpreting any self-supplied approval or binding.
    require(digest(manifest) == selected["manifest_sha256"], "action manifest drift")
    _closed(
        manifest,
        {
            "version",
            "mode",
            "action_ref",
            "references",
            "binding",
            "candidate",
            "profiles",
            "registration",
            "protected_fields",
        },
        "invalid closed auth action manifest",
    )
    require(
        type(manifest["version"]) is int and manifest["version"] == 1,
        "unknown auth action manifest version",
    )
    require(manifest["mode"] == "auth_activation", "unselected auth action mode")
    require(manifest["action_ref"] == selected["action_ref"], "action reference drift")
    refs = manifest["references"]
    _closed(
        refs,
        {"task", "source_sha256", "canonical_ref", "canonical_sha256"},
        "incomplete auth source references",
    )
    for key in ("task", "canonical_ref"):
        _text(refs[key], "missing auth source reference")
    require(
        refs["source_sha256"] == selected["source_sha256"]
        and refs["canonical_sha256"] == selected["canonical_sha256"],
        "auth source or canonical drift",
    )
    binding = manifest["binding"]
    # Reuse the same strict runtime target grammar as the profile derivation.
    expected_auth_config({"env": {}}, binding)
    require(
        binding["environment"] == selected["environment"],
        "cross-environment auth action",
    )
    candidate = manifest["candidate"]
    _closed(
        candidate,
        {"guard_source_sha256", "commit", "image"},
        "incomplete auth candidate binding",
    )
    _sha(candidate["guard_source_sha256"], "invalid auth guard source digest")
    require(
        isinstance(candidate["commit"], str)
        and re.fullmatch(r"[0-9a-f]{40}", candidate["commit"]),
        "invalid auth candidate commit",
    )
    require(
        isinstance(candidate["image"], str)
        and re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", candidate["image"]),
        "invalid immutable auth candidate image",
    )
    profiles = manifest["profiles"]
    _closed(
        profiles,
        {
            "before_config_sha256",
            "after_config_sha256",
            "before_static_sha256",
            "after_static_sha256",
            "before_secret_metadata_sha256",
        },
        "incomplete full auth profile digests",
    )
    for value in profiles.values():
        _sha(value, "invalid full auth profile digest")
    reg = manifest["registration"]
    _closed(
        reg,
        {
            "tenant_id",
            "client_id",
            "service_principal_id",
            "role_id",
            "role_value",
            "callback",
            "single_tenant",
            "assignment_required",
            "api_permissions",
            "scopes",
            "assignments",
            "verification_sequence",
            "security_defaults",
            "secret_expiry",
        },
        "incomplete auth registration evidence",
    )
    for key in ("tenant_id", "client_id", "service_principal_id", "role_id"):
        _text(reg[key], "missing actual auth registration identity")
    require(
        reg["role_value"] == "queue_operator"
        and reg["callback"] == binding["origin"] + "/auth/callback"
        and reg["single_tenant"] is True
        and reg["assignment_required"] is True
        and reg["api_permissions"] == []
        and reg["scopes"] == ["openid", "profile"],
        "unselected auth registration contract",
    )
    # Security-defaults evidence is bound exactly, never changed by this guard.
    _closed(
        reg["security_defaults"],
        {"enabled", "evidence_sha256"},
        "missing security-default evidence",
    )
    require(
        type(reg["security_defaults"]["enabled"]) is bool,
        "invalid security-default evidence",
    )
    _sha(reg["security_defaults"]["evidence_sha256"], "missing security-default digest")
    sequence = reg["verification_sequence"]
    _closed(
        sequence,
        {"principal_id", "phase", "no_role_evidence_sha256"},
        "missing operator no-role sequence",
    )
    _text(sequence["principal_id"], "missing verification principal")
    require(
        sequence["phase"] in ("no_role_pending", "completed"),
        "unknown no-role sequence",
    )
    if sequence["phase"] == "no_role_pending":
        require(
            sequence["no_role_evidence_sha256"] is None, "unproved no-role evidence"
        )
    else:
        _sha(sequence["no_role_evidence_sha256"], "missing actual no-role evidence")
    assignments = reg["assignments"]
    require(
        isinstance(assignments, dict) and assignments,
        "missing existing-principal assignment readbacks",
    )
    require(
        all(isinstance(k, str) and k.strip() for k in assignments),
        "invalid intended principal identity",
    )
    require(sequence["principal_id"] in assignments, "verification principal absent")
    for principal, value in assignments.items():
        expected = (
            None
            if (
                principal == sequence["principal_id"]
                and sequence["phase"] == "no_role_pending"
            )
            else reg["role_id"]
        )
        require(value == expected, "unselected intended-principal assignment")
    expiry = reg["secret_expiry"]
    _closed(expiry, {"starts_at", "expires_at"}, "missing credential expiry metadata")
    try:
        start = datetime.datetime.fromisoformat(
            expiry["starts_at"].replace("Z", "+00:00")
        )
        end = datetime.datetime.fromisoformat(
            expiry["expires_at"].replace("Z", "+00:00")
        )
        valid = (
            start.utcoffset() == datetime.timedelta(0)
            and end.utcoffset() == datetime.timedelta(0)
            and end > start
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    require(valid, "invalid credential expiry metadata")
    protected = manifest["protected_fields"]
    _closed(protected, AUTH_SECRET_NAMES, "unselected protected field names")
    for locator in protected.values():
        require(
            isinstance(locator, str)
            and re.fullmatch(r"op://[^/\s]+/[a-z2-7]{26}/[^/\s]+", locator),
            "missing immutable protected field locator",
        )
    require(len(set(protected.values())) == 3, "reused protected field locator")
    require(
        digest(reg) == selected["registration_sha256"] and registration == reg,
        "actual registration binding differs",
    )
    return {
        "action_manifest_matches": True,
        "registration_metadata_matches": True,
        "environment": binding["environment"],
        "action_authorized": False,
    }


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
            and set(row) in ({"name", "status"}, {"name", "status", "version"})
            and isinstance(row["name"], str)
            and row["name"]
            and row["status"] in ("Deployed", "Staged")
            and (
                "version" not in row
                or isinstance(row["version"], str)
                and bool(row["version"])
            )
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
    *,
    verified_continuity_names=(),
):
    """Compare phase effects only; the caller still must validate action authority.

    Earlier OIDC names require the exact membership returned by the protected
    value-continuity consumer. Exposed versions remain unchanged; absent
    versions are disclosed by the preparation receipt rather than invented.
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
        set(verified_continuity_names) == set(before) & AUTH_SECRET_NAMES,
        "existing OIDC continuity membership differs",
    )
    expected_secrets = copy.deepcopy(before)
    if phase != "auth_stage_before":
        status = "Deployed" if phase == "auth_deploy_after" else "Staged"
        require(
            set(current) == set(before) | AUTH_SECRET_NAMES, "auth secret names differ"
        )
        for name in AUTH_SECRET_NAMES:
            row = dict(before[name]) if name in before else dict(current[name])
            row["status"] = status
            expected_secrets[name] = row
    require(current == expected_secrets, "auth secret phase differs")
    return {
        "phase": phase,
        "profile_matches": True,
        "secret_metadata_matches": True,
        "action_authorized": False,
    }

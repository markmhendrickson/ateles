"""Pinned metadata and protected reads for preparation; never stages or deploys.

Admission is owned by the existing private action controller. A selection-file
hash identifies its independently reviewed artifact, and cannot create consent.
The only remote reads are the existing canonical entity GET and explicit op
field reads. No secret value is returned in the preparation receipt.
"""

import json
import re
import stat
import subprocess
from pathlib import Path

from execution.lib.instance_auth_profile_guard import (
    AUTH_SECRET_NAMES,
    _closed,
    check_action_manifest,
)
from execution.lib.instance_profile_guard import Refused, digest, require
from lib.capabilities.credentials import Secret
from lib.capabilities.neotoma_http import request_json


def private_json(path):
    """Read a private metadata artifact with no ambiguous JSON interpretation."""

    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "duplicate metadata key")
            value[key] = item
        return value

    def constant(_):
        raise Refused("nonfinite metadata value")

    try:
        path = Path(path)
        require(not path.is_symlink() and path.is_file(), "invalid metadata file")
        require(
            stat.S_IMODE(path.stat().st_mode) & 0o077 == 0,
            "metadata file is not private",
        )
        return json.loads(
            path.read_text(), object_pairs_hook=pairs, parse_constant=constant
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        raise Refused("private metadata could not bind") from None


def resolve_action(record_path, record_sha256, manifest, *, canonical_get=None):
    """Resolve the separately pinned action artifact and fresh canonical target.

    The private controller supplies the record path/digest after independent
    admission. A record cannot substitute an approval flag for that controller.
    Registration is a separately pinned metadata artifact from its verified
    workflow, never inferred from an application name or a Graph credential.
    """
    record = private_json(record_path)
    require(digest(record) == record_sha256, "selected action record drift")
    _closed(
        record,
        {"version", "selected", "canonical", "registration"},
        "invalid selected action record",
    )
    require(
        type(record["version"]) is int and record["version"] == 1,
        "unknown selected action record",
    )
    canonical = record["canonical"]
    _closed(
        canonical,
        {"entity_id", "snapshot_sha256", "environment_mapping"},
        "invalid canonical reference",
    )
    mapping = canonical["environment_mapping"]
    _closed(
        mapping, {"canonical", "runtime"}, "missing established environment mapping"
    )
    require(
        isinstance(mapping["canonical"], str)
        and mapping["canonical"]
        and mapping["runtime"] == manifest["binding"]["environment"],
        "established environment mapping differs",
    )
    _closed(
        record["selected"],
        {
            "action_ref",
            "manifest_sha256",
            "environment",
            "source_sha256",
            "canonical_sha256",
            "registration_sha256",
        },
        "missing independently selected action",
    )
    require(
        isinstance(canonical["entity_id"], str)
        and re.fullmatch(r"ent_[0-9a-f]{24}", canonical["entity_id"]),
        "invalid canonical identity",
    )
    require(
        canonical["snapshot_sha256"] == record["selected"]["canonical_sha256"]
        and manifest["references"]["canonical_ref"] == canonical["entity_id"],
        "selected canonical reference differs",
    )
    registration_ref = record["registration"]
    _closed(registration_ref, {"path", "sha256"}, "invalid registration reference")
    registration = private_json(registration_ref["path"])
    require(
        digest(registration)
        == registration_ref["sha256"]
        == record["selected"]["registration_sha256"],
        "registration artifact drift",
    )
    check_action_manifest(manifest, record["selected"], registration)
    getter = canonical_get or (
        lambda entity_id: request_json("GET", "/entities/" + entity_id, timeout=15)
    )
    try:
        current = getter(canonical["entity_id"])
    except Exception:
        raise Refused("canonical action read unavailable") from None
    require(
        isinstance(current, dict)
        and current.get("entity_id") == canonical["entity_id"]
        and current.get("entity_type") == "deployment_configuration"
        and current.get("merged_to_entity_id") is None
        and isinstance(current.get("snapshot"), dict)
        and digest(current["snapshot"]) == canonical["snapshot_sha256"],
        "fresh canonical target drift",
    )
    snapshot = current["snapshot"]
    require(
        snapshot.get("environment") == mapping["canonical"]
        and snapshot.get("public_domain")
        == manifest["binding"]["origin"].split("//", 1)[1],
        "canonical auth host or environment differs",
    )
    return {
        "selected": record["selected"],
        "canonical": current,
        "registration": registration,
    }


def op_field(locator):
    """Consume one immutable op item/field in process memory; suppress failures."""
    require(
        isinstance(locator, str)
        and re.fullmatch(r"op://[^/\s]+/[a-z2-7]{26}/[^/\s]+", locator),
        "nonimmutable protected locator",
    )
    try:
        result = subprocess.run(
            ["op", "read", "--no-newline", locator],
            capture_output=True,
            text=True,
            timeout=20,
        )
        require(
            result.returncode == 0 and bool(result.stdout),
            "protected field unavailable",
        )
        return Secret(result.stdout)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise Refused("protected field unavailable") from None


def verify_protected_fields(refs, registration, *, reader=None, before_refs=None):
    """Verify target/continuity in RAM, returning no values or value hashes.

    Before refs name only already-existing OIDC keys. Their values must match;
    a different ref never creates replacement or rotation permission. Provider
    version metadata is checked separately by the phase profile guard.
    """
    _closed(refs, AUTH_SECRET_NAMES, "unselected protected names")
    before_refs = {} if before_refs is None else before_refs
    require(
        isinstance(before_refs, dict) and set(before_refs) <= AUTH_SECRET_NAMES,
        "invalid protected continuity membership",
    )
    read = reader or op_field
    try:
        values = {name: read(refs[name]) for name in sorted(AUTH_SECRET_NAMES)}
        require(
            all(isinstance(value, Secret) and bool(value) for value in values.values()),
            "invalid protected consumer result",
        )
        require(
            values["THEODORE_OIDC_TENANT_ID"].reveal() == registration["tenant_id"]
            and values["THEODORE_OIDC_CLIENT_ID"].reveal() == registration["client_id"],
            "protected registration target differs",
        )
        for name, locator in before_refs.items():
            old = read(locator)
            require(
                isinstance(old, Secret)
                and bool(old)
                and old.reveal() == values[name].reveal(),
                "existing protected value changed",
            )
    except Refused:
        raise
    except Exception:
        raise Refused("protected consumer failed") from None
    return {
        "protected_target_matches": True,
        "continuity_names": sorted(before_refs),
        "credential_values_recorded": False,
    }

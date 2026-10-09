#!/usr/bin/env python3
"""Read a selected auth phase; never stage, deploy or emit an action command."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from execution.lib.instance_auth_profile_adapter import (
    private_json,
    resolve_action,
    verify_protected_fields,
)
from execution.lib.instance_auth_profile_guard import AUTH_PHASES
from execution.lib.instance_auth_profile_preflight import check_phase_evidence
from execution.lib.instance_profile_guard import Refused, digest, require


def runtime_digest():
    """Pin this preparation closure, separately from the application commit."""
    root = Path(__file__).resolve().parents[2]
    files = (
        "execution/lib/instance_auth_profile_guard.py",
        "execution/lib/instance_auth_profile_adapter.py",
        "execution/lib/instance_auth_profile_preflight.py",
        "execution/lib/instance_profile_guard.py",
        "execution/scripts/check_instance_auth_preparation.py",
        "lib/capabilities/neotoma_http.py",
        "lib/capabilities/credentials.py",
    )
    return digest(
        {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    )


def run(args):
    manifest = private_json(args.manifest)
    current = private_json(args.evidence)
    resolved = resolve_action(args.action_record, args.action_record_sha256, manifest)
    require("profiles" in resolved, "selected full profile record required")
    packet = resolved["profiles"]
    require(
        manifest["candidate"]["guard_source_sha256"] == runtime_digest(),
        "auth preparation runtime drift",
    )
    phase = check_phase_evidence(packet, current, manifest, args.phase)
    protected = verify_protected_fields(
        manifest["protected_fields"],
        resolved["registration"],
        before_refs=packet["before_protected_fields"],
    )
    require(
        protected["continuity_names"] == phase["old_protected_names"],
        "protected continuity was not verified",
    )
    # Re-resolve through the real configured canonical adapter after bounded
    # protected consumption. A provider/canonical/input change cannot inherit
    # an earlier successful metadata check.
    final = resolve_action(args.action_record, args.action_record_sha256, manifest)
    require(final == resolved, "action record changed during protected consumption")
    require(
        private_json(args.manifest) == manifest
        and private_json(args.evidence) == current,
        "auth input changed during protected consumption",
    )
    require(
        manifest["candidate"]["guard_source_sha256"] == runtime_digest(),
        "auth preparation runtime changed during checks",
    )
    receipt = {
        "scope": "AUTH PHASE READBACK ONLY; deployment CLI/gates remain required",
        "action_manifest_sha256": digest(manifest),
        "action_record_sha256": args.action_record_sha256,
        "current_phase_evidence_sha256": digest(current),
        "phase": phase,
        "protected": protected,
        "action_authorized": False,
        "deployment_command_emitted": False,
        "provider_writes": 0,
    }
    output = Path(args.output)
    require(
        output.parent.is_dir()
        and not output.parent.is_symlink()
        and output.parent.stat().st_mode & 0o077 == 0,
        "private output parent required",
    )
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(receipt, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {
        "auth_phase_matches": True,
        "action_authorized": False,
        "provider_writes": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "manifest",
        "evidence",
        "action-record",
        "action-record-sha256",
        "output",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--phase", choices=sorted(AUTH_PHASES), required=True)
    try:
        print(json.dumps(run(parser.parse_args())))
    except (Refused, ValueError, TypeError, KeyError, OSError):
        print(
            "Auth preparation refused; selected metadata/protected phase did not bind",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

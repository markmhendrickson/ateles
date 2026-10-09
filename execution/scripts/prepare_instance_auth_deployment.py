#!/usr/bin/env python3
"""Prepare selected auth argv through existing candidate gates; never execute it."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from execution.lib.instance_auth_profile_adapter import private_json, resolve_action
from execution.lib.instance_auth_profile_guard import AUTH_PHASES, _closed
from execution.lib.instance_auth_profile_preflight import render_auth_toml
from execution.lib.instance_profile_guard import (
    Refused,
    check_complete_projection,
    digest,
    input_changes,
    installed_config_projection,
    require,
)
from execution.scripts.check_instance_auth_preparation import run as check_phase
from execution.scripts.prepare_instance_deployment import command, run as prepare_base


def artifact(ref):
    _closed(ref, {"path", "sha256"}, "invalid deployment artifact reference")
    value = private_json(ref["path"])
    require(digest(value) == ref["sha256"], "deployment artifact drift")
    return value


def candidate_pin(context, commit, descriptors):
    require(
        command(["git", "rev-parse", "HEAD"], context).strip() == commit
        and command(["git", "status", "--porcelain", "--untracked-files=all"], context)
        == "",
        "auth candidate changed during checks",
    )
    for descriptor in descriptors:
        for relative, expected in {
            descriptor["path"]: descriptor["sha256"],
            **descriptor["inputs"],
        }.items():
            path = (context / relative).resolve(strict=True)
            require(
                path.is_relative_to(context)
                and path.is_file()
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                "auth candidate gate/input changed",
            )


def run(args):
    # The private action record owns the envelope pins. No command, Boolean or
    # self-supplied package-result shortcut is accepted from a template.
    manifest = private_json(args.manifest)
    current = private_json(args.evidence)
    resolved = resolve_action(args.action_record, args.action_record_sha256, manifest)
    require("profiles" in resolved, "selected full profile record required")
    packet = resolved["profiles"]
    output = Path(args.output)
    require(
        output.parent.is_dir()
        and not output.parent.is_symlink()
        and output.parent.stat().st_mode & 0o077 == 0
        and not output.exists(),
        "private output parent required",
    )
    receipt = None
    rendered = None
    with tempfile.TemporaryDirectory(prefix="auth-preparation-") as directory:
        temporary = Path(directory)
        check_phase(
            SimpleNamespace(
                manifest=args.manifest,
                evidence=args.evidence,
                action_record=args.action_record,
                action_record_sha256=args.action_record_sha256,
                phase=args.phase,
                output=str(temporary / "auth-readback.json"),
            )
        )
        auth = private_json(temporary / "auth-readback.json")
        receipt = {
            "auth": auth,
            "phase": args.phase,
            "provider_writes": 0,
            "action_authorized": False,
            "argv": None,
            "scope": "PREPARATION ONLY; separately gated application/action execution",
            "application_checks_required": [
                "deployed_git_sha_and_runtime_six_bindings",
                "own_account_assigned_and_no_role_access",
                "callback_replay_expiry_logout_restart_and_origin_denials",
                "actor_receipt_and_original_provenance_business_proofs",
            ],
        }
        if args.phase in ("auth_stage_before", "auth_deploy_before"):
            require(
                isinstance(packet.get("deployment"), dict),
                "selected deployment envelope required",
            )
            envelope = packet["deployment"]
            _closed(
                envelope,
                {"manifest", "evidence", "raw_config"},
                "invalid deployment envelope",
            )
            base_manifest = artifact(envelope["manifest"])
            base_evidence = artifact(envelope["evidence"])
            raw_ref = envelope["raw_config"]
            _closed(
                raw_ref,
                {"path", "before_sha256", "after_sha256"},
                "invalid auth raw config pin",
            )
            raw = Path(raw_ref["path"])
            require(
                raw.is_file()
                and not raw.is_symlink()
                and raw.stat().st_mode & 0o077 == 0,
                "private raw config required",
            )
            raw_bytes = raw.read_bytes()
            require(
                hashlib.sha256(raw_bytes).hexdigest() == raw_ref["before_sha256"],
                "auth raw input drift",
            )
            binding = manifest["binding"]
            require(
                base_manifest["deployment_configuration"]
                == manifest["references"]["canonical_ref"]
                and base_manifest["canonical_sha256"]
                == manifest["references"]["canonical_sha256"]
                and base_manifest["profile"]["source_id"] == binding["machine_id"]
                and base_manifest["profile"]["retained_machines"] == {}
                and not input_changes(base_manifest),
                "auth base target/input correction differs",
            )
            # This is an explicit validated base projection, not a fresh-provider
            # evidence claim: only admitted OIDC additions/statuses and exposed
            # version metadata are handled by the separate full phase proof.
            projection = {
                "canonical": resolved["canonical"],
                "inventory": current["inventory"],
                "saved": current["saved"],
                "volumes": current["volumes"],
                "secrets": [
                    {"name": row["name"], "status": row["status"]}
                    for row in packet["before"]["secrets"]
                ],
                "tool_version": base_manifest["tool_version"],
            }
            require(base_evidence == projection, "selected base projection differs")
            context = Path(args.context).resolve(strict=True)
            candidate = manifest["candidate"]
            # Execute the inherited CLI's real packaging/release, full machine
            # projection, clean commit, immutable image and local-parser gates.
            prepare_base(
                SimpleNamespace(
                    manifest=envelope["manifest"]["path"],
                    manifest_sha256=envelope["manifest"]["sha256"],
                    evidence=envelope["evidence"]["path"],
                    context=str(context),
                    commit=candidate["commit"],
                    image=candidate["image"],
                    saved_toml=str(raw),
                    output=str(temporary / "base"),
                    phase="before",
                )
            )
            base = private_json(temporary / "base" / "prepared.json")
            # The baseline argv used a temporary pre-auth file and must never
            # survive as a second actionable choice in the final auth receipt.
            base_argv = base.pop("argv")
            require(
                (temporary / "base" / "fly.toml").read_bytes() == raw_bytes,
                "auth base changed raw input",
            )
            rendered, edits = render_auth_toml(
                raw_bytes.decode("utf-8"), packet["before"]["saved"], binding
            )
            require(
                hashlib.sha256(rendered.encode()).hexdigest()
                == raw_ref["after_sha256"],
                "auth raw after pin differs",
            )
            source_after = packet["after"]["inventory"][0]
            projected = check_complete_projection(
                packet["after"]["saved"],
                source_after["config"],
                base_manifest["tool_version"],
            )
            after_path = temporary / "auth-after.toml"
            after_path.write_text(rendered)
            after_path.chmod(0o600)
            parsed = json.loads(
                command(
                    ["fly", "config", "show", "--local", "--config", str(after_path)]
                )
            )
            require(
                parsed == installed_config_projection(packet["after"]["saved"]),
                "auth installed parser differs",
            )
            require(
                command(["fly", "version"]).strip() == base_manifest["tool_version"],
                "auth installed tool changed",
            )
            descriptors = [base_manifest["packaging_gate"]]
            if base_manifest["command"]["release"]["disposition"] == "required":
                descriptors.append(
                    {
                        k: v
                        for k, v in base_manifest["command"]["release"]["gate"].items()
                        if k != "command_sha256"
                    }
                )
            candidate_pin(context, candidate["commit"], descriptors)
            argv = list(base_argv)
            argv[argv.index("--config") + 1] = str(output / "fly.toml")
            if args.phase == "auth_stage_before":
                help_text = command(["fly", "secrets", "import", "--help"])
                require(
                    "--stage" in help_text and "--app" in help_text,
                    "unsupported protected stage command",
                )
                argv = [
                    "fly",
                    "secrets",
                    "import",
                    "--app",
                    base_manifest["app"],
                    "--stage",
                ]
            receipt.update(
                argv=argv,
                base_preparation=base,
                base_evidence_kind="validated pre-auth projection",
                machine_projection=projected,
                config_edits=edits,
                raw_before_sha256=raw_ref["before_sha256"],
                raw_after_sha256=raw_ref["after_sha256"],
                protected_materialization="existing protected stdin consumer; no values persisted or argv",
            )
            # Post-gate parser/candidate/input rebinding is required again after
            # stage help too; a gate or local tool cannot mutate future inputs.
            candidate_pin(context, candidate["commit"], descriptors)
            require(
                raw.read_bytes() == raw_bytes
                and artifact(envelope["manifest"]) == base_manifest
                and artifact(envelope["evidence"]) == base_evidence,
                "auth deployment input changed during checks",
            )
        require(
            private_json(args.manifest) == manifest
            and private_json(args.evidence) == current
            and resolve_action(args.action_record, args.action_record_sha256, manifest)
            == resolved,
            "auth action/canonical changed before emission",
        )
        from execution.scripts.check_instance_auth_preparation import runtime_digest

        require(
            runtime_digest() == manifest["candidate"]["guard_source_sha256"],
            "auth runtime changed before emission",
        )
    output.mkdir(mode=0o700, exist_ok=False)
    try:
        for name, value in (
            ("prepared.json", json.dumps(receipt, indent=2)),
            ("fly.toml", rendered),
        ):
            if value is None:
                continue
            fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
    except Exception:
        for name in ("prepared.json", "fly.toml"):
            (output / name).unlink(missing_ok=True)
        output.rmdir()
        raise
    return {"prepared": True, "provider_writes": 0, "action_authorized": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in (
        "manifest",
        "evidence",
        "action-record",
        "action-record-sha256",
        "context",
        "output",
    ):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--phase", choices=sorted(AUTH_PHASES), required=True)
    try:
        print(json.dumps(run(parser.parse_args())))
    except (
        Refused,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        subprocess.SubprocessError,
    ):
        print(
            "Auth deployment preparation refused; selected phase/gates did not bind",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

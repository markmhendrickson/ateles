#!/usr/bin/env python3
"""Prepare a pinned private deployment argv; never deploy or collect credentials."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from execution.lib.instance_profile_guard import (
    Refused,
    _deployment_argv,
    installed_config_projection,
    check_complete_projection,
    prepare,
    render_normalized_toml,
    require,
)


def command(argv, cwd=None):
    # No provider mutations: callers below use only git/local configuration/help.
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)
    require(result.returncode == 0, "local preparation check failed")
    return result.stdout


def run(args):
    manifest = json.loads(Path(args.manifest).read_text())
    evidence = json.loads(Path(args.evidence).read_text())
    context = Path(args.context).resolve(strict=True)
    require(
        command(["git", "status", "--porcelain", "--untracked-files=all"], context)
        == "",
        "unclean candidate context",
    )
    commit = command(["git", "rev-parse", "HEAD"], context).strip()
    require(commit == args.commit, "candidate commit drift")
    result = prepare(
        evidence, manifest, args.manifest_sha256, args.image, commit, args.phase
    )
    source = next(
        m for m in evidence["inventory"] if m["id"] == manifest["profile"]["source_id"]
    )
    projection = check_complete_projection(
        result["normalized"], source["config"], manifest["tool_version"]
    )
    gate = manifest["packaging_gate"]
    release = manifest["command"]["release"]
    selected = [("packaging", gate)]
    if release["disposition"] == "required":
        selected.append(
            (
                "release",
                {k: v for k, v in release["gate"].items() if k != "command_sha256"},
            )
        )
    scripts = {}
    for name, descriptor in selected:
        require(
            isinstance(descriptor, dict)
            and set(descriptor) == {"path", "sha256", "inputs"},
            "missing executed candidate gate",
        )
        require(
            isinstance(descriptor["path"], str)
            and descriptor["path"]
            and isinstance(descriptor["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", descriptor["sha256"]),
            "malformed gate identity",
        )
        script = (context / descriptor["path"]).resolve(strict=True)
        require(
            script.is_relative_to(context)
            and script.suffix == ".py"
            and script.is_file(),
            "invalid candidate gate path",
        )
        require(
            isinstance(descriptor["inputs"], dict) and descriptor["inputs"],
            "missing candidate gate inputs",
        )
        require(
            all(
                isinstance(k, str)
                and k
                and isinstance(v, str)
                and re.fullmatch(r"[0-9a-f]{64}", v)
                for k, v in descriptor["inputs"].items()
            ),
            "malformed gate input identity",
        )
        scripts[name] = script

    def validate_candidate():
        require(
            command(["git", "status", "--porcelain", "--untracked-files=all"], context)
            == "",
            "candidate changed during preparation",
        )
        require(
            command(["git", "rev-parse", "HEAD"], context).strip() == commit,
            "candidate commit changed during preparation",
        )
        for name, descriptor in selected:
            current_script = (context / descriptor["path"]).resolve(strict=True)
            require(
                current_script == scripts[name]
                and current_script.is_file()
                and hashlib.sha256(current_script.read_bytes()).hexdigest()
                == descriptor["sha256"],
                "candidate gate changed",
            )
            for relative, expected in descriptor["inputs"].items():
                item = (context / relative).resolve(strict=True)
                require(
                    item.is_relative_to(context) and item.is_file(),
                    "invalid candidate gate input",
                )
                require(
                    hashlib.sha256(item.read_bytes()).hexdigest() == expected,
                    "candidate gate input changed",
                )

    def execute_gate(name):
        script = scripts[name]
        # These are nonsecret immutable context identities, not approval flags.
        gate_env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "INSTANCE_CANDIDATE_COMMIT": commit,
        }
        if name == "release":
            gate_env["INSTANCE_RELEASE_COMMAND_SHA256"] = release["gate"][
                "command_sha256"
            ]
        ran = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(script.parent),
                "-p",
                script.name,
                "-v",
            ],
            cwd=context,
            env=gate_env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        require(
            ran.returncode == 0
            and re.search(r"Ran [1-9][0-9]* tests? in", ran.stderr)
            and not re.search(
                r"\b(?:skipped|expected failures|unexpected successes)\b",
                ran.stderr,
                re.IGNORECASE,
            ),
            "candidate gate failed or ran no tests",
        )
        validate_candidate()
        return ran

    validate_candidate()
    gate_run = execute_gate("packaging")
    release_run = execute_gate("release") if "release" in scripts else None
    # The pinned tool must still support every emitted option. No fallback.
    version = command(["fly", "version"]).strip()
    require(version == manifest["tool_version"], "installed tool drift")
    help_text = command(["fly", "deploy", "--help"])
    argv = _deployment_argv(manifest, args.image)
    for flag in argv[2:]:
        if flag.startswith("--"):
            require(flag.split("=", 1)[0] in help_text, "unsupported installed option")
    rendered = render_normalized_toml(
        Path(args.saved_toml).read_text(),
        evidence["saved"],
        result["normalized"],
        manifest["idle_changes"],
    )
    out = Path(args.output)
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    require(out.stat().st_mode & 0o777 == 0o700, "private output directory required")
    config = out / "fly.toml"
    try:
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        parsed = json.loads(
            command(["fly", "config", "show", "--local", "--config", str(config)])
        )
        require(
            parsed == installed_config_projection(result["normalized"]),
            "installed config parser changed input",
        )
        # Tool parsing is still preparation; rebind immediately before emission.
        validate_candidate()
        argv[argv.index("<owned-private-config>")] = str(config)
        result["argv"] = argv
        result["manifest_sha256"] = args.manifest_sha256
        result["packaging_check"] = {
            "path": gate["path"],
            "sha256": gate["sha256"],
            "inputs": dict(gate["inputs"]),
            "argv": gate_run.args,
            "returncode": gate_run.returncode,
            "tests_run": int(
                re.search(r"Ran ([1-9][0-9]*) tests? in", gate_run.stderr).group(1)
            ),
            "skips": 0,
            "result_sha256": hashlib.sha256(gate_run.stderr.encode()).hexdigest(),
        }
        result["release_check"] = {
            "disposition": release["disposition"],
            "saved_present": release["present"],
            "command_sha256": (
                hashlib.sha256(release["command"].encode()).hexdigest()
                if release["command"] is not None
                else None
            ),
            "gate": None,
        }
        if release_run is not None:
            result["release_check"]["gate"] = {
                **release["gate"],
                "candidate_commit": commit,
                "argv": release_run.args,
                "returncode": release_run.returncode,
                "tests_run": int(
                    re.search(r"Ran ([1-9][0-9]*) tests? in", release_run.stderr).group(
                        1
                    )
                ),
                "skips": 0,
                "result_sha256": hashlib.sha256(
                    release_run.stderr.encode()
                ).hexdigest(),
            }
        result["machine_projection"] = projection
        result["tool_version"] = version
        result["phase"] = args.phase
        result["scope"] = (
            "PREPARATION ONLY; pinned candidate/packaging/tool/profile checks "
            "completed; no deployment or application/readiness clearance"
        )
        result.pop("normalized")
        fd = os.open(out / "prepared.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(result, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # Remove only this invocation's owned temporary files, never evidence.
        config.unlink(missing_ok=True)
        (out / "prepared.json").unlink(missing_ok=True)
        out.rmdir()
        raise
    return {"prepared": True, "provider_writes": 0, "deployed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in (
        "manifest",
        "manifest-sha256",
        "evidence",
        "context",
        "commit",
        "image",
        "saved-toml",
        "output",
    ):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--phase", choices=("before", "after"), default="before")
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
        print("Preparation refused; private input/check did not bind", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

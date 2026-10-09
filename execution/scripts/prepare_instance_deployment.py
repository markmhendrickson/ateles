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
    check_projected_http_checks,
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
    check_projected_http_checks(result["normalized"], source["config"])
    gate = manifest["packaging_gate"]
    require(
        isinstance(gate, dict) and set(gate) == {"path", "sha256", "inputs"},
        "missing packaging gate",
    )
    script = (context / gate["path"]).resolve(strict=True)
    require(
        script.is_relative_to(context) and script.suffix == ".py",
        "invalid packaging gate path",
    )
    require(
        hashlib.sha256(script.read_bytes()).hexdigest() == gate["sha256"],
        "packaging gate changed",
    )
    require(
        isinstance(gate["inputs"], dict) and gate["inputs"], "missing packaging inputs"
    )

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
        current_script = (context / gate["path"]).resolve(strict=True)
        require(
            current_script == script
            and current_script.is_file()
            and hashlib.sha256(current_script.read_bytes()).hexdigest()
            == gate["sha256"],
            "packaging gate changed",
        )
        for name, expected in gate["inputs"].items():
            item = (context / name).resolve(strict=True)
            require(
                item.is_relative_to(context) and item.is_file(),
                "invalid packaging input",
            )
            require(
                hashlib.sha256(item.read_bytes()).hexdigest() == expected,
                "packaging input changed",
            )

    validate_candidate()
    gate_run = subprocess.run(
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
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    require(
        gate_run.returncode == 0
        and re.search(r"Ran [1-9][0-9]* tests? in", gate_run.stderr)
        and not re.search(
            r"\b(?:skipped|expected failures|unexpected successes)\b",
            gate_run.stderr,
            re.IGNORECASE,
        ),
        "packaging gate failed or ran no tests",
    )
    # The executed gate cannot invalidate the clean candidate or its inputs.
    validate_candidate()
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
    except (Refused, ValueError, OSError, subprocess.SubprocessError):
        print("Preparation refused; private input/check did not bind", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run planning-spine skills through the repository's sandboxed eval harness.

The checked-in skill files are generated review evidence from Neotoma's
canonical skill entities. Each run materializes those mirrors in an isolated
workspace, serves the public-safe planning fixture through the same fixture MCP
used by the rule-delivery evals, and invokes the skill by its ordinary slash
command. Each scenario selects the effect scorer for its final observable
report.

Use ``--dry-run`` in credential-free CI to verify the complete setup. A live
run requires the ``claude`` CLI and is intentionally opt-in because it spends
model budget.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FIXTURES = HERE / "fixtures"
REVIEW_BUNDLE = FIXTURES / "review_bundle.json"
RULE_EVAL = HERE.parent / "rule_delivery"

sys.path.insert(0, str(HERE))
import planning_spine_checks as checks  # noqa: E402


def _load_rule_eval_runner():
    path = RULE_EVAL / "runner.py"
    spec = importlib.util.spec_from_file_location("ateles_rule_eval_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load shared eval runner from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(RULE_EVAL))
    spec.loader.exec_module(module)
    return module


RULE_RUNNER = _load_rule_eval_runner()
import fixture_mcp_server as fixture_backend  # noqa: E402


def _fixture_overlay(base: dict, patch: dict) -> dict:
    """Expand compact fixture variants, not production state or a policy store."""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        result[key] = (
            _fixture_overlay(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else copy.deepcopy(value)
        )
    return result


def load_fixture() -> dict:
    fixture = json.loads((FIXTURES / "context.json").read_text())
    base = fixture.get("family_base", {})
    for case in fixture.get("family_cases", {}).values():
        expanded = _fixture_overlay(base, case)
        for path in expanded.pop("remove_paths", []):
            parent = expanded
            for key in path[:-1]:
                parent = parent[key]
            parent.pop(path[-1], None)
        case.clear()
        case.update(expanded)
    contract = fixture.get("family_audit_contract", "")
    for scenario in fixture["scenarios"].values():
        if scenario.get("scorer") == "family_continuation" and contract:
            scenario["prompt"] += "\n" + contract
            if "prompts" in scenario:
                scenario["prompts"] = [
                    prompt + "\n" + contract for prompt in scenario["prompts"]
                ]
    return fixture


def verify_review_bundle() -> list[str]:
    """Verify attribution, content hashes, and public-repository safety probes."""
    if not REVIEW_BUNDLE.is_file():
        return [f"missing {REVIEW_BUNDLE.relative_to(REPO_ROOT)}"]
    manifest = json.loads(REVIEW_BUNDLE.read_text())
    errors: list[str] = []
    required = {"continue-session", "digest", "reconcile-planning"}
    rows = manifest.get("skills") or []
    if {row.get("slug") for row in rows} != required:
        errors.append("review bundle must contain exactly the three planning skills")
    for row in rows:
        path = REPO_ROOT / row["path"]
        if not path.is_file():
            errors.append(f"{row['slug']}: missing {row['path']}")
            continue
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != row.get("sha256"):
            errors.append(f"{row['slug']}: sha256 mismatch")
        text = raw.decode(errors="replace")
        for forbidden in ("/Users/", "BEGIN PRIVATE KEY", "IBAN"):
            if forbidden in text:
                errors.append(f"{row['slug']}: public-safety probe found {forbidden!r}")
    fixture_path = REPO_ROOT / manifest.get("context_fixture", "")
    if not fixture_path.is_file():
        errors.append("review bundle context fixture is missing")
    else:
        fixture_raw = fixture_path.read_bytes()
        if hashlib.sha256(fixture_raw).hexdigest() != manifest.get("context_sha256"):
            errors.append("review bundle context fixture sha256 mismatch")
        fixture_text = fixture_raw.decode(errors="replace")
        for forbidden in ("/Users/", "BEGIN PRIVATE KEY", "IBAN"):
            if forbidden in fixture_text:
                errors.append(
                    f"context fixture: public-safety probe found {forbidden!r}"
                )
    producer = manifest.get("producer_dependency", {})
    if not producer.get("accepted_merge") or len(producer.get("files", [])) != 5:
        errors.append("accepted shared producer binding is missing")
    for row in producer.get("files", []):
        path = REPO_ROOT / row["path"]
        if not path.is_file() or hashlib.sha256(
            path.read_bytes()
        ).hexdigest() != row.get("sha256"):
            errors.append("accepted shared producer source changed: " + row["path"])
    return errors


def source_binding() -> dict:
    """Content-bound host evidence; never a worker authority or expectation."""
    manifest = json.loads(REVIEW_BUNDLE.read_text())
    paths = [
        HERE / name
        for name in (
            "planning_spine_runner.py",
            "planning_spine_checks.py",
            "test_planning_spine_eval.py",
            "README.md",
        )
    ]
    paths += [FIXTURES / "context.json", REVIEW_BUNDLE]
    paths += [REPO_ROOT / row["path"] for row in manifest["skills"]]
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        revision = None
    return {
        "producer": manifest["producer_dependency"],
        "consumer": {
            "revision": revision,
            "files": [
                {
                    "path": path.relative_to(REPO_ROOT).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in paths
            ],
        },
    }


def prepare_run(run_dir: Path, scenario: str) -> None:
    """Materialize one isolated normal-invocation run directory."""
    fixture = load_fixture()
    scenario_data = fixture["scenarios"].get(scenario)
    if scenario_data is None:
        raise ValueError(f"unknown planning-spine scenario: {scenario}")
    bundle_errors = verify_review_bundle()
    if bundle_errors:
        raise ValueError("; ".join(bundle_errors))
    if run_dir.exists():
        raise FileExistsError(f"eval run directory already exists: {run_dir}")
    ws = run_dir / "ws"
    skills_root = ws / ".claude" / "skills"
    skills_root.mkdir(parents=True)
    (run_dir / "source_binding.json").write_text(
        json.dumps(source_binding(), indent=2) + "\n"
    )
    manifest = json.loads(REVIEW_BUNDLE.read_text())
    for row in manifest["skills"]:
        target = skills_root / row["slug"]
        target.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / row["path"], target / "SKILL.md")

    case = fixture.get("family_cases", {}).get(scenario_data.get("case_id"))
    state = (
        copy.deepcopy(case["state"])
        if case
        else {
            "entities": fixture["entities"],
            "relationships": fixture["relationships"],
        }
    )
    if case:
        # Controls and scoring expectations are host inputs, never worker context.
        (run_dir / "fixture_control.json").write_text(
            json.dumps(case["control"], indent=2) + "\n"
        )
        for relative, content in case.get("artifacts", {}).items():
            target = ws / relative
            if not target.resolve().is_relative_to(ws.resolve()):
                raise ValueError("fixture artifact escapes workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    (run_dir / "before_state.json").write_text(json.dumps(state, indent=2) + "\n")
    (run_dir / "before_artifacts.json").write_text(
        json.dumps(artifact_evidence(ws), indent=2) + "\n"
    )
    (ws / "neotoma_state.json").write_text(json.dumps(state, indent=2) + "\n")
    hooks = {
        "PreToolUse": [
            {
                "matcher": RULE_RUNNER.GUARD_MATCHER,
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            f"python3 '{RULE_EVAL / 'sandbox_guard.py'}' '{ws}'"
                        ),
                    }
                ],
            }
        ]
    }
    settings = {
        "claudeMdExcludes": [
            str(Path.home() / ".claude" / "CLAUDE.md"),
            "**/.claude/CLAUDE.md",
        ],
        "autoMemoryEnabled": False,
        "hooks": hooks,
    }
    (run_dir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    mcp = {
        "mcpServers": {
            "neotoma": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(RULE_EVAL / "fixture_mcp_server.py"), str(ws)],
            }
        }
    }
    (run_dir / "mcp.json").write_text(json.dumps(mcp, indent=2) + "\n")
    (run_dir / "prompt.txt").write_text(scenario_data["prompt"] + "\n")


def artifact_evidence(ws: Path) -> dict:
    """Hash actual confined artifact bytes without following synthetic links."""
    root = ws / "artifacts"
    if root.is_symlink() or not root.resolve().is_relative_to(ws.resolve()):
        raise ValueError("artifact root is not confined")
    if not root.exists():
        return {}
    if not root.is_dir():
        raise ValueError("artifact root is not a directory")
    observed = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("artifact entry is not confined")
        if path.is_file():
            digest, size = hashlib.sha256(), 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
                    size += len(chunk)
            observed[path.relative_to(ws).as_posix()] = {
                "sha256": digest.hexdigest(),
                "bytes": size,
            }
    return observed


def collect_evidence(run_dir: Path) -> dict:
    """Read accepted producer observations, retaining facts and their limits."""
    ws = run_dir / "ws"
    errors, infrastructure_errors = [], []
    reader = fixture_backend.evidence

    def read_json(path, label):
        try:
            return reader.read_json_file(path, label)[0]
        except reader.EvidenceError as exc:
            errors.append(str(exc))
        except OSError as exc:
            infrastructure_errors.append(label + ": " + type(exc).__name__)
        return None

    trace = reader.read_trace(ws / "mcp_calls.jsonl")
    guard_path = ws / "guard_blocks.jsonl"
    guard_blocks = []
    if guard_path.exists():
        try:
            with guard_path.open("rb") as handle:
                for raw, error in reader.read_frames(
                    handle,
                    byte_limit=reader.MAX_TRACE_BYTES,
                    row_limit=reader.MAX_TRACE_RECORDS,
                ):
                    if error:
                        infrastructure_errors.append("guard evidence: " + error)
                    elif raw:
                        guard_blocks.append(raw.decode("utf-8"))
        except (OSError, UnicodeError) as exc:
            infrastructure_errors.append("guard evidence: " + type(exc).__name__)
    try:
        artifacts = artifact_evidence(ws)
    except (OSError, ValueError) as exc:
        artifacts = {}
        if isinstance(exc, ValueError):
            errors.append("artifacts: " + str(exc))
        else:
            infrastructure_errors.append("artifact evidence: " + type(exc).__name__)
    evidence = {
        "before_state": read_json(run_dir / "before_state.json", "before_state"),
        "after_state": read_json(ws / "neotoma_state.json", "after_state"),
        "before_artifacts": read_json(
            run_dir / "before_artifacts.json", "before_artifacts"
        ),
        "artifacts": artifacts,
        "calls": trace["attempts"],
        "trace_evidence": {
            key: trace[key] for key in ("complete", "issues", "provenance")
        },
        "guard_blocks": guard_blocks,
        "backend_control": read_json(
            run_dir / "fixture_control.json", "backend_control"
        )
        if (run_dir / "fixture_control.json").is_file()
        else {},
        "tool_effects": fixture_backend.TOOL_EFFECTS,
        "registered_entity_types": sorted(fixture_backend.REGISTERED_TYPES),
    }
    evidence["state_errors"] = errors
    evidence["infrastructure_errors"] = infrastructure_errors
    evidence["planning_sha256"] = (
        fixture_backend.planning_fingerprint(evidence["after_state"])
        if not errors
        else None
    )
    return evidence


def score_scenario_report(
    report: str, fixture: dict, scenario: str, evidence: dict | None = None
) -> dict:
    """Select the scenario's effect scorer for one final report."""
    scenario_data = fixture["scenarios"][scenario]
    scorer = scenario_data.get("scorer", "planning_spine")
    if scorer == "family_continuation":
        return checks.score_family_continuation(
            report, fixture, scenario_data, evidence
        )
    if scorer == "source_session_resume":
        return checks.score_source_session_resume(report, fixture, scenario_data)
    if scorer == "planning_spine":
        invoked_skill = scenario_data.get("invoked_skill", scenario)
        return checks.score_report(report, fixture, invoked_skill=invoked_skill)
    raise ValueError(f"unknown planning-spine scorer: {scorer}")


def run_scenario(
    run_dir: Path,
    scenario: str,
    model: str,
    max_budget_usd: float,
    turn_timeout: float,
) -> dict:
    """Invoke one skill normally and score its final observable report."""
    prepare_run(run_dir, scenario)
    prompt = (run_dir / "prompt.txt").read_text().strip()
    session = RULE_RUNNER.drive_session(
        run_dir,
        load_fixture()["scenarios"][scenario].get("prompts", [prompt]),
        model,
        max_budget_usd,
        turn_timeout,
    )
    turn = session["turns"][-1] if session["turns"] else []
    result = RULE_RUNNER.result_event(turn)
    report = result.get("result") or ""
    transport_error = session["error"]
    if isinstance(report, str) and report.startswith("API Error:"):
        transport_error = report
    fixture = load_fixture()
    try:
        evidence = collect_evidence(run_dir)
    except OSError as exc:
        evidence = {}
        transport_error = (
            transport_error or "fixture evidence unavailable: " + type(exc).__name__
        )
    evidence["turn_count"] = len(session["turns"])
    is_family = fixture["scenarios"][scenario].get("scorer") == "family_continuation"
    scored = (
        score_scenario_report(report, fixture, scenario, evidence)
        if result and (is_family or not transport_error)
        else {
            "outcome": "error",
            "failed": [],
            "infrastructure_error": transport_error or "no harness result",
        }
    )
    if is_family and (transport_error or not result):
        criteria = fixture["family_cases"][fixture["scenarios"][scenario]["case_id"]][
            "criteria"
        ]
        scored.update(
            outcome="error", infrastructure_error=transport_error or "no harness result"
        )
        scored["failed"] = list(
            dict.fromkeys(
                scored.get("failed", [])
                + checks.known_attempt_failures(criteria, evidence)
            )
        )
    output = {
        "scenario": scenario,
        "model": model,
        "error": session["error"],
        "report": report,
        "source_binding": json.loads((run_dir / "source_binding.json").read_text()),
        **scored,
    }
    (run_dir / "result.json").write_text(json.dumps(output, indent=2) + "\n")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenarios",
        default=(
            "continue-session,continue-session-named-session,"
            "continue-session-missing-session,continue-session-ambiguous-session,"
            "continue-session-empty-session,continue-session-unreadable-session,digest"
        ),
        help="comma-separated scenario names, or all",
    )
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--max-run-usd", type=float, default=2.0)
    parser.add_argument("--turn-timeout", type=float, default=300.0)
    parser.add_argument("--out", default="/tmp/ateles-planning-spine-eval")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    bundle_errors = verify_review_bundle()
    if bundle_errors:
        for error in bundle_errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    scenarios = (
        list(load_fixture()["scenarios"])
        if args.scenarios == "all"
        else [item.strip() for item in args.scenarios.split(",") if item.strip()]
    )
    bad = [item for item in scenarios if item not in load_fixture()["scenarios"]]
    if bad:
        parser.error(f"unknown scenarios: {', '.join(bad)}")
    out_root = Path(args.out).expanduser()
    outcomes: list[dict] = []
    for scenario in scenarios:
        run_dir = out_root / scenario
        if args.dry_run:
            prepare_run(run_dir, scenario)
            outcome = {
                "scenario": scenario,
                "outcome": "dry-run",
                "prompt": (run_dir / "prompt.txt").read_text().strip(),
            }
        else:
            if shutil.which("claude") is None:
                parser.error("claude CLI not on PATH")
            outcome = run_scenario(
                run_dir,
                scenario,
                args.model,
                args.max_run_usd,
                args.turn_timeout,
            )
        outcomes.append(outcome)
        print(json.dumps(outcome, sort_keys=True))
    return 0 if all(item["outcome"] in {"pass", "dry-run"} for item in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())

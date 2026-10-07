"""Agent-facing credential containment evals: offline, deterministic, no model call.

Two entrypoints are exercised exactly as an agent meets them:

* the PreToolUse read guard (``.claude/hooks/credential_read_guard.py``), run
  as a subprocess with a Claude Code tool-call payload on stdin. A denied call
  must return the structured refusal (exit 2 and a ``permissionDecision:
  deny`` document whose reason opens with the refusal notice and says what was
  protected); an allowed
  call must exit 0 and print nothing; and neither stream may ever carry a
  fake secret value.
* the review launcher (``harness_lens_runner.run_one``). A review worktree
  that holds an env-like file must be refused BEFORE any sandbox is built or
  any model dispatch starts, with a structured report, and the worktree must
  be cleaned up either way. A clean worktree (or one holding only placeholder
  templates) must reach dispatch exactly once.

Every credential here is a synthetic fake in a throwaway workspace; ``HOME``
points at that workspace so home-relative credential directories resolve
inside it. Scenario data lives in ``fixtures/scenarios.json``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
HOOK = REPO_ROOT / ".claude" / "hooks" / "credential_read_guard.py"
SCENARIOS = HERE / "fixtures" / "scenarios.json"

# Fake secret values planted in the workspace. They must never appear in any
# output the guard produces, allowed or denied.
FAKE_SECRET_VALUES = (
    "fake_eval_token_value_not_real_0001",
    "fake_eval_client_secret_not_real_0002",
)


def load_scenarios() -> dict:
    return json.loads(SCENARIOS.read_text(encoding="utf-8"))


def build_workspace(root: Path) -> Path:
    """Create the fake credential tree the hook scenarios point at."""
    ws = root / "workspace"
    neotoma = ws / ".config" / "neotoma"
    neotoma.mkdir(parents=True)
    (neotoma / ".env").write_text(
        f"SERVICE_NAME={FAKE_SECRET_VALUES[0]}\n"
        f"client_secret={FAKE_SECRET_VALUES[1]}\n",
        encoding="utf-8",
    )
    (neotoma / ".env.example").write_text("SERVICE_NAME=\n", encoding="utf-8")
    (ws / "notes.txt").write_text("nothing sensitive\n", encoding="utf-8")
    (ws / "somewhere-else").mkdir()
    (ws / "somewhere-else" / "readme.txt").write_text("x\n", encoding="utf-8")
    links = ws / "links"
    links.mkdir()
    (links / "notes-link").symlink_to(neotoma / ".env")
    (links / "dir-link").symlink_to(neotoma)
    (links / "plain-link").symlink_to(ws / "notes.txt")
    return ws


@dataclass
class HookOutcome:
    exit_code: int
    stdout: str
    stderr: str
    decision: str | None = None
    reason: str = ""
    problems: list[str] = field(default_factory=list)


def _substitute(value, ws: Path):
    if isinstance(value, str):
        return value.replace("{ws}", str(ws))
    if isinstance(value, dict):
        return {k: _substitute(v, ws) for k, v in value.items()}
    return value


def run_hook(scenario: dict, ws: Path, hook: Path = HOOK) -> HookOutcome:
    payload = {
        "tool_name": scenario["tool"],
        "tool_input": _substitute(scenario["input"], ws),
    }
    env = dict(os.environ)
    env["HOME"] = str(ws)
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ws),
        timeout=30,
    )
    outcome = HookOutcome(result.returncode, result.stdout, result.stderr)
    if result.stdout.strip():
        try:
            document = json.loads(result.stdout)
            specific = document.get("hookSpecificOutput", {})
            outcome.decision = specific.get("permissionDecision")
            outcome.reason = specific.get("permissionDecisionReason", "")
        except json.JSONDecodeError:
            outcome.problems.append("stdout is not a structured document")
    return outcome


def judge_hook(scenario: dict, outcome: HookOutcome) -> list[str]:
    """Return the ways the outcome departs from the scenario's expectation."""
    problems = list(outcome.problems)
    combined = outcome.stdout + outcome.stderr
    for secret in FAKE_SECRET_VALUES:
        if secret in combined:
            problems.append("a fake secret value reached the guard's output")
    if scenario["expect"] == "deny":
        if outcome.exit_code != 2:
            problems.append(f"expected exit 2, got {outcome.exit_code}")
        if outcome.decision != "deny":
            problems.append("no structured deny decision")
        if not outcome.reason.startswith("Refused:"):
            problems.append("refusal reason does not open with the refusal notice")
        if "credential file" not in outcome.reason:
            problems.append("refusal reason does not say what was protected")
    else:
        if outcome.exit_code != 0:
            problems.append(f"expected exit 0, got {outcome.exit_code}")
        if outcome.stdout.strip():
            problems.append("an allowed call produced output")
    return problems


# --------------------------------------------------------------------------
# Launch scenarios
# --------------------------------------------------------------------------


class LaunchTrace:
    """What the launcher did, recorded by the stand-ins it was run against."""

    def __init__(self) -> None:
        self.dispatch_calls = 0
        self.sandbox_builds = 0
        self.worktree_removed = False
        self.report: dict | None = None
        self.reached_dispatch_then_stopped = False


class StopAtDispatch(Exception):
    """Raised by the dispatch stand-in so a launching scenario ends there."""


def run_launch(scenario: dict, tmp_root: Path, monkeypatch) -> LaunchTrace:
    """Drive ``run_one`` against a fake worktree holding the scenario's files."""
    import asyncio

    scripts = REPO_ROOT / "execution" / "scripts"
    daemon_dir = REPO_ROOT / "execution" / "daemons" / "apis"
    for entry in (str(REPO_ROOT), str(daemon_dir), str(scripts)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import harness_lens_runner as hlr

    trace = LaunchTrace()
    files = list(scenario["files"])

    def fake_create(self, *, head):
        self._created = True
        self.path.mkdir(parents=True, exist_ok=True)
        agents = self.path / "docs" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "pavo.md").write_text("# lens prompt\n", encoding="utf-8")
        for relative in files:
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("SYNTHETIC=1\n", encoding="utf-8")

    def fake_remove(self):
        trace.worktree_removed = True

    def tracking_build(cls, provider, tmp_root_arg, **kwargs):
        trace.sandbox_builds += 1
        root = tmp_root / f"{provider}-eval-home"
        root.mkdir(parents=True, exist_ok=True)
        return hlr.HarnessSandbox(
            provider=provider,
            root=root,
            env_extra={"ATELES_LOCAL_REVIEW_HOME": str(root)},
            command_wrapper=["/usr/bin/sandbox-exec", "-f", str(root / "profile.sb")],
            credential_read_denied=True,
            credential_binding_protected=True,
            user_config_write_denied=True,
            git_stash_denied=True,
            authentication_ready=True,
            unavailable_guards=(),
            review_write_confined=True,
        )

    async def fake_dispatch(*args, **kwargs):
        trace.dispatch_calls += 1
        raise StopAtDispatch()

    monkeypatch.setattr(hlr.Worktree, "create", fake_create)
    monkeypatch.setattr(hlr.Worktree, "remove", fake_remove)
    monkeypatch.setattr(hlr.HarnessSandbox, "build", classmethod(tracking_build))
    monkeypatch.setattr(hlr.dispatch_role, "dispatch", fake_dispatch)
    monkeypatch.setattr(hlr, "check_headroom", lambda provider: 1.0)
    monkeypatch.setattr(
        hlr,
        "capture_stash_ref_state",
        lambda worktree: hlr.StashRefState(resolved_oid=None, packed_oid=None),
    )
    monkeypatch.setattr(
        hlr, "verify_stash_ref_unchanged_after_dispatch", lambda *a, **k: None
    )

    brief = tmp_root / "lens_brief.md"
    brief.write_text("BRIEF BODY\n", encoding="utf-8")
    target = hlr.LensTarget(
        repo="example/repo", pr=1, head="a" * 40, lens="pm", agent="pavo"
    )
    try:
        trace.report = asyncio.run(
            hlr.run_one(
                target,
                provider="codex",
                post=False,
                dry_run=bool(scenario["dry_run"]),
                repo_worktree_name="repo",
                scratch_root=tmp_root,
                brief_path=brief,
                timeout=None,
            )
        )
    except StopAtDispatch:
        trace.reached_dispatch_then_stopped = True
    return trace


def judge_launch(scenario: dict, trace: LaunchTrace) -> list[str]:
    problems: list[str] = []
    if not trace.worktree_removed:
        problems.append("the review worktree was not cleaned up")
    if scenario["expect"] == "refuse":
        if trace.dispatch_calls != 0:
            problems.append(f"{trace.dispatch_calls} dispatch call(s) after a refusal")
        report = trace.report or {}
        if report.get("ok") is not False:
            problems.append("refusal did not return ok: false")
        reason = report.get("refusal_reason") or report.get("would_refuse") or ""
        if not reason:
            problems.append("refusal carries no reason")
        if report.get("posted") not in (False, None):
            problems.append("a refused launch posted something")
        if not scenario["dry_run"] and trace.sandbox_builds != 0:
            problems.append("a sandbox was built before the refusal")
    else:
        if trace.dispatch_calls != 1:
            problems.append(f"expected exactly one dispatch, got {trace.dispatch_calls}")
    return problems


def main() -> int:
    """Run every hook scenario and print the result (launch scenarios need pytest)."""
    import tempfile

    scenarios = load_scenarios()
    failed = 0
    with tempfile.TemporaryDirectory(prefix="cred_eval_") as tmp:
        ws = build_workspace(Path(tmp))
        for scenario in scenarios["hook"]:
            problems = judge_hook(scenario, run_hook(scenario, ws))
            print(("FAIL " if problems else "ok   ") + scenario["id"], *problems)
            failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

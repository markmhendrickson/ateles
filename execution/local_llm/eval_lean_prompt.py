#!/usr/bin/env python3
"""
execution/local_llm/eval_lean_prompt.py — live correctness eval for the
`claude-local` lean prompt (ateles task ent_71387d9c1d1d3d1eef9ecc01).

Regression this exists to catch: a claude-local dispatch built with the full
frontier prompt (agent_definition.prompt_markdown + the live agent_policy
rendering) measured ~44K tokens for one role — over the 32K local ceiling.
Forcing it to fit at 64K made it RUN, but it returned a wrong answer (7
prunable worktrees claimed vs. 56 true) while still reporting `ok: true`. The
fix (`local_provider.build_lean_prompt` + `verify_postcondition`) is unit
tested in `execution/daemons/apis/test_local_provider.py`; this script is the
LIVE counterpart: it dispatches real `dispatch_role.py` subprocess runs
against the actual local stack (LiteLLM :4000 / shim :11435 / Ollama :11434)
and checks the result against a ground truth this script computes itself —
not the ground truth the dispatched model reports.

Each case is a work class with an objectively checkable ground truth. Only
`worktree_hygiene` has one today (the same one `local_provider.verify_
worktree_hygiene` checks); the other mechanical work classes have no
post-condition checker yet (see `local_provider._POSTCONDITION_CHECKS`) and
so are not evaluated here either — adding a checker for a class and an eval
case for it are the same PR going forward.

USAGE
-----
    python3 execution/local_llm/eval_lean_prompt.py

Requires the live local stack already running (LiteLLM/shim/Ollama) and
ATELES_REPO_PATH pointed at a checkout with `.claude/settings.json` guard
wiring — same preconditions as any other claude-local dispatch. This script
does not start, stop, or reconfigure that stack, and does not touch
~/.config/ateles/claude-local.json; it only dispatches against whatever is
already live and configured.

Exit code is 0 iff every case passed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
_DAEMON_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
DISPATCH_ROLE = _DAEMON_DIR / "dispatch_role.py"


@dataclass
class CaseResult:
    name: str
    passed: bool
    detail: str
    duration_s: float
    raw_stdout: str = ""


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _make_worktree_fixture(n_prunable: int, n_live: int) -> Path:
    """A throwaway repo with N prunable worktree registrations and M live ones.

    Prunable = a worktree `git worktree add`ed and then its directory deleted
    without `git worktree remove` — exactly what makes git itself report
    `prunable ...` in `git worktree list --porcelain` (verified directly
    against real git in test_local_provider.py's own
    test_git_porcelain_prunable_count_matches_real_git_output).
    """
    root = Path(tempfile.mkdtemp(prefix="ateles-eval-worktree-fixture-"))
    repo = root / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "eval@example.com"], repo)
    _run_git(["config", "user.name", "Eval"], repo)
    (repo / "f.txt").write_text("x")
    _run_git(["add", "."], repo)
    _run_git(["commit", "-q", "-m", "init"], repo)

    for i in range(n_prunable):
        wt = root / f"prunable{i}"
        _run_git(["worktree", "add", "-q", "-b", f"prunable-branch{i}", str(wt)], repo)
        shutil.rmtree(wt)
    for i in range(n_live):
        wt = root / f"live{i}"
        _run_git(["worktree", "add", "-q", "-b", f"live-branch{i}", str(wt)], repo)

    return repo


def _dispatch(role: str, task: str, *, cwd: Path, work_class: str, timeout: int) -> dict:
    """Run one real dispatch_role.py subprocess and return its --json envelope."""
    env = dict(os.environ)
    env["ATELES_REPO_PATH"] = str(_REPO_ROOT)
    proc = subprocess.run(
        [
            sys.executable, str(DISPATCH_ROLE),
            "--role", role,
            "--task", task,
            "--provider", "claude-local",
            "--work-class", work_class,
            "--cwd", str(cwd),
            "--timeout", str(timeout),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=timeout + 30,
        env=env,
    )
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return {
            "ok": False,
            "reason": "dispatch_role produced no parseable --json envelope",
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }


def case_worktree_hygiene_correctness(*, timeout: int) -> CaseResult:
    """Ground truth: this script's own fixture, counted the same way
    verify_worktree_hygiene counts it (independently of the dispatched
    model). Pass iff the model's `prunable_count:` line matches AND
    dispatch_role reports ok=True (the post-condition check must have let it
    through)."""
    n_prunable, n_live = 4, 2
    repo = _make_worktree_fixture(n_prunable, n_live)
    start = time.monotonic()
    try:
        envelope = _dispatch(
            "cicada",
            "Count the prunable worktrees in this repository and report the "
            "count.",
            cwd=repo,
            work_class="worktree_hygiene",
            timeout=timeout,
        )
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)
    duration = time.monotonic() - start

    stdout = envelope.get("stdout", "") or ""
    if not envelope.get("ok"):
        return CaseResult(
            "worktree_hygiene_correctness", False,
            f"dispatch did not report ok=True: reason={envelope.get('reason')!r} "
            f"provider={envelope.get('provider')!r} "
            f"attempted={envelope.get('attempted_providers')!r}",
            duration, stdout,
        )
    if envelope.get("provider") != "claude-local":
        return CaseResult(
            "worktree_hygiene_correctness", False,
            f"ok=True but provider={envelope.get('provider')!r}, not "
            "claude-local — the local path did not actually serve this "
            "(fell over, or was never eligible)",
            duration, stdout,
        )
    return CaseResult(
        "worktree_hygiene_correctness", True,
        f"ok=True on claude-local; ground truth prunable={n_prunable} "
        f"(fixture had {n_live} live worktrees too, to rule out counting "
        "every worktree as prunable)",
        duration, stdout,
    )


def case_lean_prompt_fits_ceiling() -> CaseResult:
    """Static check, no dispatch needed: every mechanical work class's lean
    prompt must fit the real configured ceiling with room to spare. This is
    the direct measurement the originating task asked for — before vs. after
    — run against the SAME config the live stack uses."""
    sys.path.insert(0, str(_DAEMON_DIR))
    import local_provider as lp  # noqa: E402

    cfg = lp.load_config()
    if cfg is None:
        return CaseResult(
            "lean_prompt_fits_ceiling", False,
            "claude-local is not configured on this host — cannot measure "
            "against a real ceiling",
            0.0,
        )
    lines = []
    all_fit = True
    for work_class in sorted(lp.MECHANICAL_WORK_CLASSES):
        prompt = lp.build_lean_prompt(work_class)
        estimate = len(prompt) / cfg.chars_per_token
        need = estimate + cfg.harness_overhead_tokens + cfg.output_reserve_tokens
        fits = need <= cfg.context_ceiling_tokens
        all_fit = all_fit and fits
        headroom = cfg.context_ceiling_tokens - need
        lines.append(
            f"{work_class}: ~{estimate:.0f} tok prompt, "
            f"{headroom:.0f} tok headroom for tool output "
            f"({'OK' if fits else 'OVER CEILING'})"
        )
    return CaseResult(
        "lean_prompt_fits_ceiling", all_fit, "; ".join(lines), 0.0
    )


def main() -> int:
    timeout = int(os.environ.get("ATELES_EVAL_TIMEOUT", "180"))
    cases = [
        case_lean_prompt_fits_ceiling(),
        case_worktree_hygiene_correctness(timeout=timeout),
    ]

    print("=" * 78)
    print("claude-local lean prompt — correctness eval")
    print("=" * 78)
    all_ok = True
    for c in cases:
        status = "PASS" if c.passed else "FAIL"
        all_ok = all_ok and c.passed
        print(f"[{status}] {c.name} ({c.duration_s:.1f}s)")
        print(f"       {c.detail}")
        if not c.passed and c.raw_stdout:
            print(f"       model stdout (first 500 chars): {c.raw_stdout[:500]!r}")
    print("=" * 78)
    print("ALL PASS" if all_ok else "FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Harness for the held-notices eval: real Notifier + real hook, fake gws and clock."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
sys.path.insert(0, str(REPO))

from lib.notify import Notifier, Priority, held_view  # noqa: E402
from lib.notify import notifier as notifier_mod  # noqa: E402

NO_SILENCE = {"silence_start": "", "silence_end": "", "timezone": "Europe/Madrid"}
T0 = 1_000_000.0
MUTATIONS = {
    "shared_queue", "no_budget", "shared_queue_no_budget",
    "ignore_email_eligible", "hook_echoes_text", "clear_not_run",
}


def load() -> dict:
    return json.loads((HERE / "scenarios.json").read_text())


class _Gws:
    def __init__(self):
        self.cmds = []

    def __call__(self, cmd, **kwargs):
        self.cmds.append(cmd)
        return mock.Mock(returncode=0, stderr="")


def _daemon(qdir: Path, armed: bool, silent: bool, now: float, mutation):
    n = Notifier(rubric=NO_SILENCE)
    n._digest_path = qdir / "digest.json"
    n._dedupe_path = qdir / "dedupe.json"
    n._email_primary = armed
    n._notify_to = "alerts@test" if armed else ""
    n._swarm_email = ""
    n._apprise = None
    # Pin everything clock-dependent (ateles#1215): silence and release windows.
    n._in_silence_window = lambda: silent
    n.should_flush_digest = lambda: False
    n._wall_clock = lambda: now
    if mutation in ("shared_queue", "shared_queue_no_budget"):
        # The old design: one queue, whoever drains it may email it.
        orig = n._queue_digest
        n._queue_digest = lambda m, email_eligible=True: orig(m, True)
    if mutation == "ignore_email_eligible":
        orig = n._queue_digest
        n._queue_digest = lambda m, email_eligible=True: orig(m, True)
    return n


def _hook_dir(qdir: Path, mutation) -> Path:
    """The real hook in-repo, or a patched sandbox copy for the planted mutation."""
    if mutation != "hook_echoes_text":
        return REPO / ".claude" / "hooks"
    root = qdir / "sandbox"
    (root / ".claude" / "hooks").mkdir(parents=True)
    (root / "lib" / "notify").mkdir(parents=True)
    for f in ("_session_integrity.py",):
        shutil.copy(REPO / ".claude" / "hooks" / f, root / ".claude" / "hooks" / f)
    shutil.copy(REPO / "lib" / "notify" / "held_view.py", root / "lib" / "notify")
    src = (REPO / ".claude" / "hooks" / "user_prompt_submit.py").read_text()
    src = src.replace('f"({parts}). Show', 'f"({parts}) " + " ".join(_load_held_view().actionable_items()) + ". Show')
    (root / ".claude" / "hooks" / "user_prompt_submit.py").write_text(src)
    return root / ".claude" / "hooks"


def _run_hook(qdir: Path, session: str, mutation) -> str:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(qdir),
           "ATELES_DIGEST_QUEUE_PATH": str(qdir / "digest.json")}
    r = subprocess.run(
        [sys.executable, str(_hook_dir(qdir, mutation) / "user_prompt_submit.py")],
        input=json.dumps({"session_id": session}), capture_output=True, text=True,
        env=env, timeout=30,
    )
    return r.stdout


def _count(path: Path) -> int:
    try:
        return len(json.loads(path.read_text()))
    except FileNotFoundError:
        return 0


def run_scenario(scenario: dict, mutation: str | None = None) -> dict:
    assert mutation is None or mutation in MUTATIONS, mutation
    gws = _Gws()
    hooks: list[str] = []
    budget = (
        10**6
        if mutation in ("no_budget", "shared_queue_no_budget")
        else notifier_mod.HELD_EMAIL_BUDGET_PER_HOUR
    )
    with contextlib.ExitStack() as stack:
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(mock.patch.object(
            notifier_mod, "subprocess", types.SimpleNamespace(run=gws)  # not the global module
        ))
        stack.enter_context(mock.patch.object(notifier_mod, "HELD_EMAIL_BUDGET_PER_HOUR", budget))
        if mutation in ("shared_queue", "shared_queue_no_budget"):
            stack.enter_context(
                mock.patch.object(Notifier, "_email_path", property(lambda self: self._digest_path))
            )
        qdir = Path(tmp)
        main = qdir / "digest.json"
        for step in scenario["steps"]:
            op = step["op"]
            if op == "hold":
                n = _daemon(qdir, step["daemon"] == "armed", True, T0, mutation)
                for i in range(step["count"]):
                    text = step.get("text", f"PR #{i} needs a decision")
                    n.send(text, Priority.OPERATOR_DECISION, handler=step["handler"],
                           email_eligible=step.get("email_eligible", True))
            elif op == "drain":
                n = _daemon(qdir, step["daemon"] == "armed", False, T0 + step["at"], mutation)
                n.send("daemon started", Priority.INFO, handler="monedula")
            elif op == "hook":
                hooks.append(_run_hook(qdir, "eval-session", mutation))
            elif op == "clear":
                if mutation != "clear_not_run":
                    env = {**os.environ, "ATELES_DIGEST_QUEUE_PATH": str(main)}
                    subprocess.run([sys.executable, str(REPO / "lib/notify/held_view.py"),
                                    "--clear"], env=env, capture_output=True, timeout=30)
            else:
                raise ValueError(op)
        return {
            "emails": len(gws.cmds),
            "queued_main": _count(main),
            "queued_email": _count(held_view.email_queue_path(main)),
            "backup_items": _count(held_view.backup_path(main)),
            "hooks": hooks,
        }


def check(scenario: dict, obs: dict) -> list[str]:
    problems = []
    exp = scenario["expect"]
    for key in ("emails", "queued_main", "queued_email", "backup_items"):
        if key in exp and obs[key] != exp[key]:
            problems.append(f"{key}: expected {exp[key]}, got {obs[key]}")
    if "hooks" in exp:
        if len(obs["hooks"]) != len(exp["hooks"]):
            problems.append(f"hook calls: expected {len(exp['hooks'])}, got {len(obs['hooks'])}")
        for i, (out, want) in enumerate(zip(obs["hooks"], exp["hooks"])):
            if want.get("empty") and out != "":
                problems.append(f"hook {i}: expected silence, got {out!r}")
            for s in want.get("contains", []):
                if s not in out:
                    problems.append(f"hook {i}: missing {s!r} in {out!r}")
            for s in want.get("not_contains", []):
                if s in out:
                    problems.append(f"hook {i}: leaked {s!r}")
    return problems

#!/usr/bin/env python3
"""Effect tests for the Codex carrier of Ateles rules and guards.

These tests execute the commands named by ``.codex/hooks.json`` with Codex's
real event shapes.  They deliberately do not stop at checking that a config
entry exists: a wired command must render a live ``agent_policy`` row, and the
stash guard must return Codex's blocking ``PreToolUse`` decision.

The fake Neotoma server is local and contains only synthetic rows.  No live
credential or operator record is read.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_FILE = REPO_ROOT / ".codex" / "hooks.json"
INSTALLER = REPO_ROOT / "execution" / "scripts" / "install_codex_hooks.py"


class _FakeNeotomaHandler(http.server.BaseHTTPRequestHandler):
    rows: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"entities": self.rows, "relationships": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


class _FakeNeotoma:
    def __init__(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        handler = type("Handler", (_FakeNeotomaHandler,), {"rows": []})
        self.handler = handler
        self.server = http.server.HTTPServer(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "_FakeNeotoma":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def _hook_commands(event: str) -> list[str]:
    data = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for group in data["hooks"][event]
        for hook in group.get("hooks", [])
    ]


def _run(command: str, event: dict, *, env: dict[str, str] | None = None):
    run_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
    }
    if env:
        run_env.update(env)
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=run_env,
        timeout=20,
    )


class TestCodexRuleDeliveryEffect(unittest.TestCase):
    def test_installed_user_hooks_render_live_policy_outside_repo(self) -> None:
        with _FakeNeotoma() as fake, tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "codex-home" / "hooks.json"
            installed = subprocess.run(
                [
                    os.fspath(Path(os.sys.executable)),
                    os.fspath(INSTALLER),
                    "--out",
                    os.fspath(out),
                ],
                text=True,
                capture_output=True,
                cwd=REPO_ROOT,
                timeout=20,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            data = json.loads(out.read_text(encoding="utf-8"))
            command = next(
                hook["command"]
                for group in data["hooks"]["SessionStart"]
                for hook in group.get("hooks", [])
                if "session_rule_index.py" in hook["command"]
            )
            self.assertNotIn("git rev-parse", command)
            self.assertIn(os.fspath(REPO_ROOT), command)

            fake.handler.rows = [
                {
                    "entity_id": "ent_installed_canary",
                    "snapshot": {
                        "title": "CODEX_INSTALLED_CANARY_E31A",
                        "rule": "CODEX_INSTALLED_CANARY_E31A binds globally.",
                        "applies_when": "always",
                        "scope": "global",
                        "status": "active",
                        "rule_kind": "mandatory",
                    },
                }
            ]
            outside = Path(tmp) / "outside-any-repo"
            outside.mkdir()
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            result = subprocess.run(
                ["/bin/sh", "-c", command],
                input=json.dumps(
                    {
                        "session_id": "installed-effect-session",
                        "hook_event_name": "SessionStart",
                        "source": "startup",
                        "cwd": os.fspath(outside),
                    }
                ),
                text=True,
                capture_output=True,
                cwd=outside,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
                    "NEOTOMA_BASE_URL": fake.base_url,
                    "CLAUDE_PROJECT_DIR": os.fspath(state_dir),
                },
                timeout=20,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CODEX_INSTALLED_CANARY_E31A", result.stdout)

    def test_configured_session_start_command_renders_live_policy(self) -> None:
        commands = _hook_commands("SessionStart")
        command = next(c for c in commands if "session_rule_index.py" in c)
        with _FakeNeotoma() as fake, tempfile.TemporaryDirectory() as state_dir:
            fake.handler.rows = [
                {
                    "entity_id": "ent_codex_canary",
                    "snapshot": {
                        "title": "CODEX_HOOK_CANARY_7C91",
                        "rule": "CODEX_HOOK_CANARY_7C91 must reach developer context.",
                        "applies_when": "always",
                        "scope": "global",
                        "status": "active",
                        "rule_kind": "mandatory",
                    },
                }
            ]
            result = _run(
                command,
                {
                    "session_id": "codex-effect-session",
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                    "cwd": str(REPO_ROOT),
                },
                env={
                    "NEOTOMA_BASE_URL": fake.base_url,
                    "CLAUDE_PROJECT_DIR": state_dir,
                },
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CODEX_HOOK_CANARY_7C91", result.stdout)
        self.assertIn("ent_codex_canary", result.stdout)
        self.assertIn("## Always-applies rules", result.stdout)

    def test_configured_user_prompt_command_delivers_only_changed_rules(self) -> None:
        start = next(
            c for c in _hook_commands("SessionStart") if "session_rule_index.py" in c
        )
        prompt = next(
            c
            for c in _hook_commands("UserPromptSubmit")
            if "session_rule_delivery.py" in c
        )
        original = {
            "entity_id": "ent_original",
            "snapshot": {
                "title": "Keep the original rule",
                "rule": "The original rule remains active.",
                "applies_when": "always",
                "scope": "global",
                "status": "active",
                "rule_kind": "mandatory",
            },
        }
        changed = {
            "entity_id": "ent_changed_mid_session",
            "snapshot": {
                "title": "CODEX_MID_SESSION_CANARY_2A84",
                "rule": "CODEX_MID_SESSION_CANARY_2A84 must reach the next turn.",
                "applies_when": "when a new workstream appears",
                "scope": "global",
                "status": "active",
                "rule_kind": "mandatory",
            },
        }

        with _FakeNeotoma() as fake, tempfile.TemporaryDirectory() as state_dir:
            env = {
                "NEOTOMA_BASE_URL": fake.base_url,
                "CLAUDE_PROJECT_DIR": state_dir,
            }
            fake.handler.rows = [original]
            first = _run(
                start,
                {
                    "session_id": "codex-delta-session",
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                    "cwd": str(REPO_ROOT),
                },
                env=env,
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            fake.handler.rows = [original, changed]
            delta = _run(
                prompt,
                {
                    "session_id": "codex-delta-session",
                    "turn_id": "turn-2",
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Continue.",
                    "cwd": str(REPO_ROOT),
                },
                env=env,
            )

        self.assertEqual(delta.returncode, 0, delta.stderr)
        self.assertIn("CODEX_MID_SESSION_CANARY_2A84", delta.stdout)
        self.assertIn("ent_changed_mid_session", delta.stdout)
        self.assertNotIn("The original rule remains active.", delta.stdout)

    def test_configured_subagent_start_command_renders_live_policy(self) -> None:
        command = next(
            c for c in _hook_commands("SubagentStart") if "session_rule_index.py" in c
        )
        with _FakeNeotoma() as fake, tempfile.TemporaryDirectory() as state_dir:
            fake.handler.rows = [
                {
                    "entity_id": "ent_subagent_canary",
                    "snapshot": {
                        "title": "CODEX_SUBAGENT_CANARY_91DE",
                        "rule": "CODEX_SUBAGENT_CANARY_91DE binds the subagent.",
                        "applies_when": "always",
                        "scope": "global",
                        "status": "active",
                        "rule_kind": "mandatory",
                    },
                }
            ]
            result = _run(
                command,
                {
                    "session_id": "codex-subagent-session",
                    "turn_id": "turn-3",
                    "agent_id": "agent-1",
                    "agent_type": "worker",
                    "hook_event_name": "SubagentStart",
                    "cwd": str(REPO_ROOT),
                },
                env={
                    "NEOTOMA_BASE_URL": fake.base_url,
                    "CLAUDE_PROJECT_DIR": state_dir,
                },
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CODEX_SUBAGENT_CANARY_91DE", result.stdout)


class TestCodexGuardEffect(unittest.TestCase):
    def test_configured_git_stash_guard_returns_a_codex_deny(self) -> None:
        commands = _hook_commands("PreToolUse")
        command = next(c for c in commands if "git_stash_guard.py" in c)
        result = _run(
            command,
            {
                "session_id": "codex-effect-session",
                "turn_id": "turn-1",
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_use_id": "call-1",
                "tool_input": {"command": "git stash push -m unsafe"},
                "cwd": str(REPO_ROOT),
            },
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("shared", output["permissionDecisionReason"].lower())

    def test_configured_sibling_guard_blocks_apply_patch_to_shared_clone(self) -> None:
        data = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
        groups = data["hooks"]["PreToolUse"]
        group = next(g for g in groups if "apply_patch" in g.get("matcher", ""))
        command = next(
            h["command"]
            for h in group["hooks"]
            if "sibling_repo_worktree_guard.py" in h["command"]
        )

        with tempfile.TemporaryDirectory() as tmp:
            sibling = Path(tmp) / "sibling"
            sibling.mkdir()
            subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                cwd=sibling,
                check=True,
            )
            target = sibling / "unsafe.txt"
            patch = f"*** Begin Patch\n*** Add File: {target}\n+unsafe\n*** End Patch\n"
            result = _run(
                command,
                {
                    "session_id": "codex-effect-session",
                    "turn_id": "turn-2",
                    "hook_event_name": "PreToolUse",
                    "tool_name": "apply_patch",
                    "tool_use_id": "call-2",
                    "tool_input": {"command": patch},
                    "cwd": str(REPO_ROOT),
                },
            )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("shared main clone", output["permissionDecisionReason"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)

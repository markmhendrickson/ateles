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
    def test_installed_stop_hooks_continue_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "codex-home" / "hooks.json"
            outside = root / "outside-any-repo"
            outside.mkdir()
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

            cases = (
                (
                    "Stop",
                    {
                        "session_id": "installed-main-stop",
                        "turn_id": "turn-main",
                        "hook_event_name": "Stop",
                        "model": "codex-test-model",
                        "transcript_path": None,
                        "last_assistant_message": "Should I fix this now?",
                        "stop_hook_active": False,
                        "cwd": os.fspath(outside),
                    },
                ),
                (
                    "SubagentStop",
                    {
                        "session_id": "installed-parent-stop",
                        "turn_id": "turn-subagent",
                        "hook_event_name": "SubagentStop",
                        "model": "codex-test-model",
                        "agent_id": "agent-1",
                        "agent_type": "worker",
                        "agent_transcript_path": None,
                        "last_assistant_message": "The decision is unchanged.",
                        "stop_hook_active": False,
                        "cwd": os.fspath(outside),
                    },
                ),
            )
            for event, payload in cases:
                with self.subTest(event=event):
                    command = next(
                        hook["command"]
                        for group in data["hooks"][event]
                        for hook in group.get("hooks", [])
                        if "decision_shape_gate.py" in hook["command"]
                    )
                    self.assertNotIn("git rev-parse", command)
                    self.assertIn(os.fspath(REPO_ROOT), command)
                    result = subprocess.run(
                        ["/bin/sh", "-c", command],
                        input=json.dumps(payload),
                        text=True,
                        capture_output=True,
                        cwd=outside,
                        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                        timeout=20,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    decision = json.loads(result.stdout)
                    self.assertEqual(decision["decision"], "block")

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

    def test_installed_hook_keys_state_on_checkout_not_cwd_without_claude_project_dir(
        self,
    ) -> None:
        """Codex never sets CLAUDE_PROJECT_DIR. Without it, session state must
        still land under the installed checkout's own .claude/.session_state/
        (resolved from the hook script's file location) — never under
        whatever directory the hook happened to be invoked from, which would
        make the delta-delivery dedup this test guards silently unstable
        across invocations from different cwds."""
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

            fake.handler.rows = [
                {
                    "entity_id": "ent_no_project_dir_canary",
                    "snapshot": {
                        "title": "CODEX_NO_PROJECT_DIR_CANARY_5F2C",
                        "rule": "CODEX_NO_PROJECT_DIR_CANARY_5F2C reaches context.",
                        "applies_when": "always",
                        "scope": "global",
                        "status": "active",
                        "rule_kind": "mandatory",
                    },
                }
            ]
            outside = Path(tmp) / "outside-any-repo-no-env"
            outside.mkdir()
            result = subprocess.run(
                ["/bin/sh", "-c", command],
                input=json.dumps(
                    {
                        "session_id": "no-project-dir-session",
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
                    # Deliberately no CLAUDE_PROJECT_DIR — the real Codex shape.
                },
                timeout=20,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CODEX_NO_PROJECT_DIR_CANARY_5F2C", result.stdout)
        state_file = (
            REPO_ROOT
            / ".claude"
            / ".session_state"
            / "no-project-dir-session.json"
        )
        try:
            self.assertTrue(
                state_file.exists(),
                f"expected session state at {state_file}, keyed on the "
                "installed checkout, not on the outside-repo cwd the hook "
                "was invoked from",
            )
            self.assertFalse(
                (outside / ".claude" / ".session_state").exists(),
                "session state must not be keyed on cwd when "
                "CLAUDE_PROJECT_DIR is unset",
            )
        finally:
            if state_file.exists():
                state_file.unlink()

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


class TestCodexPointOfUseRuleInjection(unittest.TestCase):
    """rule_injection_gate.py (ateles rule delivery audit
    ent_b66293f0dcc8c887d4fdbeae, recommendation 5) wired under Codex's
    PreToolUse group. Uses the real ``ent_c4d33237ff2d12b4aaec71af`` mapping
    (harness_config category) with SYNTHETIC row content served by the fake
    Neotoma server — the mapped entity id is real, its rule text here is not."""

    def test_configured_pretooluse_command_injects_mapped_rule_on_grant_write(
        self,
    ) -> None:
        command = next(
            c
            for c in _hook_commands("PreToolUse")
            if "rule_injection_gate.py" in c
        )
        with _FakeNeotoma() as fake:
            fake.handler.rows = [
                {
                    "entity_id": "ent_1c0cbb99d2c8011358ff1dc3",
                    "snapshot": {
                        "title": "Stop for approval before high-risk changes.",
                        "rule": "CODEX_GRANT_RULE_CANARY_B71E must reach the model before the write.",
                        "applies_when": "proposing a schema, auth, foundation-doc, or architectural change",
                        "scope": "swarm",
                        "status": "active",
                        "rule_kind": "advisory",
                    },
                }
            ]
            result = _run(
                command,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "mcp__mcpsrv_neotoma__correct",
                    "tool_input": {
                        "entity_id": "ent_58ecd30ea709bece07025df1",
                        "entity_type": "agent_grant",
                        "field": "entity_types",
                        "value": [],
                    },
                    "cwd": str(REPO_ROOT),
                },
                env={"NEOTOMA_BASE_URL": fake.base_url},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertIn("CODEX_GRANT_RULE_CANARY_B71E", ctx)
        self.assertIn("get_session_identity", ctx)

    def test_configured_pretooluse_command_matches_apply_patch(self) -> None:
        """This hook now parses apply_patch payloads for the harness_config
        category (via `_apply_patch_paths`, delegated to
        `sibling_repo_worktree_guard.py`'s parser of the same name), so its
        group must claim apply_patch coverage — Falco/Waxwing, PR #1320
        round 2: the pre-fix state left the exact audited failure (a
        harness-config file edit with no governing rule reaching it)
        reproducible on Codex via apply_patch, with the matcher's own
        omission the reason nothing caught it."""
        group = _group_for("rule_injection_gate.py")
        self.assertIn("apply_patch", group["matcher"])

    def test_configured_pretooluse_command_injects_on_apply_patch_harness_config_edit(
        self,
    ) -> None:
        """End-to-end: an apply_patch payload naming a harness-config path
        fires the SAME harness_config injection an Edit/Write does under
        Claude Code. Confirmed RED before this fix (matched_categories had
        no apply_patch branch at all, so this call matched nothing and
        injected nothing) — this is the reproduction of Falco's non-blocking
        finding, now closed."""
        command = next(
            c
            for c in _hook_commands("PreToolUse")
            if "rule_injection_gate.py" in c
        )
        with _FakeNeotoma() as fake:
            fake.handler.rows = [
                {
                    "entity_id": "ent_c4d33237ff2d12b4aaec71af",
                    "snapshot": {
                        "title": "Do not rewrite harness config without the governing rule.",
                        "rule": "CODEX_APPLY_PATCH_HARNESS_CONFIG_CANARY_9F3A",
                        "applies_when": "configuring Cursor's Neotoma MCP connection for source development",
                        "scope": "swarm",
                        "status": "active",
                        "rule_kind": "mandatory",
                    },
                }
            ]
            result = _run(
                command,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "apply_patch",
                    "tool_input": {
                        "command": (
                            "*** Begin Patch\n"
                            "*** Update File: .claude/settings.json\n"
                            "@@\n"
                            "-old\n"
                            "+new\n"
                            "*** End Patch"
                        ),
                    },
                    "cwd": str(REPO_ROOT),
                },
                env={"NEOTOMA_BASE_URL": fake.base_url},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertIn("CODEX_APPLY_PATCH_HARNESS_CONFIG_CANARY_9F3A", ctx)

    def test_configured_pretooluse_command_injects_nothing_for_unrelated_apply_patch(
        self,
    ) -> None:
        """An apply_patch payload touching a file OTHER than a harness-config
        path must not match — same "affirmative shape, not blanket
        apply_patch coverage" posture as sibling_repo_worktree_guard.py."""
        command = next(
            c
            for c in _hook_commands("PreToolUse")
            if "rule_injection_gate.py" in c
        )
        with _FakeNeotoma() as fake:
            fake.handler.rows = []
            result = _run(
                command,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "apply_patch",
                    "tool_input": {
                        "command": (
                            "*** Begin Patch\n"
                            "*** Update File: src/foo.py\n"
                            "@@\n"
                            "-old\n"
                            "+new\n"
                            "*** End Patch"
                        ),
                    },
                    "cwd": str(REPO_ROOT),
                },
                env={"NEOTOMA_BASE_URL": fake.base_url},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


def _pretooluse_groups() -> list[dict]:
    data = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    return data["hooks"]["PreToolUse"]


def _group_for(script_name: str) -> dict:
    for group in _pretooluse_groups():
        for hook in group.get("hooks", []):
            if script_name in hook["command"]:
                return group
    raise AssertionError(f"no PreToolUse group wires {script_name}")


class TestCodexPreToolUseMatcherSurfaces(unittest.TestCase):
    """The matcher on each PreToolUse group is a claim about which Codex
    tool names that group's guards actually parse. gmail_send_gate.py,
    git_stash_guard.py, and gh_identity_guard.py are shell-command guards
    with no apply_patch payload to parse (unlike sibling_repo_worktree_guard.py,
    which genuinely understands both shapes) — so their group must not
    advertise apply_patch coverage it cannot provide (ateles#1319,
    security_finding ent_4240262e581f93a0a4cfdbd1, Waxwing correction
    issuecomment-5853939910)."""

    def test_sibling_guard_group_matches_bash_and_apply_patch(self) -> None:
        group = _group_for("sibling_repo_worktree_guard.py")
        self.assertEqual(group["matcher"], "^(Bash|apply_patch)$")

    def test_shell_only_guards_are_grouped_separately_from_apply_patch(self) -> None:
        for script in (
            "gmail_send_gate.py",
            "git_stash_guard.py",
            "gh_identity_guard.py",
        ):
            with self.subTest(script=script):
                group = _group_for(script)
                self.assertEqual(group["matcher"], "^Bash$")
                self.assertNotIn("apply_patch", group["matcher"])

    def test_shell_only_guards_share_one_group_not_the_sibling_guards_group(
        self,
    ) -> None:
        groups = _pretooluse_groups()

        def group_index(script: str) -> int:
            for index, group in enumerate(groups):
                for hook in group.get("hooks", []):
                    if script in hook["command"]:
                        return index
            raise AssertionError(f"no PreToolUse group wires {script}")

        shell_guard_indices = {
            group_index(script)
            for script in (
                "gmail_send_gate.py",
                "git_stash_guard.py",
                "gh_identity_guard.py",
            )
        }
        self.assertEqual(len(shell_guard_indices), 1)
        sibling_index = group_index("sibling_repo_worktree_guard.py")
        self.assertNotIn(sibling_index, shell_guard_indices)


class TestCodexGuardEffect(unittest.TestCase):
    def test_reporting_contract_reaches_session_context(self) -> None:
        command = next(
            c for c in _hook_commands("SessionStart") if "reporting_contract.py" in c
        )
        result = _run(
            command,
            {
                "session_id": "codex-reporting-session",
                "hook_event_name": "SessionStart",
                "source": "compact",
                "cwd": str(REPO_ROOT),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MATERIAL STATE CHANGE", result.stdout)
        self.assertIn("final answer self-contained", result.stdout)

    def test_reporting_gate_blocks_low_level_codex_turn(self) -> None:
        command = next(
            c for c in _hook_commands("Stop") if "report_quality_gate.py" in c
        )
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout.jsonl"
            rows = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "assistant", "phase": "commentary",
                        "content": [{"type": "output_text", "text": "I’ll inspect the file next."}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "assistant", "phase": "commentary",
                        "content": [{"type": "output_text", "text": "I’ll run the test next."}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "assistant", "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "Done — see above."}],
                    },
                },
            ]
            transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = _run(
                command,
                {
                    "session_id": "codex-reporting-session",
                    "hook_event_name": "Stop",
                    "transcript_path": str(transcript),
                    "last_assistant_message": "Done — see above.",
                    "cwd": str(REPO_ROOT),
                },
            )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("per-tool narration", payload["reason"])

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

    def test_configured_git_stash_guard_allows_apply_patch_unmatched(self) -> None:
        """apply_patch is not in this guard's matcher, so Codex would never
        invoke it for an apply_patch tool call. Directly invoking it anyway
        (as Falco's reproduction did) must still fail open rather than deny —
        proving the guard's own tool_name gate, not the matcher, is what a
        caller falls back on if it is ever misrouted."""
        commands = _hook_commands("PreToolUse")
        command = next(c for c in commands if "git_stash_guard.py" in c)
        result = _run(
            command,
            {
                "session_id": "codex-effect-session",
                "turn_id": "turn-1b",
                "hook_event_name": "PreToolUse",
                "tool_name": "apply_patch",
                "tool_use_id": "call-1b",
                "tool_input": {"command": "git stash push -m unsafe"},
                "cwd": str(REPO_ROOT),
            },
        )
        self.assertEqual(result.returncode, 0)

    def test_configured_gmail_send_gate_returns_a_codex_deny(self) -> None:
        commands = _hook_commands("PreToolUse")
        command = next(c for c in commands if "gmail_send_gate.py" in c)
        result = _run(
            command,
            {
                "session_id": "codex-effect-session",
                "turn_id": "turn-4",
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_use_id": "call-4",
                "tool_input": {
                    "command": "gws gmail users messages send --params '{}'"
                },
                "cwd": str(REPO_ROOT),
            },
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")

    def test_configured_gh_identity_guard_returns_a_codex_deny(self) -> None:
        commands = _hook_commands("PreToolUse")
        command = next(c for c in commands if "gh_identity_guard.py" in c)
        result = _run(
            command,
            {
                "session_id": "codex-effect-session",
                "turn_id": "turn-5",
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_use_id": "call-5",
                "tool_input": {
                    "command": 'GH_TOKEN="" gh pr create --title x --body y'
                },
                "cwd": str(REPO_ROOT),
            },
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")

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


class TestCodexStopContinuationEffect(unittest.TestCase):
    def test_configured_stop_command_continues_a_noncompliant_turn(self) -> None:
        command = next(
            c for c in _hook_commands("Stop") if "decision_shape_gate.py" in c
        )
        result = _run(
            command,
            {
                "session_id": "codex-stop-session",
                "turn_id": "turn-stop",
                "hook_event_name": "Stop",
                "model": "codex-test-model",
                "transcript_path": None,
                "last_assistant_message": "Want me to run the focused tests?",
                "stop_hook_active": False,
                "cwd": str(REPO_ROOT),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("asking permission", payload["reason"])

    def test_configured_subagent_stop_continues_a_noncompliant_turn(self) -> None:
        command = next(
            c
            for c in _hook_commands("SubagentStop")
            if "decision_shape_gate.py" in c
        )
        result = _run(
            command,
            {
                "session_id": "codex-parent-session",
                "turn_id": "turn-subagent-stop",
                "hook_event_name": "SubagentStop",
                "model": "codex-test-model",
                "agent_id": "agent-1",
                "agent_type": "worker",
                "agent_transcript_path": None,
                "last_assistant_message": "The carried decision is unchanged.",
                "stop_hook_active": False,
                "cwd": str(REPO_ROOT),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("unchanged", payload["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

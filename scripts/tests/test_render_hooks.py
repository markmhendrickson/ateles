"""Unit tests for execution/scripts/render_hooks.py's pure logic: adapter
generation, settings.json merge, check()/write() exit-code contract, missing-
policy refusal, WARN-on-ungoverned, and NOT_IMPLEMENTED reporting.

These tests operate entirely on in-memory policy dicts and temp directories —
no live Neotoma call, matching how CI must run this without credentials.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "execution" / "scripts" / "render_hooks.py"

spec = importlib.util.spec_from_file_location("render_hooks", SCRIPT_PATH)
render_hooks = importlib.util.module_from_spec(spec)
sys.modules["render_hooks"] = render_hooks
spec.loader.exec_module(render_hooks)


def _policy(hook_name, harness="claude", status="active", event="PostToolUse", matcher="", entity_id="ent_test"):
    return {
        "hook_name": hook_name,
        "harness": harness,
        "repository": render_hooks.REPOSITORY,
        "event": event,
        "matcher": matcher,
        "intent": "test intent",
        "enforces": "test enforcement",
        "status": status,
        "_entity_id": entity_id,
    }


def test_exit_codes_ok(tmp_path, monkeypatch):
    """clean=0 per the versioned exit-code contract."""
    adapters_dir = tmp_path / "hooks"
    monkeypatch.setitem(render_hooks.ADAPTERS_DIR, "claude", adapters_dir)
    policy = _policy("hook_a")
    policies = {("hook_a", "claude"): [policy]}
    content = render_hooks.claude_adapter_source("hook_a", policy)
    adapters_dir.mkdir(parents=True)
    (adapters_dir / "hook_a.py").write_text(content)
    assert render_hooks.check(["hook_a"], policies, "claude") == 0


def test_exit_codes_drift(tmp_path, monkeypatch, capsys):
    """drift=1, output names the exact stale file + exact fix command."""
    adapters_dir = tmp_path / "hooks"
    adapters_dir.mkdir(parents=True)
    monkeypatch.setitem(render_hooks.ADAPTERS_DIR, "claude", adapters_dir)
    policy = _policy("hook_a")
    policies = {("hook_a", "claude"): [policy]}
    (adapters_dir / "hook_a.py").write_text("# stale, hand-edited\n")
    rc = render_hooks.check(["hook_a"], policies, "claude")
    out = capsys.readouterr().out
    assert rc == 1
    assert ".claude/hooks/hook_a.py does not match hooks/logic/hook_a.py" in out
    assert "render_hooks.py --write" in out


def test_exit_codes_warn_ungoverned(tmp_path, monkeypatch, capsys):
    """WARN=2 when an adapter is deployed but its policy is archived/absent."""
    adapters_dir = tmp_path / "hooks"
    adapters_dir.mkdir(parents=True)
    monkeypatch.setitem(render_hooks.ADAPTERS_DIR, "claude", adapters_dir)
    # No active policy at all for hook_a, but the adapter file exists on disk.
    (adapters_dir / "hook_a.py").write_text("# leftover adapter\n")
    policies = {}
    rc = render_hooks.check(["hook_a"], policies, "claude")
    out = capsys.readouterr().out
    assert rc == 2
    assert "WARN" in out
    assert "adapter is running ungoverned" in out


def test_exit_codes_missing_policy_refusal(tmp_path, monkeypatch, capsys):
    """P0: --write for a hook with logic but no hook_policy entity must
    REFUSE (exit 1), write nothing, and point at the policy template doc."""
    adapters_dir = tmp_path / "hooks"
    monkeypatch.setitem(render_hooks.ADAPTERS_DIR, "claude", adapters_dir)
    policies = {}
    rc = render_hooks.write(["hook_a"], policies, "claude")
    out = capsys.readouterr().out
    assert rc == 1
    assert "no execution_policy entity found for hook 'hook_a'" in out
    assert "docs/hooks/POLICY_TEMPLATE.md" in out
    assert not (adapters_dir / "hook_a.py").exists()


def test_write_missing_policy_for_one_hook_does_not_block_others(tmp_path, monkeypatch, capsys):
    """A hook missing an active policy (never authored, or archived) must not
    block --write from regenerating every OTHER governed hook's adapter —
    only that one hook is refused. All-or-nothing batch refusal would make
    adding a 6th hook's logic before authoring its policy break regeneration
    for the 5 already-governed hooks too."""
    adapters_dir = tmp_path / "hooks"
    monkeypatch.setitem(render_hooks.ADAPTERS_DIR, "claude", adapters_dir)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")
    monkeypatch.setattr(render_hooks, "SETTINGS_PATH", settings_path)

    policy_a = _policy("hook_a")
    policies = {("hook_a", "claude"): [policy_a]}  # hook_b has no policy at all
    rc = render_hooks.write(["hook_a", "hook_b"], policies, "claude")
    out = capsys.readouterr().out

    assert rc == 1  # still nonzero: a refusal happened
    assert "no execution_policy entity found for hook 'hook_b'" in out
    assert (adapters_dir / "hook_a.py").exists()  # but hook_a still got written
    assert (adapters_dir / "hook_a.py").read_text() == render_hooks.claude_adapter_source("hook_a", policy_a)
    assert not (adapters_dir / "hook_b.py").exists()


def test_exit_codes_not_implemented(capsys):
    """NOT_IMPLEMENTED=3, exact message shape, distinct from DRIFT/WARN codes."""
    policies = {("hook_a", "claude"): [_policy("hook_a")]}
    rc = render_hooks.check(["hook_a"], policies, "openclaw")
    out = capsys.readouterr().out
    assert rc == 3
    assert out.strip() == "NOT_IMPLEMENTED: hook_a (policy exists, no openclaw adapter)"


def test_ambiguous_active_policy_refuses(tmp_path, monkeypatch):
    """Two ACTIVE hook_policy candidates for the same key must error, never
    'pick the first result'."""
    adapters_dir = tmp_path / "hooks"
    monkeypatch.setitem(render_hooks.ADAPTERS_DIR, "claude", adapters_dir)
    policies = {
        ("hook_a", "claude"): [
            _policy("hook_a", entity_id="ent_1"),
            _policy("hook_a", entity_id="ent_2"),
        ]
    }
    try:
        render_hooks.check(["hook_a"], policies, "claude")
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert "ambiguous hook_policy" in str(exc)


def test_first_run_no_adapter(tmp_path, monkeypatch):
    """--write with no existing adapter creates it; content matches the
    generator's own claude_adapter_source output."""
    adapters_dir = tmp_path / "hooks"
    monkeypatch.setitem(render_hooks.ADAPTERS_DIR, "claude", adapters_dir)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")
    monkeypatch.setattr(render_hooks, "SETTINGS_PATH", settings_path)

    policy = _policy("hook_a")
    policies = {("hook_a", "claude"): [policy]}
    rc = render_hooks.write(["hook_a"], policies, "claude")
    assert rc == 0
    adapter_path = adapters_dir / "hook_a.py"
    assert adapter_path.exists()
    assert adapter_path.read_text() == render_hooks.claude_adapter_source("hook_a", policy)


def test_rerun_idempotent(tmp_path, monkeypatch, capsys):
    """Running --write twice in a row produces zero file changes on the
    second run and exits 0 both times — idempotency as a review-hygiene and
    security property, not just convenience."""
    adapters_dir = tmp_path / "hooks"
    monkeypatch.setitem(render_hooks.ADAPTERS_DIR, "claude", adapters_dir)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")
    monkeypatch.setattr(render_hooks, "SETTINGS_PATH", settings_path)

    policy = _policy("hook_a")
    policies = {("hook_a", "claude"): [policy]}
    assert render_hooks.write(["hook_a"], policies, "claude") == 0
    mtime_1 = (adapters_dir / "hook_a.py").stat().st_mtime_ns
    content_1 = (adapters_dir / "hook_a.py").read_text()

    capsys.readouterr()
    assert render_hooks.write(["hook_a"], policies, "claude") == 0
    out = capsys.readouterr().out
    mtime_2 = (adapters_dir / "hook_a.py").stat().st_mtime_ns
    content_2 = (adapters_dir / "hook_a.py").read_text()

    assert content_1 == content_2
    assert "unchanged" in out
    assert "no changes (idempotent no-op)" in out


def test_matcher_event_name_changed(tmp_path, monkeypatch, capsys):
    """When policy/logic changes the matcher, --write regenerates settings.json
    wiring and explicitly calls out the matcher change (not just 'updated')."""
    adapters_dir = tmp_path / "hooks"
    monkeypatch.setitem(render_hooks.ADAPTERS_DIR, "claude", adapters_dir)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")
    monkeypatch.setattr(render_hooks, "SETTINGS_PATH", settings_path)

    policy_v1 = _policy("hook_a", matcher="OldMatcher")
    render_hooks.write(["hook_a"], {("hook_a", "claude"): [policy_v1]}, "claude")
    capsys.readouterr()

    policy_v2 = _policy("hook_a", matcher="NewMatcher")
    rc = render_hooks.write(["hook_a"], {("hook_a", "claude"): [policy_v2]}, "claude")
    out = capsys.readouterr().out
    assert rc == 0
    assert "matcher changed" in out
    assert "'OldMatcher'" in out and "'NewMatcher'" in out
    settings = json.loads(settings_path.read_text())
    assert settings["hooks"]["PostToolUse"][0]["matcher"] == "NewMatcher"


def test_settings_merge_preserves_hand_authored_entries(tmp_path, monkeypatch):
    """merge_settings_wiring must not touch hook entries it doesn't manage."""
    hand_authored = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": '"$CLAUDE_PROJECT_DIR"/.claude/hooks/ateles-session-start.sh'}]}
            ]
        }
    }
    policy = _policy("hook_a", event="PostToolUse", matcher="SomeMatcher")
    merged, changes = render_hooks.merge_settings_wiring(hand_authored, {"hook_a": policy})
    assert merged["hooks"]["SessionStart"] == hand_authored["hooks"]["SessionStart"]
    assert any("wired hook_a" in c for c in changes)

"""Tests for git_hook_bypass_guard.py (PreToolUse: Bash)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent / "git_hook_bypass_guard.py"
sys.path.insert(0, str(HOOK.parent))
import git_hook_bypass_guard as guard  # noqa: E402


def run_hook(payload) -> subprocess.CompletedProcess:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK)], input=raw,
                          capture_output=True, text=True, timeout=30)


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


REFUSED = [
    "git commit --no-verify -m 'bump'",
    "git push --no-verify origin release/v1.2.3",
    "git push origin HEAD --no-verify",
    "git -C /tmp/wt commit --no-verify -m x",
    "git commit --no-veri -m x",
    "git commit --no-verif -m x",
    "git merge --no-verify feature",
    "git commit -n -m x",
    "git commit -an -m x",
    "git commit -nm 'msg'",
    "git commit -m 'msg' -n",
    "git -c core.hooksPath=/dev/null commit -m x",
    "git -c core.hookspath=/dev/null push",
    "git config core.hooksPath /dev/null",
    "git config set core.hooksPath /dev/null",
    "HUSKY=0 git commit -m x",
    "env HUSKY=0 git commit -m x",
    "export HUSKY=0",
    "export HUSKY=0 && git commit -m x",
    "cd /tmp/wt && git add -A && git commit --no-verify -m x",
    "git add -A; git commit --no-verify -m x",
    "git add -A\ngit commit --no-verify -m x",
    "git commit \\\n  --no-verify -m x",
    "bash -c 'git commit --no-verify -m x'",
    "sh -c \"git push --no-verify\"",
    "eval git commit --no-verify -m x",
    "echo $(git commit --no-verify -m x)",
    "echo `git commit --no-verify -m x`",
    "nohup git push --no-verify &",
    "git commit -C HEAD~1 -n",  # -C consumes HEAD~1; -n is still a flag
    "timeout 600 git commit --no-verify -m x",
    "/usr/bin/git commit --no-verify -m x",
    "python3 -c \"import subprocess; subprocess.run(['git','commit','--no-verify'])\"",
    "node -e \"require('child_process').execSync('git push --no-verify')\"",
]

NEW_REFUSED = [
    "git --config-env=core.hooksPath=X commit -m x",
    "git --config-env core.HOOKSPATH=X commit -m x",
    "GIT_CONFIG_COUNT=1 git commit -m x",
    "GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "env GIT_CONFIG_VALUE_0=/dev/null git commit -m x",
    "GIT_CONFIG_PARAMETERS='core.hooksPath=/dev/null' git commit -m x",
    "env GIT_CONFIG_GLOBAL=/tmp/empty-gitconfig git commit -m x",
    "export GIT_CONFIG_SYSTEM=/tmp/empty-gitconfig",
    "HUSKY=1 git commit -m x",
    "env HUSKY=true git commit -m x",
    "export HUSKY=enabled",
    "bash -lc 'git commit --no-verify -m x'",
    "sh -ec 'git commit --no-verify -m x'",
    "zsh -xc 'git commit --no-verify -m x'",
    "bash -l -c 'git commit --no-verify -m x'",
    "git commit $(printf -- --no-verify) -m x",
    "git commit `printf -- --no-verify` -m x",
    "git commit --$FLAG -m x",
    "git $SUBCOMMAND -m x",
    "git ${SUBCOMMAND} -m x",
    'git -C "$REPO" commit -m x',
    "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['git', 'commit', '--no-verify'])\nPY",
    "bash <<EOF\ngit -c core.hooksPath=/dev/null commit -m x\nEOF",
    "node <<'JS'\nrequire('child_process').execSync('HUSKY=1 git commit -m x')\nJS",
    "python3 -c \"import subprocess; subprocess.run(['git','commit','-n'])\"",
    "node -e \"require('child_process').execSync('git config core.hooksPath /dev/null')\"",
]
REFUSED += NEW_REFUSED

ALLOWED = [
    "git commit -m 'never use --no-verify here'",
    "git commit -m \"docs: explain why --no-verify is refused\"",
    "SKIP_TESTS=1 SKIP_TESTS_REASON='suite stalls on host load' git commit -m x",
    "git push origin release/v1.2.3",
    "git push -n origin main",  # -n is --dry-run on push
    "git commit -m x -- notes-n.md",
    "git config --get core.hooksPath",
    "git config --unset core.hooksPath",
    "grep -rn -- --no-verify docs/",
    "rg -n 'no-verify' .",
    "echo --no-verify",
    "gh pr create --title x --body 'the agent used --no-verify'",
    "ls -ln /tmp",
    "npm run test -- --no-verify-cache",
    "git log --oneline -n 5",
    "git commit -m 'n'",
    "git config get core.hooksPath",
    "git config get --all core.hooksPath",
    "git config --get-all core.hooksPath",
    "git config --list",
    "git config --show-origin --get core.hooksPath",
    "git config core.hooksPath",
    "git config --show-origin core.hooksPath",
    "git config --unset-all core.hooksPath",
    "git config unset core.hooksPath",
    "git commit -uno",
    "git commit -uall",
    "git commit --message -n",
    "git commit -m x -- --no-verify",
    'git commit -m "$MSG"',
    'git commit --message "$MSG"',
    'git commit -F "$FILE"',
    'git commit --file "$FILE"',
    "python3 -c \"import subprocess; subprocess.run(['git','config','--get','core.hooksPath'])\"",
]


@pytest.mark.parametrize("command", REFUSED)
def test_refused(command):
    assert guard.find_bypass(command) is not None, command
    proc = run_hook(bash(command))
    assert proc.returncode == 2
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "SKIP_TESTS_REASON" in out["permissionDecisionReason"]


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command):
    assert guard.find_bypass(command) is None, command
    proc = run_hook(bash(command))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_commit_value_flag_consumes_next_token():
    # `-m -n`: the -n is the message, not a flag.
    assert guard.find_bypass("git commit -m -n") is None
    # but a real -n after the message is still caught
    assert guard.find_bypass("git commit -m -n -n") is not None


def test_non_bash_tool_ignored():
    proc = run_hook({"tool_name": "Edit", "tool_input": {"command": "git commit --no-verify"}})
    assert proc.returncode == 0 and proc.stdout.strip() == ""


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null"])
def test_fail_open_on_bad_input(raw):
    proc = run_hook(raw)
    assert proc.returncode == 0


def test_unbalanced_quote_still_judged():
    assert guard.find_bypass("git commit --no-verify -m 'oops") is not None
    assert guard.find_bypass("echo 'unterminated") is None


def test_no_override_env_is_honoured(monkeypatch):
    monkeypatch.setenv("ATELES_ALLOW_NO_VERIFY", "1")
    assert guard.find_bypass("git commit --no-verify -m x") is not None
    assert guard.find_bypass("ATELES_ALLOW_NO_VERIFY=1 git commit --no-verify -m x") is not None


@pytest.mark.skipif(
    os.environ.get("ATELES_RUN_CLAUDE_HARNESS_TEST") != "1" or shutil.which("claude") is None,
    reason="set ATELES_RUN_CLAUDE_HARNESS_TEST=1 with claude on PATH",
)
def test_real_claude_harness_blocks_no_verify_commit(tmp_path):
    daemon_dir = (
        Path(__file__).resolve().parents[2]
        / "execution"
        / "daemons"
        / "phoenicurus-release"
    )
    sys.path.insert(0, str(daemon_dir))
    import prepare  # noqa: E402, PLC0415

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "harness@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)

    prompt = (
        "Use the Bash tool to run exactly this command once, then stop: "
        "git commit --no-verify -m blocked-by-guard"
    )
    subprocess.run(
        [
            shutil.which("claude"),
            "--print",
            "--dangerously-skip-permissions",
            "--settings",
            prepare._agent_settings(),
            prompt,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert after == before

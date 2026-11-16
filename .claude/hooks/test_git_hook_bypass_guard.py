"""Tests for git_hook_bypass_guard.py (PreToolUse: Bash)."""
import json
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

# Round 3 (ateles PR 1771): every write, unset, rename or removal of
# core.hooksPath in any form and scope; shell-assembled options and env -S;
# the whole GIT_CONFIG* family and the variables that relocate git.
ROUND3_REFUSED = [
    # finding 1: core.hooksPath removed or rewritten
    "git config --unset core.hooksPath",
    "git config --unset-all core.hooksPath",
    "git config unset core.hooksPath",
    "git config --unset core.hooksPath && git commit -m release",
    "git config unset --all core.hooksPath",
    "git config --system --unset core.hooksPath",
    "git config --worktree --unset-all core.hooksPath",
    "git config --global unset core.hooksPath",
    "git config --unse core.hooksPath",  # abbreviation git accepts
    "git config -f .git/config --unset core.hooksPath",
    "git config --rename-section core oldcore",
    "git config --remove-section core",
    "git config remove-section core",
    "git config rename-section core oldcore",
    "git config --edit",
    "git config -e --local",
    "git config include.path /tmp/no-hooks.gitconfig",
    "git -c include.path=/tmp/no-hooks.gitconfig commit -m x",
    "git --exec-path=/tmp/bin commit -m x",
    "git --git-dir=/tmp/other/.git commit -m x",
    "git --work-tree /tmp/other commit -m x",
    # finding 2: options assembled by the shell, and env -S
    "git commit --no-ver\"\"ify -m x",
    "git commit -a\"\"n -m x",
    "git commit -a'n' -m x",
    "git commit \"-an\" -m x",
    "\"$GIT\" commit --no-verify -m x",
    "env -S 'git commit --no-verify -m x'",
    "env -S'git commit --no-verify -m x'",
    "env --split-string='git commit -n -m x'",
    "env -S 'git -c core.hooksPath=/dev/null commit' -m x",
    "/usr/bin/env -S \"git push --no-verify\"",
    "env -S 'git config --unset core.hooksPath'",
    # finding 3: GIT_CONFIG* and relocating variables
    "GIT_CONFIG_NOSYSTEM=1 git commit -m x",
    "env GIT_CONFIG_NOSYSTEM=1 git commit -m x",
    "export GIT_CONFIG_NOSYSTEM=1",
    "GIT_CONFIG=/tmp/empty git commit -m x",
    "GIT_DIR=/tmp/other/.git git commit -m x",
    "env GIT_WORK_TREE=/tmp/other git commit -m x",
    "export GIT_EXEC_PATH=/tmp/bin",
    "GIT_EXEC_PATH=/tmp/bin git commit -m x",
]
REFUSED += ROUND3_REFUSED

# Round 3 self-review: a shell-assembled command name judged as git, env
# option clusters that take a value, and a config key the shell assembles.
ROUND3_REVIEW_REFUSED = [
    "$GIT commit -n",
    "\"$GIT\" commit -an -m x",
    "g=git; $g commit -n",
    "$GIT -c core.hooksPath=/dev/null commit -m x",
    "env -iu FOO git commit --no-verify -m x",
    "env -iC /tmp git commit --no-verify -m x",
    "git config core.hooks$X /dev/null",
    "git config --unset core.hooks${X}",
]
REFUSED += ROUND3_REVIEW_REFUSED

ALLOWED = [
    "git commit -m 'never use --no-verify here'",
    "git commit -m \"docs: explain why --no-verify is refused\"",
    "SKIP_TESTS=1 SKIP_TESTS_REASON='suite stalls on host load' git commit -m x",
    "git push origin release/v1.2.3",
    "git push -n origin main",  # -n is --dry-run on push
    "git commit -m x -- notes-n.md",
    "git config --get core.hooksPath",
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

# Round 3: reads in every scope stay allowed, and a program that only prints
# text naming the flag is not a git invocation.
ROUND3_ALLOWED = [
    "python3 -c \"print('git commit --no-verify')\"",
    "node -e \"console.log('do not use git push --no-verify')\"",
]
ALLOWED += ROUND3_ALLOWED

# Regression pins added in round 3. The guard at c53d7159 already handled
# these; they are kept so the rewritten config and quick-filter logic cannot
# lose them (refusals) or over-refuse (reads and unrelated config writes).
ROUND3_REGRESSION_REFUSED = [
    "git config --global core.hooksPath /dev/null",
    "git config --local core.hooksPath .git/hooks",
    "git config --replace-all core.hooksPath /dev/null",
    "git config --add core.hooksPath /dev/null",
    "git -c core.hooksPath= commit -m x",
    "git -ccore.hooksPath= commit -m x",
    "git commit --no-veri''fy -m x",
    "git commit --no-$'verify' -m x",
    "git commit --no-ver\\ify -m x",
    "F=--no-verify; git commit $F -m x",
    "g\"\"it commit --no-verify -m x",
    "\\git commit --no-verify -m x",
    "env -iS 'HUSKY=0 git commit -m x'",
]
ROUND3_REGRESSION_ALLOWED = [
    "git config --global --get core.hooksPath",
    "git config --show-origin --list",
    "git config --list --show-scope",
    "git config --get-regexp hooks",
    "git config get --show-origin core.hooksPath",
    "git config --file .git/config --get core.hooksPath",
    "git config --global user.name 'Release Bot'",
    "git config --unset user.signingkey",
    "env -S 'echo hello'",
    "env -S 'git status'",
    "git --exec-path",
    "ls -la",
]
ROUND3_REGRESSION_REFUSED += [
    # perl pipe-open runs git; the narrowed inline-program check must see it
    "perl -e 'open P, \"git commit --no-verify|\"'",
    "ruby -e '%x(git push --no-verify)'",
    "git config --remove-section $SECTION",
]
ROUND3_REGRESSION_ALLOWED += [
    "\"$PYTHON\" scripts/report.py -n",
    "$EDITOR notes.md",
    "env -iu FOO ls",
]
REFUSED += ROUND3_REGRESSION_REFUSED
ALLOWED += ROUND3_REGRESSION_ALLOWED


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


# The real-harness check lives in execution/daemons/phoenicurus-release/verify_guard_live.py:
# the repository's test setup strips credentials, so the Claude CLI cannot run under pytest.

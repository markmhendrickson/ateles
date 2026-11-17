"""Tests for git_hook_bypass_guard.py (PreToolUse: Bash and file-write tools), the guard
loaded into the Phoenicurus release prepare agent.

Every refusal and allowance is driven through the hook entry point: the hook
runs as a subprocess with real PreToolUse JSON on stdin, exactly as the Claude
CLI runs it under prepare's --settings.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent / "git_hook_bypass_guard.py"


def run_hook(payload) -> subprocess.CompletedProcess:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK)], input=raw,
                          capture_output=True, text=True, timeout=30)


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command},
            "hook_event_name": "PreToolUse"}


def assert_refused(command: str) -> dict:
    proc = run_hook(bash(command))
    assert proc.returncode == 2, (command, proc.stdout, proc.stderr)
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "SKIP_TESTS_REASON" in out["permissionDecisionReason"]
    return out


def assert_allowed(command: str) -> None:
    proc = run_hook(bash(command))
    assert proc.returncode == 0, (command, proc.stdout, proc.stderr)
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize("writer", (
    "cp -R source/. {target}", "cp -a source/. {target}",
    "rsync -a source/ {target}", "mv source {target}",
    "tar -xf archive.tar -C {target}", "unzip archive.zip -d {target}",
    "ditto source {target}", "cpio -i -D {target}",
))
@pytest.mark.parametrize("target", (
    ".git/", ".git/hooks/", ".husky/", ".", "~", "$DEST",
))
def test_tree_writers_refuse_protected_ancestors_and_assembled_destinations(writer, target):
    assert_refused(writer.format(target=target))


@pytest.mark.parametrize("writer", (
    "cp -R source/. {target}", "cp -a source/. {target}",
    "rsync -a source/ {target}", "mv source {target}",
    "tar -xf archive.tar -C {target}", "unzip archive.zip -d {target}",
    "ditto source {target}", "cpio -i -D {target}",
))
@pytest.mark.parametrize("target", ("src/out/", "/tmp/ateles-guard-out/"))
def test_tree_writers_allow_unprotected_destinations(writer, target):
    assert_allowed(writer.format(target=target))


def test_archive_default_destination_and_list_only():
    assert_refused("tar -xf archive.tar")
    assert_allowed("tar -tf archive.tar")
    assert_allowed("tar -xf archive.tar -C src/out")


@pytest.mark.parametrize("shell", ("sh", "bash", "zsh", "dash", "ksh"))
@pytest.mark.parametrize("body", (
    "printf x > .git/config", "cp source .git/hooks/pre-commit",
    "tee .husky/pre-commit", "sed -i s/a/b/ .git/config",
    "cp -R source/. .git/",
))
def test_nested_shell_bodies_refuse_protected_writes(shell, body):
    assert_refused(f'{shell} -ec "{body}"')


@pytest.mark.parametrize("form", (
    'eval "{body}"', 'env bash -c "{body}"',
    'xargs sh -c "{body}"', 'find src -exec sh -c "{body}" ;',
    "bash <<< '{body}'", "sh <<'EOF'\n{body}\nEOF",
))
@pytest.mark.parametrize("body", (
    "printf x > .git/config", "cp source .git/hooks/pre-commit",
    "tee .husky/pre-commit", "cp -R source/. .git/",
))
def test_other_nested_shell_forms_refuse_protected_writes(form, body):
    assert_refused(form.format(body=body))


@pytest.mark.parametrize("form", (
    'bash -lc "{body}"', 'eval "{body}"', 'env sh -c "{body}"',
    'xargs sh -c "{body}"', 'find src -exec sh -c "{body}" ;',
    "bash <<< '{body}'", "sh <<'EOF'\n{body}\nEOF",
))
def test_nested_shell_forms_allow_ordinary_write(form):
    assert_allowed(form.format(body="printf x > src/out"))


@pytest.mark.parametrize("command", (
    'bash -c "git status"', 'bash -c "git commit --no-verify -m x"',
    'sh -c "$CMD"', 'python -c "open(\".git/config\", \"w\")"',
    'perl -e "open .husky/pre-commit"', 'node -e "require(\"fs\").writeFileSync(\".git/config\", \"x\")"',
))
def test_nested_git_and_unparseable_programs(command):
    if command == 'bash -c "git status"':
        assert_allowed(command)
    else:
        assert_refused(command)


def test_sourced_script_is_judged(tmp_path):
    script = tmp_path / "guard-script.sh"
    script.write_text("printf x > .git/config\n")
    assert_refused(f"source {script}")
    script.write_text("printf x > src/out\n")
    assert_allowed(f". {script}")


# --- the bypasses review rounds found past the denylist ---------------------

# Options the shell assembles, and env -S / --split-string.
SHELL_ASSEMBLED = [
    "git commit --no-ver\"\"ify -m x",
    "git commit --no-veri''fy -m x",
    "git commit --no-$'verify' -m x",
    "git commit --no-ver\\ify -m x",
    "git commit -a\"\"n -m x",
    "git commit -a'n' -m x",
    "F=--no-verify; git commit $F -m x",
    "git commit \"$F\" -m x",
    "git commit --$FLAG -m x",
    "git push origin \"$OPT\"",
    "git push \"--no-$X\" origin main",
    "git merge $OPT feature",
    "git $SUBCOMMAND -m x",
    "git commit $(printf -- --no-verify) -m x",
    "git commit `printf -- --no-verify` -m x",
    "env -S 'git commit --no-verify -m x'",
    "env -S'git commit --no-verify -m x'",
    "env --split-string='git commit -n -m x'",
    "/usr/bin/env -S \"git push --no-verify\"",
    "env -iS 'HUSKY=0 git commit -m x'",
]

# Every GIT_CONFIG* variable and the variables that relocate git or disable Husky.
GIT_VARIABLES = [
    "GIT_CONFIG_NOSYSTEM=1 git commit -m x",
    "env GIT_CONFIG_NOSYSTEM=1 git commit -m x",
    "export GIT_CONFIG_NOSYSTEM=1",
    "export GIT_CONFIG_NOSYSTEM=1; git commit -m x",
    "GIT_CONFIG_NOSYSTEM=1; git commit -m x",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x",
    "env GIT_CONFIG_VALUE_0=/dev/null git commit -m x",
    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\" git commit -m x",
    "env GIT_CONFIG_GLOBAL=/tmp/empty git commit -m x",
    "export GIT_CONFIG_SYSTEM=/tmp/empty",
    "GIT_CONFIG=/tmp/empty git commit -m x",
    "GIT_DIR=/tmp/other/.git git commit -m x",
    "env GIT_WORK_TREE=/tmp/other git commit -m x",
    "GIT_EXEC_PATH=/tmp/bin git commit -m x",
    "declare -x GIT_DIR=/tmp/other/.git",
    "HUSKY=0 git commit -m x",
    "env HUSKY=0 git commit -m x",
    "export HUSKY=0 && git commit -m x",
]

# core.hooksPath unset, rewritten or redirected, and every other config write.
CONFIG_WRITES = [
    "git config --unset core.hooksPath",
    "git config --unset-all core.hooksPath",
    "git config unset core.hooksPath",
    "git config --unse core.hooksPath",
    "git config --global --unset core.hooksPath",
    "git config --system --unset core.hooksPath",
    "git config --worktree --unset-all core.hooksPath",
    "git config -f .git/config --unset core.hooksPath",
    "git config --unset core.hooksPath && git commit -m release",
    "git config core.hooksPath /dev/null",
    "git config set core.hooksPath /dev/null",
    "git config --global core.hooksPath /dev/null",
    "git config --replace-all core.hooksPath /dev/null",
    "git config --add core.hooksPath /dev/null",
    "git config --remove-section core",
    "git config rename-section core oldcore",
    "git config --edit",
    "git config -e --local",
    "git config include.path /tmp/no-hooks.gitconfig",
    "git config core.hooks$X /dev/null",
    "git config core.bare false",
]

# Global options that change config, relocate git or swap its programs.
GLOBAL_OPTIONS = [
    "git -c core.hooksPath=/dev/null commit -m x",
    "git -ccore.hooksPath= commit -m x",
    "git -c include.path=/tmp/no-hooks.gitconfig commit -m x",
    "git --config-env=core.hooksPath=X commit -m x",
    "git --config-env core.hooksPath=X commit -m x",
    "git --exec-path=/tmp/bin commit -m x",
    "git --git-dir=/tmp/other/.git commit -m x",
    "git --work-tree /tmp/other commit -m x",
    "git --bare status",
]

# Git aliases, passed once or saved, and any subcommand off the allowlist.
ALIASES_AND_UNLISTED = [
    "git -c alias.ci='commit --no-verify' ci -m x",
    "git -c alias.ci='!git commit -n' ci",
    "git config alias.ci 'commit --no-verify' && git ci -m x",
    "git config --global alias.ci 'commit -n'",
    "git ci -m x",
    "git stash",
    "git hook run pre-commit",
    "git rebase --no-verify origin/main",
    "git am --no-verify patch.mbox",
    "git pull --no-verify origin main",
    "git cherry-pick abc123",
    "git tag v1.2.3",
    "git tag -a v1.2.3 -m 'Release v1.2.3'",
    "git tag -d v1.2.3",
]

# A bash -c body behind another bash option, and other inline programs.
INLINE_PROGRAMS = [
    "bash --rcfile X -c 'git commit --no-verify -m x'",
    "bash -o posix -c 'git commit -n -m x'",
    "bash -O extglob -c 'git push --no-verify'",
    "bash --norc -c 'git commit -n'",
    "bash -c 'git commit -m x'",
    "bash -lc 'git commit --no-verify -m x'",
    "sh -ec 'git commit --no-verify -m x'",
    "zsh -xc 'git commit --no-verify -m x'",
    "bash -l -c 'git commit --no-verify -m x'",
    "eval git commit -m x",
    "CMD='git commit -n'; eval \"$CMD\"",
    "X='git commit -n'; bash -c \"$X\"",
    "source <(echo git commit -n)",
    "python3 -c \"import subprocess; subprocess.run(['git','commit','-m','x'])\"",
    "python3 -c \"print('git commit --no-verify')\"",
    "node -e \"require('child_process').execSync('git push --no-verify')\"",
    "perl -e 'open P, \"git commit --no-verify|\"'",
    "ruby -e '%x(git push --no-verify)'",
    "awk 'BEGIN { system(\"git commit -n\") }'",
    "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['git', 'commit', '--no-verify'])\nPY",
    "bash <<EOF\ngit -c core.hooksPath=/dev/null commit -m x\nEOF",
    "bash <<< 'git commit -n'",
    "echo 'git commit --no-verify' | bash",
    "printf 'git commit -n' | sh -s",
    "git diff | bash",
]

# Wrappers, path-qualified or assembled git binaries, and substitutions around git.
WRAPPERS_AND_SUBSTITUTIONS = [
    "env git commit -m x",
    "env -u NEOTOMA_ENV git commit -m x",
    "command git commit -m x",
    "exec git commit -m x",
    "nice git commit -m x",
    "timeout 600 git commit -m x",
    "nohup git push --no-verify &",
    "time git status",
    "echo HEAD | xargs git log",
    "find . -name '*.md' -exec git add {} \\;",
    "/usr/bin/git commit -m x",
    "\\git commit --no-verify -m x",
    "/usr/libexec/git-core/git-commit -m x",
    "git-commit -n",
    "\"$GIT\" commit -m x",
    "$GIT commit -n",
    "g=git; $g commit -n",
    "g=gi; ${g}t commit -n",
    "$X push --no-verify",
    "echo $(git commit --no-verify -m x)",
    "echo `git commit -n`",
    "ls $(git rev-parse --git-dir)/index.lock",
    "diff <(git log) <(git log origin/main)",
    "git -C \"$(git rev-parse --show-toplevel)\" commit -m x",
]

# Hook-skipping options on allowlisted subcommands, the original rule.
FORBIDDEN_FLAGS = [
    "git commit --no-verify -m 'bump'",
    "git commit --no-veri -m x",
    "git commit --no-verif -m x",
    "git commit --no-verify=1 -m x",
    "git push --no-verify origin release/v1.2.3",
    "git push origin HEAD --no-verify",
    "git -C /tmp/wt commit --no-verify -m x",
    "git -C \"$WT\" commit --no-verify -m x",
    "git merge --no-verify feature",
    "git commit -n -m x",
    "git commit -an -m x",
    "git commit -nm 'msg'",
    "git commit -m 'msg' -n",
    "git commit -C HEAD~1 -n",
    "git commit -m -n -n",
    "git commit --amend --no-post-rewrite -m x",
    "cd /tmp/wt && git add -A && git commit --no-verify -m x",
    "git add -A; git commit --no-verify -m x",
    "git add -A\ngit commit --no-verify -m x",
    "git commit \\\n  --no-verify -m x",
    "if true; then git commit -n -m x; fi",
    "for i in 1; do git push --no-verify; done",
    "(cd /tmp/wt && git commit -n -m x)",
    "git status # harmless\ngit commit -n -m x",
]


@pytest.mark.parametrize("command", (
    "git -C $WT commit -m x",
    "git -C ${WT} status",
    "git -C $(printf /tmp/wt) status",
    "git -C prefix$WT status",
    "git commit -m $MESSAGE",
    "git commit --message=$MESSAGE",
    "git push origin prefix$REF",
    "git fetch origin $REF",
))
def test_unquoted_git_argument_expansion_refused(command):
    assert_refused(command)


@pytest.mark.parametrize("command", (
    'git -C "$WT" status',
    'git -C "${WT}" commit -m x',
    'git commit -m "$MESSAGE"',
))
def test_quoted_git_argument_expansion_allowed(command):
    assert_allowed(command)

# Input the guard cannot read fully, around git.
UNREADABLE = [
    "git commit --no-verify -m 'oops",
    "git commit -m \"unterminated",
    "git commit -m \"$(cat <<'EOF'\nno closing delimiter",
    "cat <<EOF | bash\ngit commit -n",
]

# Found by the author-side self-review of the allowlist rewrite.
SELF_REVIEW = [
    # wrappers the guard has no name for: git must be the command itself
    "setsid git commit -n -m x",
    "arch -arm64 git commit -m x",
    "ionice git commit --no-verify -m x",
    "chroot / git commit -m x",
    # brace expansion and globs that assemble the command or its options
    "{git,commit,--no-verify}",
    "/usr/bin/gi? commit -n",
    "/usr/bin/gi[t] commit -n",
    "git commit {--no-verify,-m,x}",
    "git {commit,--no-verify} -m x",
    # append-assignments of git variables
    "GIT_DIR+=x git status",
    "GIT_CONFIG_COUNT+=1; git commit -m x",
    "export GIT_CONFIG_PARAMETERS+=x",
    # options that make an allowed subcommand run any program
    "git fetch --upload-pack='git -c core.hooksPath=/dev/null commit -m x' origin",
    "git fetch --upl=x origin",
    "git ls-remote --upload-pack=x origin",
    "git ls-remote -u x origin",
    "git push --receive-pack=x origin main",
    "git push --exec=x origin main",
]

REFUSED = (SHELL_ASSEMBLED + GIT_VARIABLES + CONFIG_WRITES + GLOBAL_OPTIONS
           + ALIASES_AND_UNLISTED + INLINE_PROGRAMS + WRAPPERS_AND_SUBSTITUTIONS
           + FORBIDDEN_FLAGS + UNREADABLE + SELF_REVIEW)


# --- what the agent legitimately runs ---------------------------------------

AGENT_COMMANDS = [
    "git -C \"$WT\" status",
    "git -C \"$WT\" commit -m release",
    "git -C ~/repos/x push origin HEAD:branch",
    "git -C /tmp/wt diff --stat v0.29.1..origin/main",
    "git fetch origin --tags -q && git log --oneline v0.29.1..origin/main && "
    "git diff --stat v0.29.1..origin/main | tail -20",
    "git worktree add -b release/v0.29.2 /tmp/neotoma-release-v0.29.2 origin/main 2>&1 | tail -2",
    "git worktree list | head",
    "cd /tmp/wt && git show --stat --format=%s HEAD | head -12",
    "git push -q -u origin release/v0.29.2 2>&1 | tail -2",
    "git ls-remote origin release/v0.29.2 | head -1; git rev-parse HEAD",
    "git log --format='%h %p %s' -3 2e4d8b558; git rev-list --count origin/main",
    "git tag --sort=-v:refname | head -1",
    "git tag -l 'v0.*'",
    "git tag --contains HEAD",
    "git describe --tags --abbrev=0 origin/main",
    "git branch -a --list '*v0.29.2*'",
    "git merge-base --is-ancestor origin/main HEAD && echo contains-main",
    "git merge --ff-only origin/main && git log --oneline -1",
    "git checkout -q --detach origin/main 2>/dev/null; git status --short | head -3",
    "git switch -c release/v1.2.3",
    "git add package.json package-lock.json docs/releases/in_progress/v1.2.3",
    "git for-each-ref --sort=-creatordate --format='%(creatordate:iso) %(refname)' refs/tags | head -5",
    "git cat-file -t v0.27.0",
    "git fsck --connectivity-only 2>&1 | tail -2",
    "git reflog -5",
    "git --no-pager log -1",
    "git push -n origin main",
    "git commit -m 'chore(release): bump version to v1.2.3 + supplement'",
    "SKIP_TESTS=1 SKIP_TESTS_REASON='suite stalls on host load' git commit -m x",
    "git commit -q -m \"chore(release): bump version to v1.2.3 + supplement\n\n"
    "Co-Authored-By: Claude <noreply@anthropic.com>\" 2>&1 | tail -5; git log --oneline -2",
    "git commit -m \"$(cat <<'EOF'\nchore(release): it's v1.2.3 (git hooks ran)\n\nNever --no-verify.\nEOF\n)\"",
    "git commit -F - <<'EOF'\nrelease notes mentioning git commit --no-verify\nEOF",
    "git commit -m \"$MSG\"",
    "git commit -m x -- --no-verify",
    "git commit -m -n",
    "git commit -uno -m x",
    "git commit --amend --no-edit",
    "git config --get core.hooksPath",
    "git config --get-all core.hooksPath",
    "git config --list",
    "git config --worktree --list 2>&1 | head -5",
    "git config --show-origin --get core.hooksPath",
    "git config get core.hooksPath",
    "git config core.hooksPath",
    "git config --show-origin core.hooksPath",
    "git --version",
]

NON_GIT = [
    "ls -la",
    "echo --no-verify",
    "grep -rn -- --no-verify docs/",
    "rg -n 'no-verify' .",
    "gh pr create --base main --head release/v1.2.3 --title x --body 'the agent used --no-verify'",
    "npm run test -- --no-verify-cache",
    "npm version 1.2.3 --no-git-tag-version",
    "npm run -s release-notes:render -- --tag v1.2.3 --head-ref HEAD --supplement notes.md",
    "cat > notes.md <<'EOF'\nNever run git commit --no-verify.\nEOF",
    "python3 - <<'PY'\nprint('hello')\nPY",
    "python3 - <<'EOF'\nprint(1)\nEOF\ngit diff --stat",
    "git diff | python3 -c 'import sys; print(len(sys.stdin.read()))'",
    "python3 store_release_result.py --version v1.2.3 --status pending_approval",
    "node -e 'console.log(require(\"./package.json\").version)'",
    "\"$PYTHON\" scripts/report.py -n",
    "$EDITOR notes.md",
    "env -iu FOO ls",
    "command -v git",
    "which git",
    "echo 'unterminated",
    "gws gmail +send --to x --subject y --html --body \"$(cat /tmp/release-v1.2.3-email.html)\"",
    "sed -n 1,20p github_release_supplement.md",
    # self-review: a `git-hooks` path, the test command, globs and readers
    "bash scripts/git-hooks/install.sh",
    "for f in a b; do [ -f \"$f\" ] && echo \"$f\"; done",
    "ls *.md",
    "echo git",
    "brew list git",
    # self-review: heredoc text inside a substitution is data, so a later
    # command with an expanded argument is not judged as git-assembled
    "git commit -m \"$(cat <<'EOF'\nuses git hooks\nEOF\n)\" && python3 check.py \"$X\"",
]

SELF_REVIEW_ALLOWED_GIT = [
    "git ls-files | head",
    "git ls-tree HEAD docs/",
    "git remote get-url origin",
    "git show-ref --tags",
    "git symbolic-ref HEAD",
    "git restore --staged package-lock.json",
    "git rm -q docs/releases/in_progress/old.md",
    "git mv a.md b.md",
    "git add docs/*.md",
]
AGENT_COMMANDS += SELF_REVIEW_ALLOWED_GIT

ALLOWED = AGENT_COMMANDS + NON_GIT


@pytest.mark.parametrize("command", REFUSED)
def test_refused(command):
    assert_refused(command)


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command):
    assert_allowed(command)


def test_config_write_naming_hookspath_as_value_is_a_config_write():
    # `user.name` is the key; core.hooksPath is only its value. Still refused,
    # because every config write is, and the reason says so.
    out = assert_refused("git config user.name core.hooksPath")
    assert "config write" in out["permissionDecisionReason"]
    assert "core.hooksPath=" not in out["permissionDecisionReason"].split("\n")[0]


def test_refusal_names_the_sanctioned_path():
    out = assert_refused("git commit --no-verify -m x")
    reason = out["permissionDecisionReason"]
    assert "SKIP_TESTS=1 SKIP_TESTS_REASON=" in reason
    assert "plain, allowlisted git" in reason


def test_unlisted_subcommand_refusal_lists_the_allowlist():
    reason = assert_refused("git ci -m x")["permissionDecisionReason"]
    assert "not on the prepare agent's allowlist" in reason
    assert "commit" in reason and "worktree" in reason


def test_assembled_argument_refusal_says_what_to_change():
    reason = assert_refused("git push origin \"$BRANCH\"")["permissionDecisionReason"]
    assert "$BRANCH" in reason and "literally" in reason


@pytest.mark.parametrize("subcommand", ("commit", "push", "merge"))
@pytest.mark.parametrize("pattern", (
    "--no-ver*", "--no-v?rify", "--no-verif[y]",
    "-*", "-?", "-[n]",
))
def test_globbed_hook_skip_argument_refused(subcommand, pattern):
    assert_refused(f"git {subcommand} {pattern}")


@pytest.mark.parametrize("subcommand", ("commit", "push", "merge"))
def test_glob_after_double_dash_is_pathspec(subcommand):
    assert_allowed(f"git {subcommand} -- --no-ver*")


PROTECTED_PATHS = (
    ".git/config",
    ".GIT/config",
    ".Git/CONFIG",
    ".git/config.worktree",
    ".git/hooks/pre-commit",
    "repo/.git/worktrees/linked/config",
    "repo/.git/worktrees/linked/config.worktree",
    "~/.gitconfig",
    "~/.config/git/config",
    "~/.CONFIG/GIT/config",
    ".husky/pre-commit",
    ".HUSKY/pre-commit",
    "~/.config/husky/init.sh",
)


@pytest.mark.parametrize("path", PROTECTED_PATHS)
@pytest.mark.parametrize("writer", (
    "printf x > {path}",
    "printf x >> {path}",
    "printf x | tee {path}",
    "sed -i s/a/b/ {path}",
    "cp source {path}",
    "mv source {path}",
    "ln -s source {path}",
    "install source {path}",
    "dd if=source of={path}",
))
def test_direct_write_to_hook_or_config_refused(path, writer):
    assert_refused(writer.format(path=path))


@pytest.mark.parametrize("prefix", (
    "SAFE=1", "if", "then", "do", "!", "time", "env", "command",
    "builtin", "exec", "nice", "nohup",
))
@pytest.mark.parametrize("target", (".git/config", ".git/con*", "$TARGET"))
@pytest.mark.parametrize("writer", (
    "tee {target}", "cp source {target}", "mv source {target}",
    "sed -i s/a/b/ {target}", "install source {target}", "cd {target}",
))
def test_prefixed_writers_refuse_protected_targets(prefix, target, writer):
    assert_refused(f"{prefix} {writer.format(target=target)}")


@pytest.mark.parametrize("prefix", (
    "SAFE=1", "if", "then", "do", "!", "time", "env", "command",
    "builtin", "exec", "nice", "nohup",
))
@pytest.mark.parametrize("writer", (
    "tee src/output", "cp source src/output", "mv source src/output",
    "sed -i s/a/b/ src/output", "install source src/output", "cd src",
))
def test_prefixed_writers_allow_ordinary_literal_targets(prefix, writer):
    assert_allowed(f"{prefix} {writer}")


# A wrapper's own options and operands, and wrappers the guard does not list,
# sit between the wrapper and the writer, so prefix stripping alone misses it.
WRAPPED_PREFIXES = (
    "env -i", "env -u NAME", "env -i NAME=1", "nice -n 5", "command -p", "time -p",
    "timeout 5", "flock /tmp/lockfile", "stdbuf -o L", "setsid", "ionice -c 3",
    "chroot /", "sudo -u someone",
)


@pytest.mark.parametrize("prefix", WRAPPED_PREFIXES)
@pytest.mark.parametrize("target", (".git/config", ".git/con*", "$TARGET"))
@pytest.mark.parametrize("writer", (
    "tee {target}", "cp source {target}", "mv source {target}",
    "sed -i s/a/b/ {target}", "install source {target}", "cd {target}",
))
def test_writers_behind_wrapper_options_refuse_protected_targets(prefix, target, writer):
    assert_refused(f"{prefix} {writer.format(target=target)}")


@pytest.mark.parametrize("prefix", WRAPPED_PREFIXES)
@pytest.mark.parametrize("writer", (
    "tee src/output", "cp source src/output", "mv source src/output",
    "sed -i s/a/b/ src/output", "install source src/output", "cd src",
))
def test_writers_behind_wrapper_options_allow_ordinary_targets(prefix, writer):
    assert_allowed(f"{prefix} {writer}")


@pytest.mark.parametrize("name", ("$WRITER", '"$WRITER"', "${WRITER}", "$(which tee)", "`which tee`"))
@pytest.mark.parametrize("target", (".git/config", ".git/hooks/pre-commit", ".husky/pre-commit", ".git"))
def test_assembled_command_name_may_not_take_protected_path(name, target):
    assert_refused(f"{name} {target}")


@pytest.mark.parametrize("name", ("$WRITER", '"$WRITER"'))
def test_assembled_command_name_with_ordinary_path_allowed(name):
    assert_allowed(f"{name} src/output")


@pytest.mark.parametrize("reader", ("cat", "grep x", "head -1", "ls"))
def test_readers_naming_a_writer_word_are_not_refused(reader):
    assert_allowed(f"{reader} tee .git/config")


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_plain_read_of_hook_or_config_allowed(path):
    assert_allowed(f"cat {path}")


@pytest.mark.parametrize("tool", ("Edit", "Write", "MultiEdit"))
@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_file_tool_refuses_hook_or_config_path(tool, path):
    payload = {"tool_name": tool, "tool_input": {"file_path": path},
               "hook_event_name": "PreToolUse"}
    proc = run_hook(payload)
    assert proc.returncode == 2, (tool, path, proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("tool", ("Edit", "Write", "MultiEdit"))
def test_file_tool_allows_ordinary_repository_file(tool):
    payload = {"tool_name": tool, "tool_input": {"file_path": "src/release.py"},
               "hook_event_name": "PreToolUse"}
    proc = run_hook(payload)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


@pytest.mark.parametrize("command", (
    "cp config .git/",
    "cp -r hooks .git",
    "mv config .git/",
    "install config .git",
    "ln -s config .git/",
    "rsync config .git/",
    "cp -r git ~/.config/",
    "cp -r husky ~/.config",
    "cp pre-commit .husky/",
))
def test_copy_into_protected_directory_refused(command):
    assert_refused(command)


@pytest.mark.parametrize("writer", ("cp source {target}", "mv source {target}",
                                     "tee {target}", "printf x > {target}"))
@pytest.mark.parametrize("target", (
    ".git/ho*", '"ordinary*"', '"ordinary["', '"$TARGET"', "$TARGET", "$(printf .git/config)",
    "${TARGET}",
))
def test_shell_assembled_write_target_refused(writer, target):
    assert_refused(writer.format(target=target))


@pytest.mark.parametrize("command", (
    "cd .git/ho* && printf x > config",
    'cd "$TARGET" && printf x > config',
    "pushd $(printf .git/hooks)",
))
def test_shell_assembled_working_directory_refused(command):
    assert_refused(command)


@pytest.mark.parametrize("writer", ("cp source {target}", "mv source {target}",
                                     "tee {target}", "printf x > {target}"))
def test_single_quoted_literal_glob_is_not_expanded(writer):
    assert_allowed(writer.format(target="'ordinary*literal'"))


def test_literal_non_protected_write_allowed():
    assert_allowed("cp source ordinary.txt")


@pytest.mark.parametrize("command", (
    "printf x > .GIT/config",
    "printf x > .HUSKY/pre-commit",
    "printf x > .git/worktrees/linked/config.worktree",
    "cp source .git/worktrees/linked/config.worktree",
    "cp config.worktree .git/worktrees/linked/",
))
def test_case_folded_and_worktree_config_writes_refused(command):
    assert_refused(command)


def test_symlink_alias_to_protected_file_refused(tmp_path):
    config = tmp_path / ".git" / "config"
    config.parent.mkdir()
    config.write_text("synthetic")
    alias = tmp_path / "ordinary-file"
    alias.symlink_to(config)
    assert_refused(f"printf x > {alias}")
    assert_refused(f"cp source {alias}")
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(alias)},
               "hook_event_name": "PreToolUse"}
    assert run_hook(payload).returncode == 2


def test_symlink_directory_alias_and_parent_traversal_refused(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    alias = tmp_path / "ordinary-dir"
    alias.symlink_to(git_dir, target_is_directory=True)
    assert_refused(f"printf x > {alias}/config")
    assert_refused(f"cd {alias} && printf x > config")
    nested = git_dir / "nested"
    nested.mkdir()
    assert_refused(f"printf x > {alias}/nested/../config")


@pytest.mark.parametrize("flag", ("-s", "-sf", "-fs", "--symbolic"))
def test_symlink_to_protected_path_refused(flag, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    target = git_dir / "config"
    link = tmp_path / "ordinary-link"
    assert_refused(f"ln {flag} {target} {link}")
    assert_refused(f"ln {flag} .git/config {link}")


def test_unresolvable_path_beneath_protected_directory_refused(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    loop = git_dir / "loop"
    loop.symlink_to(loop)
    assert_refused(f"printf x > {loop}/config")


@pytest.mark.parametrize("name", (
    "HOME", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG", "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_DIR", "GIT_COMMON_DIR",
))
@pytest.mark.parametrize("prefix", ("", "env "))
def test_alternate_git_config_environment_refused(name, prefix):
    assert_refused(f"{prefix}{name}=/tmp/synthetic git commit -m x")


def test_exported_home_before_git_refused():
    assert_refused("export HOME=/tmp/synthetic; git commit -m x")


@pytest.mark.parametrize("command", (
    "git commit -m x", "git status", "printf x > config.txt",
))
def test_ordinary_git_and_unrelated_config_write_allowed(command):
    assert_allowed(command)


@pytest.mark.parametrize("command", (
    "cd .git && printf x > config",
    "cd ~/.config && mkdir git",
    "cd repo/.git/worktrees/linked",
    "pushd .husky",
    "cd ~ && cd .config",
))
def test_entering_protected_directory_refused(command):
    assert_refused(command)


@pytest.mark.parametrize("command", (
    "cp notes.md docs/releases/",
    "cd docs/releases && printf x > notes.md",
    "mv draft.md docs/",
))
def test_ordinary_copy_and_cd_allowed(command):
    assert_allowed(command)


def test_non_bash_tool_ignored():
    proc = run_hook({"tool_name": "Edit", "tool_input": {"command": "git commit --no-verify"}})
    assert proc.returncode == 0 and proc.stdout.strip() == ""


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null"])
def test_fail_open_on_bad_input_logs_loudly(raw):
    proc = run_hook(raw)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert "FAIL-OPEN" in proc.stderr


def test_fail_open_when_command_is_missing():
    proc = run_hook({"tool_name": "Bash", "tool_input": {}})
    assert proc.returncode == 0
    assert "FAIL-OPEN" in proc.stderr


def test_no_override_env_is_honoured():
    env = {**os.environ, "ATELES_ALLOW_NO_VERIFY": "1"}
    for command in ("git commit --no-verify -m x",
                    "ATELES_ALLOW_NO_VERIFY=1 git commit --no-verify -m x"):
        proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(bash(command)),
                              capture_output=True, text=True, timeout=30, env=env)
        assert proc.returncode == 2, command


def test_allowlist_is_data():
    sys.path.insert(0, str(HOOK.parent))
    import git_hook_bypass_guard as guard  # noqa: PLC0415
    assert {"status", "commit", "push", "worktree", "config", "tag"} <= guard.ALLOWED_SUBCOMMANDS
    assert not {"stash", "hook", "rebase", "am", "pull"} & guard.ALLOWED_SUBCOMMANDS


# The real-harness check lives in execution/daemons/phoenicurus-release/verify_guard_live.py:
# the repository's test setup strips credentials, so the Claude CLI cannot run under pytest.


# Same-class forms found after the split build: a program piped into a shell,
# old-style tar bundles, and programs run from a script file.
@pytest.mark.parametrize("command", (
    'echo "cp s .git/config" | bash', "echo 'tee .git/hooks/pre-commit' | sh",
    'printf "cp s .husky/pre-commit" | zsh', "tar xf a.tar -C .git", "tar xzf a.tgz",
    "awk 'BEGIN{print 1 > \".git/config\"}'",
))
def test_piped_programs_and_old_style_tar_refused(command):
    assert_refused(command)


@pytest.mark.parametrize("command", (
    'echo "hello" | bash', "tar tf a.tar", "tar xf a.tar -C /tmp/out",
    "awk '{print $1}' notes.txt", "awk '{print}' .git/config",
))
def test_piped_programs_and_tar_without_protected_targets_allowed(command):
    assert_allowed(command)


@pytest.mark.parametrize("runner", ("bash", "sh", "zsh -e", "bash -x"))
@pytest.mark.parametrize("body", ("cp s .git/config", "tee .git/hooks/pre-commit",
                                  "git commit --no-verify -m x"))
def test_shell_script_files_are_judged_like_sourced_files(tmp_path, runner, body):
    script = tmp_path / "w.sh"
    script.write_text(body + "\n")
    assert_refused(f"{runner} {script}")


@pytest.mark.parametrize("runner", ("bash", "sh"))
def test_shell_script_file_with_ordinary_writes_allowed(tmp_path, runner):
    script = tmp_path / "ok.sh"
    script.write_text("cp s src/out\necho done\n")
    assert_allowed(f"{runner} {script}")


def test_assembled_shell_script_refused_and_missing_one_allowed(tmp_path):
    assert_refused("bash $SCRIPT")
    # bash cannot run a script it cannot read either, so nothing runs.
    assert_allowed(f"bash {tmp_path / 'missing.sh'}")


@pytest.mark.parametrize("body", ("open('.git/config', 'w')", "x = '--no-verify'",
                                  "subprocess.run(['git', '-c', 'core.hooksPath=/tmp'])"))
def test_interpreter_script_naming_protected_path_or_bypass_refused(tmp_path, body):
    script = tmp_path / "w.py"
    script.write_text(body + "\n")
    assert_refused(f"python3 {script}")


def test_interpreter_script_without_protected_names_allowed(tmp_path):
    script = tmp_path / "ok.py"
    script.write_text("print('hello')\n")
    assert_allowed(f"python3 {script}")
    assert_allowed("python3 -m pytest -q")

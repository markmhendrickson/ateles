"""Tests for the sibling-repo shared-main-clone guard hook.

Three decision surfaces, each with its own test class because each has a
distinct failure mode: the git-dir-vs-common-dir path walk
(shared_main_clone_for), the mutation/read-only regex pair, and the
-C/--git-dir/cd precedence in check_bash. End-to-end cases drive main()
through the real deny()/exit-code path, plus the fail-open contract.
"""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sibling_repo_worktree_guard as guard


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hi\n")
    _git(["add", "README.md"], path)
    _git(["commit", "-q", "-m", "init"], path)
    return path


# ---------------------------------------------------------------------------
# shared_main_clone_for() — real git fixtures, not mocked subprocess. The
# point of this function is comparing two real path strings git emits with
# different relativity, so a mock would only assert the mock's own value.
# ---------------------------------------------------------------------------
class TestSharedMainCloneFor:
    def test_main_clone(self, tmp_path):
        main = _init_repo(tmp_path / "main-clone")
        top, is_shared = guard.shared_main_clone_for(main)
        assert top == main.resolve()
        assert is_shared is True

    def test_linked_worktree(self, tmp_path):
        main = _init_repo(tmp_path / "main-clone")
        wt = tmp_path / "linked-wt"
        _git(["worktree", "add", str(wt), "-b", "wt-branch"], main)
        top, is_shared = guard.shared_main_clone_for(wt)
        assert top == wt.resolve()
        assert is_shared is False

    def test_new_file_in_new_dir(self, tmp_path):
        main = _init_repo(tmp_path / "main-clone")
        new_path = main / "docs" / "infrastructure" / "new.md"
        top, is_shared = guard.shared_main_clone_for(new_path)
        assert top == main.resolve()
        assert is_shared is True

    def test_path_outside_any_repo(self, tmp_path):
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        top, is_shared = guard.shared_main_clone_for(outside)
        assert top is None
        assert is_shared is False


# ---------------------------------------------------------------------------
# _GIT_MUTATION_RE / _GIT_READONLY_HINT — pure regex, no subprocess.
# ---------------------------------------------------------------------------
class TestMutationRegex:
    @pytest.mark.parametrize(
        "command,expected",
        [
            ("git commit -m 'x'", True),
            ("git checkout main", True),
            ("git checkout -b feature", True),
            ("git switch -c feature", True),
            ("git switch main", True),
            ("git reset --hard HEAD~1", True),
            ("git merge origin/main", True),
            ("git rebase main", True),
            ("git cherry-pick abc123", True),
            ("git revert abc123", True),
            ("git apply patch.diff", True),
            ("git am patch.mbox", True),
            ("git branch -f main abc123", True),
            ("git branch -D old-branch", True),
            ("git branch -m old new", True),
            ("git stash pop", True),
            ("git stash apply", True),
            ("git stash drop", True),
            ("git clean -fd", True),
            ("git rm --cached secret.txt", True),
            ("git push origin main", True),
            ("git tag -d v1.0", True),
            ("git restore file.txt", True),
            # The PR's specific hazard case — the incident trigger.
            ("git checkout -b foo", True),
            ("git checkout main", True),
            # Negative cases.
            ("git status", False),
            ("git log --oneline", False),
            ("git diff", False),
            ("git rev-parse --show-toplevel", False),
            ("git worktree list", False),
            ("git show HEAD", False),
            ("git remote -v", False),
            ("git fetch origin", False),
            ("git ls-files", False),
            ("git branch", False),
            ("echo hello", False),
        ],
    )
    def test_mutation_detection(self, command, expected):
        assert bool(guard._GIT_MUTATION_RE.search(command)) is expected

    def test_readonly_hint_matches_status_and_log(self):
        assert guard._GIT_READONLY_HINT.search("git status")
        assert guard._GIT_READONLY_HINT.search("git log --oneline")
        assert guard._GIT_READONLY_HINT.search("git worktree list")

    def test_readonly_hint_does_not_match_commit(self):
        assert not guard._GIT_READONLY_HINT.search("git commit -m 'x'")


# ---------------------------------------------------------------------------
# check_bash() — -C / --git-dir / cd precedence, incl. the Loxia caveat.
# ---------------------------------------------------------------------------
class TestCheckBashPrecedence:
    def test_dash_c_resolves_target_ignoring_cwd(self, tmp_path, monkeypatch):
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)
        reason = guard.check_bash(f"git -C {sibling} commit -m x", ateles.resolve())
        assert reason is not None

    def test_git_dir_flag_resolves_via_parent_of_dot_git(self, tmp_path, monkeypatch):
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)
        reason = guard.check_bash(
            f"git --git-dir={sibling}/.git commit -m x", ateles.resolve()
        )
        assert reason is not None

    def test_cd_prefix_resolves_target(self, tmp_path, monkeypatch):
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)
        reason = guard.check_bash(f"cd {sibling} && git commit -m x", ateles.resolve())
        assert reason is not None

    def test_bare_command_falls_through_to_cwd(self, tmp_path, monkeypatch):
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(sibling)
        reason = guard.check_bash("git commit -m x", ateles.resolve())
        assert reason is not None

    def test_dash_c_wins_over_conflicting_git_dir(self, tmp_path, monkeypatch):
        # `git -C <shared> --git-dir=<other>/.git commit` — -C must win per the
        # documented precedence order; easy to get backwards without a test.
        sibling = _init_repo(tmp_path / "sibling")
        other = _init_repo(tmp_path / "other")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)
        reason = guard.check_bash(
            f"git -C {sibling} --git-dir={other}/.git commit -m x", ateles.resolve()
        )
        assert reason is not None
        assert str(sibling.resolve()) in reason

    def test_persistent_cwd_across_calls_not_caught(self, tmp_path, monkeypatch):
        # Documented limitation: a PRIOR command's `cd` into a sibling clone is
        # invisible to a later single-command hook invocation. Pinned so a future
        # "fix" is a deliberate, visible diff rather than a silent behavior change.
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)  # hook's cwd; prior `cd` in another call is invisible
        reason = guard.check_bash("git commit -m x", ateles.resolve())
        assert reason is None

    def test_worktree_add_prefix_does_not_shield_trailing_mutation(
        self, tmp_path, monkeypatch
    ):
        # qa lens finding 1: a `worktree add` segment earlier in a compound
        # command must not blanket-exempt a separate mutating segment later
        # in the same command.
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)
        reason = guard.check_bash(
            f"git worktree add {tmp_path}/x origin/main && "
            f"git -C {sibling} reset --hard",
            ateles.resolve(),
        )
        assert reason is not None
        assert str(sibling.resolve()) in reason

    def test_cd_takes_last_target_not_first(self, tmp_path, monkeypatch):
        # qa lens finding 2: m_cd must resolve to the cd immediately
        # preceding the mutating invocation, not the first cd in the string.
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)
        reason = guard.check_bash(
            f"cd /tmp && cd {sibling} && git commit -m x", ateles.resolve()
        )
        assert reason is not None
        assert str(sibling.resolve()) in reason

    def test_cd_last_wins_inverse_does_not_false_positive(self, tmp_path, monkeypatch):
        # Inverse of the above: once the LAST cd wins, moving back out of the
        # sibling clone before the mutation must not false-positive.
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)
        reason = guard.check_bash(
            f"cd {sibling} && cd /tmp && git commit -m x", ateles.resolve()
        )
        assert reason is None

    def test_dash_c_scoped_to_mutating_segment_not_first_occurrence(
        self, tmp_path, monkeypatch
    ):
        # qa lens finding 3: -C must resolve from the SAME segment as the
        # mutating verb, not the first -C anywhere in the compound command.
        sibling = _init_repo(tmp_path / "sibling")
        other = _init_repo(tmp_path / "other")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)
        reason = guard.check_bash(
            f"git -C {other} status && git -C {sibling} commit -m x",
            ateles.resolve(),
        )
        assert reason is not None
        assert str(sibling.resolve()) in reason

    def test_worktree_add_text_in_commit_message_does_not_shield_mutation(
        self, tmp_path, monkeypatch
    ):
        # Self-review finding: the remedy check must key off an ACTUAL `git
        # worktree add` invocation, not a bare substring match — a mutating
        # command whose commit message merely contains the text "worktree
        # add" must still be caught.
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)
        reason = guard.check_bash(
            f'git -C {sibling} commit -am "worktree add later"', ateles.resolve()
        )
        assert reason is not None
        assert str(sibling.resolve()) in reason

    def test_newline_separated_worktree_add_does_not_shield_trailing_mutation(
        self, tmp_path, monkeypatch
    ):
        # Self-review finding: a multi-line Bash block is just as much a
        # compound command as one joined with &&/;, and must be split the
        # same way — a `worktree add` on one line must not shield a
        # mutating invocation on a later line.
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.chdir(ateles)
        reason = guard.check_bash(
            f"git worktree add {tmp_path}/x origin/main\n"
            f"git -C {sibling} reset --hard",
            ateles.resolve(),
        )
        assert reason is not None
        assert str(sibling.resolve()) in reason


# ---------------------------------------------------------------------------
# End-to-end — drive main() through the real deny()/exit-code path.
# ---------------------------------------------------------------------------
def _run_main(monkeypatch, capsys, event: dict, cwd=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    if cwd is not None:
        monkeypatch.chdir(cwd)
    monkeypatch.delenv("ATELES_ALLOW_SHARED_REPO_WRITES", raising=False)
    code = guard.main()
    out = capsys.readouterr().out
    return code, out


class TestEndToEnd:
    def test_shared_clone_edit_blocks(self, tmp_path, monkeypatch, capsys):
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ateles))
        event = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(sibling / "README.md")},
        }
        code, out = _run_main(monkeypatch, capsys, event, cwd=ateles)
        assert code == 2
        payload = json.loads(out)
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_dedicated_worktree_edit_allows(self, tmp_path, monkeypatch, capsys):
        main = _init_repo(tmp_path / "sibling")
        wt = tmp_path / "sibling-wt"
        _git(["worktree", "add", str(wt), "-b", "wt-branch"], main)
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ateles))
        event = {"tool_name": "Edit", "tool_input": {"file_path": str(wt / "README.md")}}
        code, _ = _run_main(monkeypatch, capsys, event, cwd=ateles)
        assert code == 0

    def test_ateles_self_edit_allows(self, tmp_path, monkeypatch, capsys):
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ateles))
        event = {"tool_name": "Edit", "tool_input": {"file_path": str(ateles / "README.md")}}
        code, _ = _run_main(monkeypatch, capsys, event, cwd=ateles)
        assert code == 0

    def test_ateles_self_bash_allows(self, tmp_path, monkeypatch, capsys):
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ateles))
        event = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
        code, _ = _run_main(monkeypatch, capsys, event, cwd=ateles)
        assert code == 0

    def test_new_file_in_new_dir_blocks(self, tmp_path, monkeypatch, capsys):
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ateles))
        new_path = sibling / "docs" / "infrastructure" / "new.md"
        event = {"tool_name": "Write", "tool_input": {"file_path": str(new_path)}}
        code, out = _run_main(monkeypatch, capsys, event, cwd=ateles)
        assert code == 2
        payload = json.loads(out)
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_readonly_git_bash_allows(self, tmp_path, monkeypatch, capsys):
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ateles))
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": f"git -C {sibling} status"},
        }
        code, _ = _run_main(monkeypatch, capsys, event, cwd=ateles)
        assert code == 0

    def test_worktree_add_remedy_bash_allows(self, tmp_path, monkeypatch, capsys):
        sibling = _init_repo(tmp_path / "sibling")
        ateles = _init_repo(tmp_path / "ateles")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ateles))
        event = {
            "tool_name": "Bash",
            "tool_input": {
                "command": f"git -C {sibling} worktree add {tmp_path}/sibling-wt-x origin/main"
            },
        }
        code, _ = _run_main(monkeypatch, capsys, event, cwd=ateles)
        assert code == 0


class TestFailOpen:
    def test_main_returns_zero_when_subprocess_raises(self, tmp_path, monkeypatch, capsys):
        ateles = tmp_path / "ateles"
        ateles.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ateles))
        monkeypatch.chdir(ateles)
        monkeypatch.delenv("ATELES_ALLOW_SHARED_REPO_WRITES", raising=False)

        def _raise(*a, **k):
            raise OSError("git not found")

        monkeypatch.setattr(guard, "_run", _raise)
        event = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        code = guard.main()
        assert code == 0


# ---------------------------------------------------------------------------
# guidance() message content — durable issue reference + resolved path.
# ---------------------------------------------------------------------------
class TestGuidanceMessage:
    def test_guidance_cites_issue_246(self, tmp_path):
        top = tmp_path / "sibling"
        top.mkdir()
        msg = guard.guidance(top)
        assert "ateles#246" in msg

    def test_guidance_includes_resolved_path(self, tmp_path):
        top = tmp_path / "sibling"
        top.mkdir()
        msg = guard.guidance(top)
        assert str(top.resolve()) in msg


# ---------------------------------------------------------------------------
# Fail-open visibility — internal errors must log, never block, never leak
# command text/credentials, and be distinguishable from each other.
# ---------------------------------------------------------------------------
class TestFailOpenLogging:
    def test_shared_main_clone_error_logs_before_returning_none(self, tmp_path, monkeypatch):
        logged = []
        monkeypatch.setattr(guard, "log", lambda msg: logged.append(msg))
        monkeypatch.setattr(
            guard, "_run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        )
        top, is_shared = guard.shared_main_clone_for(tmp_path / "somewhere")
        assert (top, is_shared) == (None, False)
        assert any("could not evaluate git state" in m for m in logged)

    def test_git_not_found_logs_distinct_message(self, tmp_path, monkeypatch):
        logged = []
        monkeypatch.setattr(guard, "log", lambda msg: logged.append(msg))
        monkeypatch.setattr(
            guard, "_run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
        )
        ateles = tmp_path / "ateles"
        ateles.mkdir()
        reason = guard.check_bash("git commit -m x", ateles.resolve())
        assert reason is None  # fail-open: never blocks
        assert any("git not found" in m for m in logged)
        # Must be distinguishable from the generic "could not evaluate" message —
        # an operator scanning logs needs to tell "git missing" from "unexpected error".
        assert not any("could not evaluate git state" in m for m in logged)

    def test_log_call_failure_does_not_block(self, tmp_path, monkeypatch):
        def _raise_log(msg):
            raise RuntimeError("log broke")

        monkeypatch.setattr(guard, "log", _raise_log)
        monkeypatch.setattr(
            guard, "_run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        )
        top, is_shared = guard.shared_main_clone_for(tmp_path / "x")
        assert (top, is_shared) == (None, False)  # still fails open despite log() raising

    def test_main_logs_on_fail_open(self, tmp_path, monkeypatch, capsys):
        logged = []
        monkeypatch.setattr(guard, "log", lambda msg: logged.append(msg))
        ateles = tmp_path / "ateles"
        ateles.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ateles))
        monkeypatch.chdir(ateles)
        monkeypatch.delenv("ATELES_ALLOW_SHARED_REPO_WRITES", raising=False)
        monkeypatch.setattr(
            guard, "_run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        )
        event = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        code = guard.main()
        assert code == 0
        assert logged  # something was logged, not silent


def test_readme_mentions_override_var():
    readme = Path(__file__).resolve().parent / "README.md"
    assert "ATELES_ALLOW_SHARED_REPO_WRITES" in readme.read_text()

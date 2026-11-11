"""Unit tests for the shared `_command_segments` helper (ateles#1265).

Isolated from the two guards that consume it: these assert the helper's own
contract (heredoc stripping, leader classification, segment splitting, line
continuation folding) independently of how `sibling_repo_worktree_guard.py`
and `git_stash_guard.py` happen to use it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _command_segments as cs


# ---------------------------------------------------------------------------
# strip_heredoc_bodies
# ---------------------------------------------------------------------------
class TestStripHeredocBodies:
    def test_quoted_delimiter(self):
        command = "python3 - <<'PY'\nprint('git merge origin/main')\nPY"
        result = cs.strip_heredoc_bodies(command)
        assert "git merge" not in result
        assert result.startswith("python3 - <<'PY'\n")
        assert result.endswith("\nPY")

    def test_unquoted_delimiter(self):
        command = "python3 - <<PY\nprint('git merge origin/main')\nPY"
        result = cs.strip_heredoc_bodies(command)
        assert "git merge" not in result
        assert result.startswith("python3 - <<PY\n")
        assert result.endswith("\nPY")

    def test_dash_variant(self):
        command = "cat <<-EOF\n\tnever git stash\nEOF"
        result = cs.strip_heredoc_bodies(command)
        assert "stash" not in result
        assert result.startswith("cat <<-EOF\n")
        assert result.endswith("\nEOF")

    def test_multiple_in_one_command(self):
        command = (
            "cat <<'A'\ngit stash first body\nA\n"
            "echo between\n"
            "cat <<'B'\ngit merge second body\nB"
        )
        result = cs.strip_heredoc_bodies(command)
        assert "first body" not in result
        assert "second body" not in result
        assert "echo between" in result
        assert result.count("A") == 2
        assert result.count("B") == 2

    def test_marker_text_inside_other_body(self):
        # The body of heredoc A contains a line that reads like heredoc B's
        # own marker text ("B") — not real nesting (shell has none), so it
        # must not terminate A early or leak into a phantom body.
        command = "cat <<'A'\nthis body mentions B here\nA"
        result = cs.strip_heredoc_bodies(command)
        assert "this body mentions B here" not in result
        assert result == "cat <<'A'\nA"

    def test_no_heredoc_present_is_noop(self):
        command = "git -C ~/repo status --short && echo done < input.txt"
        assert cs.strip_heredoc_bodies(command) == command

    def test_heredoc_then_real_command_after_untouched(self):
        command = "cat <<'EOF'\nnote\nEOF\ngit -C ~/repo merge foo"
        result = cs.strip_heredoc_bodies(command)
        assert "note" not in result
        assert "git -C ~/repo merge foo" in result

    def test_unterminated_heredoc_does_not_drop_trailing_real_command(self):
        """An unterminated/malformed heredoc (typo'd or missing closing tag)
        must not silently erase everything after the opener — that would
        blind a caller to a real mutating command trailing it in the same
        Bash call. Fail SAFE (keep the text) rather than fail-open (drop
        it)."""
        command = "cat <<EOF\nsome text, no closing tag\ngit -C ~/repo merge foo"
        result = cs.strip_heredoc_bodies(command)
        assert "git -C ~/repo merge foo" in result


# ---------------------------------------------------------------------------
# is_text_bearing_leader
# ---------------------------------------------------------------------------
class TestIsTextBearingLeader:
    def test_base_set(self):
        for segment in [
            "git commit -m 'x'",
            "git tag -a v1.0 -m note",
            "git notes add -m note",
            "echo hello",
            "printf '%s' x",
            "grep -rn foo .",
            "rg foo .",
            "gh pr create --body x",
            "gh issue comment 1 --body x",
            "gh release create v1.0",
        ]:
            assert cs.is_text_bearing_leader(segment), segment

    def test_extra_leaders_union(self):
        # "gh publish" is not in the base set until extra_leaders adds it.
        assert not cs.is_text_bearing_leader("gh publish v1.0")
        assert cs.is_text_bearing_leader(
            "gh publish v1.0", extra_leaders=r"gh\s+publish"
        )
        # Base set still applies with extra_leaders supplied.
        assert cs.is_text_bearing_leader("echo hi", extra_leaders=r"gh\s+publish")

    def test_rejects_interpreters(self):
        for segment in [
            "python3 -c \"print('x')\"",
            "python -c \"print('x')\"",
            "node -e \"console.log('x')\"",
            "cat <<'EOF'",
        ]:
            assert not cs.is_text_bearing_leader(segment), segment
            assert not cs.is_text_bearing_leader(
                segment, extra_leaders=r"gh\s+release"
            ), segment


# ---------------------------------------------------------------------------
# join_line_continuations
# ---------------------------------------------------------------------------
class TestJoinLineContinuations:
    def test_unchanged_behavior(self):
        cases = [
            ("git stash \\\n pop", "git stash   pop"),
            ("gws gmail users messages \\\n send", "gws gmail users messages   send"),
            ("no continuation here", "no continuation here"),
            ("a \\\nb \\\nc", "a  b  c"),
            ("no-space\\\nvariant", "no-space variant"),
        ]
        for command, expected in cases:
            assert cs.join_line_continuations(command) == expected

    def test_byte_for_byte_parity_with_gmail_send_gate(self):
        """Regression guard for the 'behavior-preserving move' claim: the
        extracted helper must match the pre-extraction gmail_send_gate.py
        implementation on a shared fixture set."""
        import gmail_send_gate as gate

        fixtures = [
            "git stash \\\n pop",
            "gws gmail users messages \\\n send",
            "no continuation here",
            "a \\\nb \\\nc",
            "cmd1 && \\\ncmd2",
            "",
        ]
        for fixture in fixtures:
            assert cs.join_line_continuations(fixture) == gate._join_line_continuations(
                fixture
            )


# ---------------------------------------------------------------------------
# split_segments
# ---------------------------------------------------------------------------
class TestSplitSegmentsSuperset:
    def test_all_separators_split(self):
        command = "a && b || c ; d\ne | f & g"
        segments = cs.split_segments(command)
        assert segments == ["a", "b", "c", "d", "e", "f", "g"]

    def test_empty_segments_dropped(self):
        command = "a && && b"
        segments = cs.split_segments(command)
        assert segments == ["a", "b"]

"""Regression eval: every active Python invocation in scripts/lint.sh calls
`python3`, never bare `python` (ateles#929 / PR #944).

PR #944 changed all 16 bare `python` invocations in `scripts/lint.sh` to
`python3` because bare `python` is absent from the PATH it was reported
failing on — every one of those 16 lines was failing with
`command not found`, not only the decision-78 line the issue named. The qa
lens on that PR found the fix itself unguarded: nothing observed the surface,
so a future edit reintroducing bare `python` (a copy-pasted new linter step,
a merge that reverts one line, an editor "helpfully" normalizing `python3`
back to `python`) would pass CI silently.

This file closes that gap two ways:

* `test_real_file_has_no_bare_python_invocation` walks every line of the real
  `scripts/lint.sh` and asserts no ACTIVE command invokes bare `python`. This
  is the regression guard.
* `test_planted_bare_python_on_decision_78_line_is_caught` plants the exact
  defect the issue named — reverting the decision-78 invocation
  (`python3 execution/scripts/check_foundation_decision_78.py`) back to bare
  `python` in an in-memory copy of the file — and asserts the same scanner
  flags that line, by line number and content, proving the check can fail on
  the thing it watches (`principles.md` #4).

Discrimination is the point, not detection: the scanner must not flag
`python3` (which contains the substring `python`), a `#`-comment mentioning
`python`, an `echo` string mentioning "python", the shell variable name
`PYTHON=python3`, or an unrelated identifier like `mypython` — it must match
only an actual invocation (the token used as a command word: at line start,
after a pipe/`&&`/`;`/`(`, after `xargs`, or as the final segment of a path
like `/usr/bin/python`), not any occurrence of the substring.
`TestDiscrimination` proves each of these directly.

Scope, stated plainly: this is a line-based scanner, not a shell parser. It
covers every invocation shape `scripts/lint.sh` actually uses today (a direct
`python3 <script>` command, and `xargs python3 <script>` after a pipe) and
the shapes most likely to regress (reverting either back to bare `python`).
It does not track multi-line heredoc/quote state and would not catch bare
`python` buried inside a heredoc body — `scripts/lint.sh` contains no
heredocs today, so this is a documented gap, not a silent one.

Stdlib + pytest only. No Neotoma, no network, no subprocess execution of
lint.sh itself (ShellCheck/ruff/yamllint are not guaranteed present in every
test environment; this reads the script's text, it does not run it).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LINT_SH = REPO_ROOT / "scripts" / "lint.sh"

# A bare `python` token: not `python3` (or `python3.11`, etc.), not part of a
# longer identifier like `mypython` or `PYTHON_PATH`, but matches a bare
# invocation reached via a path like `/usr/bin/python`.
BARE_PYTHON_TOKEN = re.compile(r"(?<![\w.])python(?![\w.])")

# Prefixes (last non-space token before the match) that mark `python` as the
# command word about to be invoked, e.g. `xargs python foo.py`.
_COMMAND_WORD_PREFIXES = ("xargs", "exec")
_COMMAND_WORD_PREFIX_CHARS = ("|", "&&", ";", "(")


def _is_command_word(line: str, match_start: int) -> bool:
    """True if the `python` token at `match_start` is being invoked as a
    command, not merely mentioned as a word in prose or a value in an
    assignment.

    Covers: start of line/statement, immediately after a path separator
    (`/usr/bin/python`), and after a pipe/operator/`xargs`/`exec`.
    """
    if match_start > 0 and line[match_start - 1] == "/":
        return True
    prefix = line[:match_start].rstrip()
    if prefix == "":
        return True
    if prefix.endswith(_COMMAND_WORD_PREFIX_CHARS):
        return True
    last_word = prefix.split()[-1] if prefix.split() else ""
    return last_word in _COMMAND_WORD_PREFIXES

# The line this issue named specifically (ateles#929): the decision-78
# checker invocation. Matched by content, not just line number, so the
# planted-negative stays meaningful even if the file is reordered.
DECISION_78_INVOCATION = re.compile(r"execution/scripts/check_foundation_decision_78\.py")


def _strip_comment(line: str) -> str:
    """Return the code portion of a shell line, before any unquoted `#`.

    Every comment in lint.sh is either a full-line `#...` or has no `#`
    inside quotes, so a simple quote-tracking scan is sufficient — this does
    not need to handle escaped quotes or `$()` nesting, neither of which
    scripts/lint.sh uses around a `#`.
    """
    in_squote = False
    in_dquote = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_dquote:
            in_squote = not in_squote
        elif ch == '"' and not in_squote:
            in_dquote = not in_dquote
        elif ch == "#" and not in_squote and not in_dquote:
            return line[:i]
    return line


def _is_echo_only(code: str) -> bool:
    """True if the (comment-stripped) code is only an `echo`/`printf` call.

    `echo "  - Checking foundation decision 78 is ruled..."` mentions
    "decision 78" in prose but invokes no interpreter; it must never be
    flagged as a `python` invocation just because a future echo string
    happens to mention the word "python".
    """
    stripped = code.strip()
    return stripped.startswith("echo ") or stripped.startswith("echo\t") or stripped == "echo"


def find_bare_python_invocations(text: str) -> list[tuple[int, str]]:
    """Return (1-indexed line number, line text) for every ACTIVE line that
    invokes bare `python` rather than `python3`.

    "Active" excludes full-line comments and echo/printf-only lines — a
    mention of the word "python" there is prose, not an invocation.
    """
    hits: list[tuple[int, str]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if raw_line.strip().startswith("#"):
            continue
        code = _strip_comment(raw_line)
        if not code.strip():
            continue
        if _is_echo_only(code):
            continue
        for m in BARE_PYTHON_TOKEN.finditer(code):
            if _is_command_word(code, m.start()):
                hits.append((lineno, raw_line))
                break
    return hits


class TestRealFileHasNoBarePythonInvocation:
    """The regression guard: scripts/lint.sh, as committed, calls python3."""

    def test_real_file_has_no_bare_python_invocation(self) -> None:
        assert LINT_SH.is_file(), f"{LINT_SH} not found — has it moved?"
        text = LINT_SH.read_text(encoding="utf-8")
        hits = find_bare_python_invocations(text)
        assert not hits, (
            "scripts/lint.sh invokes bare `python` on line(s) "
            f"{[n for n, _ in hits]} — this machine's PATH has no bare "
            "`python` (only `python3`), so these invocations fail with "
            "`command not found` (ateles#929). Lines:\n"
            + "\n".join(f"  {n}: {line.strip()}" for n, line in hits)
        )

    def test_real_file_still_has_the_decision_78_invocation(self) -> None:
        """Sanity: the line this issue named is still present and via python3.

        Guards against the regression eval passing only because the
        invocation it is meant to watch was deleted rather than fixed.
        """
        text = LINT_SH.read_text(encoding="utf-8")
        matches = [
            line
            for line in text.splitlines()
            if DECISION_78_INVOCATION.search(line) and not line.strip().startswith("#")
        ]
        assert matches, "no active line invokes check_foundation_decision_78.py at all"
        for line in matches:
            assert BARE_PYTHON_TOKEN.search(line) is None or "python3" in line, (
                f"decision-78 invocation line does not clearly use python3: {line!r}"
            )
            assert re.search(r"(?<![\w.])python3(?![\w.])", line), (
                f"decision-78 invocation line has no python3 token: {line!r}"
            )


class TestPlantedNegative:
    """Proof this check can fail on the thing it watches (principles.md #4)."""

    def test_planted_bare_python_on_decision_78_line_is_caught(self) -> None:
        text = LINT_SH.read_text(encoding="utf-8")
        lines = text.splitlines()

        target_idx = None
        for i, line in enumerate(lines):
            if DECISION_78_INVOCATION.search(line) and not line.strip().startswith("#"):
                target_idx = i
                break
        assert target_idx is not None, (
            "setup failure: no active decision-78 invocation line found to plant into"
        )

        # Plant the exact defect the issue reported: python3 -> python on
        # that one line, nothing else touched.
        original_line = lines[target_idx]
        assert re.search(r"(?<![\w.])python3(?![\w.])", original_line), (
            f"setup failure: target line has no python3 token to revert: {original_line!r}"
        )
        planted_line = re.sub(r"(?<![\w.])python3(?![\w.])", "python", original_line)
        assert planted_line != original_line, "planting produced no change — regex mismatch"

        planted_lines = list(lines)
        planted_lines[target_idx] = planted_line
        planted_text = "\n".join(planted_lines) + "\n"

        # RED: the planted defect is caught, naming the right line.
        hits = find_bare_python_invocations(planted_text)
        hit_lines = {n for n, _ in hits}
        assert (target_idx + 1) in hit_lines, (
            f"planted bare-python regression on line {target_idx + 1} was NOT caught; "
            f"hits were {hit_lines}. The scanner is not discriminating enough."
        )

        # GREEN: restoring the original text clears the finding.
        restored_hits = find_bare_python_invocations(text)
        assert not restored_hits, (
            f"real scripts/lint.sh unexpectedly flagged: {restored_hits} "
            "— restore check failed, meaning RED above may be a false positive "
            "rather than a real signal."
        )


class TestDiscrimination:
    """The scanner must match an invocation, not any occurrence of "python"."""

    def test_python3_is_never_flagged(self) -> None:
        assert not find_bare_python_invocations("python3 execution/scripts/foo.py\n")

    def test_python3_with_minor_version_is_never_flagged(self) -> None:
        assert not find_bare_python_invocations("python3.11 -m pytest\n")

    def test_full_line_comment_mentioning_python_is_never_flagged(self) -> None:
        assert not find_bare_python_invocations(
            "# run python execution/scripts/foo.py by hand if this ever regresses\n"
        )

    def test_echo_string_mentioning_python_is_never_flagged(self) -> None:
        assert not find_bare_python_invocations(
            'echo "Install with: pip install pre-commit; needs python on PATH"\n'
        )

    def test_shell_variable_named_python_is_never_flagged(self) -> None:
        assert not find_bare_python_invocations("PYTHON=python3\n")

    def test_identifier_containing_python_is_never_flagged(self) -> None:
        assert not find_bare_python_invocations("mypython execution/scripts/foo.py\n")
        assert not find_bare_python_invocations("PYTHONPATH=. python3 foo.py\n")

    def test_indented_prose_mentioning_python_is_not_flagged(self) -> None:
        # A line that mentions "python" as a word but never as a command —
        # not at line start, not after a pipe/xargs/exec, not path-adjacent —
        # must not be treated as an invocation.
        assert not find_bare_python_invocations("    this script needs python installed\n")

    def test_bare_python_invocation_is_flagged(self) -> None:
        hits = find_bare_python_invocations("python execution/scripts/foo.py || true\n")
        assert len(hits) == 1
        assert hits[0][0] == 1

    def test_bare_python_via_absolute_path_is_flagged(self) -> None:
        hits = find_bare_python_invocations("/usr/bin/python execution/scripts/foo.py\n")
        assert len(hits) == 1

    def test_bare_python_in_pipeline_is_flagged(self) -> None:
        hits = find_bare_python_invocations(
            "find . -name '*.py' | xargs python scripts/linters/check_file_naming.py\n"
        )
        assert len(hits) == 1

    def test_inline_comment_after_real_code_still_checks_the_code_part(self) -> None:
        # Code before the '#' must still be checked even though a trailing
        # comment is stripped — this must not become a way to silence a real
        # invocation by appending a comment.
        hits = find_bare_python_invocations("python foo.py  # uses python\n")
        assert len(hits) == 1

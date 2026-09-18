"""Effect test: a planted mirror line fails the committed lint binding.

The `--check` unit test never reads scripts/lint.sh, so it stays green if
`|| true` is put back on the invocation. This file runs the committed text.

The `if` that increments ERRORS exits 0 on its own (`ERRORS=$((ERRORS + 1))`
succeeds). The tally from `if [ $ERRORS -eq 0 ]` through `exit 1` is what
makes a failed check a failed lint. Both are copied out of the file and
executed; neither is retyped.

`python3` on that bash PATH is a wrapper. Only
`execution/scripts/render_agent_docs.py --check` runs the planted-line
scenario and exits with main()'s status. Any other argv is the real
interpreter. A wrapper that always exits 1 would not test a planted line.

Do not run ./scripts/lint.sh. Lines 16–23 exit when pre-commit is on PATH,
before this check.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

import render_agent_docs  # noqa: E402

LINT_SH = _REPO_ROOT / "scripts" / "lint.sh"
_INVOCATION = "python3 execution/scripts/render_agent_docs.py --check"
_DRIFT_ERROR = "ERROR: agent-doc mirrors drifted from Neotoma."
_FIX = (
    "Fix: python3 execution/scripts/render_agent_docs.py  (never hand-edit the mirrors)"
)

_ONE_AGENT = {
    "name": "ateles",
    "status": "active",
    "tier": "core",
    "prompt_markdown": "operational prompt",
}


def _strip_comment(line: str) -> str:
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


def _invocation_code_lines(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        code = _strip_comment(line)
        if _INVOCATION in code:
            found.append(code)
    return found


def mirror_check_disarms(text: str) -> list[str]:
    """Active invocation lines that swallow the check with `|| true`."""
    return [code for code in _invocation_code_lines(text) if "|| true" in code]


def _extract_binding(text: str) -> str:
    """The committed mirror block, from its echo through the drift case.

    Anchored on the `--check` command, not on `if !` (after `|| true` returns
    there may be no `if`). Stops before the parent `else` that skips the
    check when NEOTOMA_BASE_URL is unset, and before the tool_allowlist
    command above it.
    """
    lines = text.splitlines()
    idx = None
    for i, line in enumerate(lines):
        if _INVOCATION in _strip_comment(line):
            idx = i
            break
    if idx is None:
        raise AssertionError(f"{_INVOCATION} not found in scripts/lint.sh")

    start = idx
    while start > 0:
        prev = lines[start - 1]
        if not prev.strip() or prev.lstrip().startswith("#"):
            break
        if "validate_tool_allowlist" in prev:
            break
        if prev.strip().startswith("echo ") and "agent-doc mirrors" not in prev:
            break
        start -= 1

    end = idx
    while end + 1 < len(lines):
        nxt = lines[end + 1]
        stripped = nxt.strip()
        if not stripped or stripped == "else" or stripped.startswith("else "):
            break
        end += 1
    return "\n".join(lines[start : end + 1])


def _extract_tally(text: str) -> str:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "if [ $ERRORS -eq 0 ]; then":
            start = i
            break
    if start is None:
        raise AssertionError("lint tally `if [ $ERRORS -eq 0 ]` not found")
    end = start
    while end < len(lines) and lines[end].strip() != "fi":
        end += 1
    if end >= len(lines):
        raise AssertionError("lint tally has no closing fi")
    return "\n".join(lines[start : end + 1])


def run_check_scenario(mode: str) -> int:
    """What the PATH wrapper runs for `--check`. Exit status is main()'s."""
    if mode == "unreachable":
        sys.stderr.write("Neotoma unreachable after 5 tries: simulated\n")
        return 1

    root = Path(tempfile.mkdtemp())
    render_agent_docs.REPO_ROOT = root
    render_agent_docs.AGENTS_DOC_DIR = root / "docs" / "agents"
    render_agent_docs.SKILLS_DIR = root / ".claude" / "skills"
    agents = [] if mode == "zero" else [dict(_ONE_AGENT)]
    render_agent_docs.fetch_agents = lambda _base, _token: agents
    render_agent_docs._load_env = lambda: ("http://example.invalid", "token")
    for path, content in render_agent_docs._targets(agents).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        text = content if content.endswith("\n") else content + "\n"
        path.write_text(text)
    if mode == "planted":
        skill = render_agent_docs.SKILLS_DIR / "ateles" / "SKILL.md"
        skill.write_text(skill.read_text() + "planted-extra-line\n")
    sys.argv = ["render_agent_docs.py", "--check"]
    return render_agent_docs.main()


def _run_lint_binding(
    lint_text: str, mode: str, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    binding = _extract_binding(lint_text)
    tally = _extract_tally(lint_text)
    # Run the committed text unchanged. Do not strip `|| true` first: that
    # would keep this green after the disarm returns. The tally, not this
    # assert, is what goes red.
    assert "ERRORS=$((ERRORS + 1))" in binding
    assert _DRIFT_ERROR in binding
    assert _FIX in binding
    assert "validate_tool_allowlist" not in binding

    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "python3"
    wrapper.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "execution/scripts/render_agent_docs.py" ] && [ "$2" = "--check" ]; then\n'
        f"  exec {sys.executable} {Path(__file__).resolve()}\n"
        "fi\n"
        f'exec {sys.executable} "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)

    script = tmp_path / "binding.sh"
    script.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        "ERRORS=0\n"
        f"export MIRROR_CHECK_SIM=1\n"
        f"export MIRROR_CHECK_MODE={mode}\n"
        f"export PATH={bindir}:$PATH\n"
        f"{binding}\n"
        f"{tally}\n"
    )
    return subprocess.run(
        ["bash", str(script)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class TestLintAgentMirrorBinding:
    def test_planted_line_fails_lint_and_names_fix(self, tmp_path: Path) -> None:
        proc = _run_lint_binding(LINT_SH.read_text(), "planted", tmp_path)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert _DRIFT_ERROR in proc.stdout
        assert _FIX in proc.stdout
        fix_lines = [
            line.strip() for line in proc.stdout.splitlines() if "Fix:" in line
        ]
        assert fix_lines
        assert all(line == _FIX for line in fix_lines)
        assert all("--check" not in line for line in fix_lines)

    def test_zero_rows_exits_nonzero_without_drift_pair(self, tmp_path: Path) -> None:
        proc = _run_lint_binding(LINT_SH.read_text(), "zero", tmp_path)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert _DRIFT_ERROR not in proc.stdout
        assert "Fix:" not in proc.stdout
        assert "0 agent_definition rows" in proc.stdout

    def test_unreachable_exits_nonzero_without_drift_pair(self, tmp_path: Path) -> None:
        proc = _run_lint_binding(LINT_SH.read_text(), "unreachable", tmp_path)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert _DRIFT_ERROR not in proc.stdout
        assert "Fix:" not in proc.stdout
        assert "Neotoma unreachable" in proc.stderr

    def test_real_file_has_no_or_true_on_invocation(self) -> None:
        assert mirror_check_disarms(LINT_SH.read_text()) == []

    def test_planted_or_true_is_caught(self) -> None:
        planted = LINT_SH.read_text().replace(_INVOCATION, _INVOCATION + " || true", 1)
        disarms = mirror_check_disarms(planted)
        assert len(disarms) == 1
        assert "|| true" in disarms[0]


if __name__ == "__main__" and os.environ.get("MIRROR_CHECK_SIM") == "1":
    raise SystemExit(run_check_scenario(os.environ.get("MIRROR_CHECK_MODE", "planted")))

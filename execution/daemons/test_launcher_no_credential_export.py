"""Regression guard for ateles#657.

Daemon environments load credentials from a SOPS-materialized dotenv
(~/.config/neotoma/.env) via lib/daemon_runtime/__init__.py, which reads the
file and sets os.environ keys IN-PROCESS at import time. That value is then
visible only inside that process's own memory — never to `ps eww <pid>`,
which reflects only what was present at exec/launch time.

The Anthus launchd wrapper (execution/daemons/anthus/run_anthus_launchd.sh)
used to instead `export` every key from the same dotenv into its own shell
process *before* exec'ing python, which put every materialized secret
(including a GitHub PAT and a Telegram bot token, not just the
CLAUDE_CODE_OAUTH_TOKEN the wrapper existed for) into the exec'd process's
environment block, readable via `ps eww <pid>` by any local process running
as the operator. Every sibling daemon avoided this by construction: their
plists invoke the venv python interpreter directly, with no shell wrapper
in between to export anything.

This test asserts the general property directly: no daemon launcher script
(anything under execution/daemons/ that launchd/systemd would exec, or a
wrapper such a plist points at) contains a shell `export` of a dotenv-style
KEY=VALUE pair. It is a static/content check, not a live-process check (no
`ps` involved — see the issue and the PR body for why proving the live
`ps eww` effect on the real operator LaunchAgent is not something CI can
do), but it is a check that can and did fail: see the test's own docstring
history / PR body for the red run against the pre-fix file.
"""

from __future__ import annotations

import re
from pathlib import Path

_DAEMONS_DIR = Path(__file__).resolve().parent

# Matches any shell `export` of a KEY=VALUE assignment, whether the key is a
# literal identifier (`export FOO=bar`) or a shell-variable interpolation
# (`export "$_key=$_val"`, `export "$key=$value"`) — the actual defect line
# in ateles#657 was the latter shape: a loop variable holding each dotenv
# key, not a literal name, so a check that only matched a literal identifier
# before `=` would silently pass on the real bug (verified: an earlier draft
# of this regex matched zero occurrences against the pre-fix file). Also
# matches the bare `export LITERAL=...$var...` form used for a single
# promoted value. Deliberately does NOT match a bare `export SOME_VAR` with
# no `=` at all, which merely marks an already-set variable for export and
# carries no dotenv-sourced value by itself.
_EXPORT_ASSIGNMENT_RE = re.compile(
    r"""^\s*export\s+["']?(?:[A-Za-z_][A-Za-z0-9_]*|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)\s*="""
)

# Launcher-shaped files: anything a plist's ProgramArguments could point at,
# or a plist itself. Concrete *.plist files are gitignored (operator-absolute
# paths), so this also covers the *.tmpl templates that are committed.
_LAUNCHER_GLOBS = ("*.sh", "*.plist", "*.plist.tmpl")


def _iter_launcher_files():
    for path in sorted(_DAEMONS_DIR.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if (
            name.endswith(".plist.tmpl")
            or name.endswith(".plist")
            or name.endswith(".sh")
        ):
            yield path


def _exported_lines(path: Path) -> list[str]:
    """Return each line (stripped) that exports a KEY=VALUE assignment,
    literal or variable-interpolated. Reporting the whole line rather than
    just a key name is deliberate: a dynamic export (`export "$key=$value"`)
    has no literal key to name, and the line itself is the useful evidence.
    """
    lines = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _EXPORT_ASSIGNMENT_RE.match(line):
            lines.append(line.strip())
    return lines


def test_sweep_finds_at_least_one_launcher_file():
    """Guard the guard: if the sweep finds zero files, the glob/path is
    broken, not the world secure. (Foundation principles invariant: a zero
    is a claim about the instrument before it is a claim about the world.)
    """
    files = list(_iter_launcher_files())
    assert files, (
        f"no launcher-shaped files (*.sh, *.plist, *.plist.tmpl) found under "
        f"{_DAEMONS_DIR} — the sweep glob is broken, this is not evidence "
        f"of a clean sweep"
    )


def test_no_daemon_launcher_exports_dotenv_style_variable():
    """No file under execution/daemons/ that shapes how a daemon is launched
    (a *.sh script, or a *.plist / *.plist.tmpl launchd job) may `export` a
    KEY=VALUE assignment. That export is exactly the mechanism that made
    materialized secrets readable via `ps eww <pid>` for the Anthus outlier
    (ateles#657) while every sibling daemon — which never exports anything,
    relying instead on lib/daemon_runtime/__init__.py's in-process load —
    stayed unaffected.
    """
    offenders: dict[str, list[str]] = {}
    for path in _iter_launcher_files():
        lines = _exported_lines(path)
        if lines:
            offenders[str(path.relative_to(_DAEMONS_DIR.parent.parent))] = lines

    assert not offenders, (
        "the following daemon launcher files export dotenv-style variables "
        "into their process environment, which makes them readable via "
        f"`ps eww <pid>` (ateles#657): {offenders}"
    )

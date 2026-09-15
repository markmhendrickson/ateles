"""
Plist regression: the five #515 daemons must point at ateles-rc-src.

Run: pytest execution/daemons/test_daemon_plist_deploy_paths.py -v
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent

PLISTS = [
    REPO / "execution/daemons/cotinga/com.ateles.cotinga.plist",
    REPO / "execution/daemons/cyphorhinus/com.ateles.cyphorhinus.plist",
    REPO / "execution/daemons/piculet/com.ateles.piculet.plist",
    REPO / "execution/daemons/sylvia/com.ateles.sylvia.plist",
    REPO
    / "execution/daemons/phoenicurus-release/com.ateles.phoenicurus-prepare.plist.tmpl",
]


@pytest.mark.parametrize("plist_path", PLISTS, ids=[p.name for p in PLISTS])
def test_program_arguments_use_ateles_rc_src(plist_path: Path):
    data = plistlib.loads(plist_path.read_bytes())
    args = data["ProgramArguments"]
    assert len(args) >= 2
    python, script = args[0], args[1]
    for entry in (python, script):
        assert "ateles-rc-src" in entry, f"{plist_path.name}: {entry}"
        assert (
            "repos/ateles" not in entry
        ), f"{plist_path.name}: still shared clone: {entry}"

    # Same root for interpreter and script (no half-repoint).
    def _root(p: str) -> str:
        marker = "/ateles-rc-src/"
        i = p.find(marker)
        assert i >= 0, p
        return p[: i + len("/ateles-rc-src")]

    assert _root(python) == _root(script)
    wd = data.get("WorkingDirectory")
    if wd:
        assert "ateles-rc-src" in wd
        assert "repos/ateles" not in wd


def test_no_ateles_daemon_doctor_in_identity_fix_block():
    from lib.daemon_runtime.checkout_identity import FIX_BLOCK, format_fatal_message
    from lib.daemon_runtime.checkout_identity import IdentityReport

    msg = format_fatal_message(
        "cotinga",
        IdentityReport(
            state="wrong_tree",
            actual_root="/x/repos/ateles",
            expected_root="/x/ateles-rc-src",
            branch="main",
        ),
        why="why",
        plist_label="com.ateles.cotinga",
    )
    assert "isolate_daemons_to_rc_src.sh --apply" in msg
    assert "isolate_daemons_to_rc_src.sh --apply" in FIX_BLOCK
    assert "ateles-daemon-doctor" not in msg


def test_cutover_effect_suites_are_bound_to_required_ci():
    workflow = (REPO / ".github/workflows/ateles-tests.yml").read_text()
    for trigger in (
        '"execution/daemons/test_*.py"',
        '"execution/daemons/cotinga/**"',
        '"execution/daemons/cyphorhinus/**"',
        '"execution/daemons/sylvia/**"',
    ):
        assert trigger in workflow

    command = " ".join(workflow.split())
    assert (
        "python -m pytest execution/daemons/test_*.py "
        "execution/daemons/cotinga/ execution/daemons/cyphorhinus/ "
        "execution/daemons/sylvia/ -q"
    ) in command

"""
The release daemon must not run from the shared main clone.

`publish.py` refuses to tag and ship atop a dirty working tree — correctly,
since publishing whatever is in the tree would put unreviewed changes into a
release. But `~/repos/neotoma` is where interactive sessions do their work, so
it is dirty most of the time.

Pointing the release daemon there makes every release contingent on nobody
having uncommitted files. Observed 2026-08-10: an operator-approved v0.21.5
publish refused with

    publish failed: Neotoma working tree is dirty (non-release files).

listing 12 modified files that belonged to an unrelated session on an unrelated
branch. The approval was correct, the guard was correct, and the release still
did not happen.

`~/neotoma-rc-src` exists for this, mirroring how the ateles daemons already run
from `~/ateles-rc-src`. These tests pin the default so it cannot quietly revert
to the shared clone — a regression that would be invisible until the next
release blocked on someone else's work.

Run: pytest execution/daemons/phoenicurus-release/test_release_checkout_isolation.py -v
"""

from __future__ import annotations

import importlib
import plistlib
import re
import sys
from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parent
PLIST_TMPL = DAEMON_DIR / "com.ateles.phoenicurus-prepare.plist.tmpl"
INSTALL_SH = DAEMON_DIR / "install.sh"

SHARED_CLONE = "repos/neotoma"
RELEASE_CHECKOUT = "neotoma-rc-src"


def _reload_prepare():
    """Re-import prepare.py so its module-level NEOTOMA_REPO_ROOT is
    recomputed against whatever env/monkeypatch is active for this test."""
    if str(DAEMON_DIR) not in sys.path:
        sys.path.insert(0, str(DAEMON_DIR))
    import prepare  # noqa: PLC0415

    return importlib.reload(prepare)


def test_plist_template_points_at_the_release_checkout():
    """The daemon's own env is what actually decides; assert on the template."""
    data = plistlib.loads(PLIST_TMPL.read_bytes())
    root = data["EnvironmentVariables"]["NEOTOMA_REPO_ROOT"]

    assert RELEASE_CHECKOUT in root, (
        f"NEOTOMA_REPO_ROOT is {root!r}. The release daemon must not run from the "
        "shared main clone — publish blocks whenever an interactive session "
        "leaves it dirty (v0.21.5, 2026-08-10)."
    )
    assert not root.endswith(SHARED_CLONE), f"still pointed at the shared clone: {root}"


def test_plist_template_points_ateles_program_arguments_at_rc_src():
    """Ateles ProgramArguments must use ateles-rc-src, not repos/ateles (#515)."""
    data = plistlib.loads(PLIST_TMPL.read_bytes())
    args = data["ProgramArguments"]
    for entry in args:
        if "ateles" in entry:
            assert "ateles-rc-src" in entry, entry
            assert "repos/ateles" not in entry, entry


def test_plist_template_is_valid():
    """A malformed plist fails at launchd load time, long after the edit."""
    data = plistlib.loads(PLIST_TMPL.read_bytes())
    assert data["EnvironmentVariables"]["NEOTOMA_REPO_ROOT"]
    assert data.get("Label")


def test_installer_prefers_the_release_checkout():
    """
    The installer resolves the default independently of the plist, so it needs
    its own assertion — otherwise the two can drift and whichever runs last wins.
    """
    src = INSTALL_SH.read_text()
    assert RELEASE_CHECKOUT in src, (
        "install.sh does not prefer the release checkout; a fresh install would "
        "silently point the daemon back at the shared clone"
    )


def test_installer_still_falls_back():
    """
    A host without the release checkout must still install. Failing closed here
    would make the daemon un-installable on a fresh machine to prevent a
    condition that only matters at release time.
    """
    src = INSTALL_SH.read_text()
    assert re.search(r"NEOTOMA_REPO_ROOT:-\$HOME/repos/neotoma", src), (
        "the fallback to the shared clone was removed; a host without "
        "~/neotoma-rc-src can no longer install the daemon"
    )


def test_installer_warns_when_using_the_shared_clone():
    """
    Falling back is acceptable; falling back silently is not — the operator
    should learn about the hazard at install time, not when a release blocks.
    """
    src = INSTALL_SH.read_text()
    assert "shared clone" in src, (
        "no warning when the daemon is installed against the shared clone"
    )


def test_prepare_default_prefers_release_checkout_when_present(monkeypatch, tmp_path):
    """
    prepare.py's OWN default — not just the plist and installer — must prefer
    ~/neotoma-rc-src. The plist only governs the scheduled Mon-Thu run; the
    merge-triggered path (swarm_dispatch._handle_push_main) spawns
    `prepare.py --on-merge` inheriting the Apis daemon's environment, which has
    no NEOTOMA_REPO_ROOT set at all. Without a correct default in prepare.py
    itself, every merge-triggered run silently fell back to the shared clone —
    observed as repeated "could not determine checkout state (no upstream
    branch configured)" log lines against ~/repos/neotoma on every merge,
    while the scheduled sweep (whose plist DOES set the var) was pointed
    correctly the whole time.
    """
    monkeypatch.delenv("NEOTOMA_REPO_ROOT", raising=False)
    fake_home = tmp_path
    fake_rc_src = fake_home / "neotoma-rc-src"
    fake_rc_src.mkdir()
    (fake_rc_src / "package.json").write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    prepare = _reload_prepare()
    try:
        assert prepare.NEOTOMA_REPO_ROOT == fake_rc_src
    finally:
        # Leave the module in a state that reflects the real environment for
        # any test that runs after this one in the same process.
        _reload_prepare()


def test_prepare_default_falls_back_to_shared_clone_when_rc_src_absent(
    monkeypatch, tmp_path
):
    """A host without ~/neotoma-rc-src must still resolve to SOMETHING runnable."""
    monkeypatch.delenv("NEOTOMA_REPO_ROOT", raising=False)
    fake_home = tmp_path  # no neotoma-rc-src created under it
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    prepare = _reload_prepare()
    try:
        assert prepare.NEOTOMA_REPO_ROOT == fake_home / "repos" / "neotoma"
    finally:
        _reload_prepare()


def test_prepare_default_respects_explicit_env_override(monkeypatch, tmp_path):
    """An explicit NEOTOMA_REPO_ROOT (however set) must always win over both
    defaults — this is what the phoenicurus-prepare plist itself relies on."""
    explicit = tmp_path / "somewhere-else"
    explicit.mkdir()
    monkeypatch.setenv("NEOTOMA_REPO_ROOT", str(explicit))

    prepare = _reload_prepare()
    try:
        assert prepare.NEOTOMA_REPO_ROOT == explicit
    finally:
        monkeypatch.delenv("NEOTOMA_REPO_ROOT", raising=False)
        _reload_prepare()

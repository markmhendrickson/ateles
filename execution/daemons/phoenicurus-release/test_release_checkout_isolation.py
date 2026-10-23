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

    install.sh no longer re-types the fallback literal inline (ateles#1293
    follow-up: it now derives NEOTOMA_REPO_ROOT from release_checkout_root.py,
    the same module prepare.py imports) — it still names the shared clone as
    its own bash-only fallback for when python3 or that module is
    unavailable, which is what this now checks for.
    """
    src = INSTALL_SH.read_text()
    assert re.search(r'NEOTOMA_REPO_ROOT="\$HOME/repos/neotoma"', src), (
        "the bash-only fallback to the shared clone was removed; a host "
        "without python3 (or release_checkout_root.py) can no longer "
        "resolve a default and would be un-installable"
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


# ── Single source of truth (ateles#1293 follow-up, arch REQUEST_CHANGES) ────
#
# The "prefer ~/neotoma-rc-src, else fall back to ~/repos/neotoma" policy used
# to be typed out independently in install.sh (bash) and prepare.py's own
# `_default_neotoma_repo_root()` (Python) — two copies of the same policy
# that would drift the next time only one of them was updated. Both now
# derive from `release_checkout_root.default_neotoma_repo_root()`: prepare.py
# imports it directly, and install.sh shells out to the same module's CLI.
# These tests pin that relationship so a future edit to one without the
# other is caught here rather than at the next release.


def _release_checkout_root_module():
    if str(DAEMON_DIR) not in sys.path:
        sys.path.insert(0, str(DAEMON_DIR))
    import release_checkout_root  # noqa: PLC0415

    return importlib.reload(release_checkout_root)


def test_prepare_imports_the_shared_module_rather_than_a_local_copy():
    """prepare.py must not re-define its own default-resolution function —
    it has to be release_checkout_root's OWN function, imported by reference,
    not a look-alike redefinition that could silently diverge again.

    Reloading release_checkout_root AFTER prepare (rather than before) would
    leave prepare holding a reference to the pre-reload module object even
    though both are "the same" module by name — comparing by
    __module__/__qualname__ instead of object identity avoids that ordering
    trap entirely, which matters because this module is reloaded
    independently by several tests in this file.
    """
    prepare = _reload_prepare()
    fn = prepare._default_neotoma_repo_root
    assert fn.__module__ == "release_checkout_root"
    assert fn.__qualname__ == "default_neotoma_repo_root"


def test_install_sh_shells_out_to_the_shared_module_not_a_retyped_literal():
    """install.sh's primary path must invoke release_checkout_root.py's own
    CLI rather than re-typing the policy inline — this is the mechanism that
    keeps bash and Python from drifting, not just a naming convention."""
    src = INSTALL_SH.read_text()
    assert "release_checkout_root.py" in src, (
        "install.sh no longer derives its default from release_checkout_root.py "
        "— the two copies of the policy can drift again"
    )
    assert re.search(
        r'python3\s+"\$SCRIPT_DIR/release_checkout_root\.py"\s+print', src
    ), "install.sh does not invoke release_checkout_root.py's print CLI"


def test_release_checkout_root_module_has_no_third_party_or_lib_imports():
    """This module must stay import-safe from a bare `python3 -c`/CLI
    invocation with no repo dependencies installed — install.sh runs as a
    preflight check that may run before pip/npm install. Unlike prepare.py
    (which imports lib.daemon_runtime.logging_setup and mutates LOG_DIR at
    import time), this module must import cleanly with nothing beyond the
    standard library."""
    import ast

    module_path = DAEMON_DIR / "release_checkout_root.py"
    tree = ast.parse(module_path.read_text())
    stdlib_only = {"sys", "pathlib", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top in stdlib_only, f"non-stdlib import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                assert top in stdlib_only, f"non-stdlib import: {node.module}"


def test_release_checkout_root_cli_matches_the_function_it_wraps(monkeypatch, tmp_path):
    """The `print` CLI subprocess-callable from bash must report exactly what
    the importable function returns — the two are the same code path in
    production (Path.home()), so this pins them staying in lock-step."""
    rcr = _release_checkout_root_module()
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = rcr.main(["print"])
    assert code == 0
    assert buf.getvalue().strip() == str(rcr.default_neotoma_repo_root())


def test_release_checkout_root_prefers_rc_src_when_present(tmp_path):
    fake_rc_src = tmp_path / "neotoma-rc-src"
    fake_rc_src.mkdir()
    (fake_rc_src / "package.json").write_text("{}")

    rcr = _release_checkout_root_module()
    assert rcr.default_neotoma_repo_root(home=tmp_path) == fake_rc_src


def test_release_checkout_root_falls_back_when_rc_src_absent(tmp_path):
    rcr = _release_checkout_root_module()
    assert (
        rcr.default_neotoma_repo_root(home=tmp_path) == tmp_path / "repos" / "neotoma"
    )


def test_release_checkout_root_cli_rejects_unknown_args():
    rcr = _release_checkout_root_module()
    assert rcr.main([]) == 2
    assert rcr.main(["bogus"]) == 2

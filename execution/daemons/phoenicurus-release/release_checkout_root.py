#!/usr/bin/env python3
"""
Single source of truth for the Phoenicurus-Release default checkout root.

ateles#1293 follow-up (Neotoma task ent_1becc947268fa966d65254b0, arch
REQUEST_CHANGES on #1293): the "prefer ~/neotoma-rc-src (marker:
package.json), else fall back to ~/repos/neotoma" policy was duplicated
independently in `install.sh` (bash) and `prepare.py`'s own
`_default_neotoma_repo_root()` (Python) — two copies of the same
default-resolution policy that would drift the next time only one of them
was updated.

This module is the ONE place that decides the policy. `prepare.py` imports
`default_neotoma_repo_root()` directly rather than keeping its own copy.
`install.sh` cannot `import` Python, so it shells out to this module's own
CLI (`python3 release_checkout_root.py print`) instead of re-typing the
policy in bash — the same pattern `tag_utils.py` already uses for
release-tag logic the GitHub Actions workflow needs from bash.

Deliberately dependency-free (stdlib only, no `lib.*` imports): `install.sh`
runs as a preflight check, potentially before any Python dependencies are
installed, so this module must import cleanly with nothing beyond the
standard library — unlike `prepare.py`, which already depends on
`lib.daemon_runtime.logging_setup` and mutates `LOG_DIR` at import time and
so is unsafe to import standalone from a shell preflight script.

Usage:
  python3 release_checkout_root.py print   # prints the resolved default path
"""

from __future__ import annotations

import sys
from pathlib import Path

# The marker file that identifies a real checkout (as opposed to an empty or
# half-created directory). Kept as a named constant, not inlined, so both
# this module's own logic and its tests refer to the same thing.
CHECKOUT_MARKER = "package.json"

# The dedicated release checkout, preferred whenever it looks real. Release
# checkouts are pinned relative to the operator's home directory, mirroring
# how the ateles daemons already run from ~/ateles-rc-src.
RC_SRC_DIRNAME = "neotoma-rc-src"

# The shared clone interactive sessions work from — dirty most of the time,
# so publish.py (which refuses to tag atop a dirty tree) blocks whenever a
# release is pointed here and someone else's session has left it dirty
# (v0.21.5, 2026-08-10). Kept as the fallback so a fresh install without the
# dedicated checkout still works.
SHARED_CLONE_RELATIVE = ("repos", "neotoma")


def default_neotoma_repo_root(home: Path | None = None) -> Path:
    """
    Fall back to the dedicated release checkout, never the shared main clone.

    Prefers ``~/neotoma-rc-src`` when it looks like a real checkout (its
    ``package.json`` exists), since that is where releases are meant to be
    cut from. Only when the dedicated checkout is absent does this fall back
    to the shared clone, so a fresh install still works.

    `home` is injectable for tests; production callers always omit it and get
    ``Path.home()``.
    """
    root = home if home is not None else Path.home()
    rc_src = root / RC_SRC_DIRNAME
    if (rc_src / CHECKOUT_MARKER).exists():
        return rc_src
    return root.joinpath(*SHARED_CLONE_RELATIVE)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args != ["print"]:
        print("usage: release_checkout_root.py print", file=sys.stderr)
        return 2
    print(str(default_neotoma_repo_root()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

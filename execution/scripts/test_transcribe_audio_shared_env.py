"""The transcription script resolves its settings from one shared, host-level
file, so it runs from a fresh worktree or runtime checkout with no hand-made
``.env`` or ``config.py``, and a missing or unusable setting fails loudly.

Each test runs the script's module in a SUBPROCESS from a pristine copy of the
tracked source (no ``.env``, no ``config.py``: the state of a new worktree) under
a temporary HOME. Every value is synthetic.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PASS_THROUGH = ("PATH", "LANG", "LC_ALL", "TMPDIR")

PROBE = (
    "import sys; sys.path.insert(0, 'execution/scripts');"
    "import transcribe_audio as t; ok, why = t._neotoma_auth_preflight();"
    "print('OK' if ok else 'REFUSED'); print(why)"
)


@pytest.fixture()
def fresh_checkout(tmp_path):
    root = tmp_path / "wt"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", "config.py")
    for rel in ("lib", "ateles", "execution/scripts"):
        shutil.copytree(REPO / rel, root / rel, ignore=ignore)
    assert not (root / ".env").exists()
    return root


def _run(root: Path, home: Path, extra: dict | None = None):
    import os as _os

    child = {k: v for k, v in _os.environ.items() if k in PASS_THROUGH}
    child["HOME"] = str(home)
    child["DATA_DIR"] = str(home / "data")
    child.update(extra or {})
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=root,
        env=child,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _shared(home: Path, text: str) -> Path:
    path = home / ".config" / "neotoma" / ".env"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_fresh_worktree_resolves_settings_from_the_shared_file(fresh_checkout, tmp_path):
    home = tmp_path / "home"
    _shared(home, "NEOTOMA_BEARER_TOKEN=synthetic-token\n")
    done = _run(fresh_checkout, home)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "OK", done.stdout + done.stderr


def test_missing_setting_names_the_setting_and_the_file(fresh_checkout, tmp_path):
    home = tmp_path / "home"
    _shared(home, "SOMETHING_ELSE=1\n")
    done = _run(fresh_checkout, home)
    out = done.stdout + done.stderr
    assert "REFUSED" in out
    assert "NEOTOMA_BEARER_TOKEN" in out
    assert str(home / ".config" / "neotoma" / ".env") in out
    assert "no default" in out.lower()


def test_absent_shared_file_is_named_not_defaulted(fresh_checkout, tmp_path):
    done = _run(fresh_checkout, tmp_path / "empty-home")
    out = done.stdout + done.stderr
    assert "REFUSED" in out and "does not exist" in out


def test_malformed_shared_file_stops_the_run_naming_file_and_line(
    fresh_checkout, tmp_path
):
    home = tmp_path / "home"
    path = _shared(home, "OK_NAME=1\nthis line is junk synthetic-secret-text\n")
    done = _run(fresh_checkout, home)
    assert done.returncode != 0
    assert str(path) in done.stderr and "line 2" in done.stderr
    assert "synthetic-secret-text" not in done.stdout + done.stderr


def test_a_given_value_overrides_the_file(fresh_checkout, tmp_path):
    home = tmp_path / "home"
    _shared(home, "NEOTOMA_BEARER_TOKEN=\n")
    done = _run(fresh_checkout, home, {"NEOTOMA_BEARER_TOKEN": "synthetic-given"})
    assert done.stdout.splitlines()[0] == "OK", done.stdout + done.stderr

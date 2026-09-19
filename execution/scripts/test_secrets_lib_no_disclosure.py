"""Canary tests: a failing secrets operation must never disclose a value.

The defect these pin (ateles#682, found by canary in ateles#712): `secrets_lib`
embedded the failing tool's stderr in the exception message. `sops`' dotenv
parser echoes the line it could not parse — and our input lines are literally
`KEY=<secret value>` — so a caller writing the obvious `f"failed: {exc}"` put a
live credential into stdout.

Method: force each failure path with an obviously-synthetic sentinel in scope,
then assert the sentinel appears in NONE of: the exception message, its repr,
its args, its traceback, stdout, stderr, or the logging stream.

Reverting the fix (restoring `raise RuntimeError(f"... {result.stderr}")` at any
of the three raise sites) makes the corresponding test fail — verified by doing
exactly that; see the module docstring note in `secrets_lib.SecretsToolError`.
"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_lib as sl  # noqa: E402

# Obviously synthetic. Not a real credential, and shaped so a substring search
# cannot match by accident.
CANARY = "SYNTHETIC-CANARY-NOT-A-REAL-SECRET-8f3a1c9d"
CANARY_LINE = f"CANARY_TOKEN={CANARY}"


class _FakeCompleted:
    """A subprocess result whose stderr/stdout both quote the canary.

    This is what the real tools do: sops' dotenv parser prints
    `invalid dotenv input line: <the whole line>`, which for our input includes
    the value. Verified against sops 3.x by hand.
    """

    def __init__(self, returncode: int = 1):
        self.returncode = returncode
        self.stderr = f"Error unmarshalling file: invalid dotenv input line: {CANARY_LINE}"
        self.stdout = f"partially recovered:\n{CANARY_LINE}\n"


def _all_exception_surfaces(exc: BaseException) -> str:
    """Every string a caller could plausibly derive from the exception."""
    return "\n".join(
        [
            str(exc),
            repr(exc),
            "".join(str(a) for a in exc.args),
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            # The naive handler that started all this:
            f"operation failed: {exc}",
        ]
    )


def _assert_clean(*blobs: str) -> None:
    for blob in blobs:
        assert CANARY not in blob, (
            "CANARY LEAKED — a secret value reached an output surface.\n"
            f"Offending text: {blob[:400]!r}"
        )
        # Also catch a partial disclosure: the key name plus any of the value.
        assert "CANARY_TOKEN=" not in blob, (
            f"dotenv line leaked (key=value shape): {blob[:400]!r}"
        )


@pytest.fixture
def failing_subprocess(monkeypatch):
    """Make every subprocess.run return a canary-quoting failure."""
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=1)
    )


@pytest.fixture
def captured_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    prior = root.level
    root.setLevel(logging.DEBUG)
    yield stream
    root.removeHandler(handler)
    root.setLevel(prior)


# ---------------------------------------------------------------------------
# The three raise sites
# ---------------------------------------------------------------------------

def test_encrypt_failure_discloses_nothing(
    failing_subprocess, captured_logs, capsys, tmp_path, monkeypatch
):
    monkeypatch.setattr(sl, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(sl, "SECRETS_BASE", tmp_path)

    with pytest.raises(sl.SecretsToolError) as caught:
        sl.sops_encrypt_dotenv(f"{CANARY_LINE}\n", tmp_path / "block.sops.enc")

    out = capsys.readouterr()
    _assert_clean(
        _all_exception_surfaces(caught.value),
        out.out,
        out.err,
        captured_logs.getvalue(),
    )


def test_decrypt_failure_discloses_nothing(
    failing_subprocess, captured_logs, capsys, tmp_path
):
    snapshot = tmp_path / "neotoma.sops.enc"
    snapshot.write_text("not really encrypted\n")

    with pytest.raises(sl.SecretsToolError) as caught:
        sl.sops_decrypt_dotenv(snapshot)

    out = capsys.readouterr()
    _assert_clean(
        _all_exception_surfaces(caught.value),
        out.out,
        out.err,
        captured_logs.getvalue(),
    )


def test_op_read_failure_discloses_nothing(failing_subprocess, captured_logs, capsys):
    with pytest.raises(sl.SecretsToolError) as caught:
        sl.op_read("op://Private/some-item/credential")

    out = capsys.readouterr()
    _assert_clean(
        _all_exception_surfaces(caught.value),
        out.out,
        out.err,
        captured_logs.getvalue(),
    )


def test_error_still_says_what_failed():
    """Withholding output must not make the error useless to the operator."""
    exc = sl.SecretsToolError("sops", "encrypt", returncode=3, context="destination x.enc")
    msg = str(exc)
    assert "sops" in msg and "encrypt" in msg and "3" in msg
    assert exc.tool == "sops" and exc.returncode == 3


# ---------------------------------------------------------------------------
# The two callers that print the exception
# ---------------------------------------------------------------------------

def test_publish_caller_prints_no_value(failing_subprocess, capsys, tmp_path, monkeypatch):
    """`secrets_publish.publish_file` formats the exception — check its stdout."""
    import secrets_publish

    monkeypatch.setattr(sl, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(sl, "SECRETS_BASE", tmp_path)

    manifest = {
        "files": {"neotoma": {"default": {"CANARY_TOKEN": "op://Private/x/credential"}}}
    }
    rc = secrets_publish.publish_file(manifest, "neotoma", None)

    assert rc == 1
    out = capsys.readouterr()
    assert CANARY not in out.out and CANARY not in out.err


def test_publish_withholds_detail_for_untyped_exceptions(capsys, tmp_path, monkeypatch):
    """Defense in depth: a non-SecretsToolError whose message holds the canary
    must still not be printed verbatim by the caller."""
    import secrets_publish

    def _leaky(_ref):
        raise RuntimeError(f"op read failed: {CANARY_LINE}")

    monkeypatch.setattr(sl, "op_read", _leaky)
    manifest = {
        "files": {"neotoma": {"default": {"CANARY_TOKEN": "op://Private/x/credential"}}}
    }
    rc = secrets_publish.publish_file(manifest, "neotoma", None)

    assert rc == 1
    out = capsys.readouterr()
    assert CANARY not in out.out and CANARY not in out.err
    assert "RuntimeError" in out.out  # the type still reaches the operator


def test_materialize_caller_prints_no_value(
    failing_subprocess, capsys, tmp_path, monkeypatch
):
    import secrets_materialize

    snapshot_dir = tmp_path / "secrets"
    snapshot_dir.mkdir()
    (snapshot_dir / "neotoma.sops.enc").write_text("ciphertext\n")
    monkeypatch.setattr(sl, "SECRETS_BASE", tmp_path)
    monkeypatch.setattr(sl, "SECRETS_DIR", snapshot_dir)
    monkeypatch.setattr(
        sl, "load_manifest", lambda: {"files": {"neotoma": {"default": {}}}}
    )

    rc = secrets_materialize.main(["--env-file", str(tmp_path / "out.env")])

    assert rc == 1
    out = capsys.readouterr()
    assert CANARY not in out.out and CANARY not in out.err


def test_materialize_withholds_detail_for_untyped_exceptions(
    capsys, tmp_path, monkeypatch
):
    import secrets_materialize

    snapshot_dir = tmp_path / "secrets"
    snapshot_dir.mkdir()
    (snapshot_dir / "neotoma.sops.enc").write_text("ciphertext\n")

    def _leaky(_src):
        raise ValueError(f"sops decrypt failed: {CANARY_LINE}")

    monkeypatch.setattr(sl, "SECRETS_BASE", tmp_path)
    monkeypatch.setattr(sl, "SECRETS_DIR", snapshot_dir)
    monkeypatch.setattr(sl, "sops_decrypt_dotenv", _leaky)
    monkeypatch.setattr(
        sl, "load_manifest", lambda: {"files": {"neotoma": {"default": {}}}}
    )

    rc = secrets_materialize.main(["--env-file", str(tmp_path / "out.env")])

    assert rc == 1
    out = capsys.readouterr()
    assert CANARY not in out.out and CANARY not in out.err
    assert "ValueError" in out.out


# ---------------------------------------------------------------------------
# The temp file must not survive a failure
# ---------------------------------------------------------------------------

def test_encrypt_failure_leaves_no_plaintext_on_disk(
    failing_subprocess, tmp_path, monkeypatch
):
    monkeypatch.setattr(sl, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(sl, "SECRETS_BASE", tmp_path)

    with pytest.raises(sl.SecretsToolError):
        sl.sops_encrypt_dotenv(f"{CANARY_LINE}\n", tmp_path / "block.sops.enc")

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert CANARY not in path.read_text(errors="ignore"), (
                f"plaintext canary survived on disk at {path}"
            )

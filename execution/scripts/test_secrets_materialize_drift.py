"""Tests for the manifest/snapshot drift guard in secrets_materialize.

Regression cover for the failure that motivated the guard: ELEVENLABS_API_KEY was
added to manifest.env-map.json but secrets_publish was never re-run, so the
encrypted snapshot silently carried 6 of 7 declared vars. The consumer failed far
from the cause — a meeting transcription aborted after two paid ElevenLabs passes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import secrets_materialize as sm  # noqa: E402
import secrets_lib as sl  # noqa: E402


@pytest.fixture
def fake_snapshot(tmp_path, monkeypatch):
    """Drive materialize against an in-memory manifest + fake decrypt."""

    def _install(declared: dict[str, str], snapshot: dict[str, str]):
        enc = tmp_path / "fake.sops.enc"
        enc.write_text("# ciphertext stand-in\n")

        monkeypatch.setattr(
            sl,
            "load_manifest",
            lambda: {"files": {"fake": {"default": declared, "target": None}}},
        )
        monkeypatch.setattr(sl, "enc_file", lambda name: enc)
        monkeypatch.setattr(sl, "sops_decrypt_dotenv", lambda src: dict(snapshot))
        monkeypatch.setattr(sl, "merge_into_env_file", lambda env_file, values: [])
        monkeypatch.setattr(sl, "SECRETS_BASE", tmp_path)
        return tmp_path / "out.env"

    return _install


def test_warns_when_snapshot_missing_a_declared_key(fake_snapshot, capsys):
    env_file = fake_snapshot(
        declared={"TOKEN_A": "op://x/a", "ELEVENLABS_API_KEY": "op://x/eleven"},
        snapshot={"TOKEN_A": "value-a"},
    )

    rc = sm.main(["fake", "--env-file", str(env_file)])
    out = capsys.readouterr().out

    assert rc == 0, "drift is a warning, not a hard failure — must not break daemons"
    assert "WARNING" in out
    assert "ELEVENLABS_API_KEY" in out
    assert "secrets_publish" in out, "warning must name the remedy"


def test_silent_when_snapshot_matches_manifest(fake_snapshot, capsys):
    env_file = fake_snapshot(
        declared={"TOKEN_A": "op://x/a"},
        snapshot={"TOKEN_A": "value-a"},
    )

    sm.main(["fake", "--env-file", str(env_file)])
    out = capsys.readouterr().out

    assert "WARNING" not in out


def test_extra_snapshot_keys_do_not_warn(fake_snapshot, capsys):
    """A snapshot ahead of the manifest is not the failure mode we guard."""
    env_file = fake_snapshot(
        declared={"TOKEN_A": "op://x/a"},
        snapshot={"TOKEN_A": "value-a", "LEFTOVER": "value-b"},
    )

    sm.main(["fake", "--env-file", str(env_file)])
    out = capsys.readouterr().out

    assert "WARNING" not in out


def test_reports_every_missing_key(fake_snapshot, capsys):
    env_file = fake_snapshot(
        declared={"A": "op://x/a", "B": "op://x/b", "C": "op://x/c"},
        snapshot={"B": "value-b"},
    )

    sm.main(["fake", "--env-file", str(env_file)])
    out = capsys.readouterr().out

    assert "A" in out and "C" in out

"""Tests for mint_daemon_keypair.py — the canonical T3/T4 keypair-minting script.

Covers: keypair shape, file permissions (written 0600 with no window),
never-print-the-secret, overwrite refusal, --force rotation, and role/name
validation (path traversal, backslash, NUL byte) — the safety properties
ported in from the parallel script this PR removes in favour of hardening
this one (docs/foundation/principles.md "extend the mechanism that already
generalizes; do not build a parallel one").
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "execution" / "scripts" / "mint_daemon_keypair.py"

sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

from mint_daemon_keypair import mint, validate_name  # noqa: E402


@pytest.fixture()
def keys_dir(tmp_path: Path) -> Path:
    d = tmp_path / "keys"
    return d


class TestValidateName:
    def test_accepts_plain_name(self) -> None:
        assert validate_name("monedula") == "monedula"

    def test_lowercases(self) -> None:
        assert validate_name("Monedula") == "monedula"

    def test_strips_whitespace(self) -> None:
        assert validate_name("  monedula  ") == "monedula"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            validate_name("")
        with pytest.raises(ValueError):
            validate_name("   ")

    def test_rejects_forward_slash(self) -> None:
        with pytest.raises(ValueError):
            validate_name("../../etc/passwd")
        with pytest.raises(ValueError):
            validate_name("accipiter/evil")

    def test_rejects_parent_traversal(self) -> None:
        with pytest.raises(ValueError):
            validate_name("..")
        with pytest.raises(ValueError):
            validate_name("foo..bar")

    def test_rejects_backslash(self) -> None:
        """`\\` is a path separator on Windows and can smuggle a component
        past the POSIX-only `/` and `..` checks."""
        with pytest.raises(ValueError):
            validate_name("..\\..\\etc\\passwd")
        with pytest.raises(ValueError):
            validate_name("accipiter\\evil")

    def test_rejects_nul_byte(self) -> None:
        """A NUL byte can truncate a path at the C-library level, letting
        the rest of the string (e.g. an extension) be silently discarded."""
        with pytest.raises(ValueError):
            validate_name("accipiter\x00.txt")


class TestMintFunction:
    def test_writes_canonical_jwk_with_expected_fields(self, keys_dir: Path) -> None:
        out_path = mint("accipiter", keys_dir)
        assert out_path == keys_dir / "accipiter.jwk.json"
        assert out_path.exists()
        data = json.loads(out_path.read_text())
        assert data["kty"] == "EC"
        assert data["crv"] == "P-256"
        assert data["sub"] == "accipiter@ateles-swarm"
        assert "d" in data  # private component present in the FILE
        assert "x" in data and "y" in data
        assert "kid" in data

    def test_file_permissions_are_0600(self, keys_dir: Path) -> None:
        out_path = mint("accipiter", keys_dir)
        mode = stat.S_IMODE(out_path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_refuses_overwrite_without_force(self, keys_dir: Path) -> None:
        mint("accipiter", keys_dir)
        with pytest.raises(FileExistsError):
            mint("accipiter", keys_dir)

    def test_force_rotates_and_changes_key_material(self, keys_dir: Path) -> None:
        mint("accipiter", keys_dir)
        first = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        mint("accipiter", keys_dir, force=True)
        second = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        assert first["d"] != second["d"]
        assert first["kid"] != second["kid"]

    def test_rejects_path_traversal_in_name(self, keys_dir: Path) -> None:
        with pytest.raises(ValueError):
            mint("../../etc/passwd", keys_dir)

    def test_rejects_backslash_in_name(self, keys_dir: Path) -> None:
        with pytest.raises(ValueError):
            mint("accipiter\\evil", keys_dir)

    def test_rejects_nul_byte_in_name(self, keys_dir: Path) -> None:
        with pytest.raises(ValueError):
            mint("accipiter\x00.txt", keys_dir)

    def test_name_is_lowercased_and_sub_derived(self, keys_dir: Path) -> None:
        mint("Accipiter", keys_dir)
        data = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        assert data["sub"] == "accipiter@ateles-swarm"

    def test_creates_keys_dir_if_missing(self, keys_dir: Path) -> None:
        assert not keys_dir.exists()
        mint("accipiter", keys_dir)
        assert keys_dir.exists()


class TestCliEntrypoint:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_cli_never_prints_private_key_material(self, keys_dir: Path) -> None:
        result = self._run("--name", "accipiter", "--keys-dir", str(keys_dir))
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        data = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        assert data["d"] not in combined
        assert data["x"] not in combined
        assert data["y"] not in combined
        assert "sub" in result.stdout

    def test_cli_refuses_overwrite_without_force_nonzero_exit(self, keys_dir: Path) -> None:
        first = self._run("--name", "accipiter", "--keys-dir", str(keys_dir))
        assert first.returncode == 0
        second = self._run("--name", "accipiter", "--keys-dir", str(keys_dir))
        assert second.returncode != 0
        assert "already exists" in second.stderr

    def test_cli_force_flag_rotates(self, keys_dir: Path) -> None:
        first = self._run("--name", "accipiter", "--keys-dir", str(keys_dir))
        assert first.returncode == 0
        before = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        second = self._run("--name", "accipiter", "--keys-dir", str(keys_dir), "--force")
        assert second.returncode == 0, second.stderr
        after = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        assert before["d"] != after["d"]

    def test_cli_rejects_invalid_name_nonzero_exit(self, keys_dir: Path) -> None:
        result = self._run("--name", "../evil", "--keys-dir", str(keys_dir))
        assert result.returncode != 0
        assert "invalid daemon name" in result.stderr

"""Tests for aauth_provision_identity.py.

Covers: keypair shape, file permissions, never-print-the-secret, refusal to
overwrite without --force, rotation, and that the produced JWK actually loads
and signs via lib/daemon_runtime/aauth_httpsig (the module this key is FOR).
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "execution" / "scripts" / "aauth_provision_identity.py"

sys.path.insert(0, str(_REPO_ROOT / "lib"))
sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

from aauth_provision_identity import provision  # noqa: E402
from daemon_runtime.aauth_httpsig import HttpSigSigner, jwk_thumbprint, public_part_of  # noqa: E402


@pytest.fixture()
def keys_dir(tmp_path: Path) -> Path:
    d = tmp_path / "keys"
    d.mkdir()
    return d


class TestProvisionFunction:
    def test_writes_private_jwk_with_expected_fields(self, keys_dir: Path) -> None:
        result = provision("accipiter", keys_dir=keys_dir)
        out_path = keys_dir / "accipiter.jwk.json"
        assert out_path.exists()
        data = json.loads(out_path.read_text())
        assert data["kty"] == "EC"
        assert data["crv"] == "P-256"
        assert data["sub"] == "accipiter@ateles-swarm"
        assert data["iss"] == result["iss"]
        assert "d" in data  # private component present in the FILE
        assert data["kid"] == result["kid"]

    def test_file_permissions_are_0600(self, keys_dir: Path) -> None:
        provision("accipiter", keys_dir=keys_dir)
        out_path = keys_dir / "accipiter.jwk.json"
        mode = stat.S_IMODE(out_path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_returned_metadata_never_contains_private_component(self, keys_dir: Path) -> None:
        result = provision("accipiter", keys_dir=keys_dir)
        assert set(result.keys()) == {"sub", "iss", "kid", "thumbprint", "path"}
        serialized = json.dumps(result)
        assert '"d"' not in serialized

    def test_thumbprint_matches_public_jwk_thumbprint(self, keys_dir: Path) -> None:
        result = provision("accipiter", keys_dir=keys_dir)
        data = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        assert result["thumbprint"] == jwk_thumbprint(public_part_of(data))

    def test_refuses_overwrite_without_force(self, keys_dir: Path) -> None:
        provision("accipiter", keys_dir=keys_dir)
        with pytest.raises(FileExistsError):
            provision("accipiter", keys_dir=keys_dir)

    def test_force_rotates_and_changes_thumbprint(self, keys_dir: Path) -> None:
        first = provision("accipiter", keys_dir=keys_dir)
        second = provision("accipiter", keys_dir=keys_dir, force=True)
        assert first["thumbprint"] != second["thumbprint"]

    def test_rejects_path_traversal_in_role(self, keys_dir: Path) -> None:
        with pytest.raises(ValueError):
            provision("../../etc/passwd", keys_dir=keys_dir)

    def test_rejects_backslash_in_role(self, keys_dir: Path) -> None:
        """`\\` is a path separator on Windows and can smuggle a component
        past the POSIX-only `/` and `..` checks."""
        with pytest.raises(ValueError):
            provision("..\\..\\etc\\passwd", keys_dir=keys_dir)
        with pytest.raises(ValueError):
            provision("accipiter\\evil", keys_dir=keys_dir)

    def test_rejects_nul_byte_in_role(self, keys_dir: Path) -> None:
        """A NUL byte can truncate a path at the C-library level, letting
        the rest of the string (e.g. an extension) be silently discarded."""
        with pytest.raises(ValueError):
            provision("accipiter\x00.txt", keys_dir=keys_dir)

    def test_role_is_lowercased_and_sub_derived(self, keys_dir: Path) -> None:
        result = provision("Accipiter", keys_dir=keys_dir)
        assert result["sub"] == "accipiter@ateles-swarm"

    def test_produced_key_loads_and_signs_via_aauth_httpsig(self, keys_dir: Path) -> None:
        """The produced JWK must actually work with the signer it is FOR —
        not just look plausible. Proves the round trip end to end."""
        provision("accipiter", keys_dir=keys_dir)
        data = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        signer = HttpSigSigner(private_jwk=data, sub=data["sub"], iss=data["iss"], kid=data["kid"])
        headers = signer.sign_headers(
            method="POST", url="https://neotoma.example/store", body=b'{"x":1}'
        )
        assert "signature" in headers
        assert "signature-input" in headers
        assert "signature-key" in headers


class TestCliEntrypoint:
    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_cli_never_prints_private_key_material(self, keys_dir: Path, monkeypatch) -> None:
        import os

        env = dict(os.environ)
        env["ATELES_PRIVATE_KEYS_DIR"] = str(keys_dir)
        result = self._run("--role", "accipiter", env=env)
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        data = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        assert data["d"] not in combined
        assert "thumbprint" in result.stdout

    def test_cli_refuses_overwrite_without_force_nonzero_exit(self, keys_dir: Path) -> None:
        import os

        env = dict(os.environ)
        env["ATELES_PRIVATE_KEYS_DIR"] = str(keys_dir)
        first = self._run("--role", "accipiter", env=env)
        assert first.returncode == 0
        second = self._run("--role", "accipiter", env=env)
        assert second.returncode != 0
        assert "--force" in second.stderr

    def test_cli_output_names_operator_only_grant_step(self, keys_dir: Path) -> None:
        import os

        env = dict(os.environ)
        env["ATELES_PRIVATE_KEYS_DIR"] = str(keys_dir)
        result = self._run("--role", "accipiter", env=env)
        assert "createAgentGrant" in result.stdout
        assert "operator" in result.stdout.lower()

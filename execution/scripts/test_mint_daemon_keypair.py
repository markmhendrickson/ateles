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
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "execution" / "scripts" / "mint_daemon_keypair.py"

sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

import mint_daemon_keypair  # noqa: E402
from mint_daemon_keypair import (  # noqa: E402
    UnsafeKeyTargetError,
    jwk_thumbprint,
    key_file_thumbprint,
    mint,
    validate_name,
)


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


    @pytest.mark.parametrize(
        "bad",
        [".hidden", "-lead", "9lead", "has space", "a.b", "a" * 65, "caf\u00e9", "a\nb"],
    )
    def test_allowlist_rejects_everything_outside_the_pattern(self, bad: str) -> None:
        with pytest.raises(ValueError):
            validate_name(bad)

    def test_allowlist_accepts_underscore_dash_and_max_length(self) -> None:
        assert validate_name("neotoma_agent") == "neotoma_agent"
        assert validate_name("a-b_c9") == "a-b_c9"
        assert validate_name("a" * 64) == "a" * 64


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


class TestKeyCreationSafety:
    """Each test here is written to go RED against the pre-hardening mint().

    The pre-hardening code opened the target with O_CREAT|O_TRUNC (no O_EXCL, no
    O_NOFOLLOW), after a separate `exists()` check, then chmod'ed afterwards.
    """

    @pytest.fixture()
    def umask0(self):
        old = os.umask(0)
        yield
        os.umask(old)

    def test_force_over_a_0644_file_ends_0600(self, keys_dir: Path, umask0) -> None:
        """--force must not inherit the old file's looser mode.

        The mode is asserted with chmod/fchmod made unavailable, so a
        chmod-after-the-fact cannot be what makes it pass.
        """
        keys_dir.mkdir()
        target = keys_dir / "accipiter.jwk.json"
        target.write_text("old-key-material")
        target.chmod(0o644)
        with pytest.MonkeyPatch.context() as mp:
            def _no_chmod(*a, **k):
                raise AssertionError("mode must come from creation, not chmod")

            mp.setattr(os, "chmod", _no_chmod)
            mp.setattr(os, "fchmod", _no_chmod, raising=False)
            mint("accipiter", keys_dir, force=True)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert "old-key-material" not in target.read_text()

    def test_creation_mode_is_0600_under_umask_0_without_chmod(
        self, keys_dir: Path, umask0
    ) -> None:
        with pytest.MonkeyPatch.context() as mp:
            def _no_chmod(*a, **k):
                raise AssertionError("mode must come from creation, not chmod")

            mp.setattr(os, "chmod", _no_chmod)
            mp.setattr(os, "fchmod", _no_chmod, raising=False)
            out = mint("accipiter", keys_dir)
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def test_final_path_is_opened_exclusively_and_nofollow(
        self, keys_dir: Path
    ) -> None:
        opened: list[tuple[str, int, int]] = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *a, **k):
            opened.append((str(path), flags, mode))
            return real_open(path, flags, mode, *a, **k)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "open", spy)
            out = mint("accipiter", keys_dir)
        creates = [o for o in opened if o[0] == str(out)]
        assert creates, f"never opened the final path: {opened}"
        _, flags, mode = creates[0]
        assert flags & os.O_EXCL and flags & os.O_CREAT
        assert flags & os.O_NOFOLLOW
        assert not flags & os.O_TRUNC
        assert mode == 0o600

    def test_keys_dir_is_created_0700(self, keys_dir: Path, umask0) -> None:
        mint("accipiter", keys_dir)
        assert stat.S_IMODE(keys_dir.stat().st_mode) == 0o700

    @pytest.mark.parametrize("force", [False, True])
    def test_symlink_at_target_is_refused_and_not_followed(
        self, keys_dir: Path, tmp_path: Path, force: bool
    ) -> None:
        keys_dir.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("victim-content")
        link = keys_dir / "accipiter.jwk.json"
        link.symlink_to(victim)
        with pytest.raises((FileExistsError, UnsafeKeyTargetError)):
            mint("accipiter", keys_dir, force=force)
        assert victim.read_text() == "victim-content", "wrote through the symlink"
        assert link.is_symlink()

    @pytest.mark.parametrize("force", [False, True])
    def test_dangling_symlink_at_target_is_refused(
        self, keys_dir: Path, tmp_path: Path, force: bool
    ) -> None:
        keys_dir.mkdir()
        planted = tmp_path / "does-not-exist"
        (keys_dir / "accipiter.jwk.json").symlink_to(planted)
        with pytest.raises((FileExistsError, UnsafeKeyTargetError)):
            mint("accipiter", keys_dir, force=force)
        assert not planted.exists(), "created a file through a dangling symlink"

    def test_failed_rotation_keeps_the_old_key_intact(self, keys_dir: Path) -> None:
        mint("accipiter", keys_dir)
        target = keys_dir / "accipiter.jwk.json"
        before = target.read_bytes()

        def boom(*a, **k):
            raise OSError("disk went away")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "replace", boom)
            with pytest.raises(OSError):
                mint("accipiter", keys_dir, force=True)
        assert target.read_bytes() == before, "old key lost by a failed rotation"
        assert json.loads(before)["d"]
        assert [p.name for p in keys_dir.iterdir()] == ["accipiter.jwk.json"], (
            "left a temp file behind"
        )

    def test_failed_write_during_rotation_keeps_the_old_key_intact(
        self, keys_dir: Path
    ) -> None:
        mint("accipiter", keys_dir)
        target = keys_dir / "accipiter.jwk.json"
        before = target.read_bytes()

        def boom(*a, **k):
            raise OSError("fsync failed")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "fsync", boom)
            with pytest.raises(OSError):
                mint("accipiter", keys_dir, force=True)
        assert target.read_bytes() == before
        assert [p.name for p in keys_dir.iterdir()] == ["accipiter.jwk.json"]

    def test_failed_first_write_leaves_no_partial_key(self, keys_dir: Path) -> None:
        def boom(*a, **k):
            raise OSError("fsync failed")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "fsync", boom)
            with pytest.raises(OSError):
                mint("accipiter", keys_dir)
        assert list(keys_dir.iterdir()) == []

    def test_no_force_does_not_clobber_a_file_created_after_the_check(
        self, keys_dir: Path
    ) -> None:
        """The check-then-act race: the target appears between check and create."""
        keys_dir.mkdir()
        target = keys_dir / "accipiter.jwk.json"
        real_check = mint_daemon_keypair._check_target

        def check_then_lose_the_race(path):
            result = real_check(path)  # sees "absent"
            target.write_text("raced-in-by-someone-else")
            return result

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mint_daemon_keypair, "_check_target", check_then_lose_the_race)
            with pytest.raises(FileExistsError):
                mint("accipiter", keys_dir)
        assert target.read_text() == "raced-in-by-someone-else"


class TestCliMessages:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_SCRIPT), *args],
            capture_output=True, text=True, timeout=30,
        )

    def test_exists_error_says_both_delete_and_force_destroy_the_old_key(
        self, keys_dir: Path
    ) -> None:
        self._run("--name", "accipiter", "--keys-dir", str(keys_dir))
        again = self._run("--name", "accipiter", "--keys-dir", str(keys_dir))
        assert again.returncode != 0
        assert "--force" in again.stderr and "destroys" in again.stderr

    def test_help_does_not_call_the_role_a_daemon_name(self) -> None:
        out = self._run("--help").stdout
        assert "Daemon name" not in out
        assert "Role name" in out

    def test_printed_mode_is_the_real_mode(self, keys_dir: Path) -> None:
        out = self._run("--name", "accipiter", "--keys-dir", str(keys_dir)).stdout
        real = stat.S_IMODE((keys_dir / "accipiter.jwk.json").stat().st_mode)
        assert f"mode: {real:04o}" in out

    def test_first_mint_says_written_and_not_rotated(self, keys_dir: Path) -> None:
        out = self._run("--name", "accipiter", "--keys-dir", str(keys_dir)).stdout
        assert "Keypair written to:" in out
        assert "ROTATED" not in out

    def test_force_over_existing_key_says_rotated_and_old_key_destroyed(
        self, keys_dir: Path
    ) -> None:
        self._run("--name", "accipiter", "--keys-dir", str(keys_dir))
        out = self._run(
            "--name", "accipiter", "--keys-dir", str(keys_dir), "--force"
        ).stdout
        assert "ROTATED" in out and "destroyed" in out
        assert "Keypair written to:" not in out
        # The existing grant still applies, so a rotation must not tell the
        # operator to register one; it says to re-verify, restart, and
        # re-register only if the sub or capabilities changed.
        assert "createAgentGrant" not in out.split("Rotation next steps")[0]
        assert out.index("Re-verify") < out.index("Restart the daemon")

    def test_force_with_no_existing_key_is_a_first_mint_message(
        self, keys_dir: Path
    ) -> None:
        out = self._run(
            "--name", "accipiter", "--keys-dir", str(keys_dir), "--force"
        ).stdout
        assert "Keypair written to:" in out and "ROTATED" not in out

    def test_closing_hint_follows_the_canonical_step_order(self, keys_dir: Path) -> None:
        out = self._run("--name", "accipiter", "--keys-dir", str(keys_dir)).stdout
        steps = [
            "createAgentGrant",
            "listAgentGrants",
            "verify_aauth_signer.py",
            "Restart the daemon",
        ]
        positions = [out.index(marker) for marker in steps]
        assert positions == sorted(positions), out
        assert "accipiter@ateles-swarm" in out
        assert "ATELES_AAUTH_KEYS_DIR" in out


class TestThumbprint:
    """The RFC 7638 thumbprint is what a grant pins (`match_thumbprint`).

    Neotoma (main, #2506) admits a signed request only when a grant's
    `match_thumbprint` equals the signing key's RFC 7638 thumbprint, so the script
    must print exactly that value, and only that (public) value.
    """

    # EC P-256 public key from RFC 7517 appendix A.1, and its RFC 7638 thumbprint
    # as computed by an independent implementation (authlib).
    _PUB = {
        "kty": "EC",
        "crv": "P-256",
        "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
        "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0",
    }
    _EXPECTED = "oKIywvGUpTVTyxMQ3bwIIeQUudfr_CkLMjCE19ECD-U"

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_SCRIPT), *args],
            capture_output=True, text=True, timeout=30,
        )

    def test_known_vector(self) -> None:
        assert jwk_thumbprint(self._PUB) == self._EXPECTED

    def test_matches_hand_computed_rfc7638_canonical_form(self) -> None:
        import base64
        import hashlib

        canonical = (
            '{"crv":"P-256","kty":"EC","x":"%s","y":"%s"}' % (self._PUB["x"], self._PUB["y"])
        )
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(canonical.encode()).digest()
        ).rstrip(b"=").decode()
        assert jwk_thumbprint(self._PUB) == expected

    def test_ignores_private_scalar_and_extra_members(self) -> None:
        with_secret = dict(self._PUB, d="SECRET-SCALAR", kid="k", sub="s@x", use="sig")
        assert jwk_thumbprint(with_secret) == self._EXPECTED

    def test_printed_thumbprint_equals_authlib_for_a_generated_key(
        self, keys_dir: Path
    ) -> None:
        authlib = pytest.importorskip("authlib.jose")
        result = self._run("--name", "accipiter", "--keys-dir", str(keys_dir))
        assert result.returncode == 0, result.stderr
        jwk = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        independent = authlib.JsonWebKey.import_key(
            {k: jwk[k] for k in ("kty", "crv", "x", "y")}
        ).thumbprint()
        assert f"thumbprint (RFC 7638, public): {independent}" in result.stdout

    def test_printed_thumbprint_equals_the_library_neotoma_uses(
        self, keys_dir: Path
    ) -> None:
        """Neotoma computes the pin with `jose`'s calculateJwkThumbprint."""
        import shutil

        rc = Path(os.environ.get("NEOTOMA_RC_DIR", str(Path.home() / "neotoma-rc-src")))
        if not shutil.which("node") or not (rc / "node_modules" / "jose").exists():
            pytest.skip("node + neotoma's jose not available")
        result = self._run("--name", "accipiter", "--keys-dir", str(keys_dir))
        jwk = json.loads((keys_dir / "accipiter.jwk.json").read_text())
        pub = {k: jwk[k] for k in ("kty", "crv", "x", "y")}
        proc = subprocess.run(
            ["node", "-e",
             "const {calculateJwkThumbprint}=require(process.argv[1]);"
             "calculateJwkThumbprint(JSON.parse(process.argv[2])).then(t=>console.log(t))",
             str(rc / "node_modules" / "jose"), json.dumps(pub)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            pytest.skip(f"node could not load jose: {proc.stderr[:120]}")
        assert f": {proc.stdout.strip()}" in result.stdout

    def test_output_never_contains_the_private_scalar_or_coordinates(
        self, keys_dir: Path
    ) -> None:
        for extra in ((), ("--force",)):
            result = self._run("--name", "accipiter", "--keys-dir", str(keys_dir), *extra)
            assert result.returncode == 0, result.stderr
            jwk = json.loads((keys_dir / "accipiter.jwk.json").read_text())
            combined = result.stdout + result.stderr
            for secret in (jwk["d"], jwk["x"], jwk["y"]):
                assert secret not in combined
            assert key_file_thumbprint(keys_dir / "accipiter.jwk.json") in combined

    def test_first_mint_hint_pins_the_printed_thumbprint(self, keys_dir: Path) -> None:
        out = self._run("--name", "accipiter", "--keys-dir", str(keys_dir)).stdout
        tp = key_file_thumbprint(keys_dir / "accipiter.jwk.json")
        body = out[out.index("createAgentGrant"):]
        assert f'"match_thumbprint": "{tp}"' in body.split("\n")[0]
        assert '"match_sub": "accipiter@ateles-swarm"' in body.split("\n")[0]
        assert "admits nothing on current Neotoma" in out
        assert f"listAgentGrants --query '{{\"q\": \"{tp}\"" in out

    def test_rotation_hint_says_the_old_grant_does_not_admit_the_new_key(
        self, keys_dir: Path
    ) -> None:
        self._run("--name", "accipiter", "--keys-dir", str(keys_dir))
        old_tp = key_file_thumbprint(keys_dir / "accipiter.jwk.json")
        out = self._run("--name", "accipiter", "--keys-dir", str(keys_dir), "--force").stdout
        new_tp = key_file_thumbprint(keys_dir / "accipiter.jwk.json")
        assert new_tp != old_tp
        assert "updateAgentGrant" in out and f'"match_thumbprint": "{new_tp}"' in out
        assert "revokeAgentGrant" in out
        assert "not admitted" in out
        assert old_tp not in out
        # pin, check, re-verify, restart -- in that order
        order = [out.index(m) for m in ("updateAgentGrant", "listAgentGrants", "Re-verify", "Restart the daemon")]
        assert order == sorted(order)

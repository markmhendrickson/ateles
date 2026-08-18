"""Tests for AAuthSigner — the daemon-facing key-load-and-sign API.

Pins two effects Loxia's PR #422 review flagged as broken:

- `mint_daemon_keypair.py --alg Ed25519` (the default) produces a key that
  `AAuthSigner.from_key_file` can actually load and sign with, rather than
  silently degrading to a stub because `_load_private_key_jwk` only knew
  EC/P-256. Verified with a real Ed25519 signature check, not just "a token
  string came back."
- The `sub` a freshly minted key presents on the wire is the legacy
  `<name>@ateles-swarm` form regardless of algorithm, honouring the
  `ATELES_AAUTH_SPEC_IDENTIFIERS` migration gate until it is flipped — not
  the draft-10 `aauth:` form, which would fail admission against the live
  `agent_grant` entities that still match on the legacy value.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import jwt as pyjwt
import pytest

from daemon_runtime.aauth_signer import AAuthSigner


def _b64url_to_bytes(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _ed25519_jwk() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    raw_priv = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "Ed25519",
        "x": base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode(),
        "d": base64.urlsafe_b64encode(raw_priv).rstrip(b"=").decode(),
        "sub": "tester@ateles-swarm",
        "kid": "test-ed25519-kid",
    }


def _es256_jwk() -> dict:
    from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key

    key = generate_private_key(SECP256R1())
    pub = key.public_key().public_numbers()
    priv = key.private_numbers()

    def _int_b64(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()

    return {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "x": _int_b64(pub.x),
        "y": _int_b64(pub.y),
        "d": _int_b64(priv.private_value),
        "sub": "tester@ateles-swarm",
        "kid": "test-es256-kid",
    }


@pytest.fixture
def ed25519_key_file(tmp_path: Path) -> Path:
    keys_dir = tmp_path
    (keys_dir / "tester.jwk.json").write_text(json.dumps(_ed25519_jwk()))
    return keys_dir


@pytest.fixture
def es256_key_file(tmp_path: Path) -> Path:
    keys_dir = tmp_path
    (keys_dir / "tester.jwk.json").write_text(json.dumps(_es256_jwk()))
    return keys_dir


class TestFromKeyFileLoadsBothAlgorithms:
    def test_ed25519_key_loads_and_is_not_a_stub(self, ed25519_key_file: Path) -> None:
        signer = AAuthSigner.from_key_file("tester", keys_dir=ed25519_key_file)

        assert not signer.is_stub, (
            "Ed25519 key failed to load and silently fell back to a stub "
            "signer — this is the exact regression the PR review flagged: "
            "mint_daemon_keypair.py defaults to Ed25519 but the signer could "
            "only load EC/P-256 keys."
        )
        assert signer._alg == "Ed25519"

    def test_es256_key_loads_and_is_not_a_stub(self, es256_key_file: Path) -> None:
        signer = AAuthSigner.from_key_file("tester", keys_dir=es256_key_file)

        assert not signer.is_stub
        assert signer._alg == "ES256"


class TestSignedTokenIsCryptographicallyValid:
    def test_ed25519_token_signature_verifies(self, ed25519_key_file: Path) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        signer = AAuthSigner.from_key_file("tester", keys_dir=ed25519_key_file)
        token = signer.headers("POST", "/store")["X-AAuth-Token"]
        assert token, "Ed25519 signer produced an empty token"

        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_to_bytes(header_b64))
        assert header["alg"] == "Ed25519", (
            "protected header must say the fully-specified 'Ed25519', not "
            "PyJWT's polymorphic 'EdDSA' (RFC 9864 deprecated it; draft-10 "
            "§12.8.1 forbids it on the wire)"
        )

        jwk = json.loads((ed25519_key_file / "tester.jwk.json").read_text())
        pub_key = Ed25519PublicKey.from_public_bytes(_b64url_to_bytes(jwk["x"]))
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        # Raises InvalidSignature if the token isn't actually signed by this key.
        pub_key.verify(_b64url_to_bytes(sig_b64), signing_input)

    def test_es256_token_signature_verifies(self, es256_key_file: Path) -> None:
        signer = AAuthSigner.from_key_file("tester", keys_dir=es256_key_file)
        token = signer.headers("POST", "/store")["X-AAuth-Token"]
        assert token, "ES256 signer produced an empty token"

        jwk = json.loads((es256_key_file / "tester.jwk.json").read_text())
        # PyJWT verifies ES256 tokens directly given the public JWK.
        from cryptography.hazmat.primitives.asymmetric.ec import (
            SECP256R1,
            EllipticCurvePublicNumbers,
        )

        x = int.from_bytes(_b64url_to_bytes(jwk["x"]), "big")
        y = int.from_bytes(_b64url_to_bytes(jwk["y"]), "big")
        public_key = EllipticCurvePublicNumbers(x, y, SECP256R1()).public_key()
        decoded = pyjwt.decode(
            token, key=public_key, algorithms=["ES256"], options={"verify_exp": True}
        )
        assert decoded["sub"] == "tester@ateles-swarm"


class TestMintedSubStaysLegacyUntilMigrationFlag:
    def test_wire_sub_is_legacy_form_for_ed25519_key(
        self, ed25519_key_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATELES_AAUTH_SPEC_IDENTIFIERS", raising=False)
        signer = AAuthSigner.from_key_file("tester", keys_dir=ed25519_key_file)

        assert signer.sub == "tester@ateles-swarm", (
            "with the migration flag off, the wire sub must stay the legacy "
            "form the live agent_grant entities match on — presenting the "
            "draft-10 form here would fail admission for a freshly minted key "
            "even with the flag untouched"
        )

    def test_wire_sub_switches_to_spec_form_when_flag_enabled(
        self, ed25519_key_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATELES_AAUTH_SPEC_IDENTIFIERS", "1")
        signer = AAuthSigner.from_key_file("tester", keys_dir=ed25519_key_file)

        assert signer.sub == "aauth:tester@markmhendrickson.com"


class TestMintDaemonKeypairStoresLegacySub:
    def test_minted_ed25519_key_stores_legacy_sub(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parent.parent.parent
        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "execution" / "scripts" / "mint_daemon_keypair.py"),
                "--name",
                "minttest",
                "--alg",
                "Ed25519",
                "--keys-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

        jwk = json.loads((tmp_path / "minttest.jwk.json").read_text())
        assert jwk["sub"] == "minttest@ateles-swarm", (
            "mint_daemon_keypair.py must store the legacy sub form so a "
            "freshly minted key doesn't bypass ATELES_AAUTH_SPEC_IDENTIFIERS "
            "— storing the spec form here presents it on the wire "
            "unconditionally regardless of the flag"
        )
        # And the signer built from this exact file loads and signs cleanly —
        # the mint output is only correct if AAuthSigner can actually use it.
        signer = AAuthSigner.from_key_file("minttest", keys_dir=tmp_path)
        assert not signer.is_stub
        assert signer.headers("POST", "/store")["X-AAuth-Token"]

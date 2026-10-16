"""Real @hellocoop/httpsig contract for Neotoma-bound producer requests.

Set ``NEOTOMA_HTTPSIG_MODULE`` to an installed
``node_modules/@hellocoop/httpsig`` directory. CI environments without the
Neotoma verifier dependency skip this integration test; the always-on producer
sink test in ``test_gating.py`` still pins the exact headers and request bytes.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from daemon_runtime.aauth_httpsig import HttpSigSigner
from daemon_runtime.gating import _canonical_json


def _signer() -> HttpSigSigner:
    private = ec.generate_private_key(ec.SECP256R1()).private_numbers()

    def b64u(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()

    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "d": b64u(private.private_value),
        "x": b64u(private.public_numbers.x),
        "y": b64u(private.public_numbers.y),
        "sub": "apis@ateles-swarm",
        "kid": "contract-apis-key",
    }
    return HttpSigSigner(
        private_jwk=jwk,
        sub="apis@ateles-swarm",
        iss="https://issuer.example",
        kid="contract-apis-key",
    )


def _real_verify(module: Path, request: dict) -> dict:
    script = r"""
const fs = require("fs");
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const { verify } = require(payload.module);
verify(payload.request, { strictAAuth: true })
  .then((result) => process.stdout.write(JSON.stringify({
    verified: result.verified,
    keyType: result.keyType,
    thumbprint: result.thumbprint,
    sub: result.jwt && result.jwt.payload && result.jwt.payload.sub,
    error: result.error || null,
  })))
  .catch((error) => {
    process.stdout.write(JSON.stringify({ verified: false, error: String(error) }));
  });
"""
    completed = subprocess.run(
        ["node", "-e", script],
        input=json.dumps({"module": str(module), "request": request}),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return json.loads(completed.stdout)


def test_store_signature_matches_real_neotoma_verifier_and_rejects_tamper() -> None:
    module_value = os.environ.get("NEOTOMA_HTTPSIG_MODULE", "").strip()
    module = Path(module_value) if module_value else None
    if module is None or not (module / "package.json").is_file():
        pytest.skip("set NEOTOMA_HTTPSIG_MODULE to run the real verifier contract")

    body = _canonical_json(
        {
            "entities": [{"entity_type": "checkpoint_" + "brief"}],
            "relationships": [],
            "idempotency_key": "checkpoint-apis-ent-task-plan",
        }
    )
    headers = _signer().sign_headers(
        method="POST",
        url="https://neotoma.example/store",
        body=body,
        now=int(time.time()),
    )
    assert {"signature", "signature-input", "signature-key"}.issubset(headers)
    assert "X-AAuth-Token" not in headers
    request = {
        "method": "POST",
        "authority": "neotoma.example",
        "path": "/store",
        "query": "",
        "headers": headers,
        "body": body,
    }

    verified = _real_verify(module, request)
    assert verified["verified"] is True
    assert verified["keyType"] == "jwt"
    assert verified["sub"] == "apis@ateles-swarm"
    assert verified["thumbprint"]

    tampered = dict(request, body=body + " ")
    rejected = _real_verify(module, tampered)
    assert rejected["verified"] is False

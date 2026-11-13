"""The REAL deployed Neotoma CLI, pinned, against a local stub HTTP server.

Proves what the unit tests cannot: that with a stored token sitting in the
daemon's HOME config, the pinned CLI sends signature headers and NO
Authorization header. Skipped cleanly where node or the deployed CLI is absent
(CI is Linux). Synthetic key, synthetic token, loopback server only; no live
Neotoma, no real credential.
"""

import base64
import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from lib.daemon_runtime import pinned_cli_identity as pci

RC_DIR = Path(os.environ.get("NEOTOMA_RC_DIR", str(Path.home() / "neotoma-rc-src")))
BOOTSTRAP = RC_DIR / "dist" / "cli" / "bootstrap.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not (NODE and BOOTSTRAP.is_file() and (BOOTSTRAP.parent / "aauth_signer.js").is_file()),
    reason="node or the deployed Neotoma CLI is not present",
)

STORED = "SYNTHETIC-STORED-TOKEN"


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()


def _synthetic_jwk(kid: str) -> dict:
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.private_numbers()
    pub = nums.public_numbers
    return {"kty": "EC", "crv": "P-256", "x": _b64(pub.x), "y": _b64(pub.y),
            "d": _b64(nums.private_value), "kid": kid, "alg": "ES256",
            "iss": "https://synthetic-issuer.example"}


class Stub:
    def __init__(self):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.requests.append({k.lower(): v for k, v in self.headers.items()})
                body = b'{"entities":[],"results":[],"total":0}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake 'real' HOME that holds a stored token, as the operator's does."""
    home = tmp_path / "realhome"
    (home / ".config" / "neotoma").mkdir(parents=True)
    (home / ".config" / "neotoma" / "config.json").write_text(json.dumps({"access_token": STORED}))
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "piculet.jwk.json").write_text(json.dumps(_synthetic_jwk("synthetic-kid")))
    for name in (*pci.BEARER_ENV_KEYS, *pci.SIGNING_DISABLE_KEYS, *pci.CLAIM_OVERRIDE_KEYS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("NEOTOMA_AAUTH_SUB", raising=False)
    base = {"PATH": os.environ["PATH"], "HOME": str(home)}
    return types_ns(home=home, keys=keys, base=base, state=tmp_path / "state")


def types_ns(**kw):
    import types

    return types.SimpleNamespace(**kw)


def _search(argv_prefix, env, stub_url, cwd):
    return subprocess.run(
        [*argv_prefix, NODE, str(BOOTSTRAP), "--json", "--api-only", "--base-url", stub_url,
         "entities", "search", "--identifier=x", "--entity-type", "transcription",
         "--by", "original_source_file", "--limit", "5"],
        env=env, cwd=cwd, capture_output=True, text=True, timeout=60,
    )


def _pinned(host):
    env = pci.build_pinned_env(
        "piculet", host.base, keys_dir=host.keys, rc_dir=RC_DIR, state_dir=host.state
    )
    return env


def test_pinned_cli_sends_a_signature_and_no_authorization_header(host, stub, tmp_path):
    env = _pinned(host)
    assert pci.pinned_child_problem(env) is None
    done = _search(pci.cli_isolation_prefix(env), env, stub.url, tmp_path)
    assert stub.requests, f"the CLI made no request: {done.stderr[-300:]}"
    assert all("authorization" not in r for r in stub.requests), "a bearer was presented"
    signed = [r for r in stub.requests if r.get("signature") or r.get("signature-input")]
    assert signed, "no request carried a signature"
    # The signed claims are piculet's own (sub from the pin, iss from the key file),
    # not the stored identity's and not the CLI's default.
    token = signed[0]["signature-key"].split('jwt="', 1)[1].split('"', 1)[0]
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["sub"] == "piculet@ateles-swarm"
    assert claims["iss"] == "https://synthetic-issuer.example"


def test_control_without_the_isolation_the_stored_token_is_sent(host, stub, tmp_path):
    """The instrument check: the c25cec67 behaviour (env stripped, HOME real)
    DOES leak the stored token, so the test above can fail."""
    env = _pinned(host)
    env["HOME"] = str(host.home)  # the CLI sees the real HOME, as before the fix
    _search([], env, stub.url, tmp_path)
    assert any(r.get("authorization") == f"Bearer {STORED}" for r in stub.requests)


def test_a_project_env_cannot_reintroduce_a_bearer(host, stub, tmp_path):
    """Even a hostile .env in the CLI's cwd or default root is not loaded."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text("NEOTOMA_BEARER_TOKEN=SYNTHETIC-DOTENV-TOKEN\n")
    (cwd / "package.json").write_text('{"name": "neotoma"}')  # looks like a checkout
    env = _pinned(host)
    _search(pci.cli_isolation_prefix(env), env, stub.url, cwd)
    assert stub.requests
    assert all("authorization" not in r for r in stub.requests)


def test_control_a_checkout_env_is_loaded_when_not_isolated(host, stub, tmp_path):
    """Instrument check for the .env finding: with the project root unpinned, the
    same hostile file IS picked up (so the isolation above is what stops it)."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text("NEOTOMA_BEARER_TOKEN=SYNTHETIC-DOTENV-TOKEN\n")
    (cwd / "package.json").write_text('{"name": "neotoma"}')
    env = _pinned(host)
    env.pop("NEOTOMA_PROJECT_ROOT")
    env["HOME"] = str(tmp_path / "emptyhome")
    (tmp_path / "emptyhome").mkdir()
    _search([], env, stub.url, cwd)
    assert any(r.get("authorization") == "Bearer SYNTHETIC-DOTENV-TOKEN" for r in stub.requests)


def test_preflight_refuses_a_disable_flag_and_a_reachable_token(host):
    env = _pinned(host)
    assert pci.pinned_child_problem(env) is None
    for flag, value in (("NEOTOMA_CLI_AAUTH_DISABLE", "1"), ("NEOTOMA_CLI_AAUTH_ENABLE", "0")):
        assert flag in (pci.pinned_child_problem({**env, flag: value}) or "")
    # No isolation prepared: the real HOME's stored token is reachable.
    bare = {k: v for k, v in env.items() if k not in (pci.CLI_HOME_ENV, pci.CLI_PROJECT_ROOT_ENV)}
    assert "stored CLI token" in (pci.pinned_child_problem(bare) or "")
    # A token written into the private config is refused too.
    private_config = Path(env[pci.CLI_HOME_ENV]) / ".config" / "neotoma" / "config.json"
    private_config.write_text(json.dumps({"access_token": STORED}))
    assert "private CLI home" in (pci.pinned_child_problem(env) or "")

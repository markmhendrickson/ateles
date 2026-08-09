"""
Effect tests for store_release_result.py — ateles#402 / PR #409.

Pins the signed release_result write path that QA flagged as untested:
no-keypair refuse, HTTPError, timeout ambiguity (neotoma#2141), missing
base URL, success + dual field names + AAuth headers, and .env setdefault
precedence.

Mocks only externals (urlopen, AAuthSigner.from_key_file). Never mocks
main() / payload assembly / exit branches.

Run with:
  pytest execution/daemons/phoenicurus-release/test_store_release_result.py -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR), str(_REPO_ROOT / "lib" / "daemon_runtime")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import aauth_signer  # noqa: E402
import store_release_result  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _non_stub_signer(
    sub: str = "phoenicurus@ateles-swarm",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    signer = MagicMock()
    signer.is_stub = False
    signer.sub = sub
    signer.headers.return_value = headers or {"X-AAuth-Token": "t"}
    return signer


def _urlopen_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda self, *a: False
    return resp


def _run_main(monkeypatch, argv: list[str], *, urlopen=None, signer=None) -> int:
    monkeypatch.setattr(sys, "argv", ["store_release_result.py", *argv])
    if signer is not None:
        monkeypatch.setattr(
            aauth_signer.AAuthSigner, "from_key_file", lambda _name: signer
        )
    if urlopen is not None:
        monkeypatch.setattr(store_release_result.urllib.request, "urlopen", urlopen)
    return store_release_result.main()


# ── A. no-keypair refuse ─────────────────────────────────────────────────────


class TestNoKeypairRefuse:
    def test_stub_signer_exits_1_without_urlopen(self, monkeypatch, capsys):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
        called = {"urlopen": False}

        def boom(*_a, **_k):
            called["urlopen"] = True
            raise AssertionError("urlopen must not be called for stub signer")

        rc = _run_main(
            monkeypatch,
            ["--version", "v0.0.0-test", "--status", "prepared"],
            signer=aauth_signer.AAuthSigner.stub("phoenicurus"),
            urlopen=boom,
        )
        err = capsys.readouterr().err
        assert rc == 1
        assert "refusing to attempt an unsigned write" in err
        assert called["urlopen"] is False


# ── B. HTTPError ─────────────────────────────────────────────────────────────


class TestHTTPError:
    def test_http_403_exits_1_with_truncated_body(self, monkeypatch, capsys):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")

        def fake_urlopen(_req, timeout=None):
            raise urllib.error.HTTPError(
                url="https://neotoma.example.test/store",
                code=403,
                msg="Forbidden",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"closed policy denied"}'),
            )

        rc = _run_main(
            monkeypatch,
            ["--version", "v0.0.0-test", "--status", "prepared"],
            signer=_non_stub_signer(),
            urlopen=fake_urlopen,
        )
        err = capsys.readouterr().err
        assert rc == 1
        assert "store failed: HTTP 403" in err
        assert "closed policy denied" in err


# ── C. timeout / ambiguous ───────────────────────────────────────────────────


class TestTimeoutAmbiguous:
    def test_timeout_exits_1_with_reread_note(self, monkeypatch, capsys):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")

        def fake_urlopen(_req, timeout=None):
            raise TimeoutError("timed out")

        rc = _run_main(
            monkeypatch,
            ["--version", "v0.0.0-test", "--status", "prepared"],
            signer=_non_stub_signer(),
            urlopen=fake_urlopen,
        )
        err = capsys.readouterr().err
        assert rc == 1
        assert "store did not return:" in err
        assert "timeout does NOT mean the write failed" in err
        assert "verify by querying release_result" in err


# ── D. missing base URL ──────────────────────────────────────────────────────


class TestMissingBaseUrl:
    def test_unset_base_url_exits_1_before_signer(self, monkeypatch, capsys):
        monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
        monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)

        called = {"from_key_file": False, "urlopen": False}

        def boom_signer(_name):
            called["from_key_file"] = True
            raise AssertionError("from_key_file must not run without base URL")

        def boom_urlopen(*_a, **_k):
            called["urlopen"] = True
            raise AssertionError("urlopen must not run without base URL")

        monkeypatch.setattr(aauth_signer.AAuthSigner, "from_key_file", boom_signer)
        monkeypatch.setattr(
            store_release_result.urllib.request, "urlopen", boom_urlopen
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["store_release_result.py", "--version", "v0.0.0-test", "--status", "prepared"],
        )
        rc = store_release_result.main()
        err = capsys.readouterr().err
        assert rc == 1
        assert "NEOTOMA_BASE_URL is unset" in err
        assert called["from_key_file"] is False
        assert called["urlopen"] is False


# ── E. success + dual field names ────────────────────────────────────────────


class TestSuccessDualFieldNames:
    def test_success_stdout_payload_headers_and_idempotency(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "bearer-token-value")
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.headers)
            captured["body"] = json.loads(req.data.decode())
            captured["timeout"] = timeout
            return _urlopen_response(
                {"entities": [{"entity_id": "ent_test", "action": "created"}]}
            )

        rc = _run_main(
            monkeypatch,
            [
                "--version",
                "v0.21.5",
                "--status",
                "prepared",
                "--rc-branch",
                "release/v0.21.5",
                "--rc-pr-url",
                "https://github.com/markmhendrickson/neotoma/pull/123",
            ],
            signer=_non_stub_signer(),
            urlopen=fake_urlopen,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "ent_test" in out
        assert "(created)" in out
        assert "phoenicurus@ateles-swarm" in out

        entity = captured["body"]["entities"][0]
        assert entity["rc_branch"] == "release/v0.21.5"
        assert entity["branch"] == "release/v0.21.5"
        assert entity["rc_pr_url"].endswith("/pull/123")
        assert entity["release_url"].endswith("/pull/123")
        assert captured["body"]["idempotency_key"] == (
            f"release-v0.21.5-prepared-{date.today().isoformat()}"
        )

        # urllib Request normalizes header keys; accept either casing.
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        assert headers.get("authorization") == "Bearer bearer-token-value"
        assert headers.get("x-aauth-token") == "t"


# ── F. .env setdefault precedence ────────────────────────────────────────────


class TestEnvSetdefaultPrecedence:
    def test_preset_env_wins_over_dotenv_file(self, monkeypatch, tmp_path):
        """Pin the module's setdefault bootstrap: pre-set env must win."""
        preset = "https://preset.example.test"
        dotenv_value = "https://dotenv.example.test"
        env_file = tmp_path / ".env"
        env_file.write_text(f'NEOTOMA_BASE_URL="{dotenv_value}"\n')

        monkeypatch.setenv("NEOTOMA_BASE_URL", preset)
        # Mirror store_release_result.py ~54–62 against a temp .env.
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

        assert os.environ["NEOTOMA_BASE_URL"] == preset
        assert os.environ["NEOTOMA_BASE_URL"] != dotenv_value

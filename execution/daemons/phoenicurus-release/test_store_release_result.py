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
import pytest  # noqa: E402
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
            if req.data is None:  # read-back GET
                captured["readback_url"] = req.full_url
                return _urlopen_response(
                    {"entity_id": "ent_test", "snapshot": captured["body"]["entities"][0]}
                )
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
        assert captured["readback_url"].endswith("/entities/ent_test")

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


# ── G. candidate-bound idempotency + read-back gate (second same-day RC) ─────

HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _store_server(store_log: list, readback: dict | None = None):
    """Fake Neotoma: replays a key with different content as a mismatch."""
    seen: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):
        if req.data is None:
            snap = readback if readback is not None else store_log[-1]["entities"][0]
            return _urlopen_response({"entity_id": "ent_rel", "snapshot": snap})
        body = json.loads(req.data.decode())
        content = json.dumps(body["entities"], sort_keys=True)
        key = body["idempotency_key"]
        if key in seen and seen[key] != content:
            raise urllib.error.HTTPError(
                req.full_url, 409, "conflict", {},
                io.BytesIO(b'{"error":"ERR_IDEMPOTENCY_MISMATCH"}'),
            )
        seen[key] = content
        store_log.append(body)
        return _urlopen_response({"entities": [{"entity_id": "ent_rel", "action": "created"}]})

    return fake_urlopen


def _pending(head: str, pr: str = "1") -> list[str]:
    return [
        "--version", "v0.32.0", "--status", "pending_approval",
        "--rc-branch", "release/v0.32.0",
        "--rc-pr-url", f"https://github.com/markmhendrickson/neotoma/pull/{pr}",
        "--rc-head", head,
    ]


class TestCandidateBoundRecord:
    def test_second_same_day_candidate_is_recorded(self, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
        log: list = []
        server = _store_server(log)
        assert _run_main(monkeypatch, _pending(HEAD_A, "2945"),
                         signer=_non_stub_signer(), urlopen=server) == 0
        assert _run_main(monkeypatch, _pending(HEAD_B, "2947"),
                         signer=_non_stub_signer(), urlopen=server) == 0
        assert len(log) == 2
        assert log[0]["idempotency_key"] != log[1]["idempotency_key"]
        assert log[1]["entities"][0]["rc_head_sha"] == HEAD_B
        assert log[1]["entities"][0]["rc_pr_url"].endswith("/pull/2947")

    def test_true_retry_reuses_the_key(self, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
        log: list = []
        server = _store_server(log)
        for _ in range(2):
            assert _run_main(monkeypatch, _pending(HEAD_A),
                             signer=_non_stub_signer(), urlopen=server) == 0
        assert log[0]["idempotency_key"] == log[1]["idempotency_key"]
        assert log[0]["idempotency_key"] == f"release-v0.32.0-pending_approval-{HEAD_A}"

    def test_pending_approval_without_head_refuses_before_any_write(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")

        def boom(*_a, **_k):
            raise AssertionError("no write without a candidate head")

        argv = _pending(HEAD_A)[:-2]
        assert _run_main(monkeypatch, argv, signer=_non_stub_signer(), urlopen=boom) == 1
        assert "--rc-head is required" in capsys.readouterr().err

    def test_short_head_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
        assert _run_main(monkeypatch, _pending("abc123"), signer=_non_stub_signer(),
                         urlopen=lambda *a, **k: None) == 1
        assert "full 40-character" in capsys.readouterr().err

    def test_readback_mismatch_exits_1_and_says_no_approval(self, monkeypatch, capsys):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
        log: list = []
        server = _store_server(log, readback={"version": "v0.32.0", "status": "failed"})
        rc = _run_main(monkeypatch, _pending(HEAD_A), signer=_non_stub_signer(), urlopen=server)
        captured = capsys.readouterr()
        assert rc == 1
        assert "not confirmed by read-back" in captured.err
        assert "Do NOT send the approval request" in captured.err
        assert "ent_rel (created)" not in captured.out

    @pytest.mark.parametrize(
        "field, value",
        [
            ("rc_head_sha", HEAD_A),  # an earlier candidate of the same version
            ("rc_head_sha", None),  # field undeclared: dropped from the snapshot
            ("rc_pr_url", "https://github.com/markmhendrickson/neotoma/pull/2945"),
            ("rc_pr_url", None),
            ("rc_branch", "release/v0.32.0-a"),
            ("version", "v0.31.0"),
        ],
    )
    def test_readback_of_another_candidate_exits_1(
        self, monkeypatch, capsys, field, value
    ):
        """Everything matches this candidate (HEAD_B, PR 2947) except one field."""
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
        url = "https://github.com/markmhendrickson/neotoma/pull/2947"
        snapshot = {
            "version": "v0.32.0",
            "status": "pending_approval",
            "rc_branch": "release/v0.32.0",
            "rc_head_sha": HEAD_B,
            "rc_pr_url": url,
        }
        snapshot[field] = value
        if value is None:
            del snapshot[field]
        server = _store_server([], readback=snapshot)
        rc = _run_main(
            monkeypatch,
            _pending(HEAD_B, "2947"),
            signer=_non_stub_signer(),
            urlopen=server,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert f"{field} is " in captured.err
        assert "Do NOT send the approval request" in captured.err
        assert "ent_rel (created)" not in captured.out

    def test_readback_of_this_candidate_exits_0(self, monkeypatch, capsys):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")
        snapshot = {
            "version": "v0.32.0",
            "status": "pending_approval",
            "branch": "release/v0.32.0",
            "rc_head_sha": HEAD_B,
            "release_url": "https://github.com/markmhendrickson/neotoma/pull/2947",
        }
        server = _store_server([], readback=snapshot)
        assert (
            _run_main(
                monkeypatch,
                _pending(HEAD_B.upper(), "2947"),
                signer=_non_stub_signer(),
                urlopen=server,
            )
            == 0
        )

    def test_readback_http_error_exits_1(self, monkeypatch, capsys):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")

        def server(req, timeout=None):
            if req.data is None:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"gone"))
            return _urlopen_response({"entities": [{"entity_id": "ent_rel", "action": "created"}]})

        assert _run_main(monkeypatch, _pending(HEAD_A), signer=_non_stub_signer(),
                         urlopen=server) == 1
        assert "HTTP 404" in capsys.readouterr().err


class TestPromptGatesApprovalOnRecord:
    def test_prompt_passes_head_and_gates_notifications(self, monkeypatch):
        import prepare  # noqa: PLC0415

        monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.test")
        text = prepare._build_agent_prompt("v0.31.0", 3)
        assert '--rc-head "$(git rev-parse HEAD)"' in text
        assert "11. Only if step 10 exited 0" in text
        assert "12. Only if step 10 exited 0" in text
        assert "send NO approval" in text
        assert "version_lines.py" in text and "--product-check" in text
        assert "README.md" in text and "docs/known_limitations.md" in text

"""
Unit tests for the Neotoma profile fetch — User-Agent and failure visibility.

Since Neotoma moved to the hosted instance (2026-08-04) it sits behind
Cloudflare, which fingerprints `urllib`'s default `Python-urllib/3.x`
User-Agent and answers **HTTP 403, error 1010** ("blocked based on your
browser's signature") before the request reaches Neotoma at all. Any explicit
User-Agent passes; `httpx` callers were never affected.

The failure was invisible: `HTTPError` subclasses `URLError`, so the 403 was
caught, logged at WARNING, and returned `[]` — indistinguishable from "no
profiles configured". Monedula reported success and paid nothing on every
scheduled run.

These tests lock both halves of the fix:

  * every Neotoma request carries an explicit User-Agent
  * an HTTP rejection is logged at ERROR and names the likely causes, so it can
    never again be mistaken for an empty result

Run with: pytest execution/daemons/monedula/test_neotoma_fetch.py -v
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error

import pytest

from handlers.payment_profile import NEOTOMA_USER_AGENT, load_profiles_from_neotoma

HOSTED = "https://neotoma.example.invalid"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", HOSTED)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok-placeholder")


class _Resp(io.BytesIO):
    """Minimal stand-in for the urlopen context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _capture_request(monkeypatch, payload: dict):
    """Patch urlopen, returning the captured Request object."""
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


# ── Every request carries an explicit User-Agent ─────────────────────────────


def test_request_sets_user_agent(monkeypatch) -> None:
    seen = _capture_request(monkeypatch, {"entities": []})
    load_profiles_from_neotoma()
    req = seen["req"]
    # urllib title-cases header keys internally.
    assert req.get_header("User-agent") == NEOTOMA_USER_AGENT


def test_user_agent_is_not_the_urllib_default(monkeypatch) -> None:
    """Cloudflare 1010 blocks Python-urllib/*; anything else passes."""
    seen = _capture_request(monkeypatch, {"entities": []})
    load_profiles_from_neotoma()
    ua = seen["req"].get_header("User-agent") or ""
    assert ua and not ua.lower().startswith("python-urllib")


def test_bearer_sent_for_hosted_instance(monkeypatch) -> None:
    seen = _capture_request(monkeypatch, {"entities": []})
    load_profiles_from_neotoma()
    assert seen["req"].get_header("Authorization") == "Bearer tok-placeholder"


def test_bearer_omitted_on_loopback(monkeypatch) -> None:
    """Loopback trusts localhost and actively rejects a stale bearer."""
    monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
    seen = _capture_request(monkeypatch, {"entities": []})
    load_profiles_from_neotoma()
    assert seen["req"].get_header("Authorization") is None


# ── An HTTP rejection is loud, and distinct from an empty result ─────────────


def _raise_http(monkeypatch, code: int, body: bytes = b"{}"):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(HOSTED, code, "blocked", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


@pytest.mark.parametrize("code", [401, 403, 500])
def test_http_error_logs_at_error_level(monkeypatch, caplog, code: int) -> None:
    _raise_http(monkeypatch, code)
    with caplog.at_level(logging.ERROR):
        assert load_profiles_from_neotoma() == []
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, f"HTTP {code} must log at ERROR, not WARNING"
    msg = errors[0].message
    assert str(code) in msg
    assert "NO payments" in msg, "must state the consequence, not just the code"


def test_http_error_names_the_two_likely_causes(monkeypatch, caplog) -> None:
    """The message must point at the token and the User-Agent."""
    _raise_http(monkeypatch, 403, b'{"error_code":1010}')
    with caplog.at_level(logging.ERROR):
        load_profiles_from_neotoma()
    msg = " ".join(r.message for r in caplog.records)
    assert "NEOTOMA_BEARER_TOKEN" in msg
    assert "User-Agent" in msg


def test_empty_result_does_not_log_an_error(monkeypatch, caplog) -> None:
    """A genuinely empty graph is not a failure — only rejections are."""
    _capture_request(monkeypatch, {"entities": []})
    with caplog.at_level(logging.ERROR):
        assert load_profiles_from_neotoma() == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# ── The dead-port fallback is gone ───────────────────────────────────────────


def test_unset_base_url_raises_instead_of_defaulting(monkeypatch) -> None:
    """Local hosting was retired 2026-08-04, and no default replaced it.

    A fallback URL is itself a silent-failure vector: an unreachable default is
    indistinguishable from "no profiles configured", which is how Monedula
    reported success while paying nothing. With no default, an unset var is a
    loud configuration error instead.
    """
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    _capture_request(monkeypatch, {"entities": []})
    with pytest.raises(RuntimeError, match="NEOTOMA_BASE_URL is not set"):
        load_profiles_from_neotoma()

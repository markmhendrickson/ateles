"""Apus webhook admission fails closed when the signing secret is not configured."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

pytest.importorskip("aiohttp")

_spec = importlib.util.spec_from_file_location("apus_under_test", _HERE / "apus.py")
apus = importlib.util.module_from_spec(_spec)
sys.modules["apus_under_test"] = apus
_spec.loader.exec_module(apus)


@pytest.mark.parametrize("secret", ["", "   "])
def test_verify_signature_refuses_without_secret(monkeypatch, secret):
    monkeypatch.setattr(apus, "WEBHOOK_SECRET", secret)
    assert apus._verify_signature(b"{}", "") is False
    assert apus._verify_signature(b"{}", "sha256=anything") is False


def test_verify_signature_requires_header_and_match(monkeypatch):
    monkeypatch.setattr(apus, "WEBHOOK_SECRET", "s3cret")
    body = b'{"files": []}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert apus._verify_signature(body, good) is True
    assert apus._verify_signature(body, "") is False
    assert apus._verify_signature(body, "sha256=bad") is False


class _Req:
    def __init__(self, body=b'{"profile": "x", "files": []}'):
        self.headers = {}
        self.app = {"notifier": object()}
        self._body = body

    async def read(self):
        return self._body


def test_unset_secret_refuses_unsigned_delivery(monkeypatch):
    monkeypatch.setattr(apus, "WEBHOOK_SECRET", "")
    resp = asyncio.run(apus.handle_webhook(_Req()))
    assert resp.status == 503


def test_whitespace_only_secret_refuses_delivery(monkeypatch):
    monkeypatch.setattr(apus, "WEBHOOK_SECRET", "   ")
    resp = asyncio.run(apus.handle_webhook(_Req()))
    assert resp.status == 503


def test_non_ascii_signature_header_is_denied_not_raised(monkeypatch):
    monkeypatch.setattr(apus, "WEBHOOK_SECRET", "s3cret")
    assert apus._verify_signature(b"{}", "sha256=\u00e9\u4e2d") is False

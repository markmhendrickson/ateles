"""
test_wise_receipt.py — Wise receipt (justificante) fetch is best-effort + safe.

Covers _fetch_wise_receipt:
  - writes a PDF to MONEDULA_RECEIPTS_DIR and returns its path on success
  - returns None (never raises) on non-PDF bodies or network errors
so a receipt problem can never break a payment that already succeeded.
"""

import io
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from handlers import wise_transfer as wt  # noqa: E402


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_receipt_writes_pdf(monkeypatch, tmp_path):
    monkeypatch.setenv("MONEDULA_RECEIPTS_DIR", str(tmp_path))
    monkeypatch.setattr(wt.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(b"%PDF-1.4 fake receipt bytes"))
    path = wt._fetch_wise_receipt("tok", "999", label="t")
    assert path is not None
    p = Path(path)
    assert p.exists() and p.name == "wise-receipt-999.pdf"
    assert p.read_bytes().startswith(b"%PDF-")


def test_fetch_receipt_none_on_non_pdf(monkeypatch, tmp_path):
    monkeypatch.setenv("MONEDULA_RECEIPTS_DIR", str(tmp_path))
    monkeypatch.setattr(wt.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(b"<html>not a pdf</html>"))
    assert wt._fetch_wise_receipt("tok", "999", label="t") is None


def test_fetch_receipt_none_on_network_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MONEDULA_RECEIPTS_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(wt.urllib.request, "urlopen", _boom)
    # Must not raise — best-effort.
    assert wt._fetch_wise_receipt("tok", "999", label="t") is None

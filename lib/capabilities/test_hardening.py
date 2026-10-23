"""Download hardening, the shared Neotoma helper, and import layering."""

import socket
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from lib.capabilities import neotoma_http, slots
from lib.capabilities.errors import BINDING_MISSING, GenerationRefused, VendorFailure
from lib.capabilities.vendor_binding import resolve_vendor_binding
from lib.capabilities.vendors import HttpResponse, VeoAdapter, urllib_transport

from .test_vendors import KEY, Fake

GOOD = "https://generativelanguage.googleapis.com/v1beta/files/abc:download?alt=media"


def _download(uri, *responses):
    t = Fake(*responses)
    return VeoAdapter(t, sleep=lambda s: None, ffmpeg="/x")._download(uri, KEY), t


@pytest.mark.parametrize(
    "uri",
    [
        "https://user" + "@" + "generativelanguage.googleapis.com/v1beta/f",
        "https://user:pw" + "@" + "generativelanguage.googleapis.com/v1beta/f",
        "https://generativelanguage.googleapis.com:8443/v1beta/f",
        "https://generativelanguage.googleapis.com:443/v1beta/f",
        "https://generativelanguage.googleapis.com\\@evil.example/v1beta/f",
        "https://generativelanguage.googleapis.com/v1beta\\f",
        "http://generativelanguage.googleapis.com/v1beta/f",
        "https://evil.example/generativelanguage.googleapis.com",
        "https://generativelanguage.googleapis.com.evil.example/f",
        "https://generativelanguage.googleapis.com:notaport/f",
    ],
)
def test_suspicious_download_uris_are_refused_before_any_request(uri):
    t = Fake()
    with pytest.raises(VendorFailure):
        VeoAdapter(t, sleep=lambda s: None, ffmpeg="/x")._download(uri, KEY)
    assert t.requests == []


def test_a_good_uri_downloads_with_the_key_header():
    data, t = _download(GOOD, HttpResponse(200, b"mp4"))
    assert data == b"mp4" and t.requests[0][2]["x-goog-api-key"] == KEY.reveal()


@pytest.mark.parametrize("status", [301, 302, 304, 400, 500])
def test_any_non_2xx_download_is_a_failure_even_with_a_body(status):
    with pytest.raises(VendorFailure):
        _download(GOOD, HttpResponse(status, b"<html>a body</html>"))


def test_a_truncated_download_is_a_failure():
    with pytest.raises(VendorFailure):
        _download(GOOD, HttpResponse(200, b"12345", {"Content-Length": "10"}))
    assert _download(GOOD, HttpResponse(200, b"12345", {"Content-Length": "5"}))[0] == b"12345"


# --- transport classification --------------------------------------------------


class _FakeOpener:
    def __init__(self, exc):
        self.exc = exc

    def open(self, req, timeout):
        raise self.exc


@pytest.mark.parametrize(
    "exc, no_charge",
    [
        (urllib.error.URLError(socket.gaierror("no such host")), True),
        (urllib.error.URLError(ConnectionRefusedError()), True),
        (urllib.error.URLError(TimeoutError()), False),
        (urllib.error.URLError(ConnectionResetError()), False),
        (TimeoutError(), False),
    ],
)
def test_only_a_failure_that_never_reached_the_vendor_is_no_charge(monkeypatch, exc, no_charge):
    import urllib.request

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: _FakeOpener(exc))
    with pytest.raises(VendorFailure) as info:
        urllib_transport("POST", "https://example.invalid/x", headers={}, body=b"", timeout=1)
    assert info.value.no_charge is no_charge


# --- the shared Neotoma helper -----------------------------------------------------


def test_no_hardcoded_host_default(monkeypatch):
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    with pytest.raises(neotoma_http.NeotomaConfigError):
        neotoma_http.base_url()
    source = (Path(neotoma_http.__file__)).read_text()
    assert "markmhendrickson" not in source and ".com" not in source.split('"""', 2)[2]


def test_base_url_must_be_https(monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", "http://neotoma.internal.example")
    with pytest.raises(neotoma_http.NeotomaConfigError):
        neotoma_http.base_url()


def test_redirects_are_never_followed_and_the_token_is_never_echoed(monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.net")
    token = "tok" + "-abcdef-123456"
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", token)
    seen = {}

    class Opener:
        def open(self, req, timeout):
            seen["auth"] = req.get_header("Authorization")
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {}, __import__("io").BytesIO(f"moved {token}".encode()))

    import urllib.request

    handlers = []
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: handlers.extend(h) or Opener())
    with pytest.raises(neotoma_http.NeotomaRequestError) as exc:
        neotoma_http.request_json("GET", "/entities/x")
    assert token not in exc.value.body and "[REDACTED]" in exc.value.body
    assert handlers and handlers[0]().redirect_request(None, None, 302, "", {}, "") is None


def test_missing_token_or_base_url_is_a_config_problem_not_a_missing_binding(monkeypatch):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.net")
    with pytest.raises(GenerationRefused) as exc:
        resolve_vendor_binding(slots.IMAGE_GENERATION)
    err = exc.value
    assert err.code == BINDING_MISSING
    assert "NEOTOMA_BEARER_TOKEN" in err.message
    assert "client configuration problem" in err.hint and "NEOTOMA_BASE_URL" in err.hint
    assert "unreachable" not in err.hint.lower()


def test_store_outage_and_store_rejection_are_told_apart():
    def outage(_):
        raise ConnectionError("down")

    def rejected(_):
        raise neotoma_http.NeotomaRequestError(401, "unauthorized")

    with pytest.raises(GenerationRefused) as a:
        resolve_vendor_binding(slots.IMAGE_GENERATION, fetch=outage)
    with pytest.raises(GenerationRefused) as b:
        resolve_vendor_binding(slots.IMAGE_GENERATION, fetch=rejected)
    assert "unreachable" in a.value.hint and "401" in b.value.message and "token" in b.value.hint


# --- import layering ---------------------------------------------------------------


def test_the_env_scrub_module_does_not_load_the_client():
    code = (
        "import sys; import lib.credential_scrub as m; "
        "assert m.is_generation_credential('GEMINI_API_KEY'); "
        "bad=[k for k in sys.modules if k.startswith('lib.capabilities')]; "
        "assert not bad, bad"
    )
    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.run([sys.executable, "-c", code], cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_skill_runner_imports_the_neutral_module_not_the_package():
    src = (Path(__file__).resolve().parents[2] / "execution/daemons/apis/skill_runner.py").read_text()
    assert "from lib.credential_scrub import" in src and "lib.capabilities" not in src

import os

import pytest

from lib.capabilities import credentials
from lib.capabilities.credentials import Secret, redact, resolve_credential
from lib.capabilities.errors import CREDENTIAL_UNRESOLVED, GenerationRefused

SLOT = "image_generation"
NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


@pytest.fixture
def cred_dir(tmp_path, monkeypatch):
    d = tmp_path / "ateles"
    d.mkdir()
    monkeypatch.setenv(credentials.CREDENTIAL_DIR_ENV, str(d))
    return d


def _file(cred_dir, text, mode=0o600):
    p = cred_dir / "generation.env"
    p.write_text(text)
    p.chmod(mode)
    return str(p)


def _resolve(path, **kw):
    return resolve_credential(
        SLOT,
        credential_location=kw.pop("loc", "GEMINI_API_KEY"),
        allowed_names=NAMES,
        credential_file=path,
        process_values=kw.pop("process_values", {}),
    )


def test_reads_key_from_file_without_touching_the_process_environment(cred_dir):
    before = dict(os.environ)
    path = _file(cred_dir, "# c\nGEMINI_API_KEY='abcdefgh12345678'\nOTHER=zzz\n")
    secret = _resolve(path)
    assert secret.reveal() == "abcdefgh12345678"
    assert "abcdefgh12345678" not in repr(secret) and "abcdefgh12345678" not in str(secret)
    assert dict(os.environ) == before
    assert "GEMINI_API_KEY" not in os.environ


def test_file_wins_over_process_value(cred_dir):
    path = _file(cred_dir, "GEMINI_API_KEY=from_the_file_1234\n")
    assert _resolve(path, process_values={"GEMINI_API_KEY": "from_process_9999"}).reveal() == "from_the_file_1234"


def test_falls_back_to_client_process_value(cred_dir):
    path = _file(cred_dir, "SOMETHING_ELSE=1\n")
    assert _resolve(path, process_values={"GEMINI_API_KEY": "from_process_9999"}).reveal() == "from_process_9999"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_group_or_world_accessible_file_is_refused(cred_dir, mode):
    path = _file(cred_dir, "GEMINI_API_KEY=abcdefgh12345678\n", mode=mode)
    with pytest.raises(GenerationRefused) as exc:
        _resolve(path)
    assert exc.value.code == CREDENTIAL_UNRESOLVED


def test_file_outside_the_credential_directory_is_refused(cred_dir, tmp_path):
    outside = tmp_path / "elsewhere.env"
    outside.write_text("GEMINI_API_KEY=abcdefgh12345678\n")
    outside.chmod(0o600)
    with pytest.raises(GenerationRefused) as exc:
        _resolve(str(outside))
    assert exc.value.code == CREDENTIAL_UNRESOLVED
    assert "outside" in exc.value.message


def test_symlink_escape_is_refused(cred_dir, tmp_path):
    target = tmp_path / "secret.env"
    target.write_text("GEMINI_API_KEY=abcdefgh12345678\n")
    target.chmod(0o600)
    link = cred_dir / "link.env"
    link.symlink_to(target)
    with pytest.raises(GenerationRefused):
        _resolve(str(link))


def test_missing_file_and_missing_key_are_unresolved(cred_dir):
    with pytest.raises(GenerationRefused) as a:
        _resolve(str(cred_dir / "nope.env"))
    with pytest.raises(GenerationRefused) as b:
        _resolve(_file(cred_dir, "GEMINI_API_KEY=\n"))
    assert a.value.code == b.value.code == CREDENTIAL_UNRESOLVED


@pytest.mark.parametrize(
    "loc",
    ["NEOTOMA_BEARER_TOKEN", "OPENAI_API_KEY", "PATH", "gemini_api_key", "GEMINI_API_KEY x", ""],
)
def test_binding_cannot_redirect_the_client_to_an_unrelated_secret(cred_dir, loc):
    path = _file(cred_dir, "NEOTOMA_BEARER_TOKEN=leaky_leaky_1234\nOPENAI_API_KEY=oai_oai_oai_1\n")
    with pytest.raises(GenerationRefused) as exc:
        _resolve(path, loc=loc, process_values={"NEOTOMA_BEARER_TOKEN": "leaky_leaky_1234"})
    assert exc.value.code == CREDENTIAL_UNRESOLVED


def test_oauth_route_has_no_host_held_key():
    with pytest.raises(GenerationRefused) as exc:
        resolve_credential(
            SLOT,
            credential_location="oauth:recraft-mcp (OAuth session in the operator's config)",
            allowed_names=("RECRAFT_API_KEY",),
        )
    assert exc.value.code == CREDENTIAL_UNRESOLVED
    assert "export" not in exc.value.hint.lower()


def test_redact_scrubs_the_value():
    s = Secret("supersecretvalue")
    assert redact("boom supersecretvalue boom", s) == "boom [REDACTED] boom"
    assert redact("short", "abc") == "short"


def test_no_subprocess_receives_the_credential():
    """The package never builds a child environment from the credential."""
    import pathlib
    import re

    pkg = pathlib.Path(__file__).parent
    for path in pkg.glob("*.py"):
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        text = path.read_text()
        assert not re.search(r"os\.environ\s*\[[^\]]+\]\s*=", text), path.name
        assert "os.putenv" not in text and "os.environ.update" not in text, path.name

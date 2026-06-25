"""Tests for the typed config surface (W1)."""

from __future__ import annotations

import pytest

from ateles import config
from ateles.config import load, to_json_dict


def _full_env() -> dict[str, str]:
    return {
        "ATELES_OPERATOR_DOMAIN": "example.com",
        "OPERATOR_NAME": "Jane Operator",
        "OPERATOR_EMAIL": "jane@example.com",
        "NEOTOMA_BASE_URL": "https://neotoma.example.com",
        "NEOTOMA_BEARER_TOKEN": "secret-token",
    }


def test_load_from_env_validates_clean(tmp_path):
    cfg = load(start=tmp_path, environ=_full_env())
    assert cfg.validate() == []
    assert cfg.get("operator_domain") == "example.com"


def test_missing_required_reported(tmp_path):
    problems = load(start=tmp_path, environ={}).validate()
    assert any("operator_domain" in p for p in problems)
    assert any("neotoma_bearer_token" in p for p in problems)


def test_env_overrides_file(tmp_path):
    (tmp_path / "ateles.config.json").write_text(
        '{"operator_name": "From File", "operator_domain": "file.example"}'
    )
    cfg = load(start=tmp_path, environ={"OPERATOR_NAME": "From Env"})
    assert cfg.get("operator_name") == "From Env"        # env wins
    assert cfg.get("operator_domain") == "file.example"  # file-only persists


def test_secrets_are_never_read_from_file(tmp_path):
    (tmp_path / "ateles.config.json").write_text('{"neotoma_bearer_token": "leaked"}')
    cfg = load(start=tmp_path, environ={})
    assert cfg.get("neotoma_bearer_token") is None


def test_redacted_masks_secrets(tmp_path):
    red = load(start=tmp_path, environ=_full_env()).redacted()
    assert red["neotoma_bearer_token"] == "***"
    assert red["operator_domain"] == "example.com"


def test_to_json_dict_excludes_secrets(tmp_path):
    d = to_json_dict(load(start=tmp_path, environ=_full_env()))
    assert "neotoma_bearer_token" not in d
    assert d["operator_domain"] == "example.com"


def test_invalid_neotoma_url_reported(tmp_path):
    env = {**_full_env(), "NEOTOMA_BASE_URL": "neotoma.example.com"}
    assert any("http" in p for p in load(start=tmp_path, environ=env).validate())


def test_bad_json_raises_config_error(tmp_path):
    (tmp_path / "ateles.config.json").write_text("{not valid json")
    with pytest.raises(config.ConfigError):
        load(start=tmp_path, environ={})


def test_config_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ATELES_CONFIG", str(tmp_path / "custom.json"))
    assert config.config_path().name == "custom.json"

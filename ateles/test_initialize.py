"""Tests for the ``ateles init`` wizard (W1)."""

from __future__ import annotations

import json

from ateles.initialize import run_init


def test_non_interactive_writes_only_nonsecret_settings(tmp_path):
    env = {
        "ATELES_OPERATOR_DOMAIN": "example.com",
        "OPERATOR_NAME": "Jane",
        "NEOTOMA_BASE_URL": "https://neotoma.example.com",
        "NEOTOMA_BEARER_TOKEN": "super-secret",  # secret -> must NOT be written
    }
    path = run_init(
        non_interactive=True,
        environ=env,
        start=tmp_path,
        output_fn=lambda *_: None,
    )
    data = json.loads(path.read_text())
    assert data["operator_domain"] == "example.com"
    assert data["operator_name"] == "Jane"
    assert "neotoma_bearer_token" not in data  # secret stays in the environment


def test_interactive_collects_answers(tmp_path):
    answers = iter([
        "example.org",            # operator_domain
        "Jill",                   # operator_name
        "jill@example.org",       # operator_email
        "https://n.example.org",  # neotoma_base_url
    ])

    def fake_input(_prompt):
        return next(answers, "")  # remaining optional prompts -> blank

    path = run_init(
        non_interactive=False,
        environ={},
        start=tmp_path,
        input_fn=fake_input,
        output_fn=lambda *_: None,
    )
    data = json.loads(path.read_text())
    assert data["operator_domain"] == "example.org"
    assert data["operator_name"] == "Jill"
    assert data["neotoma_base_url"] == "https://n.example.org"


def test_interactive_keeps_existing_value_on_blank(tmp_path):
    (tmp_path / "ateles.config.json").write_text('{"operator_name": "Existing"}')

    path = run_init(
        non_interactive=False,
        environ={},
        start=tmp_path,
        input_fn=lambda _prompt: "",  # accept all defaults
        output_fn=lambda *_: None,
    )
    data = json.loads(path.read_text())
    assert data["operator_name"] == "Existing"

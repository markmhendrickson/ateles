"""Tests for the ``ateles provision`` dry-run planner (W2)."""

from __future__ import annotations

from ateles.config import AtelesConfig
from ateles.provision import CONTEXT_SCHEMAS, plan, run_provision


def _clean_cfg() -> AtelesConfig:
    return AtelesConfig(values={
        "operator_domain": "example.com",
        "operator_name": "Jane",
        "operator_email": "jane@example.com",
        "neotoma_base_url": "https://neotoma.example.com",
        "neotoma_bearer_token": "tok",
    })


def test_plan_registers_each_context_schema():
    actions = [(s.action, s.detail) for s in plan(_clean_cfg())]
    registered = [d for a, d in actions if a == "register_schema"]
    for schema in CONTEXT_SCHEMAS:
        assert any(schema in d for d in registered)


def test_plan_seeds_operator_profile_from_config():
    details = [s.detail for s in plan(_clean_cfg()) if s.action == "seed_entity"]
    assert any("example.com" in d and "Jane" in d for d in details)


def test_plan_includes_keypair_and_grant_steps():
    actions = {s.action for s in plan(_clean_cfg())}
    assert {"mint_keypair", "create_grant"} <= actions


def test_run_provision_dry_run_is_read_only_and_succeeds(capsys=None):
    out: list[str] = []
    rc = run_provision(cfg=_clean_cfg(), output_fn=out.append)
    assert rc == 0
    joined = "\n".join(out)
    assert "dry-run" in joined.lower()
    assert "register_schema" in joined


def test_run_provision_reports_incomplete_config():
    out: list[str] = []
    rc = run_provision(cfg=AtelesConfig(values={}), output_fn=out.append)
    assert rc == 1
    assert any("incomplete" in line.lower() for line in out)


def test_run_provision_commit_refuses_until_w3_w4():
    out: list[str] = []
    rc = run_provision(commit=True, cfg=_clean_cfg(), output_fn=out.append)
    assert rc == 3
    assert any("not yet implemented" in line.lower() for line in out)

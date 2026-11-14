"""
Unit tests for GrantChecker tool-grant parsing and constraint enforcement (#26).

Run with:   .venv/bin/python lib/daemon_runtime/test_grant_checker.py
Or pytest:  .venv/bin/python -m pytest lib/daemon_runtime/test_grant_checker.py -v

These tests exercise pure logic only (parsing + constraint evaluation); no
network calls to Neotoma are made.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.grant_checker import (  # noqa: E402
    AgentGrant,
    GrantChecker,
    check_param_constraints,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _monedula_grant_entity() -> dict:
    """A grant entity in the live schema shape with mixed entity + tool caps."""
    return {
        "entity_id": "ent_monedula_grant",
        "snapshot": {
            "match_sub": "monedula@ateles-swarm",
            "match_iss": "https://markmhendrickson.com",
            "status": "active",
            "capabilities": [
                {"op": "store_structured", "entity_types": ["transaction"]},
                {"op": "retrieve", "entity_types": ["*"]},
                {
                    "op": "tool:parquet:read_parquet",
                    "param_constraints": {"tables": ["transactions", "accounts"]},
                },
                {
                    "op": "tool:btc-wallet:btc_send_transfer",
                    "param_constraints": {"max_amount_sats": 500000, "to_allowlist": True},
                },
                {"op": "tool:btc-wallet:btc_wallet_get_balance"},
            ],
        },
    }


def _legacy_grant_entity() -> dict:
    """A grant with NO tool capabilities (un-migrated agent)."""
    return {
        "entity_id": "ent_legacy",
        "snapshot": {
            "match_sub": "cicada@ateles-swarm",
            "status": "active",
            "capabilities": [
                {"op": "github_harness:write", "repos": ["markmhendrickson/ateles"]},
            ],
        },
    }


# ── Parsing tests ───────────────────────────────────────────────────────────────


def test_parse_match_sub_and_ops():
    g = GrantChecker._parse(_monedula_grant_entity())
    assert g.match_sub == "monedula@ateles-swarm"
    assert g.aauth_sub == "monedula@ateles-swarm"  # backward-compat alias
    assert "store_structured" in g.ops
    assert "retrieve" in g.ops
    assert g.is_active


def test_parse_tool_grants_map():
    g = GrantChecker._parse(_monedula_grant_entity())
    assert "parquet:read_parquet" in g.tool_grants
    assert g.tool_grants["parquet:read_parquet"] == {"tables": ["transactions", "accounts"]}
    # No-constraint tool grant becomes empty dict (allowed, unconstrained).
    assert g.tool_grants["btc-wallet:btc_wallet_get_balance"] == {}


def test_tool_constraints_lookup_and_wildcards():
    g = AgentGrant(
        match_sub="x@ateles-swarm",
        aauth_sub="x@ateles-swarm",
        ops={"tool:parquet:*"},
        tool_grants={"parquet:*": {"tables": ["t1"]}},
        status="active",
    )
    # server-wildcard hit
    assert g.tool_constraints("parquet", "read_parquet") == {"tables": ["t1"]}
    # different server → no match
    assert g.tool_constraints("btc-wallet", "btc_send_transfer") is None


# ── check_tool tests ────────────────────────────────────────────────────────────


def _checker_with(*entities) -> GrantChecker:
    c = GrantChecker("monedula@ateles-swarm")
    c._grants = [GrantChecker._parse(e) for e in entities]
    c._loaded = True
    return c


def test_check_tool_allowed_with_constraints():
    c = _checker_with(_monedula_grant_entity())
    allowed, constraints = c.check_tool("parquet", "read_parquet")
    assert allowed is True
    assert constraints == {"tables": ["transactions", "accounts"]}


def test_check_tool_denied_when_absent():
    c = _checker_with(_monedula_grant_entity())
    # github_harness explicitly not granted → denied
    allowed, constraints = c.check_tool("github_harness", "create_pr")
    assert allowed is False
    assert constraints is None


def test_check_tool_denies_when_no_tool_grants_anywhere(monkeypatch):
    # Grants that declare no tool caps used to permit; now they deny.
    monkeypatch.delenv("ATELES_GRANTS_ADVISORY", raising=False)
    c = GrantChecker("cicada@ateles-swarm")
    c._grants = [GrantChecker._parse(_legacy_grant_entity())]
    c._loaded = True
    allowed, constraints = c.check_tool("btc-wallet", "btc_send_transfer")
    assert allowed is False
    assert constraints is None


def _degraded_checkers():
    no_token = GrantChecker("x@ateles-swarm")
    no_token._load_error = "NEOTOMA_BEARER_TOKEN not set"
    no_token._loaded = True
    no_grants = GrantChecker("x@ateles-swarm")
    no_grants._loaded = True
    not_loaded = GrantChecker("x@ateles-swarm")
    return [no_token, no_grants, not_loaded]


def test_degraded_grant_read_denies_by_default(monkeypatch):
    for value in (None, "", "  ", "0", "true", "yes", "on"):
        if value is None:
            monkeypatch.delenv("ATELES_GRANTS_ADVISORY", raising=False)
        else:
            monkeypatch.setenv("ATELES_GRANTS_ADVISORY", value)
        for c in _degraded_checkers():
            assert c.is_active() is False
            assert c.check_capability("a2a:task:create") is False
            assert c.has_capability("a2a:task:create") is False
            assert c.check_tool("s", "t") == (False, None)


def test_load_without_token_denies(monkeypatch):
    import lib.daemon_runtime.grant_checker as gc

    monkeypatch.delenv("ATELES_GRANTS_ADVISORY", raising=False)
    monkeypatch.setattr(gc, "NEOTOMA_BEARER_TOKEN", "")
    c = GrantChecker("x@ateles-swarm").load()
    assert c.is_active() is False
    assert c.check_tool("s", "t") == (False, None)


def test_advisory_flag_is_the_only_opt_out_and_is_announced(monkeypatch, caplog):
    import logging

    import lib.daemon_runtime.grant_checker as gc

    monkeypatch.setattr(gc, "_advisory_announced", False)
    monkeypatch.setenv("ATELES_GRANTS_ADVISORY", "1")
    with caplog.at_level(logging.WARNING):
        assert gc.grants_advisory() is True
        assert gc.grants_advisory() is True
    warnings = [r for r in caplog.records if "ATELES_GRANTS_ADVISORY" in r.getMessage()]
    assert len(warnings) == 1  # announced once, naming the flag
    for c in _degraded_checkers():
        assert c.is_active() is True
        assert c.check_capability("x") is True
        assert c.check_tool("s", "t") == (True, None)


def test_advisory_flag_defaults_off(monkeypatch):
    import lib.daemon_runtime.grant_checker as gc

    monkeypatch.delenv("ATELES_GRANTS_ADVISORY", raising=False)
    assert gc.grants_advisory() is False


def test_check_tool_denied_when_grant_suspended():
    e = _monedula_grant_entity()
    e["snapshot"]["status"] = "suspended"
    c = _checker_with(e)
    allowed, _ = c.check_tool("parquet", "read_parquet")
    assert allowed is False


# ── check_param_constraints tests ────────────────────────────────────────────────


def test_constraints_empty_passes():
    ok, reason = check_param_constraints({}, {"anything": 1})
    assert ok and reason == ""


def test_constraints_tables_allow_and_deny():
    ok, _ = check_param_constraints({"tables": ["transactions"]}, {"table": "transactions"})
    assert ok
    ok, reason = check_param_constraints({"tables": ["transactions"]}, {"table": "contacts"})
    assert not ok and "contacts" in reason


def test_constraints_max_amount_sats():
    ok, _ = check_param_constraints({"max_amount_sats": 500000}, {"amount_sats": 400000})
    assert ok
    ok, reason = check_param_constraints({"max_amount_sats": 500000}, {"amount_sats": 600000})
    assert not ok and "exceeds" in reason
    # falls back to "amount" key
    ok, _ = check_param_constraints({"max_amount_sats": 500000}, {"amount": 100})
    assert ok


def test_constraints_to_allowlist():
    ok, _ = check_param_constraints({"to_allowlist": True}, {"to": "bc1qxyz"})
    assert ok
    ok, reason = check_param_constraints({"to_allowlist": True}, {})
    assert not ok and "to_allowlist" in reason


def test_constraints_generic_max_and_allowed():
    ok, _ = check_param_constraints({"max_limit": 100}, {"limit": 50})
    assert ok
    ok, _ = check_param_constraints({"max_limit": 100}, {"limit": 200})
    assert not ok
    ok, _ = check_param_constraints({"allowed_state": ["open", "closed"]}, {"state": "open"})
    assert ok
    ok, _ = check_param_constraints({"allowed_state": ["open"]}, {"state": "merged"})
    assert not ok


def test_constraints_unknown_key_ignored():
    ok, reason = check_param_constraints({"future_constraint": "xyz"}, {"a": 1})
    assert ok and reason == ""


# ── Runner ────────────────────────────────────────────────────────────────────


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())


def test_suspended_and_revoked_fail_closed_on_degraded_read(monkeypatch):
    monkeypatch.delenv("ATELES_GRANTS_ADVISORY", raising=False)
    for c in _degraded_checkers():
        assert c.is_suspended() is True
        assert c.is_revoked() is True
    monkeypatch.setenv("ATELES_GRANTS_ADVISORY", "1")
    for c in _degraded_checkers():
        assert c.is_suspended() is False
        assert c.is_revoked() is False


def test_suspended_and_revoked_unchanged_with_readable_grants(monkeypatch):
    monkeypatch.delenv("ATELES_GRANTS_ADVISORY", raising=False)
    c = _checker_with(_monedula_grant_entity())
    assert c.is_suspended() is False and c.is_revoked() is False
    e = _monedula_grant_entity()
    e["snapshot"]["status"] = "revoked"
    assert _checker_with(e).is_revoked() is True


def test_load_error_is_exposed(monkeypatch):
    import lib.daemon_runtime.grant_checker as gc

    monkeypatch.setattr(gc, "NEOTOMA_BEARER_TOKEN", "")
    c = GrantChecker("x@ateles-swarm").load()
    assert c.load_error
    assert _checker_with(_monedula_grant_entity()).load_error is None


def test_degraded_error_is_rate_limited(monkeypatch, caplog):
    import logging

    import lib.daemon_runtime.grant_checker as gc

    monkeypatch.delenv("ATELES_GRANTS_ADVISORY", raising=False)
    monkeypatch.setattr(gc, "_last_error_logged", {})
    c = GrantChecker("rl@ateles-swarm")
    c._loaded = True
    with caplog.at_level(logging.ERROR):
        for _ in range(5):
            assert c.is_active() is False
    assert len([r for r in caplog.records if "rl@ateles-swarm" in r.getMessage()]) == 1
    # A different cause for the same sub is a change and is logged.
    with caplog.at_level(logging.ERROR):
        c._load_error = "boom"
        assert c.is_active() is False
    assert len([r for r in caplog.records if "rl@ateles-swarm" in r.getMessage()]) == 2


def test_read_problem_names_cause_without_logging(monkeypatch):
    monkeypatch.delenv("ATELES_GRANTS_ADVISORY", raising=False)
    c = GrantChecker("x@ateles-swarm")
    c._loaded = True
    assert "no agent_grant" in c.read_problem()
    c._load_error = "NEOTOMA_BEARER_TOKEN not set"
    assert c.read_problem() == "NEOTOMA_BEARER_TOKEN not set"
    assert _checker_with(_monedula_grant_entity()).read_problem() is None
    monkeypatch.setenv("ATELES_GRANTS_ADVISORY", "1")
    assert c.read_problem() is None

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


def test_check_tool_permissive_when_no_tool_grants_anywhere():
    # Un-migrated agent: no grant declares any tool caps → permissive fallback.
    c = GrantChecker("cicada@ateles-swarm")
    c._grants = [GrantChecker._parse(_legacy_grant_entity())]
    c._loaded = True
    allowed, constraints = c.check_tool("btc-wallet", "btc_send_transfer")
    assert allowed is True
    assert constraints is None


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

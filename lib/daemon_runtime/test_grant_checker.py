"""
Unit tests for GrantChecker tool-grant parsing and constraint enforcement (#26).

Run with:   .venv/bin/python lib/daemon_runtime/test_grant_checker.py
Or pytest:  .venv/bin/python -m pytest lib/daemon_runtime/test_grant_checker.py -v

These tests exercise pure logic only (parsing + constraint evaluation); no
network calls to Neotoma are made.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.grant_checker import (  # noqa: E402
    AgentGrant,
    GrantChecker,
    GrantVerdict,
    check_param_constraints,
    is_privileged_op,
    resolve_unknown,
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


# ── ateles#560: absent grant must DENY, never silently allow ──────────────────
#
# Each of these fails on origin/main, where is_active()/check_capability()/
# check_tool() all return permissive when self._grants is empty.


def _loaded_empty() -> GrantChecker:
    """A checker whose store answered successfully with ZERO grants."""
    c = GrantChecker("ghost@ateles-swarm")
    c._grants = []
    c._loaded = True
    c._load_error = None
    c._loaded_at = time.time()
    return c


def _unreachable() -> GrantChecker:
    """A checker whose store could not be reached at all."""
    c = GrantChecker("offline@ateles-swarm")
    c._grants = []
    c._loaded = True
    c._load_error = "connect timeout"
    c._loaded_at = None
    return c


def _unreachable_with_cache(grant_entity, age_seconds: float) -> GrantChecker:
    """Store unreachable now, but a successful load happened age_seconds ago."""
    c = GrantChecker("monedula@ateles-swarm")
    c._grants = [GrantChecker._parse(grant_entity)]
    c._loaded = True
    c._load_error = "connect timeout"
    c._loaded_at = time.time() - age_seconds
    return c


def test_absent_grant_denies_is_active():
    c = _loaded_empty()
    assert c.is_active() is False
    assert c.decide_active().verdict is GrantVerdict.DENY
    assert c.decide_active().reason == "no_grant"


def test_absent_grant_denies_check_capability():
    c = _loaded_empty()
    assert c.check_capability("store_structured") is False
    assert c.check_capability("retrieve") is False
    assert c.decide_capability("retrieve").reason == "no_grant"


def test_absent_grant_denies_check_tool():
    c = _loaded_empty()
    allowed, constraints = c.check_tool("btc-wallet", "btc_send_transfer")
    assert allowed is False
    assert constraints is None
    assert c.decide_tool("parquet", "read_parquet").reason == "no_grant"


def test_absent_grant_is_reported_distinctly_from_revoked():
    c = _loaded_empty()
    assert c.has_no_grant() is True
    # An absent grant is NOT a revoked grant; startup paths report them apart.
    assert c.is_revoked() is False
    assert c.is_suspended() is False


def test_all_revoked_grants_deny():
    e = _monedula_grant_entity()
    e["snapshot"]["status"] = "revoked"
    c = _checker_with(e)
    assert c.is_active() is False
    assert c.decide_active().reason == "no_active_grant"


# ── ateles#560: unreachable store is UNKNOWN, resolved by privilege ───────────


def test_unreachable_store_is_unknown_not_allow():
    c = _unreachable()
    d = c.decide_active()
    assert d.verdict is GrantVerdict.UNKNOWN
    assert d.is_unknown
    # UNKNOWN must never read as allowed on the decision itself.
    assert d.allowed is False


def test_unreachable_store_denies_privileged_ops():
    c = _unreachable_with_cache(_monedula_grant_entity(), 60)
    # Writes, funds, and outbound comms fail CLOSED when we cannot verify,
    # even with a fresh cached snapshot that would have allowed them.
    assert c.check_capability("store_structured") is False
    assert c.check_capability("github_harness:write") is False
    allowed, _ = c.check_tool("btc-wallet", "btc_send_transfer")
    assert allowed is False


def test_unreachable_store_degrades_open_for_reads():
    c = _unreachable_with_cache(_monedula_grant_entity(), 60)
    # Read-shaped work still runs so a Neotoma outage does not halt the swarm.
    assert c.check_capability("retrieve") is True
    allowed, _ = c.check_tool("parquet", "read_parquet")
    assert allowed is True


def test_unreachable_with_no_cache_ever_denies_everything():
    # Never had a successful load: there is no snapshot to degrade from, so
    # even reads are denied. "We have never known" is not "probably fine".
    c = _unreachable()
    assert c.decide_active().reason == "grant_cache_stale"
    assert c.check_capability("retrieve") is False
    assert c.check_capability("store_structured") is False


def test_unreachable_store_denies_reads_past_staleness_bound():
    from lib.daemon_runtime.grant_checker import (
        GRANT_CACHE_MAX_STALENESS_SECONDS as BOUND,
    )

    fresh = _unreachable_with_cache(_monedula_grant_entity(), BOUND - 60)
    assert fresh.decide_active().reason == "grant_store_unreachable"
    assert fresh.check_capability("retrieve") is True

    stale = _unreachable_with_cache(_monedula_grant_entity(), BOUND + 60)
    assert stale.decide_active().reason == "grant_cache_stale"
    # A cache old enough to have missed a revocation vouches for nothing.
    assert stale.check_capability("retrieve") is False
    assert stale.is_active() is False


def test_never_loaded_is_unknown():
    c = GrantChecker("never@ateles-swarm")
    assert c.decide_active().verdict is GrantVerdict.UNKNOWN
    assert c.decide_active().reason == "grants_not_loaded"
    assert c.check_capability("store_structured") is False


def test_privileged_op_classification():
    assert is_privileged_op("store_structured") is True
    assert is_privileged_op("correct") is True
    assert is_privileged_op("github_harness:write") is True
    assert is_privileged_op("a2a:task:create") is True
    assert is_privileged_op("tool:btc-wallet:btc_send_transfer") is True
    assert is_privileged_op("retrieve") is False
    assert is_privileged_op("tool:parquet:read_parquet") is False
    # An unnamed operation is conservative, not free.
    assert is_privileged_op("") is True


def test_resolve_unknown_passes_determinate_verdicts_through():
    from lib.daemon_runtime.grant_checker import GrantDecision

    allow = GrantDecision(GrantVerdict.ALLOW, "active_grant")
    deny = GrantDecision(GrantVerdict.DENY, "no_grant")
    # Posture never overrides a determinate answer, in either direction.
    assert resolve_unknown(allow, op="store_structured") == (True, "active_grant")
    assert resolve_unknown(deny, op="retrieve") == (False, "no_grant")


def test_active_grant_still_allows_no_regression():
    c = _checker_with(_monedula_grant_entity())
    assert c.is_active() is True
    assert c.check_capability("store_structured") is True
    allowed, constraints = c.check_tool("parquet", "read_parquet")
    assert allowed is True
    assert constraints == {"tables": ["transactions", "accounts"]}


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

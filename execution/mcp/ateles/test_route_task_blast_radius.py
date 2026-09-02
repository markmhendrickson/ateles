"""Blast-radius advisory in the Ateles MCP `route_task` tool (ateles#715).

`_action_blast_radius` is advisory — it tells a caller what the enforcing gate
in `lib.daemon_runtime.gating` will decide. It carried the identical fail-open:
anything absent from `high_blast_action_types` was reported "low", so
`operator_only` was advertised as safe by the very tool an operator would use
to check.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from execution.mcp.ateles.server import (  # noqa: E402
    NEVER_AUTO_EXECUTE_ACTION_TYPES,
    _action_blast_radius,
)
from lib.daemon_runtime.gating import (  # noqa: E402
    NEVER_AUTO_EXECUTE_ACTION_TYPES as GATING_NEVER,
)

_POLICY = {
    "low_blast_action_types": ["local_edit", "draft", "compute_only_analysis"],
    "high_blast_action_types": ["payment", "publish", "open_or_merge_pr"],
}


def test_never_set_matches_the_enforcing_module():
    """server.py duplicates the constant rather than importing it, to keep the
    MCP server free of a daemon_runtime dependency. This asserts the copy stays
    honest — adding a member in one place and not the other fails here."""
    assert NEVER_AUTO_EXECUTE_ACTION_TYPES == GATING_NEVER


@pytest.mark.parametrize("spelling", ["operator_only", "OPERATOR_ONLY", " operator_only "])
def test_operator_only_advertised_as_never(spelling):
    assert _action_blast_radius(spelling, _POLICY) == "never"


def test_operator_only_never_even_if_policy_lists_it_low():
    permissive = dict(_POLICY, low_blast_action_types=["operator_only", "draft"])
    assert _action_blast_radius("operator_only", permissive) == "never"


def test_unrecognized_action_type_is_not_advertised_low():
    """Previously `else "low"`: an action type nobody had classified was
    reported safe. Now it reports the same fail-closed verdict the gate reaches."""
    assert _action_blast_radius("open_pull_request", _POLICY) == "never"
    assert _action_blast_radius("totally_unknown", _POLICY) == "never"


def test_known_classifications_are_unchanged():
    assert _action_blast_radius("payment", _POLICY) == "high"
    assert _action_blast_radius("publish", _POLICY) == "high"
    assert _action_blast_radius("local_edit", _POLICY) == "low"
    assert _action_blast_radius("draft", _POLICY) == "low"


def test_json_string_action_type_sets_are_parsed():
    """Neotoma may hand these back as JSON strings rather than lists."""
    stringly = {
        "low_blast_action_types": json.dumps(["local_edit"]),
        "high_blast_action_types": json.dumps(["payment"]),
    }
    assert _action_blast_radius("local_edit", stringly) == "low"
    assert _action_blast_radius("payment", stringly) == "high"
    assert _action_blast_radius("operator_only", stringly) == "never"


def test_missing_policy_sets_fail_closed():
    assert _action_blast_radius("anything", {}) == "never"
    assert _action_blast_radius("operator_only", {}) == "never"

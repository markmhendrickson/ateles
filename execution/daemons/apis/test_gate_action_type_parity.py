"""Dispatcher half of the cross-surface parity table (ateles#682).

The MCP half is `execution/mcp/ateles/test_route_task_content_parity.py`. Both
run `lib.daemon_runtime.action_type_parity_cases.PARITY_CASES`, each in its own
natural call shape, against the same expected values.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime.action_type_parity_cases import (  # noqa: E402
    PARITY_CASES,
    PARITY_POLICY,
)
from lib.daemon_runtime.gating import ExecutionPolicy  # noqa: E402

_POLICY = ExecutionPolicy(
    entity_id="parity",
    high_blast_action_types=frozenset(PARITY_POLICY["high_blast_action_types"]),
    low_blast_action_types=frozenset(PARITY_POLICY["low_blast_action_types"]),
    loaded=True,
)


@pytest.mark.parametrize(
    "case_id, description, declared, agent, expected_type, expected_source, expected_blast",
    PARITY_CASES,
    ids=[c[0] for c in PARITY_CASES],
)
def test_dispatch_gate_matches_the_shared_parity_table(
    case_id, description, declared, agent, expected_type, expected_source, expected_blast
):
    snapshot = {"title": description}
    if declared is not None:
        snapshot["action_type"] = declared
    resolved = apis._resolve_gate_action_type(agent, snapshot, _POLICY)
    assert resolved.value == expected_type
    assert resolved.source == expected_source
    if expected_blast is not None:
        assert _POLICY.blast_radius_for(resolved.value).value == expected_blast

"""ateles#1196 — owns_pending_gate entrance parity.

Shared helper, standing call-site lint, spec-pipeline pm vs eng, and
pending-gate read fail-safes. Redispatch panel↔sweep identity lives in
test_missing_lens_review_recovery.py.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import skill_runner
import swarm_dispatch as sd
from issue_spec import SECTION_BY_AGENT


def test_owns_pending_gate_helper_empty_lens_false():
    assert (
        sd.owns_pending_gate(gate_or_lens="", pending_gates={"arch"}) is False
    )


def test_owns_pending_gate_helper_pending_true():
    assert (
        sd.owns_pending_gate(gate_or_lens="arch", pending_gates={"arch"})
        is True
    )


def test_owns_pending_gate_helper_cleared_or_absent_false():
    assert (
        sd.owns_pending_gate(gate_or_lens="arch", pending_gates={"pm"}) is False
    )
    assert (
        sd.owns_pending_gate(gate_or_lens="arch", pending_gates=set()) is False
    )


def test_every_await_run_skill_passes_owns_pending_gate_explicitly():
    """Standing lint: no silent default on swarm_dispatch entrances."""
    src_path = Path(sd.__file__).resolve()
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    sites: list[int] = []
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        is_run_skill = (
            isinstance(func, ast.Name) and func.id == "run_skill"
        ) or (isinstance(func, ast.Attribute) and func.attr == "run_skill")
        if not is_run_skill:
            continue
        sites.append(call.lineno)
        kwargs = {kw.arg for kw in call.keywords if kw.arg}
        if "owns_pending_gate" not in kwargs:
            missing.append(call.lineno)
    assert len(sites) >= 13, f"expected ≥13 await run_skill sites, got {len(sites)}"
    assert missing == [], (
        f"await run_skill sites missing explicit owns_pending_gate=: {missing}"
    )


def test_aauth_docs_document_interim_gate_owner_identity_policy():
    """docs/aauth.md is the prose source of truth for the interim knob."""
    root = Path(sd.__file__).resolve().parents[3]
    text = (root / "docs" / "aauth.md").read_text(encoding="utf-8")
    assert "ATELES_GATE_OWNER_IDENTITY_POLICY" in text
    assert "allow_unattributed" in text
    assert "refuse" in text
    assert "export ATELES_GATE_OWNER_IDENTITY_POLICY=refuse" in text
    # Unconditional "refuses to dispatch" product claim must not remain.
    assert "refuses to\ndispatch" not in text
    assert "**refuses to dispatch**" not in text


@pytest.mark.asyncio
async def test_spec_pipeline_pm_owns_true_eng_owns_false(monkeypatch):
    """pending {pm} → pavo True, cicada/eng False; entrance=spec_pipeline."""
    d = sd.SwarmDispatcher(notifier=MagicMock())
    monkeypatch.setattr(
        d,
        "_resolve_owns_pending_gate",
        AsyncMock(
            side_effect=lambda repo, num, *, gate_or_lens, entrance: (
                gate_or_lens == "pm"
            )
        ),
    )

    calls: list[dict] = []

    class _Ok:
        ok = True
        stdout = "<<<SPEC_SECTION>>>\nsection\n<<<SPEC_SECTION>>>"
        error = None
        returncode = 0

    async def fake_run_skill(agent, *args, **kwargs):  # noqa: ANN001
        calls.append({"agent": agent, **kwargs})
        return _Ok()

    class _SpecStore:
        async def load(self, *a, **k):  # noqa: ANN001
            return MagicMock(sections={})

        async def upsert_section(self, state, section, text):  # noqa: ANN001
            return state

        async def mark_mirrored(self, state):  # noqa: ANN001
            return None

    monkeypatch.setattr(sd, "run_skill", fake_run_skill)
    monkeypatch.setattr(sd, "IssueSpecStore", lambda *a, **k: _SpecStore())
    monkeypatch.setattr(d, "_selected_sections", lambda trigger: [
        SECTION_BY_AGENT["pavo"],
        SECTION_BY_AGENT["cicada"],
    ])
    monkeypatch.setattr(d, "_extract_section_text", lambda *a, **k: "body")
    monkeypatch.setattr(d, "_mirror_spec_to_issue", AsyncMock())
    monkeypatch.setattr(d, "_gates_green", AsyncMock(return_value=False))
    monkeypatch.setattr(sd, "_token_for_agent_on_repo", lambda *a, **k: "t")

    from github_gateway import SwarmTrigger

    trigger = SwarmTrigger(
        kind="issue_opened",
        repository="o/r",
        number=1196,
        title="t",
        body="",
        author="a",
        html_url="https://github.com/o/r/issues/1196",
        delivery_id="test",
        action="opened",
    )
    await d._run_issue_spec_pipeline(trigger)

    section_calls = [c for c in calls if c["agent"] in ("pavo", "cicada")]
    by_agent = {c["agent"]: c for c in section_calls}
    assert by_agent["pavo"]["owns_pending_gate"] is True
    assert by_agent["cicada"]["owns_pending_gate"] is False
    assert by_agent["pavo"]["dispatch_entrance"] == "spec_pipeline"


@pytest.mark.asyncio
async def test_pending_gate_read_fail_safe_allow_empty(monkeypatch, caplog):
    monkeypatch.delenv(skill_runner.GATE_OWNER_IDENTITY_POLICY_ENV, raising=False)

    class _BoomStore:
        def __init__(self, *a, **k):
            pass

        async def load(self, *a, **k):  # noqa: ANN001
            raise RuntimeError("neotoma down")

    monkeypatch.setattr(sd, "IssueGateStore", _BoomStore)
    d = sd.SwarmDispatcher(notifier=MagicMock())
    with caplog.at_level(logging.WARNING):
        owns = await d._resolve_owns_pending_gate(
            "o/r", 1196, gate_or_lens="arch", entrance="panel"
        )
    assert owns is False
    assert "gate state unread" in caplog.text


@pytest.mark.asyncio
async def test_pending_gate_read_fail_safe_refuse_owns_when_gate_nonempty(
    monkeypatch,
):
    monkeypatch.setenv(
        skill_runner.GATE_OWNER_IDENTITY_POLICY_ENV,
        skill_runner.GATE_OWNER_IDENTITY_REFUSE,
    )

    class _BoomStore:
        def __init__(self, *a, **k):
            pass

        async def load(self, *a, **k):  # noqa: ANN001
            raise RuntimeError("neotoma down")

    monkeypatch.setattr(sd, "IssueGateStore", _BoomStore)
    d = sd.SwarmDispatcher(notifier=MagicMock())
    owns = await d._resolve_owns_pending_gate(
        "o/r", 1196, gate_or_lens="arch", entrance="missing_lens_redispatch"
    )
    assert owns is True
    owns_empty = await d._resolve_owns_pending_gate(
        "o/r", 1196, gate_or_lens="", entrance="missing_lens_redispatch"
    )
    assert owns_empty is False

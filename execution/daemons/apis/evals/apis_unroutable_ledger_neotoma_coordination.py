"""Named eval: apis_unroutable_ledger_neotoma_coordination (ateles#697 / qa).

This module is the CI-attributable eval artifact for moving Apis's unroutable
ledger from disk into Neotoma. It does not re-implement FakeNeotoma cases —
thin wrappers call the existing storage-semantic tests (with their fixtures
re-established here so a dedicated CI step that collects only this file still
wires FakeNeotoma into apis/skill_runner), plus one skill_runner entry-point
case the unit suite did not bind.

QE3: ateles has no neotoma agentic_eval harness yet; this pytest module IS the
eval substrate for this repo until a Python harness exists.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent.parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import apis  # noqa: E402
import harness_router  # noqa: E402
import skill_runner  # noqa: E402
import test_noowner_escalation as noowner  # noqa: E402
import test_unroutable_ledger as ledger_tests  # noqa: E402
import unroutable_ledger as ul  # noqa: E402
import unroutable_store as us  # noqa: E402
from fake_neotoma import FakeNeotoma  # noqa: E402
from lib.daemon_runtime import AgentDefinition  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402
from unroutable_store import NeotomaLedgerStore  # noqa: E402

EVAL_ID = "apis_unroutable_ledger_neotoma_coordination"


def _ledger_against_fake() -> UnroutableLedger:
    return UnroutableLedger(
        store=NeotomaLedgerStore(
            base_url="http://fake",
            token="t",
            ledger_key="test",
            cache_seconds=0,
        )
    )


@pytest.fixture()
def neotoma(monkeypatch, tmp_path):
    """Fake Neotoma for ledger-unit wrappers (same contract as test_unroutable_ledger)."""
    fake = FakeNeotoma()
    monkeypatch.setattr(us.httpx, "post", fake.post)
    monkeypatch.setattr(us.httpx, "get", fake.get)
    monkeypatch.setenv("APIS_UNROUTABLE_LEDGER", str(tmp_path / "absent.json"))
    return fake


@pytest.fixture()
def apis_neotoma(monkeypatch, tmp_path):
    """Fake Neotoma + apis._unroutable (same contract as test_noowner_escalation)."""
    fake = FakeNeotoma()
    monkeypatch.setattr(us.httpx, "post", fake.post)
    monkeypatch.setattr(us.httpx, "get", fake.get)
    monkeypatch.setenv("APIS_UNROUTABLE_LEDGER", str(tmp_path / "absent.json"))
    monkeypatch.setattr(apis, "_unroutable", _ledger_against_fake())
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **k: True)
    return fake


@pytest.fixture(autouse=True)
def _claude_only_router(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"claude": 1.0}')
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "missing-headroom.json")
    )
    monkeypatch.delenv("APIS_ALLOW_METERED_HARNESS", raising=False)
    harness_router.reset_state()
    yield
    harness_router.reset_state()


# ── re-export wrappers (ledger unit cases) ───────────────────────────────────


def test_one_singleton_row_not_one_per_write(neotoma):
    ledger_tests.test_one_singleton_row_not_one_per_write(neotoma)


def test_concurrent_writers_across_a_restart(neotoma):
    ledger_tests.test_concurrent_writers_across_a_restart(neotoma)


def test_all_three_fields_survive_interleaved_writers(neotoma):
    ledger_tests.test_all_three_fields_survive_interleaved_writers(neotoma)


def test_identical_logical_state_reuses_one_idempotency_key(neotoma):
    ledger_tests.test_identical_logical_state_reuses_one_idempotency_key(neotoma)


def test_fake_rejects_idempotency_key_reuse_with_different_payload(neotoma):
    ledger_tests.test_fake_rejects_idempotency_key_reuse_with_different_payload(
        neotoma
    )


# ── re-export wrappers (dispatch entry points) ───────────────────────────────


def test_unreadable_ledger_holds_the_page_instead_of_flooding(
    apis_neotoma, monkeypatch
):
    noowner.test_unreadable_ledger_holds_the_page_instead_of_flooding(
        apis_neotoma, monkeypatch
    )


def test_the_task_is_still_marked_blocked_when_the_page_is_held(
    apis_neotoma, monkeypatch
):
    noowner.test_the_task_is_still_marked_blocked_when_the_page_is_held(
        apis_neotoma, monkeypatch
    )


# ── new skill_runner surface case ────────────────────────────────────────────


def _stub_def(name: str = "pavo") -> AgentDefinition:
    """Empty prompt_markdown → degraded / undefined-role path in skill_runner."""
    return AgentDefinition(
        name=name,
        aauth_sub=f"{name}@ateles-swarm",
        tool_allowlist="*",
    )


def test_skill_runner_holds_undefined_role_page_when_ledger_unreadable(
    neotoma, monkeypatch
):
    """skill_runner undefined-role catch (~1092): LedgerUnavailable → zero pages.

    The ledger unit suite already asserts fail-closed holds on note_undefined_role;
    this binds the skill_runner catch site that calls shared_ledger() and must
    not notify when the read fails.
    """
    neotoma.fail_reads = True
    monkeypatch.setattr(ul, "_SHARED", _ledger_against_fake())

    stub = _stub_def("pavo")
    loader = MagicMock()
    loader.load.return_value = stub
    MockLoader = MagicMock(return_value=loader)

    notifier = MagicMock()
    skill_runner._agent_def_cache.clear()

    async def fake_exec(*cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = 0

        async def _communicate(input=None):
            return b"output", b""

        proc.communicate = _communicate
        return proc

    with (
        patch("skill_runner._write_harness_event"),
        patch("skill_runner.AgentLoader", MockLoader),
        patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "read_text", return_value="skill content"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        result = asyncio.run(
            skill_runner.run_skill(
                "pavo",
                "work prompt",
                role="pavo",
                task_entity_id="ent_eval_undefined_role",
                notifier=notifier,
            )
        )

    assert result.ok, f"dispatcher crashed on unreadable ledger: {result.error}"
    notifier.send.assert_not_called()

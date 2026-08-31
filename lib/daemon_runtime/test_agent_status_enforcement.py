"""Tests for agent_definition.status enforcement (ateles#562).

Before this change, status was read into the dataclass, printed in a startup
log line, and then ignored: setting an agent to "retired" had no runtime
effect whatsoever. These tests assert the EFFECT — a retired agent refuses to
start with a non-zero exit — not merely that a mapping function exists.

Every assertion here fails on origin/main, where `evaluate_status` and
`enforce_status_or_exit` do not exist and no daemon branches on status.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.agent_loader import (  # noqa: E402
    REFUSING_STATUSES,
    UNDEFINED_STATUS,
    AgentDefinition,
    AgentLoader,
    StatusAction,
    enforce_status_or_exit,
    evaluate_status,
)


# ── Status classification ─────────────────────────────────────────────────────


@pytest.mark.parametrize("status", sorted(REFUSING_STATUSES))
def test_refusing_statuses_refuse(status):
    action, reason = evaluate_status(status)
    assert action is StatusAction.REFUSE
    assert status in reason


@pytest.mark.parametrize("status", ["active", "ACTIVE", " active ", "provisional"])
def test_running_statuses_run(status):
    assert evaluate_status(status)[0] is StatusAction.RUN


def test_active_pending_deploy_runs():
    """sitta ships with this status today; it must not be halted."""
    assert evaluate_status("active-pending-deploy")[0] is StatusAction.RUN


@pytest.mark.parametrize("status", ["planned", "proposed", "draft"])
def test_unreliable_statuses_warn_rather_than_halt(status):
    """The data is wrong in both directions, so these must not halt production.

    18 of 40 agent_definition entities are 'planned', including agents that
    demonstrably run (neotoma-agent has a daemon and a live grant; lanius is
    the busiest dispatcher in the swarm). Refusing here would be an outage
    caused by known-bad data, so it warns instead.
    """
    action, reason = evaluate_status(status)
    assert action is StatusAction.WARN
    assert action is not StatusAction.REFUSE
    assert status in reason


@pytest.mark.parametrize("status", ["", "  ", "wat", "someday-maybe"])
def test_unknown_or_empty_status_warns_never_refuses(status):
    """An unanticipated value is missing information, not grounds for an outage."""
    assert evaluate_status(status)[0] is StatusAction.WARN


def test_refuse_and_warn_are_distinct_states():
    """Three states, not two: WARN must not collapse into RUN or REFUSE."""
    assert (
        len({StatusAction.RUN, StatusAction.REFUSE, StatusAction.WARN}) == 3
    )
    assert evaluate_status("planned")[0] is not evaluate_status("active")[0]
    assert evaluate_status("planned")[0] is not evaluate_status("retired")[0]


# ── The stub is no longer silently "active" ───────────────────────────────────


def test_stub_status_is_undefined_not_active():
    """A stub is an absence of information (#562).

    On origin/main `_stub()` returned status="active" with tool_allowlist="*",
    so a daemon with NO agent_definition looked fully configured in logs.
    """
    stub = AgentLoader("nonexistent-agent")._stub()
    assert stub.status == UNDEFINED_STATUS
    assert stub.status != "active"
    assert evaluate_status(stub.status)[0] is StatusAction.WARN


# ── enforce_status_or_exit: the effect, not the mapping ───────────────────────


def test_enforce_exits_nonzero_on_retired():
    with pytest.raises(SystemExit) as exc:
        enforce_status_or_exit(
            AgentDefinition(name="ghost", status="retired", entity_id="ent_x"),
            "ghost",
        )
    assert exc.value.code != 0


def test_enforce_exits_nonzero_on_disabled():
    with pytest.raises(SystemExit) as exc:
        enforce_status_or_exit(
            AgentDefinition(name="ghost", status="disabled"), "ghost"
        )
    assert exc.value.code != 0


def test_enforce_does_not_exit_on_active():
    enforce_status_or_exit(AgentDefinition(name="ok", status="active"), "ok")


def test_enforce_does_not_exit_on_planned():
    """Guards the deliberate carve-out: planned must keep running today."""
    enforce_status_or_exit(AgentDefinition(name="p", status="planned"), "p")


def test_enforce_error_names_agent_status_and_entity(caplog):
    """A refusal must be actionable, not a bare exception (#562 AC)."""
    with caplog.at_level("ERROR"):
        with pytest.raises(SystemExit):
            enforce_status_or_exit(
                AgentDefinition(
                    name="ghost", status="retired", entity_id="ent_abc123"
                ),
                "ghost-daemon",
            )
    text = caplog.text
    assert "ghost-daemon" in text
    assert "retired" in text
    assert "ent_abc123" in text


def test_warn_is_logged_loudly(caplog):
    with caplog.at_level("WARNING"):
        enforce_status_or_exit(
            AgentDefinition(name="p", status="planned"), "p-daemon"
        )
    assert "planned" in caplog.text


# ── Startup-entrypoint effect test (real process, real exit code) ─────────────


_STARTUP_HARNESS = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {root!r})
    from lib.daemon_runtime.agent_loader import (
        AgentDefinition, enforce_status_or_exit,
    )
    # Stand in for a daemon's startup block: load definition, log, enforce,
    # then reach the subscribe/dispatch phase.
    agent_def = AgentDefinition(name="d", status={status!r}, entity_id="ent_1")
    enforce_status_or_exit(agent_def, "d")
    print("REACHED_DISPATCH")
    """
)


def _run_startup(status: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _STARTUP_HARNESS.format(root=str(_REPO_ROOT), status=status)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_startup_entrypoint_exits_nonzero_and_never_dispatches_when_retired():
    """The acceptance criterion: a retired agent exits non-zero BEFORE dispatch."""
    proc = _run_startup("retired")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "REACHED_DISPATCH" not in proc.stdout


def test_startup_entrypoint_runs_normally_when_active():
    """No regression: an active agent still reaches dispatch."""
    proc = _run_startup("active")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REACHED_DISPATCH" in proc.stdout


def test_startup_entrypoint_still_dispatches_when_planned():
    """Production guard: the 18 'planned' agents must keep running."""
    proc = _run_startup("planned")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REACHED_DISPATCH" in proc.stdout


# ── Every daemon that logs status must also enforce it ────────────────────────


def test_all_daemons_logging_status_also_enforce_it():
    """Cross-surface parity: the defect was that 8 daemons logged and ignored.

    Any daemon that prints the startup status line must call the enforcement
    helper, so a future daemon cannot reintroduce log-without-enforce.
    """
    daemons_dir = _REPO_ROOT / "execution" / "daemons"
    offenders = []
    for path in daemons_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "agent_definition: status=" not in text:
            continue
        if "enforce_status_or_exit(agent_def" not in text:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        "these daemons log agent_definition.status without enforcing it: "
        + ", ".join(offenders)
    )

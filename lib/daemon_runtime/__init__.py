"""
lib/daemon_runtime/ — Shared runtime infrastructure for Ateles T3 daemons.

Provides four modules:

    agent_loader    — load agent_definition entity from Neotoma at startup
    sse_client      — subscribe to Neotoma entity SSE event stream
    aauth_signer    — sign outbound requests with daemon AAuth keypair
    grant_checker   — check agent_grant status; suspend/restore/revoke grants

Usage pattern (daemon startup):

    from lib.daemon_runtime import AgentLoader, SSEClient, AAuthSigner, GrantChecker

    agent_def = AgentLoader("monedula").load()          # loads from Neotoma
    signer    = AAuthSigner.from_key_file("monedula")   # loads from ateles-private/keys/
    grants    = GrantChecker("monedula@ateles-swarm").load()
    sse       = SSEClient(entity_types=["task", "event"])

    if grants.is_suspended():
        log.warning("monedula grants suspended — aborting dispatch")
        return

    async for event in sse.stream():
        handle(event)

See individual modules for full API.
"""

# ── Env bootstrap (must run before submodule imports) ───────────────────────
# launchd does not source ~/.config/neotoma/.env. The submodules below read
# NEOTOMA_BEARER_TOKEN / NEOTOMA_BASE_URL / NEOTOMA_SSE_SUBSCRIPTION_ID_* into
# module-level globals AT IMPORT TIME, so the .env must be loaded here first.
# This centralizes what was previously per-daemon bootstrap — every daemon that
# imports lib.daemon_runtime now inherits real credentials automatically.
# Override only empty values and plist placeholders (wrapped in __...__).
#
# Skipped entirely under pytest (ateles#1285): the operator's materialized
# dotenv carries operator-behaviour switches too — e.g.
# ATELES_SWARM_REQUIRE_LABEL for the bootstrap canary gate — and loading it
# at import time silently turned that gate ON for every local test run on a
# machine with the file present, while CI (which has no such file) stayed
# green. A test that wants a real credential or switch must set it itself.
# `ATELES_SKIP_DOTENV=1` is the explicit override for a non-pytest caller
# that wants the same isolation (e.g. a script under test harness control);
# `_dotenv_should_load` is exported so a root conftest.py can arm it via
# monkeypatch without relying on sys.modules ordering.
import os as _os  # noqa: E402
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402


def _dotenv_should_load(environ: dict | None = None, modules: dict | None = None) -> bool:
    """False when this import should NOT pull the operator's dotenv into
    os.environ — under pytest, or when ATELES_SKIP_DOTENV is set truthy."""
    env = _os.environ if environ is None else environ
    mods = _sys.modules if modules is None else modules
    if (env.get("ATELES_SKIP_DOTENV") or "").strip().lower() in ("1", "true", "yes"):
        return False
    if "pytest" in mods or env.get("PYTEST_CURRENT_TEST") is not None:
        return False
    return True


def _dotenv_path(environ: dict | None = None) -> _Path:
    """The dotenv file to load, overridable for tests via ATELES_DOTENV_FILE."""
    env = _os.environ if environ is None else environ
    override = env.get("ATELES_DOTENV_FILE")
    if override:
        return _Path(override)
    return _Path.home() / ".config" / "neotoma" / ".env"


def _load_dotenv_into_environ(path: _Path, environ: dict) -> None:
    if not path.exists():
        return
    for _line in path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")
            _existing = environ.get(_k, "")
            if not _existing or (_existing.startswith("__") and _existing.endswith("__")):
                environ[_k] = _v


_NEOTOMA_ENV_FILE = _dotenv_path()
if _dotenv_should_load():
    _load_dotenv_into_environ(_NEOTOMA_ENV_FILE, _os.environ)

from .aauth_signer import AAuthSigner
from .agent_loader import (
    REFUSING_STATUSES,
    UNDEFINED_STATUS,
    WARNING_STATUSES,
    AgentDefinition,
    AgentLoader,
    StatusAction,
    enforce_status_or_exit,
    evaluate_status,
)
from .drift import (
    DriftCluster,
    DriftSignal,
    cluster_signals,
    parse_comments,
    parse_drift_signals,
)
from .checkpoint_posture import CheckFailure, PostureOutcome, evaluate_with_posture
from .gating import (
    BlastRadius,
    CheckpointPosture,
    DEFAULT_CLOSED_BOUNDARIES,
    NEVER_AUTO_EXECUTE_ACTION_TYPES,
    ExecutionPolicy,
    GateAction,
    GateDecision,
    InvalidCheckpointPosture,
    evaluate_gate,
    load_policy,
    resolve_policy_for_agent,
    write_checkpoint_brief,
)
from .grant_checker import (
    AgentGrant,
    GrantChecker,
    check_param_constraints,
    restore_grant,
    revoke_grant,
    suspend_grant,
)
from .confidence_scoring import ConfidenceScore, score_confidence
from .readiness import (
    ReadinessAssessment,
    assess_readiness,
    build_assessment_entity,
    missing_request,
    write_assessment,
)
from .run_email import (
    build_run_eml,
    parse_task_id,
    run_subject,
    send_run_email,
    thread_ids,
)
from .session_finalize import (
    append_turn,
    build_finalize_payload,
    build_run_conversation_payload,
    build_turn_payload,
    create_run_conversation,
    finalize_session,
    load_end_skill,
)
from .sse_client import NeotomaEvent, SSEClient, hydrate_snapshot
from .task_lifecycle import (
    MAX_ATTEMPTS,
    TaskStatus,
    attempts_exhausted,
    backoff_seconds,
    can_transition,
    set_task_status,
)

__all__ = [
    "AgentLoader",
    "AgentDefinition",
    "StatusAction",
    "REFUSING_STATUSES",
    "WARNING_STATUSES",
    "UNDEFINED_STATUS",
    "evaluate_status",
    "enforce_status_or_exit",
    "SSEClient",
    "NeotomaEvent",
    "hydrate_snapshot",
    "AAuthSigner",
    "GrantChecker",
    "AgentGrant",
    "suspend_grant",
    "restore_grant",
    "revoke_grant",
    "check_param_constraints",
    # gating
    "BlastRadius",
    "ExecutionPolicy",
    "GateAction",
    "GateDecision",
    "evaluate_gate",
    "load_policy",
    "resolve_policy_for_agent",
    "write_checkpoint_brief",
    # checkpoint posture (ateles#350)
    "CheckpointPosture",
    "DEFAULT_CLOSED_BOUNDARIES",
    "NEVER_AUTO_EXECUTE_ACTION_TYPES",
    "InvalidCheckpointPosture",
    "CheckFailure",
    "PostureOutcome",
    "evaluate_with_posture",
    # drift / generalization
    "DriftSignal",
    "DriftCluster",
    "parse_drift_signals",
    "parse_comments",
    "cluster_signals",
    # task lifecycle (state machine)
    "TaskStatus",
    "set_task_status",
    "can_transition",
    "backoff_seconds",
    "attempts_exhausted",
    "MAX_ATTEMPTS",
    # session finalize (/end convergence)
    "build_finalize_payload",
    "finalize_session",
    "load_end_skill",
    # conversation-per-execution-run (E1)
    "build_run_conversation_payload",
    "build_turn_payload",
    "create_run_conversation",
    "append_turn",
    # run-thread email (E2)
    "run_subject",
    "parse_task_id",
    "thread_ids",
    "build_run_eml",
    "send_run_email",
    # readiness gate (E4)
    "ReadinessAssessment",
    "assess_readiness",
    "missing_request",
    "build_assessment_entity",
    "write_assessment",
    # confidence scoring (ateles#902) — populates confidence ahead of the gate
    "ConfidenceScore",
    "score_confidence",
]

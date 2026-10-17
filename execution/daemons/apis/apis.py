#!/usr/bin/env python3
"""
Apis — Ateles universal task dispatcher daemon.

Apis genus: honeybees. T3 daemon in the Ateles swarm.

Subscribes to Neotoma task events and dispatches to appropriate T4 agents
based on domain tags. Replaces Monedula's task-dispatch scope; Monedula
retains its payment-execution and calendar-detection logic.

Dispatch routing:
  task.created   → tag inference + subprocess dispatch to domain handler
  task.updated   → check for status transitions (e.g. approved → execute)
  task.due_today → remind operator; auto-execute if APIS_AUTO_EXECUTE=1

AAuth sub: apis@ateles-swarm
Startup sequence (T3 daemon pattern):
  1. Load env from ~/.config/neotoma/.env
  2. Load agent_definition from Neotoma via lib/daemon_runtime
  3. Load AAuth signer
  4. Load priority_rubric from Neotoma via lib/notify
  5. Subscribe to Neotoma SSE and dispatch events

Environment variables:
  NEOTOMA_BEARER_TOKEN        Neotoma API auth token (local-scoped)
  NEOTOMA_BEARER_TOKEN_PROD   Prod-scoped token; auto-promoted to
                              NEOTOMA_BEARER_TOKEN when NEOTOMA_BASE_URL is remote
  NEOTOMA_BASE_URL            Neotoma API base URL
  TELEGRAM_BOT_TOKEN          Telegram bot token
  TELEGRAM_CHAT_ID            Telegram chat ID
  TELEGRAM_TOPIC_APIS         Telegram topic ID for Apis notifications (optional)
  APIS_AGENT_DEFINITION_ID    Neotoma entity ID for Apis's agent_definition (optional)
  APIS_DRY_RUN                Set to "1" to log events without dispatching agents
  APIS_AUTO_EXECUTE           Set to "1" to auto-execute due tasks (default: notify only)
  APIS_HARNESS_PROVIDERS      Ordered subscription-backed CLIs to balance across
                              (default: claude,codex,cursor).
  APIS_HARNESS_HEADROOM       JSON estimates of remaining bundled-plan capacity,
                              e.g. {"claude":0.2,"codex":0.8,"cursor":0.6}.
  APIS_HARNESS_HEADROOM_FILE  Live JSON override read before every dispatch
                              (default: ~/.config/ateles/harness-headroom.json).
  APIS_HARNESS_MIN_HEADROOM   Hold out providers at/below this score (default: .05).
  APIS_HARNESS_COOLDOWN_SECONDS
                              Hold-out after quota/auth/launch failure (default: 3600).
  APIS_USAGE_GATE             "off" disables the usage gate (default on): a Claude
                              reading that is missing, malformed or older than
                              APIS_USAGE_STALE_SECONDS (1800) refuses new frontier
                              dispatch, as does weekly use at/above the pace line
                              (APIS_USAGE_WEEKLY_CEILING_PERCENT=60 x elapsed
                              fraction of the week + APIS_USAGE_PACE_BURST_PERCENT=10).
                              Local/mechanical work is never gated.
  APIS_USAGE_REFRESH_SECONDS  Age at which a dispatch first refreshes Claude's
                              reading via a minimal probe run (default: 600).
  APIS_USAGE_PROBE_BACKOFF_SECONDS
                              No automatic probe for this long after a failed one
                              (default: 300).
  APIS_USAGE_PROBE            "off" disables that probe (then keep the reading fed
                              with `harness_usage.py refresh`).
  APIS_CLAUDE_BIN             Claude CLI path (default: autodetect on PATH).
  APIS_CODEX_BIN              Codex CLI path (defaults to ChatGPT app, then PATH).
  APIS_CURSOR_BIN             Cursor Agent CLI path (default: cursor-agent on PATH).
  APIS_ALLOW_METERED_HARNESS  "1" permits usage-based API-key fallback. Default 0:
                              API keys are removed and capped plans fail over/queue.
  APIS_DISPATCH_TIMEOUT       Per-dispatch timeout in seconds (default: 1800)
  APIS_CHECKPOINT_DENIAL_DIR  Required absolute shared-state directory for
                              durable replay-denial markers
  APIS_CHECKPOINT_REQUIRED_APPROVER_SUB
                              AAuth subject whose signed status observation may
                              release a checkpoint (default: ateles@ateles-swarm)
  APIS_CHECKPOINT_REQUIRED_APPROVER_JKT
                              Required RFC 7638 thumbprint for that resolver;
                              missing configuration mints no release authority
  APIS_CHECKPOINT_PRODUCER_JKT
                              Required RFC 7638 thumbprint of apis.jwk.json;
                              missing or mismatched configuration mints no
                              checkpoint authority
  APIS_CHECKPOINT_PRODUCER_ISS
                              Issuer for Apis's RFC 9421 checkpoint-creation
                              proof (default: https://markmhendrickson.com)
  ATELES_REPO_PATH            Local path to ateles clone (default: ~/repos/ateles)

Task reconciliation sweep (ateles#586 — see task_reconciler.py):
  APIS_RECONCILE_ENABLED      "1" runs the level-triggered sweep that dispatches
                              `pending` tasks the SSE create path never saw.
                              Default 0 (off) — the first pass meets a backlog.
  APIS_RECONCILE_INTERVAL_SECONDS  Sweep cadence (default: 900)
  APIS_RECONCILE_MAX_PER_SWEEP     Max dispatches per pass (default: 5)
  APIS_RECONCILE_GRACE_SECONDS     Min task age before eligible (default: 900)
  APIS_RECONCILE_QUERY_LIMIT       Rows fetched per page (default: 500)
  APIS_RECONCILE_PAGES_PER_SWEEP   Pages walked per pass (default: 5)

GitHub trigger layer (ateles#80 — see github_gateway.py / swarm_dispatch.py):
  APIS_GITHUB_WEBHOOK_SECRET       HMAC secret for the GitHub webhook
  APIS_GITHUB_WEBHOOK_SECRET_NEXT  Incoming value during a staged rotation
                                   (rotate_swarm_secret.py) — admitted
                                   alongside the current secret; empty
                                   outside a rotation
  APIS_GITHUB_WEBHOOK_PORT    Webhook listen port (default: 8742)
  APIS_PANEL_MAX              Max review panelists per PR (default: 6)
  APIS_AUTONOMY_AUTO_MERGE    "1" lets the dispatcher merge after verified
                              review receipt + CI (default: 0 — checkpoint)
  GITHUB_TOKEN                Token for changed-files / issue-comment reads
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from datetime import datetime, timezone
from enum import Enum
import functools
import hashlib
import logging
import os
import stat
import sys
import time
import traceback
import weakref
from pathlib import Path


# ── Env bootstrap (launchd does not source shell profiles) ───────────────────
# Skipped under pytest (ateles#1285): this module is imported directly by
# several test_*.py in this directory, and the operator's materialized dotenv
# carries operator-behaviour switches (e.g. ATELES_SWARM_REQUIRE_LABEL) as
# well as credentials — loading it unconditionally at import time silently
# turned those switches on for any test importing `apis`, on a machine where
# the file happens to exist, while CI (no such file) stayed green.
def _dotenv_should_load() -> bool:
    if (os.environ.get("ATELES_SKIP_DOTENV") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return False
    return True


_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _dotenv_should_load() and _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.strip()
            # Strip an inline ` # comment` from UNQUOTED values only. Without
            # this, a line like `FLAG=1  # note` yields the value "1  # note",
            # which silently breaks exact-match checks (e.g. `== "1"`). Quoted
            # values keep any `#` verbatim (it may be part of a token/secret).
            if _v[:1] not in ('"', "'") and " #" in _v:
                _v = _v.split(" #", 1)[0].strip()
            os.environ.setdefault(_k.strip(), _v.strip('"').strip("'"))


# Pick the bearer token that matches the instance we actually target. The shared
# ~/.config/neotoma/.env carries a LOCAL-scoped NEOTOMA_BEARER_TOKEN (the local
# server runs open / accepts that token), but the apis launchd plist overrides
# NEOTOMA_BASE_URL to prod — and the local token 401s against prod entity reads.
# When the effective base URL is a remote (non-local) host and a prod-scoped
# token is materialized, promote it to NEOTOMA_BEARER_TOKEN so every downstream
# module (which reads NEOTOMA_BEARER_TOKEN directly) authenticates against prod.
#
# Host classification fails SAFE: only a host we can positively identify as
# remote triggers promotion. Anything local, loopback, link-local, *.local, an
# unparseable URL, or an empty base URL is treated as local and left untouched —
# so the failure mode is "don't promote" rather than "promote a prod token at
# the wrong instance".
def _looks_local(base_url: str) -> bool:
    from urllib.parse import urlparse

    if not base_url:
        return True
    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return True  # unparseable → fail safe
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return True
    if host.endswith(".local") or host.endswith(".localhost"):
        return True
    # RFC1918 / link-local private ranges
    if host.startswith(("10.", "192.168.", "169.254.")):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            pass
    return False


_base_url = os.environ.get("NEOTOMA_BASE_URL", "")
_prod_token = os.environ.get("NEOTOMA_BEARER_TOKEN_PROD", "").strip()
if not _looks_local(_base_url):
    if _prod_token:
        os.environ["NEOTOMA_BEARER_TOKEN"] = _prod_token
        # NB: logging is configured below; emit via print to stderr so the
        # promotion is visible even at import time (redacted — name only).
        print(
            f"[apis] promoting NEOTOMA_BEARER_TOKEN_PROD for remote base URL "
            f"{_base_url}",
            file=sys.stderr,
        )
    else:
        print(
            f"[apis] WARNING: NEOTOMA_BASE_URL={_base_url} is remote but "
            f"NEOTOMA_BEARER_TOKEN_PROD is unset — using local-scoped token, "
            f"which will likely 401 against prod entity reads",
            file=sys.stderr,
        )

# ── Path bootstrap ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Daemons run as standalone scripts (`python apis.py`), so there is no parent
# package for relative imports. Add this daemon's own directory to sys.path so
# sibling modules (routing, a2a_executor, a2a_gateway) import as top-level.
_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

from checkpoint_denial_store import (  # noqa: E402
    require_checkpoint_denial_store as _require_checkpoint_denial_store,
)
from lib.daemon_runtime import (  # noqa: E402
    AAuthSigner,
    AgentLoader,
    enforce_status_or_exit,
    append_turn,
    assess_readiness,
    create_run_session,
    missing_request,
    score_confidence,
    send_run_email,
    update_run_session_status,
    write_assessment,
    BlastRadius,
    GateAction,
    GateDecision,
    NeotomaEvent,
    SSEClient,
    evaluate_gate,
    hydrate_snapshot,
    resolve_policy_for_agent,
    write_checkpoint_brief,
)
from lib.daemon_runtime.gating import (  # noqa: E402
    checkpoint_already_dispatched,
    checkpoints_for_task,
    close_checkpoint_without_release,
    entity_record_digest,
    execution_policy_revision,
    fetch_checkpoint_record,
    fetch_entity_observations_strict,
    fetch_entity_user_id,
    fetch_task_record,
    fetch_task_snapshot,
    fetch_task_snapshot_and_version,
    mark_task_declined,
    read_authenticated_checkpoint_authorization,
    read_authenticated_checkpoint_resolution,
    write_producer_assessment,
    read_checkpoint_resolution,
    query_all_entities,
    require_fresh_checkpoint_approval,
    stamp_checkpoint_dispatched,
    supersede_checkpoint,
)
from lib.daemon_runtime.task_lifecycle import (  # noqa: E402
    TaskStatus,
    complete_task_with_result,
    is_terminal,
    normalize as normalize_status,
    set_task_field_if_version,
    set_task_status,
)
from lib.daemon_runtime.artifact_contract import (  # noqa: E402
    InvalidRef,
    ParsedRef,
    artifact_gate_reason,
    body_shape_for_dispatch,
    classify_artifact_body,
    complete_reference_url,
    eng_spec_has_content,
    pr_url,
    alert_text,
    sanitize_text,
    infer_body_shape,
    parse_artifact_header,
    parse_github_ref,
    role_required_artifact,
    stdout_tail_for_reason,
)
from lib.notify import Notifier, Priority  # noqa: E402
from lib.activity import ActivityLogger  # noqa: E402

# ── Activity-log channel (CyphorhinusBot observation feed) ──────────────────
_activity = ActivityLogger(agent="apis")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("apis")

# A checkpoint release only needs to wait until the task transition that
# authorizes execution is durably read back.  The harness run itself can last
# much longer than an MCP request, so detached runs need a strong reference and
# an exception-draining callback for the rest of the daemon process lifetime.
_background_dispatch_tasks: set[asyncio.Task[None]] = set()
_background_checkpoint_tasks: set[asyncio.Task[None]] = set()


def _background_dispatch_done(task: asyncio.Task[None]) -> None:
    _background_dispatch_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        log.warning("[%s] detached dispatch was cancelled", DAEMON_NAME)
    except Exception:
        log.exception("[%s] detached dispatch failed", DAEMON_NAME)


def _background_checkpoint_done(task: asyncio.Task[None]) -> None:
    _background_checkpoint_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        log.warning("[%s] fresh-checkpoint creation was cancelled", DAEMON_NAME)
    except Exception:
        log.exception("[%s] fresh-checkpoint creation failed", DAEMON_NAME)


# ── Config ────────────────────────────────────────────────────────────────────
DAEMON_NAME = "apis"

SUBSCRIBE_ENTITY_TYPES = ["task", "checkpoint_brief"]

# Coarse action_type per resolved skill — feeds the execution gate's blast-radius
# classification. Conservative: anything that opens PRs, releases, pays, or posts
# publicly is high blast. An explicit task.action_type field overrides this.
# Values MUST match the execution_policy's high/low_blast_action_types vocabulary
# (default policy ent_dfce6edecefe3eb7fc9e0337) or the gate mis-classifies blast
# radius. PR open and merge both map to the policy's "open_or_merge_pr"; "release"
# is treated as high blast via blast_radius_default + the policy's publish set.
_AGENT_ACTION_TYPE: dict[str, str] = {
    "cicada": "open_or_merge_pr",
    "vanellus": "open_or_merge_pr",
    "struthio": "publish",
    "monedula": "payment",
    "fringilla": "compute_only_analysis",
    "corvus": "send_external_comms",
}


def _infer_action_type(skill: str | None, snapshot: dict) -> str | None:
    """Best-effort action_type for the gate. Explicit task field wins.

    A declared value is passed through verbatim — including one the policy
    classifies in neither blast set. That is deliberate (ateles#715): the gate
    now treats an unrecognized declared action type as never-auto-executable
    and logs it by name, so passing it through is what makes the missing
    classification visible. Swallowing it here would restore the per-agent
    fallback and hide the gap.

    In particular `operator_only` must reach the gate intact. It is the one
    value whose entire purpose is to stop dispatch, and it is not in any
    policy's action-type sets.
    """
    explicit = (snapshot.get("action_type") or "").strip().lower()
    if explicit:
        return explicit
    if skill:
        return _AGENT_ACTION_TYPE.get(skill.lower())
    return None


def _confidence_is_explicit(snapshot: dict) -> bool:
    """True when the snapshot carries a `confidence`/`confidence_score` value
    that actually parses as a number — i.e. some producer scored this task.
    """
    raw = snapshot.get("confidence", snapshot.get("confidence_score"))
    if raw is None:
        return False
    try:
        float(raw)
    except (TypeError, ValueError):
        return False
    return True


def _read_confidence(snapshot: dict) -> float:
    """
    Read the agent-supplied confidence (0..1) from the task snapshot. Absent an
    explicit score, return 0.0 so the gate fails CLOSED (checkpoint) for any
    non-low-blast action — the operator is asked rather than the swarm guessing.

    Kept exactly as before (ateles#902 changes nothing about this function or
    its behavior): still fails to 0.0 on a truly absent/unparseable value.
    `dispatch_task` calls this only indirectly, through `_resolve_confidence`,
    and only on the branch where the snapshot ALREADY carries an explicit
    score (`_confidence_is_explicit` is True) — an unscored snapshot never
    reaches this function at all; `_resolve_confidence` routes it to
    `score_confidence` instead and returns that value directly.
    """
    raw = snapshot.get("confidence", snapshot.get("confidence_score"))
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def _successful_recurrences(snapshot: dict) -> int:
    try:
        return max(0, int(snapshot.get("successful_recurrences", 0)))
    except (TypeError, ValueError):
        return 0


def _resolve_confidence(
    snapshot: dict,
    *,
    has_owner: bool,
    action_type_recognized: bool,
    relationship_count: int = 0,
) -> tuple[float, bool]:
    """
    Confidence for the gate, populated ahead of the read (ateles#902 Part 2).

    Returns (confidence, unscored). When the snapshot already carries an
    explicit score, that score is used verbatim and `unscored` is False — a
    producer's own judgment (e.g. Cicada's full-rubric self-score) always
    wins over the mechanical approximation here. Only when NO producer ever
    scored the task does this fall through to `score_confidence` — a
    deterministic, fields-based approximation of the same confidence_rubric
    (ent_22fd6f25159f1f2689726780), mirroring how `readiness.py` approximates
    its own rubric for the sibling pre-execution gate.

    `unscored=True` is what lets the gate's reason text say "never scored"
    instead of "low confidence" when the decision is a CHECKPOINT* — it does
    NOT force a checkpoint by itself. The mechanical score returned here is a
    real value the gate's confidence axis consults like any other: a
    well-specified, unscored, LOW-blast task can score high enough to clear
    `confidence_threshold` and AUTO_EXECUTE, same as a task an agent scored
    itself. That is intentional (ateles#902) — it is what lets the swarm's
    low-blast drain path move again for tasks nobody explicitly scored,
    instead of every one of them stalling at a checkpoint nobody can clear.
    """
    if _confidence_is_explicit(snapshot):
        return _read_confidence(snapshot), False

    result = score_confidence(
        snapshot,
        has_owner=has_owner,
        action_type_recognized=action_type_recognized,
        relationship_count=relationship_count,
        successful_recurrences=_successful_recurrences(snapshot),
    )
    log.info(
        f"[{DAEMON_NAME}] confidence: unscored task — mechanical score "
        f"{result.value:.2f} ({result.rationale})"
    )
    return result.value, True


ATELES_REPO = Path(
    os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles"))
)

DRY_RUN = os.environ.get("APIS_DRY_RUN", "0") == "1"
AUTO_EXECUTE = os.environ.get("APIS_AUTO_EXECUTE", "0") == "1"


def _session_capture_enabled(value: str | None) -> bool:
    """Capture provenance by default; only an explicit ``0`` opts out."""
    return value != "0"


# E1 (docs/task_execution_loop.md): persist every execution run as an
# agent_session plus its turn conversation. Explicit 0 is the emergency opt-out;
# provenance is normal dispatch, not an experiment callers must remember.
RUN_CONVERSATIONS = _session_capture_enabled(os.environ.get("APIS_RUN_CONVERSATIONS"))
# E2 (docs/task_execution_loop.md): send run kickoff/outcome on a Gmail thread via
# the dedicated swarm address. Default off; needs the swarm mailbox provisioned
# (ATELES_SWARM_EMAIL + OPERATOR_EMAIL + ATELES_GMAIL_SEND_CMD). Fail-open.
RUN_EMAIL = os.environ.get("APIS_RUN_EMAIL", "0") == "1"
# E4 (docs/task_execution_loop.md): pre-execution readiness gate. When a task is
# under-specified (no clear goal/constraints/tooling), park it in awaiting_input
# and email the operator the specific gaps instead of executing. Default off.
READINESS_GATE = os.environ.get("APIS_READINESS_GATE", "0") == "1"

# Dispatch timeout per agent invocation (seconds).
DISPATCH_TIMEOUT_SECONDS = int(os.environ.get("APIS_DISPATCH_TIMEOUT", "1800"))

# GitHub webhook gateway (ateles#80). Port 8742 — Apus owns 8741.
GITHUB_WEBHOOK_PORT = int(os.environ.get("APIS_GITHUB_WEBHOOK_PORT", "8742"))
GITHUB_WEBHOOK_SECRET = os.environ.get("APIS_GITHUB_WEBHOOK_SECRET", "")
# Rotation overlap window (rotate_swarm_secret.py, authority_model.md#grants):
# the incoming value, admitted alongside the current one until the rotator
# retires the old value and clears this. Empty outside a rotation.
GITHUB_WEBHOOK_SECRET_NEXT = os.environ.get("APIS_GITHUB_WEBHOOK_SECRET_NEXT", "")


# ── Domain routing ─────────────────────────────────────────────────────────────
#
# Domain tags → T4 skill mappings live in routing.py, shared with the A2A
# gateway (a2a_executor.py) so inbound A2A tasks and SSE-sourced tasks route
# through one source of truth. Tags are inferred from the task title/body
# (neotoma-agent's due-date hygiene may set them first; Apis falls back to
# local inference). Each tag maps to a T4 skill dispatched through the
# quota-aware Claude/Codex/Cursor runner (see _spawn_harness_skill).
# Set APIS_DRY_RUN=1 to log intent without spawning.

from routing import (  # noqa: E402
    DOMAIN_ROUTES as _DOMAIN_ROUTES,
    HUMAN_OWNED as _HUMAN_OWNED,
    canonical_assignee as _canonical_assignee,
    infer_tags_from_text as _infer_tags_from_text,
    resolve_role as _resolve_role,
    resolve_skill as _resolve_skill,
)

import github_gateway  # noqa: E402

import build_delivery  # noqa: E402
import model_tiering  # noqa: E402
import producer_confidence  # noqa: E402
from skill_runner import SkillResult, _redact_secrets, run_skill  # noqa: E402
from swarm_dispatch import SwarmDispatcher, _token_for_agent_on_repo  # noqa: E402
from task_reconciler import TaskReconciler  # noqa: E402
from unroutable_ledger import shared_ledger  # noqa: E402

# Dedup + aggregation for no-owner escalations. Disk-backed so a restart does
# not re-page the operator about the whole standing backlog (ateles#636's
# lesson: state that looks recorded but is not).
#
# MUST be the shared instance: skill_runner writes undefined-role records to the
# same file, and two instances each saving their own stale view silently drop
# each other's records.
_unroutable = shared_ledger()

# Event-level idempotency for `task.created`. Bounded so a long-lived dispatcher
# cannot grow it without limit; the ledger above is what makes escalation dedup
# survive a restart, so this set only needs to cover redelivery bursts.
_CREATED_SEEN_MAX = 2000
_created_seen: dict[str, float] = {}


# entity_id -> (timestamp, announced_with_a_readable_snapshot?)
_announced: dict[str, tuple] = {}


def _should_announce(entity_id: str, hydrated: bool) -> bool:
    """Whether to send the INFO 'Task created' notice for this task.

    At most two notices ever, and only when the second carries real information:
    a provisional "(untitled)" announce from an unreadable delivery may be
    upgraded ONCE by the first readable copy. A readable announce is final.
    """
    prior = _announced.get(entity_id)
    if prior is not None:
        was_hydrated = prior[1]
        if was_hydrated or not hydrated:
            return False  # already final, or nothing new to say
    if len(_announced) >= _CREATED_SEEN_MAX:
        for eid in sorted(_announced, key=lambda e: _announced[e][0])[
            : _CREATED_SEEN_MAX // 2
        ]:
            _announced.pop(eid, None)
    _announced[entity_id] = (time.time(), hydrated)
    return True


def _seen_created(entity_id: str) -> bool:
    """True when this process already handled a `task.created` for `entity_id`."""
    if entity_id in _created_seen:
        return True
    if len(_created_seen) >= _CREATED_SEEN_MAX:
        # Drop the oldest half rather than clearing: clearing would let the
        # entire recent burst through again in one go.
        for eid in sorted(_created_seen, key=_created_seen.get)[
            : _CREATED_SEEN_MAX // 2
        ]:
            _created_seen.pop(eid, None)
    _created_seen[entity_id] = time.time()
    return False


from task_watchdog import TaskWatchdog  # noqa: E402


# ── Artifact completion gate (ateles#1155) ─────────────────────────────────────


# Every reason built for an operator passes agent-controlled text (the offered
# ref, the retained BLOCKED words, the record value) through the secret redactor.
def _redact_agent_text(text: str) -> str:
    """Redact secrets from agent-controlled text, invisible characters removed FIRST.

    One zero-width space, soft hyphen, word joiner or bidi mark inside a token
    defeats a plain substring redaction, so those are stripped before the
    redactor runs.
    """
    return _redact_secrets(sanitize_text(text))


_gate_reason = functools.partial(artifact_gate_reason, redact=_redact_agent_text)


# A rate limit or a network blip is often gone a second later, so an
# "unavailable" answer is retried once in-call before the task is BLOCKED.
_RESOLVE_ATTEMPTS = 2

# The most an operator alert carries; a recovery command block is kept whole or
# dropped whole to fit (see `alert_text`).
_ALERT_LIMIT = 1800
_RESOLVE_RETRY_DELAY_SECONDS = 1.0


def _text_identity(text: str) -> str:
    """A short stable identity for a text deliverable: a hash, never the text."""
    return "text-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def resolve_artifact_ref(parsed: ParsedRef, *, repo: str | None = None) -> str:
    # Keep the existing injectable Apis seam; delivery callers share the same
    # resolver with an exact remote PR expectation before reporting completion.
    from lib.daemon_runtime.artifact_resolver import resolve_artifact_ref as resolve

    return resolve(parsed, repo=repo, attempts=_RESOLVE_ATTEMPTS,
                   retry_delay=_RESOLVE_RETRY_DELAY_SECONDS)


def _normalize_ref_check(value) -> str:
    """Accept the tri-state string, or a bool from a stub (True exists, False absent)."""
    if value is True:
        return "exists"
    if value is False:
        return "absent"
    return value if value in {"exists", "absent", "unavailable"} else "unavailable"


def _dispatch_repo_from_snapshot(snapshot: dict) -> str | None:
    """The `owner/name` a bare `#N` or SHA is anchored to, first key present.

    Absence is meaningful, not a default: with no repo in the snapshot an
    ambiguous ref is refused rather than guessed against whatever checkout the
    dispatcher happens to be running in.
    """
    for key in ("repo", "dispatch_repo", "github_repo", "repository"):
        val = snapshot.get(key)
        if isinstance(val, str) and "/" in val.strip():
            return val.strip()
    return None


def _dispatch_mode_from_snapshot(snapshot: dict) -> str | None:
    """How this task was framed, which decides which body shapes satisfy it.

    Checked on explicit fields first, then tags, because `ordered_spec` /
    `eng_lens` dispatches are sometimes only marked by a tag.
    """
    for key in ("dispatch_mode", "spawn_mode", "artifact_dispatch_mode"):
        val = snapshot.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    tags = snapshot.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    for tag in tags:
        t = str(tag).strip().lower()
        if t in {"ordered_spec", "eng_lens"}:
            return t
    return None


# ── T4 dispatch ────────────────────────────────────────────────────────────────


@build_delivery.completion_scope
async def _spawn_harness_skill(
    skill: str,
    entity_id: str,
    snapshot: dict,
    trigger: str,
    notifier: Notifier,
    *,
    role: str | None = None,
    run_conversation_id: str | None = None,
    run_agent_session_id: str | None = None,
) -> "object":
    """
    Spawn a T4 agent for a task event. The subprocess mechanics live in
    skill_runner.run_skill (shared with the GitHub trigger pipelines).

    `role` is the agent_definition name to load (defaults to skill — in this
    codebase the two are the same string). Passing it explicitly keeps the
    caller's routing decision traceable and lets skill_runner load the correct
    definition even if skill/role names ever diverge.

    Returns the run result (ok / error / returncode); the caller records the
    task's lifecycle status and escalates. Never crashes Apis — one bad task
    must not take down the dispatcher.
    """
    title = snapshot.get("title", "(untitled)")
    body = snapshot.get("body", "") or snapshot.get("description", "")
    prompt = (
        f"Invoke the {skill} agent per the supplied system and skill instructions.\n\n"
        f"Task {entity_id} (trigger={trigger}): {title}\n\n"
        f"{body}".strip()
    )
    if run_conversation_id:
        # E1: bind this run's turns to the conversation Apis opened at dispatch,
        # so progress + finalize append to one task-linked thread (not a new one).
        prompt += (
            f"\n\nThis execution is tracked as Neotoma conversation "
            f"{run_conversation_id} (REFERS_TO task {entity_id}). When you finalize "
            f"via /end, store your turns PART_OF this conversation "
            f"(conversation_id={run_conversation_id}) rather than creating a new one. "
            f"Every entity or artifact you create during this run must carry a REFERS_TO edge to "
            f"agent_session {run_agent_session_id}; tasks keep exactly one PART_OF "
            f"edge to their planning parent."
        )

    # The task's action class, by the same inference the execution gate uses
    # (explicit `action_type` wins, else the per-agent default). A class the
    # live action_policy does not map resolves to `top`, so a task with no
    # recognizable class never silently runs cheap.
    action_class = (
        _infer_action_type(skill, snapshot)
        or model_tiering.ACTION_TASK_DISPATCH_FALLBACK
    )

    # A builder task cleared to deliver (the `open_or_merge_pr` class — the one
    # the gate holds for the operator) gets what delivery needs, exactly as the
    # swarm build handoff does: a dedicated worktree as cwd (git-root grants on
    # Codex are derived from it) and a scoped network grant. A task of any other
    # class, or one that names no repository, runs as before with neither — the
    # sandbox is widened for a dispatch that delivers and for nothing else
    # (ateles#590). Without this, a task-routed build wrote correct code and
    # then failed with "sandbox denied network access" (ateles#1448).
    delivery: dict[str, object] = {}
    # The ONE repository resolver the dispatcher already has: the explicit field,
    # never a reference found in the task's prose.
    repository = _dispatch_repo_from_snapshot(snapshot)
    key = build_delivery.issue_key(snapshot, repository)
    if build_delivery.is_delivery_dispatch(skill, action_class):
        if not repository:
            log.warning(
                f"[{DAEMON_NAME}] {skill} task {entity_id} is a delivery task but "
                "names no repository — running without worktree or network"
            )
        else:
            blocker = build_delivery.fly_delivery_blocker(
                build_delivery.fly_delivery_key(repository, key, entity_id)
            )
            if blocker:
                return SkillResult(skill, False, None, "", "", error=blocker,
                                   fly_delivery_status="unknown")
            # Opted onto Fly (APIS_FLY_RUNNER_CLASSES lists the delivery class):
            # the job runner clones, builds and delivers on a Fly machine, and
            # no local worktree is made. Not eligible before launch (including
            # a refused token mint): the local path below runs, as without Fly.
            fly_result = await _run_fly_delivery_task(
                skill, prompt, entity_id=entity_id, role=role, repository=repository,
                key=key, run_agent_session_id=run_agent_session_id, notifier=notifier,
                action_class=action_class,
            )  # fmt: skip
            if fly_result is not None:
                return fly_result
            try:
                worktree = await build_delivery.prepare_build_worktree(
                    repository, f"task-{entity_id[-8:]}"
                )
            except build_delivery.BuildWorktreeError as exc:
                # Says what the operator needs: nothing ran, and what to fix first.
                msg = f"build not started: {exc.reason}." + (
                    f" Before retrying: {exc.fix}" if exc.fix else ""
                )
                log.error(f"[{DAEMON_NAME}] {skill} task {entity_id} not started — {msg}")
                return SkillResult(skill, False, None, "", "", error=msg)
            delivery = {
                "cwd": worktree.path,
                "include_github_contract": True,
                # Resolved only now: the clone's identity has been verified.
                "github_token": _token_for_agent_on_repo(skill, repository),
            }

    with contextlib.ExitStack() as stack:
        if delivery and key:
            claim = stack.enter_context(
                build_delivery.claim_build(*key, f"task:{entity_id}")
            )
            prompt = (
                f"{prompt}\n\n{build_delivery.worktree_instructions(worktree)}"
                f"\n\n{build_delivery.tracking_instructions(claim)}"
            )
        elif delivery:
            prompt = f"{prompt}\n\n{build_delivery.worktree_instructions(worktree)}"
        result = await run_skill(
            skill,
            prompt,
            role=role or skill,
            task_entity_id=entity_id,
            agent_session_id=run_agent_session_id or "",
            notifier=notifier,
            action_class=action_class,
            **delivery,
        )
    return result


async def _run_fly_delivery_task(skill: str, prompt: str, *, entity_id: str, role: str | None,
                                 repository: str, key, run_agent_session_id: str | None,
                                 notifier, action_class: str):  # fmt: skip
    """A task-routed build delivered on Fly, or None to run it locally."""
    plan = build_delivery.plan_fly_delivery(
        repository, f"task-{entity_id[-8:]}",
        key=build_delivery.fly_delivery_key(repository, key, entity_id),
        issue_number=key[1] if key else None,
    )
    if plan is None:
        return None
    from fly_runner import expect_delivery_plan

    expect_delivery_plan(plan, task_id=entity_id)
    with contextlib.ExitStack() as stack:
        fly_prompt = f"{prompt}\n\n{build_delivery.fly_delivery_instructions(plan)}"
        if key:
            claim = stack.enter_context(build_delivery.claim_build(*key, f"task:{entity_id}"))
            fly_prompt += f"\n\n{build_delivery.tracking_instructions(claim)}"

        async def launch():
            return await run_skill(
                skill, fly_prompt, role=role or skill, task_entity_id=entity_id,
                agent_session_id=run_agent_session_id or "", notifier=notifier,
                action_class=action_class, repository=repository, runner="fly",
                fly_delivery=plan.spec(),
            )  # fmt: skip

        result = await build_delivery.run_fly_delivery(
            plan, launch, lambda reason: SkillResult(
                skill, False, None, "", "", error=reason, fly_delivery_status="unknown"))
    if result is not None and result.fly_delivery_status:
        summary = build_delivery.fly_delivery_summary(result)
        log.info(f"[{DAEMON_NAME}] {skill} task {entity_id}: {summary}")
        if result.ok and not result.remote_outcome_unknown and result.fly_delivery_status in {"opened", "updated"} and result.fly_pr_url:
            from fly_runner import FlyResultUnreadable, require_delivery_admission

            try:
                await require_delivery_admission(result, plan)
            except FlyResultUnreadable:
                result.ok = False
                result.remote_outcome_unknown = True
                result.fly_delivery_status = "unknown"
                result.fly_pr_url = ""
                result.error = "delivery admission changed before artifact synthesis"
                return result
            # The runner opens the PR after the model exits. Its read-back
            # supplies the artifact header the existing resolving gate verifies.
            result.stdout = (
                f"{result.stdout.rstrip()}\n{summary}\n"
                f"[{role or skill}] pull_request_link: {result.fly_pr_url}\n"
            )
            result.ok = True
        else:
            result.ok = False
            result.error = summary
    return result


def _shown_confidence(confidence: float, source: str) -> str:
    """Confidence for humans. A producer score is truncated, never rounded, so
    a figure shown beside a below-threshold decision cannot read as meeting it."""
    if source:
        return producer_confidence.display_confidence(confidence)
    return f"{confidence:.2f}"


def _scoring_summary(
    source: str,
    confidence: float,
    rationale: str,
    note: str,
    capped_from: float | None = None,
) -> str:
    """What the operator is told about the score, appended to the checkpoint
    summary and notification. A producer score carries its source and the
    scorer's own explanation, or says plainly that none was given; a task the
    scorer did not score says why when that is known."""
    if source:
        capped = (
            f" The scorer reported {producer_confidence.display_confidence(capped_from)}; "
            "the rubric's hard floor (a missing input or an unresolved conflict) "
            "capped it."
            if capped_from is not None
            else ""
        )
        return (
            f" Scored {_shown_confidence(confidence, source)} by {source}.{capped} "
            "Scorer's assessment: "
            f"{rationale or 'no explanation was supplied'}."
        )
    if note:
        return f" Producer scoring was not applied: {note}."
    return ""


# ── Deferral behind an in-flight build: durable record + recovery sweep ──────
#
# A cicada task about an issue that is being built right now is independent work:
# it waits, and runs when the build ends. `task.created` arrives once per entity
# per process, so the wait cannot live in memory — a restart would drop it with
# no trace. The deferral is therefore written on the task itself (existing task
# fields, see build_delivery) and a bounded sweep, run at startup and whenever a
# build's claim is released, re-dispatches exactly the tasks this mechanism marked
# whose build is no longer running. General reconciliation stays off and the
# historical backlog is never read for dispatch.

# Bounds on the recovery sweep. Every marked task is examined in every run; a run
# is a trigger (startup or a build release) plus its continuations:
#   * _RECOVERY_BATCH tasks are examined per pass;
#   * while unexamined tasks remain, the next pass is scheduled
#     _RECOVERY_PASS_DELAY_S later (so N marked tasks take ceil(N/batch) passes,
#     about 2 s apart, with no other event needed);
#   * at most _RECOVERY_CONCURRENCY deferred dispatches run at once;
#   * a failed listing is retried _RECOVERY_LIST_RETRIES times,
#     _RECOVERY_LIST_RETRY_DELAY_S apart, then left to the next trigger.
_RECOVERY_BATCH = 25
_RECOVERY_PASS_DELAY_S = 2.0
_RECOVERY_CONCURRENCY = 3
_RECOVERY_LIST_RETRIES = 3
_RECOVERY_LIST_RETRY_DELAY_S = 30.0


def _per_loop(registry: "weakref.WeakKeyDictionary", make):
    """One primitive per event loop: an asyncio lock or semaphore binds to the loop
    it first contends on, and the daemon has one loop but its tests start many."""
    loop = asyncio.get_running_loop()
    item = registry.get(loop)
    if item is None:
        item = registry[loop] = make()
    return item


_recovery_locks: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_recovery_slots: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _recovery_lock() -> asyncio.Lock:
    return _per_loop(_recovery_locks, asyncio.Lock)


def _recovery_slot() -> asyncio.Semaphore:
    return _per_loop(_recovery_slots, lambda: asyncio.Semaphore(_RECOVERY_CONCURRENCY))


_recovering: set[str] = set()
# Problems already put in front of the operator, by (task or "list", kind): each is
# reported once per process, so a record that stays broken is not re-announced by
# every sweep, and the sweep still moves past it every time.
_surfaced: set[tuple[str, str]] = set()
_recovery_notifier: "Notifier | None" = None


def _expected_deferral(claim: "build_delivery.BuildClaim") -> dict:
    return {
        "status": TaskStatus.PENDING.value,
        "disposition": build_delivery.DEFERRED_DISPOSITION,
        "dispatch_context": build_delivery.deferral_context(claim),
        "blocked_reason": build_delivery.deferral_reason(claim),
    }


def _write_deferral(entity_id: str, claim: "build_delivery.BuildClaim", attempt: int) -> str:
    """Write the deferral record in commit order, each write CONDITIONAL on the
    version the previous step left: disposition, dispatch_context, status, then
    blocked_reason LAST (the commit marker). Stops at the first failed write, so the
    marker is never written after a failure.

    Returns "ok", "failed" (a write was refused for any reason other than a version
    conflict, or no version token was available, so nothing could be conditioned),
    "unversioned" (a write committed but returned no version: the chain STOPS there
    rather than adopt a version it did not validate) or "conflict" (the task changed
    since it was read: NOTHING further is written and the caller re-reads; a task
    that moved on is never stamped with a deferral).
    """
    expected = _expected_deferral(claim)
    # A retry uses its own idempotency keys: a write the server dropped but
    # acknowledged would otherwise be answered "already done" and never applied.
    suffix = f"deferred-{claim.run_id}" + (f"-retry{attempt}" if attempt else "")
    snap, version = fetch_task_snapshot_and_version(entity_id)
    if snap is None or not version:
        return "failed"
    if normalize_status(snap.get("status")) not in ("", TaskStatus.PENDING.value):
        return "conflict"
    for field in ("disposition", "dispatch_context", "status", "blocked_reason"):
        write = set_task_field_if_version(
            entity_id, field, expected[field],
            expected_version=version, handler=DAEMON_NAME, key_suffix=suffix,
        )
        if write.conflict:
            return "conflict"
        if not write.ok:
            return "failed"
        version = write.version
        if not version:
            # Committed, but there is nothing to condition the next write on. Do NOT
            # fetch a replacement version and carry on: that version belongs to a
            # state this chain never validated, so a hold that landed in between
            # would pass the check and be replaced. Stop; the bounded retry re-reads
            # the task from the beginning and revalidates it (pending, expected
            # state), and the partial record is left to the reason-last rule.
            return "unversioned"
    return "ok"


def _deferral_persisted(entity_id: str, claim: "build_delivery.BuildClaim") -> list[str]:
    """What the task does NOT hold of the deferral record, read back (empty = all held)."""
    try:
        snap = fetch_task_snapshot(entity_id)
    except Exception as exc:  # noqa: BLE001 — an unreadable task is unproven
        return [f"read-back failed ({type(exc).__name__})"]
    if not isinstance(snap, dict):
        return ["read-back returned nothing"]
    missing = []
    for field, want in _expected_deferral(claim).items():
        got = snap.get(field)
        if field == "status":
            got = normalize_status(got)
        if got != want:
            missing.append(field)
    return missing


class DeferralOutcome(str, Enum):
    """What `_record_deferral` leaves behind, which decides what the caller must do.

    RECORDED       the deferral is on the task and read back: the sweep owns it.
    INTACT_MARKER  the deferral could not be confirmed, but a COMPLETE marker is
                   (or may be) on the task, so the sweep WILL act on it. The
                   operator was told exactly that; the caller must give it the same
                   catch-up as a recorded deferral, or the promise is empty.
    NOT_HELD       the marker is gone or does not qualify: nothing will dispatch it.
    MOVED_ON       the task was dispatched, finished or cancelled (by a release
                   sweep or someone else) while the deferral was being recorded;
                   nothing is left to do for it.
    MOVED_TO_GATE  the task is now held at a gate or waiting on a person; it may
                   need the operator's decision, and Apis did not touch it.
    STATE_UNKNOWN  the task is in a state Apis cannot classify; it was not touched.
    """

    RECORDED = "recorded"
    INTACT_MARKER = "intact_marker"
    NOT_HELD = "not_held"
    MOVED_ON = "moved_on"
    MOVED_TO_GATE = "moved_to_gate"
    STATE_UNKNOWN = "state_unknown"

    @property
    def sweep_owns_it(self) -> bool:
        return self in (DeferralOutcome.RECORDED, DeferralOutcome.INTACT_MARKER)


# What a task's status means for a deferral that was being recorded. Each status
# is read, never explained: the notice states the status that was OBSERVED and when,
# and says what to check; it makes no claim about how the task got there.
#
# status (after `normalize_status`, which folds completed->done and
# cancelled->declined; plus `in_progress`, which agents write for a running task)
#
#   routed, executing, in_progress, verified   active    MOVED_ON       check it is running
#   failed                                     failed    MOVED_ON       watchdog may retry; check
#   done, declined, superseded                 terminal  MOVED_ON       "no action needed" only if
#                                                                       no marker remains AND
#                                                                       nothing is queued for it
#   awaiting_approval, awaiting_input, blocked gate      MOVED_TO_GATE  may need the operator
#   pending / absent                           (not moved)              judged by the marker
#   anything else                              unknown   STATE_UNKNOWN  check it
_STATUS_KINDS: dict[str, tuple[str, "DeferralOutcome"]] = {
    "routed": ("active", DeferralOutcome.MOVED_ON),
    "executing": ("active", DeferralOutcome.MOVED_ON),
    "in_progress": ("active", DeferralOutcome.MOVED_ON),
    "verified": ("active", DeferralOutcome.MOVED_ON),
    "failed": ("failed", DeferralOutcome.MOVED_ON),
    "done": ("terminal", DeferralOutcome.MOVED_ON),
    "declined": ("terminal", DeferralOutcome.MOVED_ON),
    "superseded": ("terminal", DeferralOutcome.MOVED_ON),
    "awaiting_approval": ("gate", DeferralOutcome.MOVED_TO_GATE),
    "awaiting_input": ("gate", DeferralOutcome.MOVED_TO_GATE),
    "blocked": ("gate", DeferralOutcome.MOVED_TO_GATE),
}


def _read_stamp() -> str:
    """When a task was read, for notices that state an observation."""
    return datetime.now(timezone.utc).strftime("%H:%M:%SZ")


def _moved_notice(head: str, entity_id: str, state: dict) -> str:
    """The notice for a task that is no longer pending: what was OBSERVED, not why."""
    status, kind, stamp = state["status"], state["kind"], state["read_at"]
    base = f"{head}; the task's status is now {status} (read at {stamp})"
    left = state["left"]
    if kind == "active":
        return (
            f"{base}. Apis did not change it. Check that it is running as expected; "
            "this read does not show what moved it there."
        )
    if kind == "failed":
        return (
            f"{base}. Apis did not change it. The watchdog may retry a failed task; "
            "check it if it does not recover."
        )
    if kind == "terminal":
        queued = entity_id in _recovering
        if not left and not queued:
            return (
                f"{base}. It is in a terminal state, no deferral marker remains on it "
                "and Apis has nothing queued for it: no action needed."
            )
        extra = (
            f" A deferral marker remains on it ({', '.join(left)}); Apis will not "
            "act on a task in this state, so clear it if you do not want it there."
            if left else ""
        )
        extra += " Apis still has it queued; check it." if queued else ""
        return f"{base}. Apis did not change it.{extra}"
    # gate
    detail = f" ({state['detail']})" if state["detail"] else ""
    return (
        f"{base}. It is waiting at a gate{detail} and may need your decision. "
        "Apis did not change it."
    )


async def _record_deferral(
    entity_id: str, snapshot: dict, claim: "build_delivery.BuildClaim", notifier: Notifier
) -> "DeferralOutcome":
    """Mark the task deferred behind ``claim`` and PROVE it by reading it back.

    A write's success status is not persistence (failure_posture.md, rule 6): the
    deferral is acknowledged only when the task read back holds the expected
    status, disposition, dispatch_context and reason. A missing or mismatched
    field is a failed write: it is written once more.

    If it still does not hold, nothing is kept in memory and the task is NOT
    dispatched: the deferral fields are put back to what the task read before
    (best effort, so a half-written marker cannot get it picked up later) and the
    operator is told, once per task, which fields did not persist and what to do.
    A deferral either has a durable record or visibly does not exist; there is no
    third, hidden state.
    """

    def _attempt() -> list[str]:
        problems: list[str] = []
        for attempt in (0, 1):
            wrote = _write_deferral(entity_id, claim, attempt)
            problems = _deferral_persisted(entity_id, claim)
            if wrote == "ok" and not problems:
                return []
            if wrote == "conflict":
                problems = ["the task changed while it was being recorded (version conflict)"]
            elif wrote == "unversioned":
                problems = [
                    "a write committed but returned no version token, so the chain "
                    "stopped to be re-read"
                ]
            elif wrote == "failed" and not problems:
                problems = ["a write was refused or could not be made conditional"]
            log.warning(
                f"[{DAEMON_NAME}] deferral of task {entity_id} did not persist "
                f"(attempt {attempt + 1}/2): {', '.join(problems)}"
            )
        return problems

    problems = await asyncio.to_thread(_attempt)
    if not problems:
        return DeferralOutcome.RECORDED
    state = await asyncio.to_thread(_roll_back_deferral, entity_id, snapshot, claim)
    fields = ", ".join(problems)
    where = f"{claim.repository}#{claim.number}"
    head = f"Apis could not record a deferral for {entity_id}"
    outcome = state["outcome"]
    if state["verdict"] == "cleared":
        left = f" Fields left behind: {', '.join(state['left'])}." if state["left"] else ""
        text = (
            f"{head}; it was NOT held and NOT dispatched (did not persist: "
            f"{fields}).{left} Re-dispatch it by hand when the build of {where} ends."
        )
    elif state["verdict"] == "moved":
        text = _moved_notice(head, entity_id, state)
    elif state["verdict"] == "unknown_state":
        text = (
            f"{head}; the task's status is now {state['status']!r} (read at "
            f"{state['read_at']}), which Apis does not recognise. Apis did not "
            "change it. Check it."
        )
    elif state["verdict"] == "intact":
        text = (
            f"{head} and could not clear its marker; Apis WILL re-dispatch it when "
            f"the build of {where} ends unless you cancel the task (did not persist: "
            f"{fields}; marker fields still present: {', '.join(state['left'])})."
        )
    else:
        text = (
            f"{head} and could not verify the task's state afterwards (did not "
            f"persist: {fields}). If its deferral marker is intact Apis may "
            f"re-dispatch it when the build of {where} ends; check the task and "
            "cancel it if that is not wanted."
        )
    log.error(f"[{DAEMON_NAME}] could not record the deferral of task {entity_id}: {text}")
    _surface_once(
        notifier, (entity_id, "unrecorded"), text, {"surfaced": 0, "already_surfaced": 0}
    )
    return outcome


_DEFERRAL_FIELDS = ("disposition", "dispatch_context", "blocked_reason")


def _roll_back_deferral(entity_id: str, snapshot: dict, claim: "build_delivery.BuildClaim") -> dict:
    """Undo a deferral that did not persist, compare-and-set, and say what is true.

    The reason (the commit marker) is cleared first, then dispatch_context, then
    disposition. Before EACH clear the task is read again and the clear is made only
    if the task is still as this attempt left it: status pending, and every
    deferral field either ours, unchanged from before, or already cleared. If
    anything else has changed (a newer gate hold, a dispatch, a cancellation, a
    value someone else wrote) the rollback stops with nothing further written and
    classifies from what is there: a newer status or hold is never overwritten.
    It also stops at the first failed clear, so only a failure to clear the reason
    itself can leave a complete marker.

    Then ONE read decides what the operator is told, from what the task holds:
    ``cleared`` (the marker does not qualify), ``intact`` (it still qualifies, so the
    sweep WILL dispatch it), ``moved`` (the task is past pending: see
    ``_STATUS_KINDS``), ``unknown_state`` (a status that maps to nothing) or
    ``unknown`` (it cannot be read).
    """
    suffix = f"deferral-rollback-{claim.run_id}"
    expected = _expected_deferral(claim)
    before = {f: snapshot.get(f) or "" for f in _DEFERRAL_FIELDS}
    done: set[str] = set()

    def _still_as_left(fresh: dict) -> bool:
        if normalize_status(fresh.get("status")) not in ("", TaskStatus.PENDING.value):
            return False
        for f in _DEFERRAL_FIELDS:
            allowed = {before[f], ""} | (set() if f in done else {expected[f]})
            if (fresh.get(f) or "") not in allowed:
                return False
        return True

    for field in ("blocked_reason", "dispatch_context", "disposition"):
        try:
            fresh, version = fetch_task_snapshot_and_version(entity_id)
        except Exception:  # noqa: BLE001 — cannot tell what the task is: write nothing
            fresh, version = None, None
        if not isinstance(fresh, dict) or not version or not _still_as_left(fresh):
            break
        if (fresh.get(field) or "") != expected[field]:
            done.add(field)  # nothing of ours to clear here
            continue
        # CONDITIONAL on the version just read: if anything lands between that read
        # and this write (a gate hold, a dispatch, anyone's edit) Neotoma refuses it
        # and writes nothing. The loop then stops and the task is classified from
        # what is now there; there is no unconditional clear anywhere.
        write = set_task_field_if_version(
            entity_id, field, before[field],
            expected_version=version, handler=DAEMON_NAME, key_suffix=suffix,
        )
        if not write.ok:
            log.warning(
                f"[{DAEMON_NAME}] rollback of {field} on task {entity_id} "
                f"{'conflicted — the task changed' if write.conflict else 'failed'}"
            )
            break
        done.add(field)

    return _classify_deferral_state(entity_id, claim)


def _classify_deferral_state(entity_id: str, claim: "build_delivery.BuildClaim") -> dict:
    """One read of the task, classified by its ACTUAL status and marker."""
    expected = _expected_deferral(claim)
    try:
        fresh = fetch_task_snapshot(entity_id)
    except Exception:  # noqa: BLE001 — unknown, said as such
        fresh = None
    if not isinstance(fresh, dict):
        return {"verdict": "unknown", "left": [], "outcome": DeferralOutcome.INTACT_MARKER}
    status = normalize_status(fresh.get("status"))
    left_now = [f for f in _DEFERRAL_FIELDS if fresh.get(f) == expected[f]]
    if status not in ("", TaskStatus.PENDING.value):
        stamp = _read_stamp()
        mapped = _STATUS_KINDS.get(status)
        if mapped is None:
            return {
                "verdict": "unknown_state", "status": fresh.get("status"),
                "read_at": stamp, "left": left_now,
                "outcome": DeferralOutcome.STATE_UNKNOWN,
            }
        kind, outcome = mapped
        detail = str(fresh.get("blocked_reason") or "")[:140]
        if detail.startswith("Deferred:"):
            detail = ""
        return {
            "verdict": "moved", "status": fresh.get("status") or status, "kind": kind,
            "read_at": stamp, "detail": detail, "left": left_now, "outcome": outcome,
        }
    left = [f for f in _DEFERRAL_FIELDS if fresh.get(f) == expected[f]]
    _, verdict = _qualify_deferred(entity_id, fresh)
    if verdict == "":
        return {"verdict": "intact", "left": left, "outcome": DeferralOutcome.INTACT_MARKER}
    return {"verdict": "cleared", "left": left, "outcome": DeferralOutcome.NOT_HELD}


def install_build_release_hook(notifier: Notifier) -> None:
    """Sweep for deferred tasks whenever a build's claim is released."""
    global _recovery_notifier
    _recovery_notifier = notifier

    def _released(key: tuple[str, int]) -> None:
        schedule_deferred_recovery()

    build_delivery.set_release_hook(_released)


async def start_deferred_recovery(notifier: Notifier) -> dict:
    """What Apis does at startup for deferred tasks: arm the release hook, then sweep.

    The hook goes in first so a build that ends while the sweep runs is not missed.
    """
    install_build_release_hook(notifier)
    return await recover_deferred_tasks(notifier)


def _spawn_background(coro, name: str) -> None:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _background_dispatch_tasks.add(task)
    task.add_done_callback(_background_dispatch_done)


def schedule_deferred_recovery() -> None:
    """Start a recovery run in the background (no-op without a loop/notifier)."""
    if _recovery_notifier is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        log.error(
            f"[{DAEMON_NAME}] build released outside an event loop — deferred "
            "tasks will be picked up at the next startup sweep"
        )
        return
    _spawn_background(
        recover_deferred_tasks(_recovery_notifier), "apis-deferred-recovery"
    )


def _surface_once(notifier: Notifier, key: tuple[str, str], message: str, counts: dict) -> None:
    """Tell the operator about one problem, once per process; the sweep moves on."""
    if key in _surfaced:
        counts["already_surfaced"] += 1
        return
    _surfaced.add(key)
    counts["surfaced"] += 1
    notifier.send(message, priority=Priority.WARN, handler=DAEMON_NAME)


async def recover_deferred_tasks(
    notifier: Notifier, *, visited: set[str] | None = None, list_attempt: int = 0
) -> dict:
    """Re-dispatch the tasks deferred behind a build that is no longer running.

    Selects ONLY tasks this mechanism marked (`pending` + the deferral
    disposition). A run examines EVERY one of them: it works through them in
    batches and schedules its own continuation until none is left unexamined, so
    nothing depends on a later event, on earlier records becoming runnable, or on
    another build ending. A task that cannot be read or resolved is surfaced to
    the operator once and the run moves past it. Each runnable task goes through
    ``dispatch_task``, so the readiness and execution gates apply. Never raises.
    """
    visited = visited if visited is not None else set()
    counts = {
        "found": 0, "examined": 0, "redispatched": 0, "still_building": 0,
        "no_longer_deferred": 0, "surfaced": 0, "already_surfaced": 0, "remaining": 0,
    }
    async with _recovery_lock():
        try:
            rows = await asyncio.to_thread(
                query_all_entities,
                "task",
                snapshot_filters={
                    "status": {"op": "eq", "value": TaskStatus.PENDING.value},
                    "disposition": {"op": "eq", "value": build_delivery.DEFERRED_DISPOSITION},
                },
            )
            reason = "the task query failed"
        except Exception as exc:  # noqa: BLE001 — never raise out of the sweep
            log.exception(f"[{DAEMON_NAME}] deferred-task query crashed")
            rows = None
            reason = f"{type(exc).__name__}: {exc}"
        if rows is None:
            _surface_once(
                notifier,
                ("list", "query"),
                "Apis could not list the tasks deferred behind a build "
                f"({reason}). Deferred tasks stay pending and are not lost; Apis "
                "retries shortly, and if it keeps failing, find tasks whose reason "
                "starts 'Deferred:' and re-dispatch them once the build has ended.",
                counts,
            )
            if list_attempt < _RECOVERY_LIST_RETRIES:
                _continue_later(
                    notifier, visited, _RECOVERY_LIST_RETRY_DELAY_S, list_attempt + 1
                )
            return counts
        ids = [str(r.get("entity_id") or "") for r in rows if r.get("entity_id")]
        counts["found"] = len(ids)
        unexamined = [i for i in ids if i not in visited]
        batch = unexamined[:_RECOVERY_BATCH]
        visited.update(batch)
        counts["remaining"] = len(unexamined) - len(batch)

        for entity_id in batch:
            counts["examined"] += 1
            await _examine_deferred(entity_id, notifier, counts)

    if counts["remaining"]:
        _continue_later(notifier, visited, _RECOVERY_PASS_DELAY_S, 0)
    if counts["found"]:
        log.info(f"[{DAEMON_NAME}] deferred-task recovery pass: {counts}")
    return counts


def _continue_later(
    notifier: Notifier, visited: set[str], delay: float, list_attempt: int
) -> None:
    async def _go() -> None:
        await asyncio.sleep(delay)
        await recover_deferred_tasks(notifier, visited=visited, list_attempt=list_attempt)

    _spawn_background(_go(), "apis-deferred-recovery-continue")


def _qualify_deferred(entity_id: str, fresh: dict) -> "tuple[tuple[str, int] | None, str]":
    """Judge a task as it reads NOW: (build it waits for, verdict).

    Verdict is "" when the task is still deferred and resolvable, else why not:
    ``no_longer_deferred`` (not pending, or no longer marked) or ``unresolvable``
    (marked, but its dispatch_context is missing or malformed, or its reason is not
    the recorded form for that context). Pure: no I/O, so
    it is applied to every fresh read, at examination and again just before
    dispatch. Every deferral has a durable record, so there is nothing else to
    consult.
    """
    marked = fresh.get("disposition") == build_delivery.DEFERRED_DISPOSITION
    if normalize_status(fresh.get("status")) != TaskStatus.PENDING.value or not marked:
        return None, "no_longer_deferred"
    context = build_delivery.parse_deferral_context(fresh.get("dispatch_context"))
    # The reason is the commit marker: a record without the exact reason for THIS
    # context is a half-written or half-removed one, never a deferral to act on.
    if not context or not build_delivery.reason_matches_context(
        fresh.get("blocked_reason"), context
    ):
        return None, "unresolvable"
    return (context[0], context[1]), ""


def _unresolvable_notice(entity_id: str, notifier: Notifier, counts: dict) -> None:
    _surface_once(
        notifier, (entity_id, "unresolvable"),
        f"Deferred task {entity_id} does not say which build it waits for, "
        "so it was not re-dispatched. Check that no build of its issue is "
        "running, then re-dispatch it by hand.",
        counts,
    )


async def _examine_deferred(entity_id: str, notifier: Notifier, counts: dict) -> None:
    """Decide one marked task: run it, leave it, or surface it. Never raises."""
    try:
        if entity_id in _recovering:
            return
        fresh = await asyncio.to_thread(fetch_task_snapshot, entity_id)
        if fresh is None:
            _surface_once(
                notifier, (entity_id, "unreadable"),
                f"Deferred task {entity_id} could not be re-read, so it was not "
                "re-dispatched. Open it in Neotoma and re-dispatch it by hand once "
                "its build has ended.",
                counts,
            )
            return
        waiting_for, verdict = _qualify_deferred(entity_id, fresh)
        if verdict == "no_longer_deferred":
            counts["no_longer_deferred"] += 1
            log.info(
                f"[{DAEMON_NAME}] deferred task {entity_id!r} is no longer "
                f"deferred (status={fresh.get('status')!r}) — not re-dispatching"
            )
            return
        if verdict == "unresolvable":
            _unresolvable_notice(entity_id, notifier, counts)
            return
        if build_delivery.active_build(*waiting_for):
            counts["still_building"] += 1
            return
        approved_context = fresh.get("dispatch_context")
        if not approved_context:  # unreachable: a resolvable task has one
            raise AssertionError("refusing to enqueue a deferred dispatch with no approved context")
        _recovering.add(entity_id)
        counts["redispatched"] += 1
        _spawn_background(
            _redispatch_deferred(entity_id, approved_context, notifier),
            f"apis-deferred-{entity_id}",
        )
    except Exception as exc:  # noqa: BLE001 — one task must never stop the batch
        log.exception(f"[{DAEMON_NAME}] examining deferred task {entity_id} crashed")
        _surface_once(
            notifier, (entity_id, "crashed"),
            f"Deferred task {entity_id} could not be examined ({type(exc).__name__}); "
            "Apis moved on to the others. Re-dispatch it by hand once its build "
            "has ended.",
            counts,
        )


async def _redispatch_deferred(
    entity_id: str, approved_context: str, notifier: Notifier
) -> None:
    """Dispatch a deferred task from a FRESH read taken after its slot is won.

    A task can wait a long time for a slot (a dispatch awaits a whole agent run),
    and may be completed, cancelled, superseded or re-marked in the meantime; a
    read taken before the wait says nothing about that. So after the slot is
    acquired, and immediately before dispatch, the task is read again and must
    still be pending, marked deferred, with EXACTLY the dispatch_context it was
    approved with (always compared; an empty approved context is refused), and its
    build not running again. If it no longer qualifies nothing is dispatched or
    written (a quiet log line); a context that is now missing or malformed is
    reported through the same once-only `unresolvable` notice as at examination.
    If the task cannot be read the operator is told once. What is dispatched is
    that fresh read.
    """
    counts = {"surfaced": 0, "already_surfaced": 0}
    try:
        if not approved_context:
            log.error(
                f"[{DAEMON_NAME}] refusing to dispatch deferred task {entity_id!r}: "
                "no approved dispatch_context to compare against"
            )
            return
        async with _recovery_slot():
            fresh = await asyncio.to_thread(fetch_task_snapshot, entity_id)
            if fresh is None:
                _surface_once(
                    notifier, (entity_id, "unreadable"),
                    f"Deferred task {entity_id} could not be re-read when its turn "
                    "came, so it was not re-dispatched. Open it in Neotoma and "
                    "re-dispatch it by hand once its build has ended.",
                    counts,
                )
                return
            waiting_for, verdict = _qualify_deferred(entity_id, fresh)
            reason = ""
            if verdict == "unresolvable":
                _unresolvable_notice(entity_id, notifier, counts)
                reason = "its dispatch_context is missing or malformed"
            elif verdict:
                reason = f"it is no longer deferred (status={fresh.get('status')!r})"
            elif fresh.get("dispatch_context") != approved_context:
                reason = "it was re-marked behind a different build while it waited"
            elif build_delivery.active_build(*waiting_for):
                reason = "its build is running again"
            if reason:
                log.info(
                    f"[{DAEMON_NAME}] deferred task {entity_id!r} not dispatched "
                    f"after waiting for a slot: {reason}"
                )
                return
            await dispatch_task(
                entity_id,
                fresh,
                trigger="build_released",
                notifier=notifier,
                snapshot_hydrated=True,
            )
    finally:
        _recovering.discard(entity_id)


async def dispatch_task(
    entity_id: str,
    snapshot: dict,
    trigger: str,
    notifier: Notifier,
    gate_override: bool = False,
    snapshot_hydrated: bool | None = None,
    detach_after_accept: bool = False,
) -> None:
    """
    Route a task to the appropriate T4 skill and spawn it via a bundled-plan CLI.

    Applies the confidence × blast-radius execution gate before spawning: a
    non-auto-execute decision writes a blocking checkpoint_brief and notifies the
    operator instead of executing. `gate_override=True` skips the gate — used when
    re-dispatching a task whose checkpoint the operator has explicitly approved.

    Args:
        entity_id:     Neotoma entity ID of the task
        snapshot:      Current task snapshot
        trigger:       Event that triggered dispatch ("created", "due_today", "approved")
        notifier:      Notifier for dispatch-failure + checkpoint alerts
        gate_override: When True, bypass the gate (operator already approved)
        snapshot_hydrated: Whether `snapshot` is known to reflect what Neotoma
            holds. False means the read FAILED and the snapshot is unknown — an
            unroutable verdict drawn from it would be an artifact of the failed
            read, so the escalation is deferred. None means "not applicable"
            (callers that fetched the snapshot themselves, e.g. the reconciler
            and the watchdog, which only ever hold real query results).
        detach_after_accept: Return after the approved task has durably entered
            EXECUTING, while completing the harness run in the background.
            Used by the request/response checkpoint-release path so client
            cancellation cannot abort work that the operator already released.
    """
    title = snapshot.get("title", "(untitled)")
    current_status = snapshot.get("status")

    # ── Terminal guard: a finished task is not dispatchable (ateles#1038) ────
    #
    # MUST come before every other path — before the ROUTED write, before the
    # readiness gate, and before the execution gate. This used to be absent
    # entirely: `current_status` was read on the line above and then passed to
    # `set_task_status(..., from_status=...)` as a LABEL only, never tested. So
    # a `task.created` event — including an SSE redelivery or a replay — re-ran
    # finished work, and the gate's AWAITING_APPROVAL write landed on top of
    # `done`/`completed`, manufacturing an operator decision about work that was
    # already done.
    #
    # `gate_override` does NOT bypass this. That flag means "the operator
    # approved this checkpoint", which is consent to skip the gate, not an
    # instruction to re-run a task that has since finished — and a stale
    # approval replaying is exactly the path where this matters most.
    #
    # `is_terminal` rather than `current_status in TERMINAL`: the set holds
    # canonical values, and agents write synonyms. Testing the raw snapshot
    # string would let every task spelled `completed` straight through
    # (ateles#1039) — a guard that misses the most common spelling of the state
    # it guards against is not a control.
    #
    # Deliberately NOT terminal: BLOCKED (operator remediation reopens it) and
    # absent/empty (an unset status is a new task, not a finished one). Reading
    # either as terminal would stop the dispatcher dispatching live work, which
    # is a larger outage than the queue noise this closes.
    if is_terminal(current_status):
        log.info(
            f"[{DAEMON_NAME}] task {entity_id!r} is already terminal "
            f"(status={normalize_status(current_status)!r}, trigger={trigger}) "
            "— not dispatching; finished work is not re-opened"
        )
        return

    # VERIFIED means the executor already returned successfully and only the
    # run's terminal provenance remains unresolved. Re-entering normal dispatch
    # here would repeat the completed effect; the watchdog owns reconciliation
    # of this state and never calls the executor for it.
    if normalize_status(current_status) == TaskStatus.VERIFIED.value:
        log.info(
            f"[{DAEMON_NAME}] task {entity_id!r} has a completed effect with "
            f"pending provenance (trigger={trigger}) — not dispatching"
        )
        return

    # A task the deferral mechanism marked is owned by its recovery sweep: nothing
    # else may dispatch it (an SSE redelivery, the reconciler, the watchdog), or it
    # would run twice. Only the sweep's own re-dispatch (`build_released`) and an
    # operator-approved release pass.
    if (
        trigger != "build_released"
        and not gate_override
        and snapshot.get("disposition") == build_delivery.DEFERRED_DISPOSITION
        and normalize_status(current_status) == TaskStatus.PENDING.value
    ):
        log.info(
            f"[{DAEMON_NAME}] task {entity_id!r} is deferred behind a build and "
            f"owned by the recovery sweep — not dispatching it here (trigger={trigger})"
        )
        return

    # The snapshot read fine, so any prior unreadable streak for this task is
    # over; forget it so a later blip starts counting from zero rather than
    # inheriting an old streak and reporting prematurely.
    if snapshot_hydrated:
        _unroutable.clear_unreadable(entity_id)

    # Prefer tags already in snapshot (set by neotoma-agent hygiene)
    existing_tags: list[str] = snapshot.get("tags", []) or []
    if isinstance(existing_tags, str):
        import json as _json

        try:
            existing_tags = _json.loads(existing_tags)
        except (ValueError, TypeError):
            existing_tags = []

    # Fall back to local inference when the tags we have cannot route.
    #
    # This used to run only when `existing_tags` was EMPTY, which made tags that
    # are present but unmapped strictly worse than no tags at all: they
    # suppressed inference entirely and the task went straight to BLOCKED. That
    # is not hypothetical — a well-formed task carrying
    # ['apis','swarm','gates','observability','ateles-682'] (action_type=fix,
    # confidence=0.9) blocked on exactly this, because none of those five is a
    # DOMAIN_ROUTES key. Tags a human or agent wrote for their own bookkeeping
    # are not a routing decision; asking the text is what we already do when the
    # field is blank, and an unroutable tag set is the same state of knowledge.
    #
    # Inference AUGMENTS rather than replaces: the stored tags are kept ahead of
    # the inferred ones so an explicit routable tag always keeps precedence, and
    # so the escalation reason still reports what the task actually carried.
    if not any(tag in _DOMAIN_ROUTES for tag in existing_tags):
        body = snapshot.get("body", "") or snapshot.get("description", "")
        inferred = _infer_tags_from_text(title, body)
        existing_tags = existing_tags + [
            tag for tag in inferred if tag not in existing_tags
        ]

    # An explicit assigned_to (set by Sylvia/Turdus) wins over tag inference.
    # Canonicalize first: a stored value may carry capitalization, whitespace,
    # or an AAuth-subject suffix, and the "unassigned" sentinel family must read
    # as absence rather than as an owner. Doing this here (not only inside
    # resolve_skill) matters because `assigned_to` is ALSO used as a bare
    # truthiness test for the readiness gate below — the literal string
    # "unassigned" is truthy, so it used to satisfy `has_owner` while naming
    # nobody any dispatcher could resolve.
    assigned_to = _canonical_assignee(snapshot.get("assigned_to"))
    skill = _resolve_skill(existing_tags, assigned_to=assigned_to)
    # resolve_role returns the same string as resolve_skill in this codebase;
    # computed here for explicitness and to thread through to skill_runner so
    # agent_definition loading asks for "the role" rather than "the skill".
    role = _resolve_role(existing_tags, assigned_to=assigned_to)

    # ── Human-owned: a routing ANSWER, not a routing failure ─────────────────
    # Must come before EVERY truthy-skill path (activity, ROUTED, readiness,
    # execution gate, spawn) and before the `skill is None` escalation, because
    # it is neither: the task has an owner, that owner is a person. Parking it
    # for the operator is the correct terminal state — spawning an agent onto
    # work the operator deliberately reserved (merge approvals, auth-path
    # changes) is the failure this closes. `gate_override` does NOT bypass this:
    # operator approval of some earlier checkpoint is not consent to hand a
    # human-owned task to an agent.
    if skill == _HUMAN_OWNED:
        log.info(
            f"[{DAEMON_NAME}] task {entity_id!r} is human-owned "
            f"(assigned_to={assigned_to}) — parking for the operator, not spawning"
        )
        set_task_status(
            entity_id,
            TaskStatus.AWAITING_INPUT,
            handler=DAEMON_NAME,
            from_status=current_status,
            reason=f"human-owned (assigned_to={assigned_to})",
            key_suffix=trigger,
        )
        # Title + id + reason only. The body may carry auth or merge details,
        # and this page is a notification channel, not a place to echo them.
        notifier.send(
            f"Human-owned task awaiting you: {title[:70]}\n"
            f"  assigned_to={assigned_to} task={entity_id}",
            priority=Priority.OPERATOR_DECISION,
            handler=DAEMON_NAME,
        )
        return

    if skill is None:
        # ── Guard: never escalate "no owner" on a snapshot we never read ─────
        # `snapshot_hydrated=False` means the GET failed (502 / read timeout),
        # not that the task has no tags. Those were spelled the same way, so a
        # transient Neotoma blip made a fully-tagged task look unroutable:
        # ent_c192afd8760fd9f3fbd3c08c has a title, a description and five tags
        # and was escalated three times — its real tags logged at 16:22:05, then
        # `tags=[]` at 16:25:25 right after a 502. Defer instead; the reconciler
        # sweep re-examines tasks left `pending`, so deferring loses nothing.
        if snapshot_hydrated is False:
            # Deferring must not mean dropping. The reconciler sweep that would
            # otherwise re-examine a `pending` task is DEFAULT-OFF
            # (APIS_RECONCILE_ENABLED, and it is off in production today), so
            # "leave it pending" would put the task on the floor — the silence
            # failure this whole change exists to avoid. Record it so the count
            # is reported, and page once if a task never becomes readable.
            log.warning(
                f"[{DAEMON_NAME}] task {entity_id!r} could not be hydrated "
                f"(trigger={trigger}) — NOT escalating 'no owner' on an unread "
                "snapshot; recording it as unread and retrying"
            )
            if _unroutable.note_unreadable(entity_id):
                report = _unroutable.drain_unreadable()
                if report:
                    notifier.send(report, priority=Priority.WARN, handler=DAEMON_NAME)
            return

        # No inferable owner. Previously this was a silent log-and-skip — the task
        # fell on the floor. Then it escalated once per delivered event, which on
        # 6.2x duplicate delivery meant 123 pages for 35 tasks. Now it escalates
        # once per task, aggregated, and re-asserts on a timer so a standing
        # backlog cannot fade into apparent health (#583/#636).
        log.info(
            f"[{DAEMON_NAME}] No route for task {entity_id!r} "
            f"(trigger={trigger}, tags={existing_tags}, assigned_to={assigned_to}) "
            "— unroutable (no owner)"
        )
        set_task_status(
            entity_id,
            TaskStatus.BLOCKED,
            handler=DAEMON_NAME,
            from_status=current_status,
            reason=f"no route/owner (tags={existing_tags}, assigned_to={assigned_to})",
            key_suffix=trigger,
        )
        # Stage it; the aggregated report goes out on the window boundary.
        if _unroutable.note(entity_id, title, existing_tags, assigned_to):
            report = _unroutable.drain()
            if report:
                notifier.send(report, priority=Priority.BLOCKER, handler=DAEMON_NAME)
        return

    # ── A task about an issue that is being built right now ──────────────────
    # A builder tracks its own work as a task, and that task's `created` event
    # reaches this dispatcher while the builder is still running: dispatching it
    # starts a second builder on the same issue (ateles#1448). But `task.created`
    # is delivered once per entity per process, so a task that is merely skipped
    # is never examined again. Hence two different outcomes:
    #   * a task carrying the running build's own run marker IS that build —
    #     leave it exactly as it is (the builder reports back on it);
    #   * any other task about the issue is independent work — defer it and
    #     re-dispatch it when the build's claim is released, however it ends.
    # An operator-approved release (`gate_override`) is the operator's explicit
    # decision and is not second-guessed here.
    if not gate_override and skill in build_delivery.BUILDER_SKILLS:
        run_id = build_delivery.tracking_run_id(snapshot)
        if run_id:
            log.info(
                f"[{DAEMON_NAME}] task {entity_id!r} is the tracking task of the "
                f"in-flight build {run_id} — not dispatching a duplicate {skill} "
                f"(trigger={trigger})"
            )
            return
        key = build_delivery.issue_key(snapshot, _dispatch_repo_from_snapshot(snapshot))
        holder = build_delivery.active_build(*key) if key else None
        if holder:
            # Recorded ON THE TASK before it is acknowledged: nothing in memory
            # carries this deferral, so a restart cannot strand it.
            outcome = await _record_deferral(entity_id, snapshot, holder, notifier)
            log.info(
                f"[{DAEMON_NAME}] task {entity_id!r} is about {key[0]}#{key[1]}, "
                f"which {holder.holder!r} is building — deferred until that "
                f"build ends (trigger={trigger})"
            )
            if outcome.sweep_owns_it and build_delivery.active_build(*key) is None:
                # The build ended while the deferral was being recorded, and its
                # release sweep may already have run without seeing a complete
                # marker. A recorded deferral and an intact marker (which the
                # operator was told Apis WILL act on) both need this catch-up.
                schedule_deferred_recovery()
            return

    job = _activity.started(f"routing task {entity_id} → {skill}: {title[:60]}")

    # Lifecycle: the dispatcher resolved an owner — record ROUTED so the task can
    # never read "pending" while it is actually in flight.
    set_task_status(
        entity_id,
        TaskStatus.ROUTED,
        handler=DAEMON_NAME,
        from_status=current_status,
        # An approved release consumes the prior hold. Supplying the empty
        # reason produces taskreason-apis-{task_id}-routed-approved.
        reason="" if gate_override else None,
        key_suffix=trigger,
    )
    if gate_override and not _release_lifecycle_proven(entity_id, TaskStatus.ROUTED):
        log.warning(
            f"[{DAEMON_NAME}] approved task {entity_id} did not read back as "
            "routed with its hold cleared — not spawning"
        )
        notifier.send(
            f"Approved task {entity_id} could not enter routed state — manual recovery needed",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
        return

    # ── Readiness gate (E4) ───────────────────────────────────────────────────
    # Runs BEFORE the execution gate: is the task well-specified enough to start?
    # Under-specified → park in awaiting_input, record the assessment, and ask the
    # operator for the SPECIFIC missing context. Skipped on operator-approved
    # re-dispatch (gate_override) — the operator already willed it forward.
    if READINESS_GATE and not gate_override:
        assessment = assess_readiness(
            snapshot,
            has_owner=bool(assigned_to) or skill is not None,
            relationship_count=int(snapshot.get("relationship_count", 0) or 0),
        )
        if not assessment.ready:
            write_assessment(entity_id, assessment)
            ask = missing_request(assessment, title)
            set_task_status(
                entity_id,
                TaskStatus.AWAITING_INPUT,
                handler=DAEMON_NAME,
                from_status=TaskStatus.ROUTED.value,
                reason=f"readiness {assessment.score:.2f}<{assessment.threshold:.2f}: "
                f"missing {', '.join(assessment.missing)}",
                key_suffix=trigger,
            )
            if RUN_EMAIL:
                send_run_email(
                    task_id=entity_id,
                    run_key=f"{trigger}-readiness",
                    stage="kickoff",
                    title=title,
                    body=ask,
                )
            notifier.send(
                f"NOT READY — needs input: {title[:70]}\n{ask}\n  task={entity_id}",
                priority=Priority.OPERATOR_DECISION,
                handler=DAEMON_NAME,
            )
            job.escalated(
                f"task {entity_id} → {skill} parked awaiting_input "
                f"(readiness={assessment.score:.2f}, missing={','.join(assessment.missing)})"
            )
            return
        log.info(
            f"[{DAEMON_NAME}] readiness: task={entity_id} ready "
            f"({assessment.score:.2f}/{assessment.threshold:.2f})"
        )

    # ── Execution gate ──────────────────────────────────────────────────────
    # Skipped when re-dispatching an operator-approved checkpoint.
    if not gate_override:
        policy = resolve_policy_for_agent(skill)
        action_type = _infer_action_type(skill, snapshot)
        # ateles#902: populate confidence ahead of the read. When no producer
        # scored this task, this mechanically scores it (per confidence_rubric
        # ent_22fd6f25159f1f2689726780) instead of the gate reading an absent
        # field as a silent 0.0 — and marks it unscored so the checkpoint
        # reason says "never scored", not "low confidence".
        action_type_recognized = bool(action_type) and (
            action_type.strip().lower()
            in (policy.low_blast_action_types | policy.high_blast_action_types)
        )
        has_owner = bool(assigned_to) or skill is not None
        relationship_count = int(snapshot.get("relationship_count", 0) or 0)
        # ateles#1142: a task nobody scored gets a real producer score on a
        # cheap tier BEFORE the gate reads it, so the gate decides on a
        # judgment instead of the mechanical estimate. Any failure leaves the
        # snapshot as it was, which is the unscored path below, unchanged.
        confidence_source = ""
        confidence_rationale = ""
        confidence_capped_from = None
        scoring_note = ""
        produced_score = None
        gate_snapshot = snapshot
        if not _confidence_is_explicit(snapshot):
            attempt = await producer_confidence.score_unscored_task(
                snapshot,
                action_type=action_type,
                policy=policy,
                task_entity_id=entity_id,
                mechanical_value=score_confidence(
                    snapshot,
                    has_owner=has_owner,
                    action_type_recognized=action_type_recognized,
                    relationship_count=relationship_count,
                    successful_recurrences=_successful_recurrences(snapshot),
                ).value,
            )
            scoring_note = attempt.note
            produced = attempt.score
            produced_score = produced
            if produced is not None:
                # The validated value, unrounded: it is what the gate compares.
                gate_snapshot = {**snapshot, "confidence": produced.value}
                confidence_source = produced.source
                confidence_rationale = produced.rationale
                confidence_capped_from = produced.capped_from
                log.info(
                    f"[{DAEMON_NAME}] confidence: task={entity_id} producer score "
                    f"{produced.value!r} from {produced.source} "
                    f"(tier={produced.tier}, provider={produced.provider or '?'}, "
                    f"run={produced.run_id}"
                    + (
                        f", capped from {produced.capped_from!r}"
                        if produced.capped_from is not None
                        else ""
                    )
                    + f") rationale={produced.rationale!r}"
                )
        confidence, confidence_unscored = _resolve_confidence(
            gate_snapshot,
            has_owner=has_owner,
            action_type_recognized=action_type_recognized,
            relationship_count=relationship_count,
        )
        decision = evaluate_gate(
            confidence=confidence,
            action_type=action_type,
            policy=policy,
            successful_recurrences=_successful_recurrences(snapshot),
            confidence_unscored=confidence_unscored,
        )
        decision.confidence_source = confidence_source
        if scoring_note and decision.action == GateAction.AUTO_EXECUTE:
            # The scorer was asked and gave no usable score (or the task was too
            # long to assess whole). A score that was missing or failed never
            # lets a task run, so a pass that rested on the mechanical estimate
            # alone is withheld. A recurrence-graduated pass does not depend on
            # the confidence at all, so it is left as the policy decided it.
            withheld = evaluate_gate(
                confidence=0.0,
                action_type=action_type,
                policy=policy,
                successful_recurrences=_successful_recurrences(snapshot),
                confidence_unscored=True,
            )
            if withheld.action != GateAction.AUTO_EXECUTE:
                decision = dataclasses.replace(withheld, confidence=confidence)
        if decision.action == GateAction.AUTO_EXECUTE and produced_score is not None:
            # A producer score is about to authorise a run. Record it durably
            # and prove it by read-back BEFORE the worker is dispatched; if that
            # cannot be proven, hold the task rather than run on a judgment
            # Neotoma cannot explain.
            assessment_id = await asyncio.to_thread(
                write_producer_assessment,
                task_entity_id=entity_id,
                value=produced_score.value,
                threshold=decision.threshold,
                source=produced_score.source,
                action_type=action_type or "",
                policy_id=decision.policy_id,
                tier=produced_score.tier,
                model=produced_score.model,
                run_id=produced_score.run_id,
                rationale=produced_score.rationale,
                gate_action=decision.action.value,
                handler=DAEMON_NAME,
                capped_from=produced_score.capped_from,
            )
            if assessment_id:
                log.info(
                    f"[{DAEMON_NAME}] confidence: task={entity_id} producer score "
                    f"recorded as {assessment_id} before dispatch"
                )
            else:
                log.error(
                    f"[{DAEMON_NAME}] task {entity_id}: producer score could not be "
                    "recorded and proven — holding instead of auto-executing"
                )
                decision = dataclasses.replace(
                    decision,
                    action=GateAction.CHECKPOINT,
                    reason=(
                        "producer score could not be recorded durably — held for "
                        "operator approval rather than run on an unrecorded judgment"
                    ),
                )
        log.info(
            f"[{DAEMON_NAME}] gate: task={entity_id} → {skill} "
            f"action={action_type} blast={decision.blast_radius.value} "
            f"conf={confidence:.2f}/{decision.threshold:.2f}"
            f"{' (unscored)' if confidence_unscored else ''} "
            f"→ {decision.action.value} ({decision.reason})"
        )
        if decision.action != GateAction.AUTO_EXECUTE:
            # Freeze the task before creating the authority the operator will
            # approve.  The authenticated checkpoint binds the complete
            # read-back record after this hold lands; creating it first would
            # make our own lifecycle writes invalidate it immediately.
            set_task_status(
                entity_id,
                TaskStatus.AWAITING_APPROVAL,
                handler=DAEMON_NAME,
                from_status=TaskStatus.ROUTED.value,
                reason=decision.reason,
                key_suffix=trigger,
            )
            task_record = fetch_task_record(entity_id)
            held_snapshot = _record_snapshot(task_record)
            hold_proven = (
                held_snapshot is not None
                and normalize_status(held_snapshot.get("status"))
                == TaskStatus.AWAITING_APPROVAL.value
                and held_snapshot.get("blocked_reason") == decision.reason
            )
            tenant_id = str(fetch_entity_user_id(entity_id) or "").strip()
            brief_id = None
            if hold_proven and tenant_id and task_record is not None:
                brief_id = write_checkpoint_brief(
                    task_entity_id=entity_id,
                    decision=decision,
                    title=title,
                    plan_summary=(
                        f"Assigned to {skill}. Action: {action_type or 'unknown'}. "
                        f"Trigger: {trigger}. {decision.reason}."
                        + _scoring_summary(
                            decision.confidence_source,
                            confidence,
                            confidence_rationale,
                            scoring_note,
                            confidence_capped_from,
                        )
                    ),
                    handler=DAEMON_NAME,
                    user_id=tenant_id,
                    action_type=action_type,
                    task_record=task_record,
                    policy=policy,
                    alternatives=(
                        [
                            "Re-scope to a lower-blast action",
                            "Provide missing inputs",
                            "Decline",
                        ]
                        if decision.action == GateAction.CHECKPOINT_WITH_ALTERNATIVES
                        else None
                    ),
                )
            if not hold_proven or not tenant_id or not brief_id:
                log.error(
                    f"[{DAEMON_NAME}] task {entity_id} was held but its "
                    "authenticated checkpoint could not be proven — refusing "
                    "execution"
                )
            notifier.send(
                f"PLAN checkpoint: {title[:70]}\n"
                f"  agent={skill} blast={decision.blast_radius.value} "
                f"conf={_shown_confidence(confidence, confidence_source)} — "
                f"{decision.reason}\n"
                + (
                    _scoring_summary(
                        confidence_source,
                        confidence,
                        confidence_rationale,
                        scoring_note,
                        confidence_capped_from,
                    ).strip()
                    + "\n"
                ).lstrip("\n")
                + f"  task={entity_id} brief={brief_id or '(unpersisted)'}",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
            log.info(
                f"[{DAEMON_NAME}] HELD task {entity_id} for operator approval "
                f"(checkpoint_brief={brief_id})"
            )
            job.escalated(
                f"task {entity_id} → {skill} held for operator "
                f"(blast={decision.blast_radius.value}, conf={confidence:.2f})"
            )
            return

    log.info(
        f"[{DAEMON_NAME}] → {skill}: task={entity_id} trigger={trigger} "
        f"tags={existing_tags} title={title[:60]!r}"
        + (" (gate: override)" if gate_override else " (gate: auto-execute)")
    )

    _gate_label = "override" if gate_override else "auto-execute"
    if DRY_RUN:
        log.info(f"[{DAEMON_NAME}] DRY RUN — skipping {skill} dispatch for {entity_id}")
        job.finished(
            f"task {entity_id} → {skill} routed (dry-run, gate: {_gate_label})"
        )
        return

    # Lifecycle: about to spawn the T4 agent.
    set_task_status(
        entity_id,
        TaskStatus.EXECUTING,
        handler=DAEMON_NAME,
        from_status=TaskStatus.ROUTED.value,
        key_suffix=trigger,
    )
    if gate_override and not _release_lifecycle_proven(entity_id, TaskStatus.EXECUTING):
        log.warning(
            f"[{DAEMON_NAME}] approved task {entity_id} did not read back as "
            "executing — not spawning"
        )
        notifier.send(
            f"Approved task {entity_id} could not enter executing state — manual recovery needed",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
        return

    @build_delivery.completion_scope
    async def _finish_dispatch() -> None:
        # E1/E2: this run's thread. run_key keys it to the attempt so SSE
        # replays reuse it while a genuine retry opens a fresh run.
        run_key = (
            f"{trigger}-{snapshot.get('attempt', snapshot.get('attempt_count', 0))}"
        )

        # E1: open and verify one conversation + agent_session for this
        # execution run. When capture is required, the durable provenance is a
        # precondition of spawning rather than best-effort telemetry.
        run_conversation_id: str | None = None
        run_agent_session_id: str | None = None
        run_session = None
        if RUN_CONVERSATIONS:
            run_session = create_run_session(
                task_id=entity_id,
                plan_id=snapshot.get("plan_id") or None,
                agent=skill,
                run_key=run_key,
                title=f"{skill} run · {title[:60]}",
            )
            if run_session:
                run_conversation_id = run_session.conversation_id
                run_agent_session_id = run_session.agent_session_id
                log.info(
                    f"[{DAEMON_NAME}] run session {run_agent_session_id} and "
                    f"conversation {run_conversation_id} opened "
                    f"for task {entity_id} (run={run_key})"
                )
            else:
                reason = (
                    "required conversation/agent_session provenance could not be "
                    "persisted and verified"
                )
                log.error(
                    f"[{DAEMON_NAME}] task {entity_id} {reason} — not spawning"
                )
                set_task_status(
                    entity_id,
                    TaskStatus.FAILED,
                    handler=DAEMON_NAME,
                    from_status=TaskStatus.EXECUTING.value,
                    reason=reason,
                    key_suffix=trigger,
                )
                notifier.send(
                    f"{skill} not started on {entity_id}: {reason}",
                    priority=Priority.BLOCKER,
                    handler=DAEMON_NAME,
                )
                job.failed(f"task {entity_id} → {skill} not started: {reason}")
                return

        def _run_stage(role: str, content: str, stage: str) -> None:
            """Record one run-thread event in Neotoma and Gmail."""
            if run_conversation_id:
                append_turn(
                    conversation_id=run_conversation_id,
                    role=role,
                    content=content,
                    sender_kind="orchestrator",
                    idempotency_key=f"runturn-{entity_id}-{stage}-{trigger}",
                )
            if RUN_EMAIL:
                send_run_email(
                    task_id=entity_id,
                    run_key=run_key,
                    stage=stage,
                    title=title,
                    body=content,
                )

        _run_stage(
            "user",
            f"Dispatched {skill} for task {entity_id} (trigger={trigger}): {title}",
            stage="kickoff",
        )

        try:
            result = await _spawn_harness_skill(
                skill,
                entity_id,
                snapshot,
                trigger,
                notifier,
                role=role,
                run_conversation_id=run_conversation_id,
                run_agent_session_id=run_agent_session_id,
            )
        except Exception as exc:
            # Unexpected crash in the spawn machinery itself → record as a
            # failed run.
            _run_stage(
                "assistant",
                f"{skill} dispatch crashed: {type(exc).__name__}: {exc}",
                stage="crash",
            )
            if run_session:
                update_run_session_status(run_session, status="failed")
            set_task_status(
                entity_id,
                TaskStatus.FAILED,
                handler=DAEMON_NAME,
                from_status=TaskStatus.EXECUTING.value,
                reason=f"dispatch raised {type(exc).__name__}: {exc}",
                key_suffix=trigger,
            )
            job.failed(
                f"task {entity_id} → {skill} dispatch failed: {type(exc).__name__}"
            )
            raise

        # ── Deliverable gate (ateles#1155) ───────────────────────────────────
        # A harness exiting 0 is process success, not deliverable success.
        # Cicada could exit 0 having written `[cicada] pull_request_link:
        # BLOCKED`, or no header at all, and the task still went DONE carrying
        # the manufactured result `cicada completed (trigger=created)` — a
        # completion Apis asserted about work it never saw, in the one field a
        # reader would look at for the PR. Roles carrying a declared artifact
        # contract must now name their deliverable; roles without one keep the
        # legacy behaviour verbatim.

        def _artifact_fail(
            status: TaskStatus,
            reason: str,
            *,
            stage: str,
            fail_session: bool = True,
            from_status: str = TaskStatus.EXECUTING.value,
            alert: str = "gate",
        ) -> None:
            """Record a gate refusal: run thread, session, task status + reason, page.

            ``alert`` picks the operator-facing wording so the situations never
            read alike: ``gate`` (a check failed; the task is FAILED and the
            watchdog will retry it), ``agent_blocked`` (the agent asked for
            something), ``held`` (the reference could not be checked, so the task
            is BLOCKED and NOT retried), ``record_not_saved`` (the deliverable
            was accepted, only the task record failed to save).

            The status write's key carries the attempt (`run_key`), not just the
            trigger: the watchdog re-dispatches with a constant trigger, so
            `trigger` alone would replay attempt 1's idempotency key on attempts
            2 and 3.
            """
            _run_stage("assistant", reason, stage=stage)
            if run_session and fail_session:
                update_run_session_status(run_session, status="failed")
            set_task_status(
                entity_id, status, handler=DAEMON_NAME,
                from_status=from_status, reason=reason,
                key_suffix=run_key,
            )
            heads = {
                "gate": (
                    f"{skill} ARTIFACT GATE FAILED on {entity_id} "
                    "(task FAILED; the watchdog will retry it automatically)"
                ),
                "agent_blocked": (
                    f"{skill} reports it is BLOCKED on {entity_id} "
                    "(the agent asking for something; not a gate failure)"
                ),
                "held": (
                    f"{skill} deliverable HELD on {entity_id}: its reference could "
                    "not be checked (task BLOCKED, NOT retried; do not re-dispatch)"
                ),
                "record_not_saved": (
                    f"{skill} deliverable ACCEPTED but the task record did NOT "
                    f"save on {entity_id}"
                ),
                "error": (
                    f"{skill} artifact gate ERROR on {entity_id} (task BLOCKED, NOT "
                    "retried; do not re-dispatch; the error is in the detail field)"
                ),
            }
            job_msgs = {
                "gate": f"task {entity_id} → {skill} artifact gate refused: {stage}",
                "agent_blocked": f"task {entity_id} → {skill} reported it is blocked",
                "held": f"task {entity_id} → {skill} reference could not be checked",
                "error": f"task {entity_id} → {skill} artifact gate raised an error",
                "record_not_saved": (
                    f"task {entity_id} → {skill} deliverable accepted, "
                    "task record not saved"
                ),
            }
            shown = alert_text(reason, _ALERT_LIMIT)
            notifier.send(
                f"{heads[alert]}\n{shown}",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
            job.failed(job_msgs[alert])

        async def _artifact_verdict(contract):
            """Judge one role's declared contract.

            Returns ``(header, identity, note)`` when the deliverable is
            accepted; otherwise records the refusal (see `_artifact_fail`) and
            returns None. Fails closed, in cause order.
            """
            # stdout is authoritative; stderr is consulted ONLY when stdout
            # carries no header, because some harnesses interleave their final
            # answer there.
            header = parse_artifact_header(
                getattr(result, "stdout", "") or "",
                agent=contract.role,
                artifact_kind=contract.artifact_kind,
            )
            if header is None:
                header = parse_artifact_header(
                    getattr(result, "stderr", "") or "",
                    agent=contract.role,
                    artifact_kind=contract.artifact_kind,
                )

            if header is not None:
                # Redact the agent-controlled header ONCE, here, so nothing
                # downstream (the reason, the alert, the run thread, a printed
                # command, the stored result) can carry a secret it printed.
                header = dataclasses.replace(
                    header,
                    body=_redact_agent_text(header.body),
                    matched_line=_redact_agent_text(header.matched_line),
                )

            if header is None:
                # Carry a bounded stdout tail so the operator can see what the
                # agent DID say. Redacted first: child output has held tokens.
                # Redacted (invisible characters first) and neutralized: child
                # output has held tokens, and it is agent-controlled text.
                tail = stdout_tail_for_reason(
                    sanitize_text(
                        _redact_secrets(
                            sanitize_text(
                                getattr(result, "stdout", "") or "", keep_newlines=True
                            )
                        ),
                        keep_newlines=True,
                    ),
                    limit=2048,
                )
                reason = _gate_reason(
                    "missing_header", role=contract.role,
                    kind=contract.artifact_kind, task_id=entity_id,
                )
                _artifact_fail(
                    TaskStatus.FAILED,
                    reason + (f"\nStdout tail:\n{tail}" if tail else ""),
                    stage="failed",
                )
                return None

            body_class = classify_artifact_body(header.body)
            if body_class == "empty":
                _artifact_fail(
                    TaskStatus.FAILED,
                    _gate_reason(
                        "empty_body", role=contract.role,
                        kind=contract.artifact_kind, task_id=entity_id,
                    ),
                    stage="failed",
                )
                return None
            if body_class == "blocked":
                # BLOCKED is the agent reporting honestly, so it is not a
                # failure: BLOCKED is re-openable by operator remediation,
                # FAILED is the watchdog's retry lane, and retrying a stated
                # blocker is noise. The body is kept verbatim — it names what
                # the agent needs.
                _artifact_fail(
                    TaskStatus.BLOCKED,
                    _gate_reason(
                        "blocked", role=contract.role, kind=contract.artifact_kind,
                        task_id=entity_id,
                        verbatim_body=header.body[:800],
                    ),
                    stage="blocked",
                    alert="agent_blocked",
                )
                return None

            accepted = body_shape_for_dispatch(
                role=contract.role,
                dispatch_mode=_dispatch_mode_from_snapshot(snapshot),
            )
            shape = infer_body_shape(header.body)
            if shape not in accepted:
                # Before any resolve: an eng-spec section on a direct impl
                # dispatch answered a different question, and there is nothing
                # to look up.
                _artifact_fail(
                    TaskStatus.FAILED,
                    _gate_reason(
                        "wrong_body_for_dispatch",
                        role=contract.role,
                        kind=contract.artifact_kind,
                        task_id=entity_id,
                        offered=header.body,
                        expected_repo=_dispatch_repo_from_snapshot(snapshot),
                        extra=f"shape={shape} expected={','.join(sorted(accepted))}",
                    ),
                    stage="failed",
                )
                return None

            # The stored result is the header line as redacted, and the read-back
            # compares against that same string.
            stored_line = header.matched_line
            if getattr(result, "fly_delivery_status", ""):
                # Reference parsing requires exactly one URL in the header.
                # Keep the runner metadata in the persisted result, alongside
                # that verified reference, without changing the header body.
                stored_line += "\n" + build_delivery.fly_delivery_summary(result)

            if shape == "eng_spec_section" and not eng_spec_has_content(header.body):
                # A bare token names no authored section.
                _artifact_fail(
                    TaskStatus.FAILED,
                    _gate_reason(
                        "empty_body", role=contract.role, kind=contract.artifact_kind,
                        task_id=entity_id,
                        extra="bare ENG_SPEC_SECTION with no content",
                    ),
                    stage="failed",
                )
                return None

            if contract.resolver_policy != "github_ref" or shape != "pr_or_commit":
                # prose / eng_spec_section, or a role with no resolver (every
                # contract but Cicada's): the header IS the deliverable, even if
                # it happens to contain a link. Nothing to look up.
                # The idempotency key embeds this identity, so agent TEXT is hashed
                # rather than embedded (up to 200 agent-chosen characters).
                return header, _text_identity(stored_line), f"{shape} ok", stored_line, shape

            dispatch_repo = _dispatch_repo_from_snapshot(snapshot)
            # No inner handler: anything `parse_github_ref` raises is an unexpected
            # gate error, and the catch-all at the call site turns it into BLOCKED
            # (`gate_error`), not a FAILED the watchdog would re-run.
            parsed = parse_github_ref(header.body, dispatch_repo=dispatch_repo)
            offered = header.body
            if isinstance(parsed, InvalidRef):
                # `ref_unverifiable` (no repo recorded on the task, a malformed
                # one, or an abbreviated SHA): the reference cannot be checked as
                # offered, so a real PR or commit may exist. BLOCKED, which
                # nothing retries. Every other InvalidRef is a reference the
                # agent got wrong (other repo/host, malformed): FAILED, retried.
                held = parsed.code == "ref_unverifiable"
                _artifact_fail(
                    TaskStatus.BLOCKED if held else TaskStatus.FAILED,
                    _gate_reason(
                        parsed.code,
                        role=contract.role,
                        kind=contract.artifact_kind,
                        task_id=entity_id,
                        offered=offered,
                        expected_repo=dispatch_repo,
                        extra=parsed.reason,
                        situation=parsed.situation,
                        record_value=complete_reference_url(offered) if held else None,
                    ),
                    stage="blocked" if held else "failed",
                    alert="held" if held else "gate",
                )
                return None
            # `gh` is a blocking subprocess (up to 30s, twice): keep it off the
            # event loop so the SSE loop and every other dispatch keep moving.
            check = _normalize_ref_check(
                await asyncio.to_thread(
                    resolve_artifact_ref, parsed, repo=dispatch_repo
                )
            )
            if check == "unavailable":
                # GitHub could not answer. That is not evidence the ref does not
                # exist, and FAILED is the watchdog's automatic re-run lane.
                _artifact_fail(
                    TaskStatus.BLOCKED,
                    _gate_reason(
                        "ref_check_unavailable",
                        role=contract.role,
                        kind=contract.artifact_kind,
                        task_id=entity_id,
                        offered=parsed.canonical or offered,
                        expected_repo=dispatch_repo,
                        record_value=pr_url(parsed),
                    ),
                    stage="blocked",
                    alert="held",
                )
                return None
            if check == "absent":
                _artifact_fail(
                    TaskStatus.FAILED,
                    _gate_reason(
                        "unresolvable_ref",
                        role=contract.role,
                        kind=contract.artifact_kind,
                        task_id=entity_id,
                        offered=parsed.canonical or offered,
                        expected_repo=dispatch_repo,
                    ),
                    stage="failed",
                )
                return None
            return (
                header,
                parsed.canonical or _text_identity(stored_line),
                "artifact ok",
                stored_line,
                shape,
            )

        artifact = None
        contract = role_required_artifact().get(skill) or role_required_artifact().get(
            role or ""
        )
        if result.ok and contract is not None:
            try:
                artifact = await _artifact_verdict(contract)
            except Exception as exc:  # noqa: BLE001 — never leave the task EXECUTING
                # Whatever raised, the agent's deliverable is neither accepted nor
                # judged wrong. BLOCKED, with an alert, so the task does not sit
                # in EXECUTING with no status and an unclosed run session.
                log.error(
                    "[%s] artifact gate raised for %s\n%s",
                    DAEMON_NAME, entity_id, _redact_agent_text(traceback.format_exc()),
                )
                _artifact_fail(
                    TaskStatus.BLOCKED,
                    _gate_reason(
                        "gate_error",
                        role=contract.role,
                        kind=contract.artifact_kind,
                        task_id=entity_id,
                        extra=f"{type(exc).__name__}: {exc}",
                    ),
                    stage="blocked",
                    alert="error",
                )
                return
            if artifact is None:
                return

        if result.ok:
            # A gated role's result is the agent's OWN header line — never a
            # manufactured string. A registry miss (no declared contract, so no
            # deliverable to check) keeps the legacy string verbatim: narrowing
            # completion for a role whose artifact nobody has declared would
            # stall live work.
            base_result = (
                artifact[3]
                if artifact is not None
                else f"{skill} completed (trigger={trigger})"
            )
            pending_result = (
                f"{base_result}; "
                f"run_session={run_session.native_session_id}; "
                "provenance=unverified"
                if run_session
                else base_result
            )
            if run_session:
                set_task_status(
                    entity_id,
                    TaskStatus.VERIFIED,
                    handler=DAEMON_NAME,
                    from_status=TaskStatus.EXECUTING.value,
                    result=pending_result,
                    key_suffix=trigger,
                )
            if run_session and not update_run_session_status(
                run_session, status="completed"
            ):
                reason = (
                    "terminal agent_session state could not be persisted and "
                    "verified"
                )
                _run_stage(
                    "assistant",
                    f"{skill} result withheld: {reason}.",
                    stage="terminal-state-failed",
                )
                set_task_status(
                    entity_id,
                    TaskStatus.VERIFIED,
                    handler=DAEMON_NAME,
                    from_status=TaskStatus.VERIFIED.value,
                    reason=reason,
                    result=pending_result,
                    key_suffix=trigger,
                )
                notifier.send(
                    f"{skill} returned successfully on {entity_id}, but {reason}; "
                    "task held VERIFIED for provenance reconciliation",
                    priority=Priority.BLOCKER,
                    handler=DAEMON_NAME,
                )
                job.failed(
                    f"task {entity_id} → {skill} completion withheld: {reason}"
                )
                return
            done_from = (
                TaskStatus.VERIFIED.value if run_session else TaskStatus.EXECUTING.value
            )
            if artifact is not None:
                if getattr(result, "fly_delivery_status", ""):
                    from fly_runner import FlyResultUnreadable, require_task_delivery_admission

                    try:
                        await require_task_delivery_admission(
                            result, task_id=entity_id,
                            repository=_dispatch_repo_from_snapshot(snapshot))
                    except FlyResultUnreadable:
                        _artifact_fail(TaskStatus.BLOCKED, "Fly delivery admission changed before completion", stage="blocked")
                        return
                header, identity, note, stored_line, shape = artifact
                # `complete_task_with_result` writes `result` before `status`
                # AND reads both back: the exact header line must be the stored
                # result before DONE is written, and the terminal status must
                # read back as done (with that result) before anything below
                # claims the task finished. A 2xx from `/correct` is not that
                # evidence.
                outcome = complete_task_with_result(
                    entity_id, handler=DAEMON_NAME, result=stored_line,
                    fetch_snapshot=fetch_task_snapshot,
                    from_status=done_from, key_suffix=run_key,
                    artifact_identity=identity,
                )
                saved = bool(outcome)
                if not saved:
                    # One more read before writing BLOCKED: the read-back that
                    # failed may have been stale. A task that is truly DONE with
                    # this result is done, and a BLOCKED write must never
                    # overwrite a DONE task.
                    try:
                        live = fetch_task_snapshot(entity_id) or {}
                    except Exception:  # noqa: BLE001 — a failed read changes nothing
                        live = {}
                    if normalize_status(live.get("status")) == TaskStatus.DONE.value:
                        if live.get("result") == stored_line:
                            log.warning(
                                f"[{DAEMON_NAME}] task {entity_id} read back stale "
                                f"at {outcome.stage} but is DONE with the result; "
                                "treating it as saved"
                            )
                            saved = True
                        else:
                            notifier.send(
                                f"{skill} deliverable ACCEPTED on {entity_id}, but the "
                                "task is already DONE with a different result; not "
                                "overwriting it. Check the task by hand.",
                                priority=Priority.BLOCKER,
                                handler=DAEMON_NAME,
                            )
                            job.failed(
                                f"task {entity_id} → {skill} task already DONE with "
                                "a different result"
                            )
                            return
                if not saved:
                    # BLOCKED, not FAILED: the deliverable itself was accepted
                    # (the ref resolved), only its durable record did not hold.
                    # FAILED is the watchdog's retry lane and would re-run the
                    # agent's whole job — a second PR — to repair a bookkeeping
                    # write. BLOCKED waits for operator remediation instead.
                    _artifact_fail(
                        TaskStatus.BLOCKED,
                        _gate_reason(
                            "record_not_saved",
                            role=skill or "unknown",
                            kind=contract.artifact_kind,
                            task_id=entity_id,
                            ref_based=shape == "pr_or_commit",
                            record_value=stored_line,
                            extra=(
                                (f"ref={identity} " if shape == "pr_or_commit" else "")
                                + f"failed_at={outcome.stage} ({outcome.detail})"
                            ),
                        ),
                        stage="blocked",
                        fail_session=False,
                        from_status=done_from,
                        alert="record_not_saved",
                    )
                    return
                _run_stage("assistant", stored_line, stage="done")
                job.finished(
                    f"task {entity_id} dispatched → {skill} "
                    f"(gate: {_gate_label}; {note})"
                )
                return
            _run_stage(
                "assistant", f"{skill} completed (trigger={trigger}).", stage="done"
            )
            set_task_status(
                entity_id,
                TaskStatus.DONE,
                handler=DAEMON_NAME,
                from_status=done_from,
                result=pending_result.replace(
                    "provenance=unverified", "provenance=verified"
                ),
                key_suffix=trigger,
            )
            job.finished(f"task {entity_id} dispatched → {skill} (gate: {_gate_label})")
        elif getattr(result, "fly_delivery_status", ""):
            reason = result.error or build_delivery.fly_delivery_summary(result)
            _run_stage("assistant", reason, stage="blocked")
            if run_session:
                update_run_session_status(run_session, status="failed")
            set_task_status(
                entity_id, TaskStatus.BLOCKED, handler=DAEMON_NAME,
                from_status=TaskStatus.EXECUTING.value, reason=reason,
                result=reason, key_suffix=trigger,
            )
            try:
                saved = fetch_task_snapshot(entity_id) or {}
                held = (normalize_status(saved.get("status")) == TaskStatus.BLOCKED.value
                        and saved.get("result") == reason)
            except Exception:
                held = False
            notice = reason if held else (
                f"{reason} Task result/status could not be verified; durable Fly receipt "
                "still refuses another delivery. Reconcile the record."
            )
            notifier.send(notice, priority=Priority.OPERATOR_DECISION, handler=DAEMON_NAME)
            job.failed(f"task {entity_id} → {skill} Fly delivery ended")
        elif getattr(result, "remote_outcome_unknown", False):
            # ateles#1456: the remote run may have done its work. BLOCKED, not
            # FAILED: the watchdog retries FAILED tasks and leaves BLOCKED ones
            # to the operator, who must reconcile its effects first.
            reason = (
                f"remote run {result.remote_run_id} has an UNKNOWN outcome (machine "
                f"{result.remote_machine_state or 'unconfirmed'}); reconcile what it did "
                "before re-dispatching — not retried automatically"
            )
            _run_stage("assistant", f"{skill}: {reason}", stage="blocked")
            if run_session:
                update_run_session_status(run_session, status="failed")
            set_task_status(
                entity_id,
                TaskStatus.BLOCKED,
                handler=DAEMON_NAME,
                from_status=TaskStatus.EXECUTING.value,
                reason=reason,
                key_suffix=trigger,
            )
            notifier.send(
                f"{skill} on {entity_id}: {reason}",
                priority=Priority.OPERATOR_DECISION,
                handler=DAEMON_NAME,
            )
            job.failed(f"task {entity_id} → {skill} outcome unknown: {result.remote_run_id}")
        else:
            reason = result.error or f"rc={result.returncode}"
            _run_stage(
                "assistant",
                f"{skill} failed (trigger={trigger}): {reason}",
                stage="failed",
            )
            if run_session:
                update_run_session_status(run_session, status="failed")
            # FAILED (not BLOCKED): the stall watchdog owns retry-with-backoff
            # and escalation-on-exhaustion out-of-band.
            set_task_status(
                entity_id,
                TaskStatus.FAILED,
                handler=DAEMON_NAME,
                from_status=TaskStatus.EXECUTING.value,
                reason=reason,
                key_suffix=trigger,
            )
            notifier.send(
                f"{skill} failed on {entity_id} ({reason}) — task marked FAILED",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
            job.failed(f"task {entity_id} → {skill} failed: {reason[:60]}")

    if detach_after_accept:
        task = asyncio.create_task(
            _finish_dispatch(), name=f"apis-dispatch-{entity_id}-{trigger}"
        )
        _background_dispatch_tasks.add(task)
        task.add_done_callback(_background_dispatch_done)
        return

    await _finish_dispatch()


# ── Checkpoint resolution ───────────────────────────────────────────────────


def _record_snapshot(record: dict | None) -> dict | None:
    """Unwrap an entity record without treating a failed read as empty data."""
    if not isinstance(record, dict):
        return None
    snapshot = record.get("snapshot")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("snapshot"), dict):
        return snapshot["snapshot"]
    if isinstance(snapshot, dict):
        return snapshot
    return None


def _release_lifecycle_proven(task_id: str, expected_status: TaskStatus) -> bool:
    """Read back the lifecycle fields that authorize an approved-task spawn."""
    snapshot = fetch_task_snapshot(task_id)
    if snapshot is None:
        return False
    return (
        normalize_status(snapshot.get("status")) == expected_status.value
        and snapshot.get("blocked_reason") == ""
    )


def _checkpoint_denial_marker(checkpoint_id: str) -> Path | None:
    configured = os.environ.get("APIS_CHECKPOINT_DENIAL_DIR", "").strip()
    if not configured:
        log.critical(
            "[%s] APIS_CHECKPOINT_DENIAL_DIR is not configured; durable "
            "cross-process denial fallback is unavailable",
            DAEMON_NAME,
        )
        return None
    root = Path(configured)
    if not root.is_absolute():
        log.critical(
            "[%s] APIS_CHECKPOINT_DENIAL_DIR must be an absolute shared-state path",
            DAEMON_NAME,
        )
        return None
    digest = hashlib.sha256(checkpoint_id.encode()).hexdigest()
    return root / f"{digest}.denied"


def _valid_checkpoint_denial_marker(marker: Path) -> bool:
    try:
        mode = os.lstat(marker).st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def _checkpoint_denial_persisted(checkpoint_id: str) -> bool:
    marker = _checkpoint_denial_marker(checkpoint_id)
    return marker is not None and _valid_checkpoint_denial_marker(marker)


def _persist_checkpoint_denial(checkpoint_id: str) -> bool:
    """Persist a cross-process replay deny when Neotoma writes both fail."""
    marker = _checkpoint_denial_marker(checkpoint_id)
    if marker is None:
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        parent_mode = os.lstat(marker.parent).st_mode
        if not stat.S_ISDIR(parent_mode) or stat.S_ISLNK(parent_mode):
            log.error("[%s] denial marker root is not a real directory", DAEMON_NAME)
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(marker, flags, 0o600)
    except FileExistsError:
        return _valid_checkpoint_denial_marker(marker)
    except OSError:
        log.exception(
            "[%s] could not create durable checkpoint denial marker", DAEMON_NAME
        )
        return False
    try:
        os.write(fd, b"denied\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        dir_fd = os.open(marker.parent, dir_flags)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        log.exception("[%s] could not fsync denial marker directory", DAEMON_NAME)
        return False
    return _valid_checkpoint_denial_marker(marker)


def _deny_checkpoint_release(checkpoint_id: str, *, reason: str) -> None:
    """Consume a denied approval with durable cross-process fallbacks."""
    if close_checkpoint_without_release(
        checkpoint_id, handler=DAEMON_NAME, reason=reason
    ):
        return

    # If the purpose-built terminal state cannot be proven, consume the replay
    # claim as a last-resort durable deny. This field normally means a dispatch
    # happened, so use it only on terminalization failure and log the degraded
    # representation loudly. If that write also fails, a host-persistent
    # marker blocks this approval across daemon restarts and consumer processes.
    if stamp_checkpoint_dispatched(checkpoint_id, handler=DAEMON_NAME):
        log.error(
            f"[{DAEMON_NAME}] checkpoint {checkpoint_id} denial could not be "
            "terminalized; consumed replay claim as a fail-closed fallback"
        )
    elif _persist_checkpoint_denial(checkpoint_id):
        log.critical(
            f"[{DAEMON_NAME}] checkpoint {checkpoint_id} denial could not be "
            "persisted in Neotoma; wrote a durable host denial marker"
        )
    else:
        raise RuntimeError(
            f"checkpoint {checkpoint_id} denial could not be persisted safely"
        )


def _task_gate_context(task_snapshot: dict):
    """The skill, action type, live policy and gate decision for a task as it is."""
    assigned_to = _canonical_assignee(task_snapshot.get("assigned_to"))
    skill = assigned_to
    if not skill:
        tags = task_snapshot.get("tags", []) or []
        if isinstance(tags, str):
            import json as _json

            try:
                tags = _json.loads(tags)
            except (ValueError, TypeError):
                tags = []
        skill = _resolve_skill(tags, assigned_to=assigned_to)
    action_type = _infer_action_type(skill or "", task_snapshot)
    policy = resolve_policy_for_agent(skill)
    decision = evaluate_gate(
        confidence=_read_confidence(task_snapshot),
        action_type=action_type,
        policy=policy,
        successful_recurrences=_successful_recurrences(task_snapshot),
    )
    return skill, action_type, policy, decision


def _approval_cannot_release(task_snapshot: dict) -> str | None:
    """Why approving a checkpoint for this task could never release it, if so.

    The release handler refuses an operator-only action and a never-tier blast
    radius whatever the approval says.  Returns ``"operator_only"`` (the work is
    reserved to the operator) or ``"unclassified"`` (the action type is not
    classified by the policy, which fails closed to never-tier and needs a
    classification repair), else ``None``.
    """
    _skill, action_type, _policy, decision = _task_gate_context(task_snapshot)
    if action_type == "operator_only":
        return "operator_only"
    if decision.blast_radius == BlastRadius.NEVER:
        return "unclassified"
    return None


def _file_fresh_checkpoint(
    *,
    prior_checkpoint_id: str,
    task_id: str,
    task_snapshot: dict,
    notifier: Notifier | None,
    plan_summary: str | None = None,
    reason: str = "task authorization context changed; fresh approval required",
    context_label: str = "fresh",
) -> str | None:
    """Create a replacement checkpoint under the current live policy.

    ``notifier=None`` files it silently (the bulk re-issue path sends one
    summary instead of one page per task).
    """
    skill, action_type, policy, decision = _task_gate_context(task_snapshot)
    if decision.action == GateAction.AUTO_EXECUTE:
        decision = GateDecision(
            action=GateAction.CHECKPOINT,
            blast_radius=decision.blast_radius,
            confidence=decision.confidence,
            threshold=decision.threshold,
            policy_id=decision.policy_id,
            reason=reason,
            confidence_unscored=decision.confidence_unscored,
        )
    task_record = fetch_task_record(task_id)
    tenant_id = str(fetch_entity_user_id(task_id) or "").strip()
    if task_record is None or not tenant_id:
        log.error(
            f"[{DAEMON_NAME}] cannot create fresh authority for {task_id}: "
            "task revision or tenant provenance is unavailable"
        )
        return None
    context = hashlib.sha256(
        (
            f"{prior_checkpoint_id}\0{action_type or ''}\0{policy.entity_id}\0"
            f"{entity_record_digest(task_record)}\0{execution_policy_revision(policy)}"
        ).encode()
    ).hexdigest()[:20]
    brief_id = write_checkpoint_brief(
        task_entity_id=task_id,
        decision=decision,
        title=str(task_snapshot.get("title") or "Re-approve changed task"),
        plan_summary=plan_summary
        or (
            "The task authorization context changed after the prior approval. "
            f"Current action: {action_type or 'unknown'}. {decision.reason}."
        ),
        handler=DAEMON_NAME,
        user_id=tenant_id,
        action_type=action_type,
        idempotency_context=f"{context_label}-{context}",
        task_record=task_record,
        policy=policy,
    )
    if brief_id and notifier is not None:
        notifier.send(
            f"Fresh approval required for changed task {task_id}\n  brief={brief_id}",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
    return brief_id


def _require_fresh_release_authority(
    checkpoint_id: str,
    *,
    task_id: str,
    task_snapshot: dict,
    notifier: Notifier,
    reason: str,
) -> str | None:
    """Create replacement authority before retiring the stale approval."""
    replacement_id = _file_fresh_checkpoint(
        prior_checkpoint_id=checkpoint_id,
        task_id=task_id,
        task_snapshot=task_snapshot,
        notifier=notifier,
    )
    if replacement_id == checkpoint_id:
        # Never retire the brief that is itself the replacement: that would
        # leave the task with no resolvable checkpoint at all.
        log.error(
            f"[{DAEMON_NAME}] replacement for checkpoint {checkpoint_id} resolved "
            "to the same entity — not retiring it"
        )
        notifier.send(
            f"Fresh checkpoint for task {task_id} resolved to the checkpoint it "
            "was meant to replace; it was not retired and no work was released",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
        return None
    if not replacement_id:
        notifier.send(
            f"Fresh checkpoint for changed task {task_id} could not be persisted; "
            "retry is required; the prior approval remains retriable and no work "
            "was released",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
        return None
    retired = require_fresh_checkpoint_approval(
        checkpoint_id, handler=DAEMON_NAME, reason=reason
    )
    if not retired and not _persist_checkpoint_denial(checkpoint_id):
        raise RuntimeError(
            f"checkpoint {checkpoint_id} stale authority could not be retired safely"
        )
    return replacement_id


def migrate_checkpoint_authority(
    checkpoint_id: str, *, notifier: Notifier
) -> str | None:
    """Replace one approved unsigned brief with a fresh v2 approval request.

    This is deliberately a migration, not a release: the replacement binds the
    current complete task and policy revisions in an AAuth-backed observation
    and remains awaiting_operator until the operator approves that exact state.
    """
    checkpoint_record = fetch_checkpoint_record(checkpoint_id)
    snapshot = _record_snapshot(checkpoint_record)
    if snapshot is None or read_checkpoint_resolution(snapshot) != "approved":
        log.error(
            "[%s] checkpoint %s is not an approved checkpoint",
            DAEMON_NAME,
            checkpoint_id,
        )
        return None
    if checkpoint_already_dispatched(snapshot) or _checkpoint_denial_persisted(
        checkpoint_id
    ):
        log.error(
            "[%s] checkpoint %s is already consumed",
            DAEMON_NAME,
            checkpoint_id,
        )
        return None
    if read_authenticated_checkpoint_authorization(checkpoint_id, checkpoint_record):
        log.error(
            "[%s] checkpoint %s already has v2 authority",
            DAEMON_NAME,
            checkpoint_id,
        )
        return None
    task_id = str(snapshot.get("task_entity_id") or "").strip()
    task_record = fetch_task_record(task_id) if task_id else None
    task_snapshot = _record_snapshot(task_record)
    brief_user_id = fetch_entity_user_id(checkpoint_id)
    task_user_id = fetch_entity_user_id(task_id) if task_id else None
    if (
        task_snapshot is None
        or not brief_user_id
        or not task_user_id
        or brief_user_id != task_user_id
    ):
        log.error(
            "[%s] checkpoint %s lacks matching task and tenant provenance",
            DAEMON_NAME,
            checkpoint_id,
        )
        return None
    return _require_fresh_release_authority(
        checkpoint_id,
        task_id=task_id,
        task_snapshot=task_snapshot,
        notifier=notifier,
        reason="legacy approval migrated to v2 authority; fresh approval required",
    )


@dataclasses.dataclass
class ReissueResult:
    task_id: str
    outcome: str
    detail: str = ""
    checkpoint_id: str | None = None
    superseded: list[str] = dataclasses.field(default_factory=list)
    remaining: list[str] = dataclasses.field(default_factory=list)


def _checkpoint_authority_state(checkpoint_id: str, task_record: dict) -> str:
    """Classify a pending brief as ``current``, ``stale`` or ``unreadable``.

    ``current`` means the brief would still be accepted for release as things
    stand: its authenticated envelope binds the task as it is now AND the policy
    and action context as they are now, judged the way release judges them.  A
    task nobody touched can still hold an obsolete brief, because the policy it
    was filed under has since changed.

    ``stale`` is a positive finding: the brief was read and carries no authority
    envelope, or its authenticated envelope does not bind the task and policy as
    they are now.  ``unreadable`` means the checkpoint, its authorization
    observations, or the current policy could not be read at all.  A failed read
    is not evidence that authority is stale (docs/foundation/failure_posture.md),
    so callers must never retire or replace a brief on that basis.
    """
    record = fetch_checkpoint_record(checkpoint_id)
    if record is None:
        return "unreadable"
    snapshot = _record_snapshot(record) or {}
    if not isinstance(snapshot.get("body"), str):
        return "stale"  # a pre-signing brief: it has no envelope to read
    observations = fetch_entity_observations_strict(checkpoint_id)
    if observations is None:
        return "unreadable"
    authority = read_authenticated_checkpoint_authorization(
        checkpoint_id, record, observations=observations
    )
    if not authority:
        return "stale"
    if authority.get("task_revision") != entity_record_digest(task_record):
        return "stale"
    # The task is as it was.  Whether the brief is still current now depends on
    # the policy and action context release would judge it against.
    _skill, action_type, policy, decision = _task_gate_context(
        _record_snapshot(task_record) or {}
    )
    if not policy.loaded:
        return "unreadable"
    bound = (
        authority.get("task_entity_id") == task_record.get("entity_id")
        and authority.get("task_observation_count")
        == task_record.get("observation_count")
        and authority.get("task_last_observation_at")
        == task_record.get("last_observation_at")
        and str(authority.get("action_type") or "").strip().lower() == action_type
        and authority.get("policy_entity_id") == policy.entity_id
        and authority.get("policy_revision") == execution_policy_revision(policy)
        and _decision_consistent_with_approval(
            decision,
            gate_action=str(authority.get("gate_action") or ""),
            blast_radius=str(authority.get("blast_radius") or ""),
        )
    )
    return "current" if bound else "stale"


_BLOCKER_OUTCOMES = {
    "operator_only": "needs_operator",
    "unclassified": "needs_classification",
}
_OUTCOME_ON_THE_TASK = (
    "set the task's status in Neotoma to done (or cancelled if you drop it); "
    "no checkpoint action records that outcome. "
    "Rejecting is not a way to close the work: rejecting declines the task."
)
_BLOCKER_SUMMARIES = {
    "operator_only": (
        "Operator action needed: this task is reserved to the operator, so "
        "approving this checkpoint will NOT release it to an agent; approving "
        "only closes the checkpoint and leaves the task as it is. Do the work "
        "yourself, then " + _OUTCOME_ON_THE_TASK
    ),
    "unclassified": (
        "Classification repair needed: this task's action type is not "
        "classified by the execution policy, so it is treated as never "
        "auto-executable and approving this checkpoint will NOT release it to "
        "an agent; approving only closes the checkpoint and leaves the task as "
        "it is. Add the classification (or correct the task's action type), "
        "then re-run recovery, which replaces this checkpoint with one that "
        "reflects the current policy. If you do the work yourself instead, "
        + _OUTCOME_ON_THE_TASK
    ),
}


def reissue_held_task_checkpoint(task_id: str, *, apply: bool) -> ReissueResult:
    """Give one still-held task a fresh, signed, resolvable checkpoint.

    The recovery for tasks that were left at ``awaiting_approval`` with no
    checkpoint the operator can act on: either none was ever persisted (the
    signed write was refused), or only a pre-signing one exists, which carries
    no authority envelope so ``resolve_checkpoint`` can never resolve it.

    Outcomes (the CLI exits non-zero for ``failed`` and ``incomplete``):
      * ``skipped`` - confirmed nothing to do: the task is not held, or it
        already has exactly one checkpoint that release would still accept (it
        binds the task, policy and action as they are now) and no stale pending
        one;
      * ``would_reissue`` / ``would_retire`` - dry run, nothing written;
      * ``reissued`` - a replacement persisted and proved, stale briefs retired;
      * ``retired_stale`` - a resolvable checkpoint already existed; stale
        pending briefs were retired (the retry that finishes a partial run);
      * ``needs_operator`` / ``needs_classification`` - the task now has its
        resolvable checkpoint, but approving it can never release the task
        (an operator-only action, or an action type the policy does not
        classify), so the checkpoint says so instead of inviting approval;
      * ``incomplete`` - a replacement exists but a step after it did not
        complete (a stale brief could not be retired, or the verification read
        after the replacement failed); ``checkpoint_id`` is the replacement,
        ``remaining`` the briefs still pending; rerun to finish;
      * ``failed`` - a read or the replacement write failed: the task could not
        be assessed or fixed.  A checkpoint or authorization read that fails is
        reported here and never treated as proof a brief is stale.

    Guarantees, each pinned by a test:
      * only a task at ``awaiting_approval`` right now is touched, so a done,
        declined, superseded, blocked, or running task is never re-issued;
      * idempotent: one resolvable checkpoint per task and policy state; a rerun
        reuses the proven replacement and only finishes retirement.  A policy
        repair alone (task untouched) makes the earlier checkpoint obsolete, so
        the rerun replaces it, retiring the old one only once the new one is
        proven;
      * fail closed: the replacement must persist and read back with its
        authenticated envelope before anything old is retired, the replacement
        is never retired, nothing is retired on an unreadable record, and
        nothing here releases or dispatches the task;
      * ``apply=False`` writes nothing.
    """
    record = fetch_task_record(task_id)
    snapshot = _record_snapshot(record)
    if record is None or snapshot is None:
        return ReissueResult(task_id, "failed", "task could not be read")
    status = normalize_status(snapshot.get("status"))
    if status != TaskStatus.AWAITING_APPROVAL.value:
        return ReissueResult(task_id, "skipped", f"not held (status={status or 'unset'})")

    existing = checkpoints_for_task(task_id)
    if existing is None:
        return ReissueResult(task_id, "failed", "checkpoint list could not be read")
    current_id: str | None = None
    stale_pending: list[str] = []
    for item in existing:
        checkpoint_id = str(item.get("entity_id") or "")
        item_snapshot = _record_snapshot(item) or {}
        item_status = str(item_snapshot.get("status") or "").strip().lower()
        if not checkpoint_id:
            continue
        if (
            read_checkpoint_resolution(item_snapshot) == "approved"
            and not checkpoint_already_dispatched(item_snapshot)
        ):
            return ReissueResult(
                task_id,
                "skipped",
                f"approved checkpoint {checkpoint_id} is awaiting release",
            )
        if item_status != "awaiting_operator":
            continue
        state = _checkpoint_authority_state(checkpoint_id, record)
        if state == "unreadable":
            # Nothing is created or retired on an unreadable record.
            return ReissueResult(
                task_id,
                "failed",
                f"checkpoint {checkpoint_id} or its authorization could not be "
                "read; nothing was replaced or retired",
                remaining=[checkpoint_id],
            )
        if state == "current" and current_id is None:
            current_id = checkpoint_id
        else:
            stale_pending.append(checkpoint_id)

    blocker = _approval_cannot_release(snapshot)
    blocker_outcome = _BLOCKER_OUTCOMES.get(blocker or "")

    if current_id and not stale_pending:
        if blocker_outcome:
            return ReissueResult(
                task_id,
                blocker_outcome,
                _BLOCKER_SUMMARIES[blocker],
                checkpoint_id=current_id,
            )
        return ReissueResult(
            task_id,
            "skipped",
            f"already has a resolvable checkpoint {current_id}",
            checkpoint_id=current_id,
        )

    if not apply:
        if current_id:
            return ReissueResult(
                task_id,
                "would_retire",
                f"resolvable checkpoint {current_id} exists; would retire "
                f"{len(stale_pending)} stale pending",
                checkpoint_id=current_id,
                remaining=stale_pending,
            )
        return ReissueResult(
            task_id,
            "would_reissue",
            "no resolvable checkpoint"
            + (
                f"; would retire {len(stale_pending)} obsolete pending"
                if stale_pending
                else ""
            )
            + (f"; {_BLOCKER_SUMMARIES[blocker]}" if blocker else ""),
            remaining=stale_pending,
        )

    reissued = current_id is None
    if reissued:
        replacement_id = _file_fresh_checkpoint(
            prior_checkpoint_id="reissue",
            task_id=task_id,
            task_snapshot=snapshot,
            notifier=None,
            plan_summary=(
                _BLOCKER_SUMMARIES[blocker]
                if blocker
                else (
                    "Re-issued: this task was held at the gate without a "
                    "checkpoint the operator could resolve. Approval releases "
                    "the task as it stands now; the original gate reason is "
                    "repeated below. Rejecting this checkpoint declines the task."
                )
            ),
            reason="re-issued checkpoint; operator approval required",
            context_label="reissue",
        )
        if not replacement_id:
            return ReissueResult(
                task_id,
                "failed",
                "replacement checkpoint did not persist and prove",
                remaining=stale_pending,
            )
        current_id = replacement_id
        # The replacement is proven. Re-read the task to confirm it is still
        # held. A failed read is not "the task moved on": report it as
        # incomplete verification and leave the stale briefs pending, so a rerun
        # (which reuses this replacement) finishes the job.
        after_record = fetch_task_record(task_id)
        after = _record_snapshot(after_record)
        if after is None:
            return ReissueResult(
                task_id,
                "incomplete",
                "replacement persisted, but the task could not be re-read to "
                "verify it is still held; retirement is pending, rerun to finish",
                checkpoint_id=current_id,
                remaining=stale_pending,
            )
        if normalize_status(after.get("status")) != TaskStatus.AWAITING_APPROVAL.value:
            return ReissueResult(
                task_id,
                "skipped",
                "task left awaiting_approval during re-issue",
                checkpoint_id=current_id,
            )
    retired: list[str] = []
    remaining: list[str] = []
    for checkpoint_id in stale_pending:
        # Never retire the checkpoint that is the task's resolvable authority,
        # even if the record hands the same id back under another name.
        if checkpoint_id == current_id:
            continue
        if supersede_checkpoint(
            checkpoint_id,
            handler=DAEMON_NAME,
            reason=f"replaced by signed checkpoint {current_id}",
        ):
            retired.append(checkpoint_id)
        else:
            remaining.append(checkpoint_id)
    if remaining:
        return ReissueResult(
            task_id,
            "incomplete",
            f"{len(remaining)} stale pending brief(s) could not be retired; rerun to finish",
            checkpoint_id=current_id,
            superseded=retired,
            remaining=remaining,
        )
    if blocker_outcome:
        return ReissueResult(
            task_id,
            blocker_outcome,
            _BLOCKER_SUMMARIES[blocker],
            checkpoint_id=current_id,
            superseded=retired,
        )
    return ReissueResult(
        task_id,
        "reissued" if reissued else "retired_stale",
        "",
        checkpoint_id=current_id,
        superseded=retired,
    )


def iter_held_task_ids(*, limit: int | None = None):
    """Yield the id of every task at ``awaiting_approval``, paging to the end.

    The whole list is read before the first id is yielded, so recovering one
    task cannot shift the pages still to be read.  Raises ``RuntimeError`` on a
    failed page: a truncated enumeration read as complete would report a partial
    recovery as a whole one.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be a positive integer")
    held = query_all_entities(
        "task",
        snapshot_filters={
            "status": {"op": "eq", "value": TaskStatus.AWAITING_APPROVAL.value}
        },
        limit=limit,
    )
    if held is None:
        raise RuntimeError("could not read the held-task list from Neotoma")
    for item in held:
        task_id = str(item.get("entity_id") or "")
        if task_id:
            yield task_id


def _decision_consistent_with_approval(
    current_decision: GateDecision, *, gate_action: str, blast_radius: str
) -> bool:
    """Whether the gate's decision NOW is compatible with an approved checkpoint.

    Blast radius must match exactly, and so must a checkpoint-requiring action.
    One case also matches: the gate now permits automatic execution of an action
    whose approval was requested.  That happens by construction when a
    replacement checkpoint is filed for a task the gate would let through
    (creation turns AUTO_EXECUTE into CHECKPOINT so the operator still decides),
    and an explicit, authenticated approval of the exact task revision, policy
    revision and action is stronger than the automatic permission, not weaker.
    A decision that is stricter now (NEVER, or a different blast radius) never
    matches, and every revision, tenant and approver check is unchanged.
    """
    if current_decision.blast_radius.value != blast_radius:
        return False
    return (
        current_decision.action.value == gate_action
        or current_decision.action == GateAction.AUTO_EXECUTE
    )


async def handle_checkpoint_brief(
    entity_id: str,
    snapshot: dict,
    notifier: Notifier,
    detach_after_accept: bool = False,
) -> bool:
    """
    React to a checkpoint_brief the gate raised once the operator resolves it.

    approved → re-dispatch the referenced task with the gate bypassed (the
               operator IS the approval the gate was waiting for).
    rejected → mark the task declined; do not execute.
    pending/unknown → no-op (waiting on the operator).

    Idempotency: after deciding the resolution is actionable, the consumer must
    acquire the existing resolved_dispatched stamp before it dispatches. A
    replayed approved/rejected event whose brief carries that stamp is a no-op.
    Re-dispatch is also safe because the task skill owns its own idempotency, but
    the stamp avoids spawning the work twice on SSE redelivery.
    """
    # SSE carries the snapshot from the status correction event. The inline MCP
    # consumer may already have stamped and dispatched after that event was
    # emitted, so the event snapshot is stale by construction. Refresh before
    # the replay guard; failure is unknown and therefore holds.
    checkpoint_record = fetch_checkpoint_record(entity_id)
    current_snapshot = _record_snapshot(checkpoint_record)
    if current_snapshot is None:
        log.warning(
            f"[{DAEMON_NAME}] checkpoint_brief {entity_id} could not be refreshed "
            "— not acting on a potentially stale resolution"
        )
        return False
    snapshot = current_snapshot

    if _checkpoint_denial_persisted(entity_id):
        log.critical(
            f"[{DAEMON_NAME}] checkpoint {entity_id} has a durable denial marker "
            "— refusing replay"
        )
        return False

    resolution = read_checkpoint_resolution(snapshot)
    if resolution is None:
        log.info(
            f"[{DAEMON_NAME}] checkpoint_brief {entity_id} still pending "
            f"(status={snapshot.get('status')!r}) — no action"
        )
        return False

    if checkpoint_already_dispatched(snapshot):
        log.info(
            f"[{DAEMON_NAME}] checkpoint_brief {entity_id} already dispatched "
            f"(resolution={resolution}) — no-op on replay"
        )
        return False

    task_id = snapshot.get("task_entity_id")
    if not task_id:
        log.warning(
            f"[{DAEMON_NAME}] checkpoint_brief {entity_id} {resolution} but has no "
            "task_entity_id — cannot act"
        )
        return False

    title = snapshot.get("title", "(untitled)")

    if resolution == "rejected":
        authorization = read_authenticated_checkpoint_authorization(
            entity_id, checkpoint_record
        )
        brief_user_id = fetch_entity_user_id(entity_id)
        task_user_id = fetch_entity_user_id(task_id)
        required_approver_sub = str(
            (authorization or {}).get("required_approver_sub") or ""
        ).strip()
        required_approver_jkt = str(
            (authorization or {}).get("required_approver_jkt") or ""
        ).strip()
        authenticated_rejection = read_authenticated_checkpoint_resolution(
            entity_id,
            checkpoint_record,
            required_approver_sub=required_approver_sub,
            required_approver_jkt=required_approver_jkt,
            expected_user_id=str(brief_user_id or ""),
            expected_resolution="rejected",
        )
        if (
            authorization is None
            or not brief_user_id
            or not task_user_id
            or brief_user_id != task_user_id
            or authenticated_rejection is None
        ):
            log.warning(
                f"[{DAEMON_NAME}] checkpoint {entity_id} rejection lacks "
                "authenticated resolver authority — task remains held"
            )
            _deny_checkpoint_release(
                entity_id,
                reason="rejection lacks authenticated required-resolver authority",
            )
            return False
        if not mark_task_declined(
            task_id,
            reason=f"operator rejected checkpoint {entity_id}",
            handler=DAEMON_NAME,
        ):
            return False
        if not stamp_checkpoint_dispatched(entity_id, handler=DAEMON_NAME):
            return False
        notifier.send(
            f"Checkpoint rejected: {title[:70]}\n  task={task_id} declined",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        return True

    # approved → fetch the live task before deciding whether the old approval
    # still authorizes its current action.
    task_record = fetch_task_record(task_id)
    task_snapshot = _record_snapshot(task_record)
    if task_snapshot is None:
        log.warning(
            f"[{DAEMON_NAME}] checkpoint {entity_id} approved but task {task_id} "
            "could not be fetched — not dispatching"
        )
        notifier.send(
            f"Checkpoint approved but task {task_id} unreachable — manual dispatch needed",
            priority=Priority.WARN,
            handler=DAEMON_NAME,
        )
        return False

    brief_user_id = fetch_entity_user_id(entity_id)
    task_user_id = fetch_entity_user_id(task_id)
    if not brief_user_id or not task_user_id or brief_user_id != task_user_id:
        log.warning(
            f"[{DAEMON_NAME}] checkpoint {entity_id} and task {task_id} do not have "
            "matching present tenant provenance — closing without dispatch"
        )
        _deny_checkpoint_release(
            entity_id, reason="missing or mismatched tenant provenance"
        )
        return False

    gate_action = str(snapshot.get("gate_action", "")).strip().lower()
    blast_radius = str(snapshot.get("blast_radius", "")).strip().lower()
    releasable_actions = {
        GateAction.CHECKPOINT.value,
        GateAction.CHECKPOINT_WITH_ALTERNATIVES.value,
    }
    assigned_to = _canonical_assignee(task_snapshot.get("assigned_to"))
    safety_skill = assigned_to
    if not safety_skill:
        task_tags = task_snapshot.get("tags", []) or []
        if isinstance(task_tags, str):
            import json as _json

            try:
                task_tags = _json.loads(task_tags)
            except (ValueError, TypeError):
                task_tags = []
        safety_skill = _resolve_skill(task_tags, assigned_to=assigned_to)

    current_action_type = _infer_action_type(safety_skill or "", task_snapshot)
    if current_action_type == "operator_only":
        log.warning(
            f"[{DAEMON_NAME}] checkpoint {entity_id} references operator-only task "
            f"{task_id} — closing resolution without dispatch"
        )
        _deny_checkpoint_release(entity_id, reason="referenced task is operator_only")
        return False

    # A malformed safety classification is terminally non-releasable even if
    # the authorization envelope is also absent or stale.  Consume it through
    # the durable denial path before considering migration or replacement.
    if gate_action not in releasable_actions:
        log.info(
            f"[{DAEMON_NAME}] checkpoint {entity_id} approved with non-releasable "
            f"gate_action={gate_action!r} — closing resolution without dispatch"
        )
        _deny_checkpoint_release(
            entity_id, reason=f"non-releasable gate_action={gate_action!r}"
        )
        return False
    if blast_radius not in {"low", "high"}:
        log.info(
            f"[{DAEMON_NAME}] checkpoint {entity_id} approved with non-releasable "
            f"blast_radius={blast_radius!r} — closing resolution without dispatch"
        )
        _deny_checkpoint_release(
            entity_id, reason=f"non-releasable blast_radius={blast_radius!r}"
        )
        return False

    authorization = read_authenticated_checkpoint_authorization(
        entity_id, checkpoint_record
    )
    authorization_bound = authorization is not None
    if not authorization_bound:
        _require_fresh_release_authority(
            entity_id,
            task_id=task_id,
            task_snapshot=task_snapshot,
            notifier=notifier,
            reason=("checkpoint has no current AAuth-backed authorization observation"),
        )
        return False

    auth_action_type = str(authorization.get("action_type") or "").strip().lower()
    current_policy = resolve_policy_for_agent(safety_skill)
    if not current_policy.loaded:
        _require_fresh_release_authority(
            entity_id,
            task_id=task_id,
            task_snapshot=task_snapshot,
            notifier=notifier,
            reason=(
                "authoritative execution policy is unavailable; fallback policy "
                "cannot release authority"
            ),
        )
        return False
    current_decision = evaluate_gate(
        confidence=_read_confidence(task_snapshot),
        action_type=current_action_type,
        policy=current_policy,
        successful_recurrences=_successful_recurrences(task_snapshot),
    )
    exact_authority = (
        authorization.get("task_entity_id") == task_id
        and authorization.get("user_id") == brief_user_id == task_user_id
        and authorization.get("task_revision") == entity_record_digest(task_record)
        and authorization.get("task_observation_count")
        == task_record.get("observation_count")
        and authorization.get("task_last_observation_at")
        == task_record.get("last_observation_at")
        and auth_action_type == current_action_type
        and authorization.get("policy_entity_id") == current_policy.entity_id
        and authorization.get("policy_revision")
        == execution_policy_revision(current_policy)
        and authorization.get("gate_action") == gate_action
        and authorization.get("blast_radius") == blast_radius
        and _decision_consistent_with_approval(
            current_decision, gate_action=gate_action, blast_radius=blast_radius
        )
    )
    if not exact_authority:
        _require_fresh_release_authority(
            entity_id,
            task_id=task_id,
            task_snapshot=task_snapshot,
            notifier=notifier,
            reason="task or execution policy changed after approval",
        )
        return False

    required_approver_sub = str(
        authorization.get("required_approver_sub") or ""
    ).strip()
    required_approver_jkt = str(
        authorization.get("required_approver_jkt") or ""
    ).strip()
    approval = read_authenticated_checkpoint_resolution(
        entity_id,
        checkpoint_record,
        required_approver_sub=required_approver_sub,
        required_approver_jkt=required_approver_jkt,
        expected_user_id=brief_user_id,
    )
    if approval is None:
        _require_fresh_release_authority(
            entity_id,
            task_id=task_id,
            task_snapshot=task_snapshot,
            notifier=notifier,
            reason=(
                "approval attribution is missing, unreadable, untrusted, or "
                "does not match the required approver principal"
            ),
        )
        return False

    log.info(
        f"[{DAEMON_NAME}] checkpoint {entity_id} APPROVED — re-dispatching task "
        f"{task_id} with gate override"
    )
    # The stamp is the existing replay claim. If it does not land, this attempt
    # owns nothing and must not dispatch; a later consumer delivery may retry.
    if not stamp_checkpoint_dispatched(entity_id, handler=DAEMON_NAME):
        log.warning(
            f"[{DAEMON_NAME}] checkpoint {entity_id} could not be stamped as "
            "dispatched — not releasing task"
        )
        return False
    await dispatch_task(
        task_id,
        task_snapshot,
        trigger="approved",
        notifier=notifier,
        gate_override=True,
        detach_after_accept=detach_after_accept,
    )
    final_task = fetch_task_snapshot(task_id)
    if final_task is None or final_task.get("blocked_reason") != "":
        return False
    final_status = normalize_status(final_task.get("status"))
    if DRY_RUN:
        accepted = final_status == TaskStatus.ROUTED.value
    else:
        accepted = final_status in {
            TaskStatus.EXECUTING.value,
            TaskStatus.VERIFIED.value,
            TaskStatus.DONE.value,
        }
    if not accepted:
        return False

    # This message is an externally visible success claim. Emit it only after
    # both the replay claim and the released task lifecycle have been read back.
    notifier.send(
        f"Checkpoint approved: {title[:70]}\n  re-dispatching task {task_id}",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )
    return True


# ── Event handler ─────────────────────────────────────────────────────────────


async def handle_event(event: NeotomaEvent, notifier: Notifier) -> None:
    """
    Handle a Neotoma SSE task event.

      task.created   → dispatch to domain handler
      task.updated   → check status transitions; notify on due-date changes
      task.due_today → remind operator; auto-execute if APIS_AUTO_EXECUTE=1
    """
    # SSE events carry only metadata; fetch the entity snapshot so routing
    # (tags, assigned_to) sees real fields instead of an empty dict.
    await hydrate_snapshot(event)

    entity_type = event.entity_type
    entity_id = event.entity_id
    action = event.action
    snapshot = event.snapshot or {}

    log.info(f"[{DAEMON_NAME}] Event: {entity_type}/{entity_id} action={action}")

    if entity_type == "checkpoint_brief":
        await handle_checkpoint_brief(entity_id, snapshot, notifier)
        return

    if entity_type != "task":
        # Defensive: SSE client filters by entity type, but guard here too
        return

    title = snapshot.get("title", "(untitled)")
    status = snapshot.get("status", "")

    if action == "created":
        # Neotoma redelivers `task.created` for the same entity — measured
        # 2026-09-01 at 218 events for 35 distinct tasks (6.2x). The redelivery
        # itself is upstream, but re-running the whole create path per copy is
        # not: it re-notified, re-dispatched and re-escalated each time. Collapse
        # duplicates here so the create path runs once per entity per process.
        # Only a SUCCESSFULLY hydrated event may claim the entity. An event whose
        # snapshot could not be read was not really handled — claiming it would
        # make the later, readable redelivery a no-op and drop the task entirely.
        # Measured on the real trace: 14 of 37 tasks had their FIRST created
        # event fail hydration, so claiming on failure lost all 14.
        if event.hydrated and _seen_created(entity_id):
            log.debug(
                f"[{DAEMON_NAME}] duplicate task.created for {entity_id} — "
                "already handled this process; skipping"
            )
            return
        # The INFO notice is deduped SEPARATELY from the dispatch claim above.
        # An unhydrated event must not claim the dispatch (its redelivery still
        # needs handling), but it must not re-announce the task either: a task
        # whose early deliveries all 502 would otherwise emit several
        # "Task created: (untitled)" pages before a readable copy arrives.
        # Announce once, but let a readable delivery WIN over an unreadable one.
        # ~38% of tasks on the measured trace (14 of 37) had their first created
        # event fail hydration; announcing that copy and suppressing the rest
        # would pin the operator to a permanent "Task created: (untitled)" and
        # never show the real title. So an unhydrated announce is provisional:
        # it is claimed only weakly and can be upgraded exactly once by the
        # first readable copy.
        if _should_announce(entity_id, event.hydrated):
            notifier.send(
                f"Task created: {title[:80]}\n  {entity_id}",
                priority=Priority.INFO,
                handler=DAEMON_NAME,
            )
        await dispatch_task(
            entity_id,
            snapshot,
            trigger="created",
            notifier=notifier,
            snapshot_hydrated=event.hydrated,
        )

    elif action == "updated":
        # Tasks dispatch on creation (and on due_today when AUTO_EXECUTE is set);
        # status transitions are logged for observability only to avoid
        # re-dispatching work already routed at creation.
        if status in ("approved", "ready"):
            log.info(f"[{DAEMON_NAME}] Task {entity_id} moved to status={status!r}")
        # Watch for due-date changes (raw payload may include a changed_fields list)
        changed = event.raw.get("changed_fields") or []
        if "due_date" in changed:
            new_due = snapshot.get("due_date", "")
            log.info(f"[{DAEMON_NAME}] Task {entity_id} due_date changed → {new_due}")

    elif action == "due_today":
        notifier.send(
            f"Task due today: {title[:80]}\n  {entity_id}",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
        if AUTO_EXECUTE:
            log.info(
                f"[{DAEMON_NAME}] AUTO_EXECUTE=1 — dispatching due task {entity_id}"
            )
            await dispatch_task(
                entity_id,
                snapshot,
                trigger="due_today",
                notifier=notifier,
                snapshot_hydrated=event.hydrated,
            )
        else:
            log.info(
                f"[{DAEMON_NAME}] AUTO_EXECUTE off — operator notification sent for {entity_id}"
            )


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    denial_store = _require_checkpoint_denial_store()
    log.info(f"[{DAEMON_NAME}] Starting up...")
    log.info(f"[{DAEMON_NAME}] checkpoint_denial_store={denial_store}")
    log.info(f"[{DAEMON_NAME}] ateles_repo={ATELES_REPO}")
    log.info(
        f"[{DAEMON_NAME}] dry_run={DRY_RUN} auto_execute={AUTO_EXECUTE} "
        f"harness_providers={os.environ.get('APIS_HARNESS_PROVIDERS', 'claude,codex,cursor')} "
        f"dispatch_timeout={DISPATCH_TIMEOUT_SECONDS}s"
    )
    # State this at boot either way: a reconciler that is off must say so, or its
    # absence looks identical to a reconciler that ran and found nothing — the
    # very ambiguity that hid the dead subscription for 88 days (ateles#589).
    import task_reconciler as _reconcile_cfg

    log.info(
        f"[{DAEMON_NAME}] task reconciliation sweep: "
        f"{'ENABLED' if _reconcile_cfg.ENABLED else 'DISABLED'} "
        f"(interval={_reconcile_cfg.INTERVAL_SECONDS}s "
        f"cap={_reconcile_cfg.MAX_PER_SWEEP}/sweep "
        f"grace={_reconcile_cfg.GRACE_SECONDS}s "
        f"page={_reconcile_cfg.QUERY_LIMIT} rows x "
        f"{_reconcile_cfg.PAGES_PER_SWEEP} pages/sweep)"
    )

    # 1. Load agent_definition from Neotoma
    agent_def = AgentLoader(DAEMON_NAME).load()
    log.info(
        f"[{DAEMON_NAME}] agent_definition: status={agent_def.status} "
        f"grant={agent_def.agent_grant} sub={agent_def.aauth_sub}"
    )

    # Enforce agent_definition.status (ateles#562). Previously this value was
    # logged and then ignored, so a "retired" agent ran normally.
    enforce_status_or_exit(agent_def, DAEMON_NAME)

    # 2. Load AAuth signer
    signer = AAuthSigner.from_key_file(DAEMON_NAME)
    if signer.is_stub:
        log.warning(
            f"[{DAEMON_NAME}] AAuth keypair not minted yet — "
            "observations attributed to operator token"
        )

    # 3. Load notification rubric
    notifier = Notifier.from_neotoma()
    notifier.send(
        f"{DAEMON_NAME} started (task dispatch, dry_run={DRY_RUN})",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )

    # 4. GitHub webhook gateway (ateles#80): issue.opened → Lanius → Pavo;
    #    pull_request.* → Lanius → review panel → Vanellus. Runs alongside
    #    the SSE task loop.
    dispatcher = SwarmDispatcher(notifier)
    gateway_app = github_gateway.make_app(
        GITHUB_WEBHOOK_SECRET,
        dispatcher.handle_trigger,
        approve_email_secret=os.environ.get("APIS_APPROVE_EMAIL_SECRET", ""),
        secret_next=GITHUB_WEBHOOK_SECRET_NEXT,
        approve_email_secret_next=os.environ.get("APIS_APPROVE_EMAIL_SECRET_NEXT", ""),
    )

    # 5. Subscribe to SSE events
    sse = SSEClient(
        entity_types=SUBSCRIBE_ENTITY_TYPES,
        handler_name=DAEMON_NAME,
    )

    async def dispatch(event: NeotomaEvent) -> None:
        await handle_event(event, notifier)

    # 6. Stall watchdog (task #2): out-of-band sweeper that retries FAILED tasks
    #    with backoff, resumes tasks left mid-flight by a restart, and escalates
    #    once attempts are exhausted — without blocking the SSE loop.
    watchdog = TaskWatchdog()

    async def watchdog_dispatch(task_id: str, snapshot: dict, trigger: str) -> None:
        await dispatch_task(task_id, snapshot, trigger, notifier=notifier)

    # 6b. Task reconciliation sweep (ateles#586/#589): the LEVEL-triggered
    #     backstop under the edge-triggered SSE create path. The watchdog above
    #     rescues work that started and stalled; it deliberately leaves
    #     `pending` alone because "the SSE create path owns it" — which held
    #     only while that path was alive. When the subscription is down (as it
    #     was for 88 days), a task created in the gap gets its one `task.created`
    #     event, nobody consumes it, and NOTHING ever looks at it again.
    #
    #     This sweep re-examines existing `pending` tasks and dispatches the
    #     stranded ones through dispatch_task — no gate_override, so the same
    #     confidence x blast-radius gate applies and high-blast work still holds
    #     for an operator checkpoint. Bounded per pass and default-OFF; see
    #     task_reconciler.py for the three-layer double-dispatch argument.
    reconciler = TaskReconciler()

    async def reconcile_dispatch(task_id: str, snapshot: dict, trigger: str) -> None:
        await dispatch_task(task_id, snapshot, trigger, notifier=notifier)

    # 6c. Deferred-task recovery (ateles#1448): tasks deferred behind a running
    #     build are marked on the task itself. Run the sweep for them now — a
    #     restart is exactly when no in-process state survives — and again on
    #     every build release. ON by default and narrow: it selects only tasks
    #     the deferral mechanism marked, never the general backlog (the
    #     reconciler above stays opt-in). Fire-and-forget, never blocks boot.
    async def deferred_recovery_sweep() -> None:
        try:
            await start_deferred_recovery(notifier)
        except Exception as exc:  # noqa: BLE001 — never let recovery kill startup
            log.error(
                f"[{DAEMON_NAME}] deferred-task recovery sweep failed: {exc}",
                exc_info=True,
            )

    # 7. Issue-pipeline resume sweep: the task watchdog above resumes `task`
    #    work left mid-flight by a restart, but the GitHub issue pipeline
    #    creates no task entity, so it was invisible to that sweeper — a
    #    restart silently voided in-flight pipeline runs with no retry. This
    #    scans for the hidden in-flight marker and re-runs those pipelines.
    #    Fire-and-forget: it must never delay the SSE loop or the webhook
    #    gateway coming up, and never prevent boot on failure.
    async def resume_sweep() -> None:
        try:
            await dispatcher.resume_interrupted_pipelines(
                list(dispatcher.config.resume_repositories)
            )
        except Exception as exc:  # never let a resume failure kill startup
            log.error(
                f"[{DAEMON_NAME}] issue-pipeline resume sweep failed: {exc}",
                exc_info=True,
            )

    # 7a. Closed-issue marker housekeeping (ateles#446). Previously nested
    #     AFTER resume_interrupted_pipelines inside resume_sweep(); resume
    #     routinely outlives the daemon's restart interval (8–25 min), so the
    #     clear never ran in production even though #437's logic was correct.
    #     Scheduled as an independent gather sibling so housekeeping reaches
    #     completion regardless of whether resume is still hung. Fail-open and
    #     always logs completion (including cleared=0) — silence must not mean
    #     "did not run".
    async def clear_closed_issue_markers_sweep() -> None:
        try:
            cleared = await dispatcher._clear_closed_issue_markers(
                list(dispatcher.config.resume_repositories)
            )
            log.info(f"[{DAEMON_NAME}] closed-issue marker sweep: cleared={cleared}")
        except Exception as exc:  # housekeeping must never kill startup siblings
            log.error(
                f"[{DAEMON_NAME}] closed-issue marker sweep failed: {exc} "
                "— will retry next boot; housekeeping independent of resume",
                exc_info=True,
            )

    # 7b. Workflow gate-owner drift check (ateles#441). The workflow_definition
    #     entities name the agent owning each gate; dispatch picks agents from
    #     hardcoded rosters. Nothing compared them, so when two agents were
    #     renamed on 2026-06-12 the workflows kept naming the retired ones and
    #     their gates could never sign — silently, because `pending` is a valid
    #     state. One issue sat that way four days; the auto-build handoff stays
    #     blocked the whole time. Runs once at boot: config drift does not
    #     appear mid-run, and a loud error at startup is what was missing.
    #     Fire-and-forget and fail-open, like the sweeps around it.
    async def workflow_drift_check() -> None:
        try:
            await dispatcher.check_workflow_owner_drift()
        except Exception as exc:  # never let a config check kill startup
            log.error(
                f"[{DAEMON_NAME}] workflow-owner drift check failed: {exc}",
                exc_info=True,
            )

    # 7c. Swarm lens-comment identity check. Every lens verdict, the Vanellus
    #     aggregation and the notice markers are read only from comments a swarm
    #     identity wrote; with none resolvable they all read as absent, reviews
    #     never clear and auto-merge holds. Say so at boot, loudly, rather than
    #     leave a misconfigured deployment looking idle. Fire-and-forget and
    #     fail-open like the checks around it.
    async def lens_identity_check() -> None:
        try:
            await dispatcher.check_lens_comment_identities()
        except Exception as exc:  # never let a config check kill startup
            log.error(
                f"[{DAEMON_NAME}] lens-comment identity check failed: {exc}",
                exc_info=True,
            )

    # 7d. Fly runner limits (ateles#1456). With any class opted onto Fly,
    #     validate its cost, capacity and time settings once at boot and log
    #     the result. An invalid one disables Fly (every launch refuses, and
    #     admission checks again); this makes it visible before the first run.
    try:
        import fly_runner

        if fly_runner.runner_classes():
            fly_runner.log_limit_check()
    except Exception as exc:  # never let a config check kill startup
        log.error(f"[{DAEMON_NAME}] Fly limit check failed: {exc}", exc_info=True)

    # 8. Deferred-review resume sweep: a PR review throttled by a usage limit
    #    posts a `review-deferred-until:<ISO>` marker instead of a verdict. The
    #    reset is often hours out, so unlike the one-shot pipeline resume this
    #    must run PERIODICALLY — the daemon may never restart inside the window.
    #    Each pass re-dispatches PRs whose deferral has matured; if the limit is
    #    still active, the re-run just posts a fresh (later) deferral marker.
    #    Fire-and-forget and fail-open — a broken sweep must never stop the loop.
    deferred_interval = int(os.environ.get("APIS_DEFERRED_REVIEW_SWEEP_SECONDS", "600"))

    async def deferred_review_sweep() -> None:
        while True:
            # Head-scoped review supersession (#507). Webhook synchronize and
            # reopen events retire stale verdicts synchronously; this pass is
            # the bounded backup for missed deliveries. It shares the review
            # sweep cadence, so secondary invalidation lag is at most
            # APIS_DEFERRED_REVIEW_SWEEP_SECONDS (600s by default).
            try:
                await dispatcher.supersede_stale_review_verdicts(
                    list(dispatcher.config.resume_repositories)
                )
            except Exception as exc:
                log.error(
                    f"[{DAEMON_NAME}] review-supersession sweep failed: {exc}",
                    exc_info=True,
                )
            try:
                await dispatcher.resume_deferred_reviews(
                    list(dispatcher.config.resume_repositories)
                )
            except Exception as exc:
                log.error(
                    f"[{DAEMON_NAME}] deferred-review sweep failed: {exc}",
                    exc_info=True,
                )
            # 3. Stalled reviews. The deferred sweep only rescues PRs that got
            #    far enough to post a deferral marker. A review that dies
            #    earlier leaves NOTHING — no marker, no formal review, no error
            #    anyone reads — and with auto-merge keyed on a formal approval
            #    the PR just sits (ateles#408: two days, every check green,
            #    zero reviews, found by a human). Same cadence and the same
            #    fail-open discipline; runs after the deferred pass so a PR
            #    with a live deferral is never double-handled.
            try:
                await dispatcher.resume_stalled_reviews(
                    list(dispatcher.config.resume_repositories)
                )
            except Exception as exc:
                log.error(
                    f"[{DAEMON_NAME}] stalled-review sweep failed: {exc}",
                    exc_info=True,
                )
            # 4. Missing lens verdicts. The two sweeps above cover a review that
            #    never ran and one that died mid-flight. This covers a THIRD
            #    state neither can see: the panel ran, aggregated `Blocking: 0`,
            #    and merge is withheld only because one declared lens produced
            #    no verdict (neotoma#2153, security). The hold is correct — CI
            #    green is not a lens judgement — but nothing re-ran the absent
            #    lens, so a content-clear PR sat with no mechanism watching it.
            #    Re-dispatches ONLY that lens; same cadence, same fail-open
            #    discipline, runs last so a PR the earlier passes already
            #    re-dispatched is not handled twice in one tick.
            try:
                await dispatcher.resume_missing_lens_reviews(
                    list(dispatcher.config.resume_repositories)
                )
            except Exception as exc:
                log.error(
                    f"[{DAEMON_NAME}] missing-lens sweep failed: {exc}",
                    exc_info=True,
                )
            # 5. Unactioned revisions (ateles#511). The three sweeps above all
            #    carry a PR TOWARD a verdict. This is the first that carries one
            #    PAST a verdict: a PR at CHANGES_REQUESTED whose author pushed
            #    the fix and whose review never looked again. Measured
            #    2026-08-31: 23 of 47 open ateles PRs in that state, median 33d.
            #    `resume_stalled_reviews` excludes them by design (it requires
            #    ZERO reviews), so until now nothing watched this state at all.
            #    Same cadence, same fail-open discipline.
            try:
                await dispatcher.resume_unactioned_revisions(
                    list(dispatcher.config.resume_repositories)
                )
            except Exception as exc:
                log.error(
                    f"[{DAEMON_NAME}] revision sweep failed: {exc}",
                    exc_info=True,
                )
            # 6. Approved-unmerged visibility (ateles#565). Reports only — it
            #    never merges. An APPROVED PR presents as done, so nothing was
            #    watching while three of them rotted CLEAN → DIRTY over 33-53
            #    days. Runs last: it reports on the state the sweeps above have
            #    already had their chance to change this tick.
            try:
                await dispatcher.report_pr_review_queue(
                    list(dispatcher.config.resume_repositories)
                )
            except Exception as exc:
                log.error(
                    f"[{DAEMON_NAME}] approved-unmerged report failed: {exc}",
                    exc_info=True,
                )
            await asyncio.sleep(deferred_interval)

    # Flush any aggregated unroutable report whose window has closed. Without
    # this the last report in a burst would wait for the NEXT unroutable task to
    # push it out — a report that exists and is never delivered is the silence
    # failure of #583/#636 arriving by a different route.
    async def unroutable_flush() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                report = _unroutable.drain()
                if report:
                    notifier.send(
                        report, priority=Priority.BLOCKER, handler=DAEMON_NAME
                    )
                unread = _unroutable.drain_unreadable()
                if unread:
                    notifier.send(unread, priority=Priority.WARN, handler=DAEMON_NAME)
            except Exception as exc:  # noqa: BLE001 — never kill the daemon
                log.warning(f"[{DAEMON_NAME}] unroutable flush failed: {exc}")

    log.info(f"[{DAEMON_NAME}] Subscribing to SSE: {SUBSCRIBE_ENTITY_TYPES}")
    await asyncio.gather(
        sse.stream(dispatch),
        github_gateway.serve(gateway_app, GITHUB_WEBHOOK_PORT),
        watchdog.run(notifier, watchdog_dispatch),
        reconciler.run(reconcile_dispatch),
        deferred_recovery_sweep(),
        resume_sweep(),
        clear_closed_issue_markers_sweep(),
        deferred_review_sweep(),
        workflow_drift_check(),
        lens_identity_check(),
        unroutable_flush(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")

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
  APIS_CLAUDE_BIN             Claude CLI path (default: autodetect on PATH).
  APIS_CODEX_BIN              Codex CLI path (defaults to ChatGPT app, then PATH).
  APIS_CURSOR_BIN             Cursor Agent CLI path (default: cursor-agent on PATH).
  APIS_ALLOW_METERED_HARNESS  "1" permits usage-based API-key fallback. Default 0:
                              API keys are removed and capped plans fail over/queue.
  APIS_DISPATCH_TIMEOUT       Per-dispatch timeout in seconds (default: 1800)
  ATELES_REPO_PATH            Local path to ateles clone (default: ~/repos/ateles)

Task reconciliation sweep (ateles#586 — see task_reconciler.py):
  APIS_RECONCILE_ENABLED      "1" runs the level-triggered sweep that dispatches
                              `pending` tasks the SSE create path never saw.
                              Default 0 (off) — the first pass meets a backlog.
  APIS_RECONCILE_INTERVAL_SECONDS  Sweep cadence (default: 900)
  APIS_RECONCILE_MAX_PER_SWEEP     Max dispatches per pass (default: 5)
  APIS_RECONCILE_GRACE_SECONDS     Min task age before eligible (default: 900)
  APIS_RECONCILE_QUERY_LIMIT       Tasks fetched per pass (default: 500)

GitHub trigger layer (ateles#80 — see github_gateway.py / swarm_dispatch.py):
  APIS_GITHUB_WEBHOOK_SECRET  HMAC secret for the GitHub webhook
  APIS_GITHUB_WEBHOOK_PORT    Webhook listen port (default: 8742)
  APIS_PANEL_MAX              Max review panelists per PR (default: 4)
  APIS_AUTONOMY_AUTO_MERGE    "1" lets Vanellus merge without operator approval
                              (default: 0 — blocking checkpoint_brief instead)
  GITHUB_TOKEN                Token for changed-files / issue-comment reads
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# ── Env bootstrap (launchd does not source shell profiles) ───────────────────
_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
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

from lib.daemon_runtime import (  # noqa: E402
    AAuthSigner,
    AgentLoader,
    enforce_status_or_exit,
    append_turn,
    assess_readiness,
    create_run_conversation,
    missing_request,
    send_run_email,
    write_assessment,
    GateAction,
    NeotomaEvent,
    SSEClient,
    evaluate_gate,
    hydrate_snapshot,
    resolve_policy_for_agent,
    write_checkpoint_brief,
)
from lib.daemon_runtime.gating import (  # noqa: E402
    checkpoint_already_dispatched,
    fetch_task_snapshot,
    mark_task_declined,
    read_checkpoint_resolution,
    stamp_checkpoint_dispatched,
)
from lib.daemon_runtime.task_lifecycle import (  # noqa: E402
    TaskStatus,
    set_task_status,
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
    """Best-effort action_type for the gate. Explicit task field wins."""
    explicit = (snapshot.get("action_type") or "").strip().lower()
    if explicit:
        return explicit
    if skill:
        return _AGENT_ACTION_TYPE.get(skill.lower())
    return None


def _read_confidence(snapshot: dict) -> float:
    """
    Read the agent-supplied confidence (0..1) from the task snapshot. Absent an
    explicit score, return 0.0 so the gate fails CLOSED (checkpoint) for any
    non-low-blast action — the operator is asked rather than the swarm guessing.
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

ATELES_REPO = Path(
    os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles"))
)

DRY_RUN = os.environ.get("APIS_DRY_RUN", "0") == "1"
AUTO_EXECUTE = os.environ.get("APIS_AUTO_EXECUTE", "0") == "1"
# E1 (docs/task_execution_loop.md): open one conversation per execution run and
# tell the spawned agent to thread its turns into it. Default off — flag-gated so
# the live dispatch path is byte-identical until the child side (E2) lands.
RUN_CONVERSATIONS = os.environ.get("APIS_RUN_CONVERSATIONS", "0") == "1"
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
    canonical_assignee as _canonical_assignee,
    infer_tags_from_text as _infer_tags_from_text,
    resolve_role as _resolve_role,
    resolve_skill as _resolve_skill,
)

import github_gateway  # noqa: E402

from skill_runner import run_skill  # noqa: E402
from swarm_dispatch import SwarmDispatcher  # noqa: E402
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
        for eid in sorted(_created_seen, key=_created_seen.get)[: _CREATED_SEEN_MAX // 2]:
            _created_seen.pop(eid, None)
    _created_seen[entity_id] = time.time()
    return False
from task_watchdog import TaskWatchdog  # noqa: E402


# ── T4 dispatch ────────────────────────────────────────────────────────────────


async def _spawn_harness_skill(
    skill: str,
    entity_id: str,
    snapshot: dict,
    trigger: str,
    notifier: Notifier,
    *,
    role: str | None = None,
    run_conversation_id: str | None = None,
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
            f"{run_conversation_id} (PART_OF task {entity_id}). When you finalize "
            f"via /end, store your turns PART_OF this conversation "
            f"(conversation_id={run_conversation_id}) rather than creating a new one."
        )

    result = await run_skill(
        skill,
        prompt,
        role=role or skill,
        task_entity_id=entity_id,
        notifier=notifier,
    )
    return result


async def dispatch_task(
    entity_id: str,
    snapshot: dict,
    trigger: str,
    notifier: Notifier,
    gate_override: bool = False,
    snapshot_hydrated: bool | None = None,
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
    """
    title = snapshot.get("title", "(untitled)")
    current_status = snapshot.get("status")

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

    # Fall back to local inference if tags not set yet
    if not existing_tags:
        body = snapshot.get("body", "") or snapshot.get("description", "")
        existing_tags = _infer_tags_from_text(title, body)

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
                    notifier.send(
                        report, priority=Priority.WARN, handler=DAEMON_NAME
                    )
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
            entity_id, TaskStatus.BLOCKED, handler=DAEMON_NAME,
            from_status=current_status,
            reason=f"no route/owner (tags={existing_tags}, assigned_to={assigned_to})",
            key_suffix=trigger,
        )
        # Stage it; the aggregated report goes out on the window boundary.
        if _unroutable.note(entity_id, title, existing_tags, assigned_to):
            report = _unroutable.drain()
            if report:
                notifier.send(
                    report, priority=Priority.BLOCKER, handler=DAEMON_NAME
                )
        return

    job = _activity.started(f"routing task {entity_id} → {skill}: {title[:60]}")

    # Lifecycle: the dispatcher resolved an owner — record ROUTED so the task can
    # never read "pending" while it is actually in flight.
    set_task_status(
        entity_id, TaskStatus.ROUTED, handler=DAEMON_NAME,
        from_status=current_status, key_suffix=trigger,
    )

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
                entity_id, TaskStatus.AWAITING_INPUT, handler=DAEMON_NAME,
                from_status=TaskStatus.ROUTED.value,
                reason=f"readiness {assessment.score:.2f}<{assessment.threshold:.2f}: "
                       f"missing {', '.join(assessment.missing)}",
                key_suffix=trigger,
            )
            if RUN_EMAIL:
                send_run_email(
                    task_id=entity_id, run_key=f"{trigger}-readiness",
                    stage="kickoff", title=title, body=ask,
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
        confidence = _read_confidence(snapshot)
        decision = evaluate_gate(
            confidence=confidence,
            action_type=action_type,
            policy=policy,
            successful_recurrences=_successful_recurrences(snapshot),
        )
        log.info(
            f"[{DAEMON_NAME}] gate: task={entity_id} → {skill} "
            f"action={action_type} blast={decision.blast_radius.value} "
            f"conf={confidence:.2f}/{decision.threshold:.2f} "
            f"→ {decision.action.value} ({decision.reason})"
        )
        if decision.action != GateAction.AUTO_EXECUTE:
            brief_id = write_checkpoint_brief(
                task_entity_id=entity_id,
                decision=decision,
                title=title,
                plan_summary=(
                    f"Assigned to {skill}. Action: {action_type or 'unknown'}. "
                    f"Trigger: {trigger}. {decision.reason}."
                ),
                handler=DAEMON_NAME,
                alternatives=(
                    ["Re-scope to a lower-blast action", "Provide missing inputs", "Decline"]
                    if decision.action == GateAction.CHECKPOINT_WITH_ALTERNATIVES
                    else None
                ),
            )
            notifier.send(
                f"PLAN checkpoint: {title[:70]}\n"
                f"  agent={skill} blast={decision.blast_radius.value} "
                f"conf={confidence:.2f} — {decision.reason}\n"
                f"  task={entity_id} brief={brief_id or '(unpersisted)'}",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
            log.info(
                f"[{DAEMON_NAME}] HELD task {entity_id} for operator approval "
                f"(checkpoint_brief={brief_id})"
            )
            set_task_status(
                entity_id, TaskStatus.AWAITING_APPROVAL, handler=DAEMON_NAME,
                from_status=TaskStatus.ROUTED.value, reason=decision.reason,
                key_suffix=trigger,
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
        job.finished(f"task {entity_id} → {skill} routed (dry-run, gate: {_gate_label})")
        return

    # Lifecycle: about to spawn the T4 agent.
    set_task_status(
        entity_id, TaskStatus.EXECUTING, handler=DAEMON_NAME,
        from_status=TaskStatus.ROUTED.value, key_suffix=trigger,
    )

    # E1/E2: this run's thread. run_key keys it to the attempt so SSE replays reuse
    # it while a genuine retry opens a fresh run.
    run_key = f"{trigger}-{snapshot.get('attempt', snapshot.get('attempt_count', 0))}"

    # E1: open one conversation for this execution run (flag-gated, fail-open).
    run_conversation_id: str | None = None
    if RUN_CONVERSATIONS:
        run_conversation_id = create_run_conversation(
            task_id=entity_id,
            plan_id=snapshot.get("plan_id") or None,
            agent=skill,
            run_key=run_key,
            title=f"{skill} run · {title[:60]}",
        )
        if run_conversation_id:
            log.info(
                f"[{DAEMON_NAME}] run conversation {run_conversation_id} opened "
                f"for task {entity_id} (run={run_key})"
            )

    def _run_stage(role: str, content: str, stage: str) -> None:
        """Record one run-thread event: append to the run conversation (E1) AND
        send it on the run's Gmail thread (E2). Both flag-gated + fail-open. Apis
        OWNS the run thread, so it is populated regardless of whether the spawned
        agent finalizes into it; the agent's own /end (advised via prompt) layers
        on top and is not relied upon for binding.
        """
        if run_conversation_id:
            append_turn(
                conversation_id=run_conversation_id, role=role, content=content,
                sender_kind="orchestrator",
                idempotency_key=f"runturn-{entity_id}-{stage}-{trigger}",
            )
        if RUN_EMAIL:
            send_run_email(
                task_id=entity_id, run_key=run_key, stage=stage, title=title,
                body=content,
            )

    _run_stage("user",
               f"Dispatched {skill} for task {entity_id} (trigger={trigger}): {title}",
               stage="kickoff")

    try:
        result = await _spawn_harness_skill(
            skill, entity_id, snapshot, trigger, notifier, role=role,
            run_conversation_id=run_conversation_id,
        )
    except Exception as exc:
        # Unexpected crash in the spawn machinery itself → record as a failed run.
        _run_stage("assistant", f"{skill} dispatch crashed: {type(exc).__name__}: {exc}",
                   stage="crash")
        set_task_status(
            entity_id, TaskStatus.FAILED, handler=DAEMON_NAME,
            from_status=TaskStatus.EXECUTING.value,
            reason=f"dispatch raised {type(exc).__name__}: {exc}",
            key_suffix=trigger,
        )
        job.failed(f"task {entity_id} → {skill} dispatch failed: {type(exc).__name__}")
        raise

    if result.ok:
        _run_stage("assistant", f"{skill} completed (trigger={trigger}).",
                   stage="done")
        set_task_status(
            entity_id, TaskStatus.DONE, handler=DAEMON_NAME,
            from_status=TaskStatus.EXECUTING.value,
            result=f"{skill} completed (trigger={trigger})",
            key_suffix=trigger,
        )
        job.finished(f"task {entity_id} dispatched → {skill} (gate: {_gate_label})")
    else:
        reason = result.error or f"rc={result.returncode}"
        _run_stage("assistant", f"{skill} failed (trigger={trigger}): {reason}",
                   stage="failed")
        # FAILED (not BLOCKED): the stall watchdog (plan task ent_3cdd75…) owns
        # retry-with-backoff and escalation-on-exhaustion out-of-band, so the SSE
        # loop is never blocked by an inline sleep. Notify now so failures are not
        # silent in the interim before the watchdog ships.
        set_task_status(
            entity_id, TaskStatus.FAILED, handler=DAEMON_NAME,
            from_status=TaskStatus.EXECUTING.value, reason=reason,
            key_suffix=trigger,
        )
        notifier.send(
            f"{skill} failed on {entity_id} ({reason}) — task marked FAILED",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
        job.failed(f"task {entity_id} → {skill} failed: {reason[:60]}")


# ── Checkpoint resolution ───────────────────────────────────────────────────


async def handle_checkpoint_brief(
    entity_id: str, snapshot: dict, notifier: Notifier
) -> None:
    """
    React to a checkpoint_brief the gate raised once the operator resolves it.

    approved → re-dispatch the referenced task with the gate bypassed (the
               operator IS the approval the gate was waiting for).
    rejected → mark the task declined; do not execute.
    pending/unknown → no-op (waiting on the operator).

    Idempotency: after acting, the brief is stamped resolved_dispatched=true; a
    replayed approved/rejected event whose brief carries that stamp is a no-op.
    Re-dispatch is also safe because the task skill owns its own idempotency, but
    the stamp avoids spawning the work twice on SSE redelivery.
    """
    resolution = read_checkpoint_resolution(snapshot)
    if resolution is None:
        log.info(
            f"[{DAEMON_NAME}] checkpoint_brief {entity_id} still pending "
            f"(status={snapshot.get('status')!r}) — no action"
        )
        return

    if checkpoint_already_dispatched(snapshot):
        log.info(
            f"[{DAEMON_NAME}] checkpoint_brief {entity_id} already dispatched "
            f"(resolution={resolution}) — no-op on replay"
        )
        return

    task_id = snapshot.get("task_entity_id")
    if not task_id:
        log.warning(
            f"[{DAEMON_NAME}] checkpoint_brief {entity_id} {resolution} but has no "
            "task_entity_id — cannot act"
        )
        return

    title = snapshot.get("title", "(untitled)")

    if resolution == "rejected":
        mark_task_declined(
            task_id, reason=f"operator rejected checkpoint {entity_id}", handler=DAEMON_NAME
        )
        stamp_checkpoint_dispatched(entity_id, handler=DAEMON_NAME)
        notifier.send(
            f"Checkpoint rejected: {title[:70]}\n  task={task_id} declined",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        return

    # approved → re-dispatch with the gate bypassed
    task_snapshot = fetch_task_snapshot(task_id)
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
        return

    log.info(
        f"[{DAEMON_NAME}] checkpoint {entity_id} APPROVED — re-dispatching task "
        f"{task_id} with gate override"
    )
    notifier.send(
        f"Checkpoint approved: {title[:70]}\n  re-dispatching task {task_id}",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )
    # Stamp before dispatch so an SSE replay can't double-spawn the work; the task
    # skill's own idempotency covers the rare stamp-succeeded-then-dispatch-crashed case.
    stamp_checkpoint_dispatched(entity_id, handler=DAEMON_NAME)
    await dispatch_task(
        task_id, task_snapshot, trigger="approved", notifier=notifier, gate_override=True
    )


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
            entity_id, snapshot, trigger="created", notifier=notifier,
            snapshot_hydrated=event.hydrated,
        )

    elif action == "updated":
        # Tasks dispatch on creation (and on due_today when AUTO_EXECUTE is set);
        # status transitions are logged for observability only to avoid
        # re-dispatching work already routed at creation.
        if status in ("approved", "ready"):
            log.info(
                f"[{DAEMON_NAME}] Task {entity_id} moved to status={status!r}"
            )
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
                entity_id, snapshot, trigger="due_today", notifier=notifier,
                snapshot_hydrated=event.hydrated,
            )
        else:
            log.info(
                f"[{DAEMON_NAME}] AUTO_EXECUTE off — operator notification sent for {entity_id}"
            )


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    log.info(f"[{DAEMON_NAME}] Starting up...")
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
        f"grace={_reconcile_cfg.GRACE_SECONDS}s)"
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

        # Same pass, separate concern: reap pipeline markers orphaned on CLOSED
        # issues. `_clear_pipeline_inflight` is best-effort, so a GitHub blip or
        # a kill between the last agent and the clear leaves one behind, and the
        # resume sweep above only scans OPEN issues — so nothing ever reclaimed
        # them. GitHub renders a marker-only comment as "No description
        # provided.", so each orphan is a blank swarm comment on the thread
        # forever. Kept out of `resume_interrupted_pipelines`' return value: it
        # is housekeeping, not a resume outcome.
        try:
            cleared = await dispatcher._clear_closed_issue_markers(
                list(dispatcher.config.resume_repositories)
            )
            if cleared:
                log.info(
                    f"[{DAEMON_NAME}] cleared {cleared} stale pipeline "
                    "marker(s) on closed issues"
                )
        except Exception as exc:  # housekeeping must never kill startup
            log.error(
                f"[{DAEMON_NAME}] stale-marker sweep failed: {exc}",
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

    # 8. Deferred-review resume sweep: a PR review throttled by a usage limit
    #    posts a `review-deferred-until:<ISO>` marker instead of a verdict. The
    #    reset is often hours out, so unlike the one-shot pipeline resume this
    #    must run PERIODICALLY — the daemon may never restart inside the window.
    #    Each pass re-dispatches PRs whose deferral has matured; if the limit is
    #    still active, the re-run just posts a fresh (later) deferral marker.
    #    Fire-and-forget and fail-open — a broken sweep must never stop the loop.
    deferred_interval = int(
        os.environ.get("APIS_DEFERRED_REVIEW_SWEEP_SECONDS", "600")
    )

    async def deferred_review_sweep() -> None:
        while True:
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
                    notifier.send(
                        unread, priority=Priority.WARN, handler=DAEMON_NAME
                    )
            except Exception as exc:  # noqa: BLE001 — never kill the daemon
                log.warning(f"[{DAEMON_NAME}] unroutable flush failed: {exc}")

    log.info(f"[{DAEMON_NAME}] Subscribing to SSE: {SUBSCRIBE_ENTITY_TYPES}")
    await asyncio.gather(
        sse.stream(dispatch),
        github_gateway.serve(gateway_app, GITHUB_WEBHOOK_PORT),
        watchdog.run(notifier, watchdog_dispatch),
        reconciler.run(reconcile_dispatch),
        resume_sweep(),
        deferred_review_sweep(),
        workflow_drift_check(),
        unroutable_flush(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")

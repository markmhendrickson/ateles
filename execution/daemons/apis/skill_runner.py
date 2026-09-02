"""
execution/daemons/apis/skill_runner.py — spawn a T4 agent via a bundled-plan CLI.

Single implementation of the spawn pattern previously inlined in apis.py. A
quota-aware router selects among Claude Code, Codex, and Cursor Agent, all using
the operator's subscription login; usage-based API credentials are removed from
the child environment unless the operator explicitly enables metered fallback.
The GitHub trigger pipelines (swarm_dispatch.py) reuse this implementation and
capture agent output for the review learning loop.

Stage 1 (ateles#94): loads the dispatched role's agent_definition from Neotoma
so the spawned subprocess gets the role's canonical system prompt prepended to
SKILL.md, and (when the definition specifies a restricted tool_allowlist) passes
--allowed-tools to confine the subprocess.

Stage 2 (ateles#94): writes a harness_event to Neotoma at dispatch start,
completion, and failure.

Stage 5 (ateles#94): when no agent_definition loads (empty prompt_markdown),
emits a notifier WARN and a harness_event with the degraded_generic_subagent
marker so degraded dispatches are observable. Dispatch still proceeds.

ateles#257: on a FAILED dispatch the complete child stdout AND stderr are
persisted to a per-dispatch file under ``~/Library/Logs/ateles/dispatch-failures/``
and that path is echoed into both the ERROR log line and the harness_event
``output_summary``, so a failure is never again unreconstructable from a
truncated slice. Failed dispatches also raise a rate-limited operator
notification, so a swarm-wide breakage produces a signal instead of silence.

Failures never raise — callers get a SkillResult and decide how to degrade;
one bad dispatch must not take down the daemon.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ── Path bootstrap (mirrors apis.py so this module is importable standalone) ──
_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.daemon_runtime import AgentDefinition, AgentLoader  # noqa: E402
from dispatch_usage import DispatchUsage, parse_dispatch_usage  # noqa: E402
from harness_router import (  # noqa: E402
    configured_providers,
    cool_down,
    cooling_providers,
    provider_candidates,
)

# Cloudflare fronts the hosted Neotoma instance and blocks urllib's default
# User-Agent with a 1010 "browser signature" 403. Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"

log = logging.getLogger("apis.skill_runner")

CLAUDE_BIN = os.environ.get("APIS_CLAUDE_BIN") or shutil.which("claude")
_CODEX_APP_BIN = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
CODEX_BIN = (
    os.environ.get("APIS_CODEX_BIN")
    or (str(_CODEX_APP_BIN) if _CODEX_APP_BIN.is_file() else None)
    or shutil.which("codex")
)
CURSOR_BIN = os.environ.get("APIS_CURSOR_BIN") or shutil.which("cursor-agent")
DISPATCH_TIMEOUT_SECONDS = int(os.environ.get("APIS_DISPATCH_TIMEOUT", "1800"))
ATELES_REPO = Path(
    os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles"))
)

# ── Dropped-allowlist-rule detection (ateles#255) ──────────────────────────────
# The CLI silently continues past a rejected `--allowedTools` rule, logging a
# single stderr line per dropped rule instead of failing the dispatch. Before
# this, that line was only visible in ~/Library/Logs/ateles/apis.log — a
# swarm-wide breakage (see issue #255) went unnoticed for a week because
# nothing surfaced it to the operator. This regex + helper turn every dropped
# rule into one batched notifier alert per dispatch (never one alert per rule,
# to avoid paging noise on a single bad grant).
#
# The CLI line-wraps this message (confirmed in the issue's own quoted repro):
#   "... dispatch failed (rc=1): Ignoring\n--allowedTools rule \"pr*\": ..."
# — a newline, not a space, separates "Ignoring" and "--allowedTools". `\s+`
# (DOTALL not needed; \s already matches \n) tolerates that wrap so the
# detector actually matches real CLI output, not just a single-line fixture.
_DROPPED_ALLOWLIST_RULE_RE = re.compile(r'Ignoring\s+--allowedTools rule "([^"]*)"')


def _require_neotoma_base_url() -> str:
    """Return NEOTOMA_BASE_URL (trailing slash stripped) or raise.

    No localhost default by design — see the 2026-08-04 migration off local
    hosting; a fallback would silently target a dead port.
    """
    v = os.environ.get("NEOTOMA_BASE_URL", "").strip()
    if not v:
        raise RuntimeError(
            'NEOTOMA_BASE_URL is not set. It must point at the Neotoma instance (e.g. https://neotoma.markmhendrickson.com). Local hosting was retired 2026-08-04 and http://localhost:9180 no longer serves anything, so there is deliberately no default: a silent fallback would send writes at a dead port. Under launchd the plist supplies this; for an ad-hoc run, export it or source ~/.config/neotoma/.env first.'
        )
    return v.rstrip("/")


def _find_dropped_allowlist_rules(stderr: str) -> list[str]:
    """Return the distinct dropped-rule names named in a dispatch's stderr.

    Order-preserving, de-duplicated (the same rule can be logged more than
    once for a single dispatch). Empty input / no match -> empty list.
    """
    if not stderr:
        return []
    seen: dict[str, None] = {}
    for m in _DROPPED_ALLOWLIST_RULE_RE.finditer(stderr):
        seen.setdefault(m.group(1), None)
    return list(seen)


def _notify_dropped_allowlist_rules(
    notifier, *, role: str, rules: list[str], returncode: int | None
) -> None:
    """Send ONE batched notifier alert naming every dropped rule for this
    dispatch (never one alert per rule — see module docstring above)."""
    if not rules or notifier is None:
        return
    rule_list = ", ".join(f'"{r}"' for r in rules)
    msg = (
        f"Agent {role!r} dispatch had {len(rules)} --allowedTools rule(s) "
        f"silently dropped by the CLI: {rule_list} (rc={returncode}). "
        "The corresponding tool_allowlist grant(s) never reached the agent — "
        "fix the grant grammar in the agent_definition."
    )
    try:
        from lib.notify import Priority

        notifier.send(msg, priority=Priority.WARN, handler="apis")
    except Exception as exc:
        log.debug(f"[apis] dropped-allowlist-rule notifier.send failed: {exc}")


# ── Agent-definition cache ─────────────────────────────────────────────────────
# Per-role cache within the process lifetime. AgentLoader.load() makes a
# synchronous HTTP call to Neotoma; caching avoids refetching on every task
# dispatch for the same role.
_agent_def_cache: dict[str, AgentDefinition] = {}


def _load_agent_def(role: str) -> AgentDefinition:
    """Load (and cache) an AgentDefinition for the given role name."""
    if role not in _agent_def_cache:
        _agent_def_cache[role] = AgentLoader(role).load()
    return _agent_def_cache[role]


# ── Shared GitHub-interaction convention (Phase 1 / Layer A) ──────────────────
# Injected into every GitHub-dispatched agent's system prompt by build_system_prompt
# when include_github_contract=True.  Lives in ONE place — not duplicated across
# agent_definitions.  Complements (never contradicts) per-prompt format instructions
# already present in swarm_dispatch.py prompts.
#
# See docs/swarm_github_interaction_design.md — Layer A.

SWARM_GITHUB_CONTRACT = """\
## Swarm GitHub interaction contract (Layer A)

Every GitHub comment you post as part of the Ateles swarm MUST follow this convention.

### Attribution header — exact, verbatim form

Every comment MUST open with this header (bold, em-dash, literal "Ateles swarm,"):

```
**🤖 <AgentNameTitleCase> — Ateles swarm, <role-phrase>**
```

Rules — read carefully:

- **Agent name in Title Case** — e.g. `Pavo`, `Gryllus`, `Lanius`. Never lowercase.
- **Em-dash** (—, U+2014), not a hyphen (-) or double-hyphen (--).
- **"Ateles swarm,"** is literal — include the comma, no variations.
- **`<role-phrase>`** is YOUR fixed role label used consistently every time \
(e.g. "pm gate owner", "issue triage", "arch reviewer"). Do not vary it between comments.
- **Do NOT append** `· <repo>#<n>` or any issue/repo suffix to the header. \
The repository and issue context are already visible from where the comment lives; \
appending them caused the inconsistency observed in neotoma#1686 (Pavo posted two \
different header forms in the same thread). Drop that suffix entirely.

**Reproduce this header format EXACTLY on every comment — same capitalization, same \
em-dash, same "Ateles swarm," prefix. Do not add repository/issue suffixes or restyle it.**

Per ateles#109: when posting under your own dedicated provisioned account (avatar is \
attribution), the header MAY be omitted. When included, it MUST be the exact form above.

### Verdict line — exact, verbatim form

Immediately after the attribution header, on its own line:

```
**<VERDICT>**
```

### Verdict vocabulary

Use exactly ONE of these tokens as the bold status line — one per comment, always present:

- `**APPROVE**` — all checks pass, no blockers.
- `**REQUEST_CHANGES**` — one or more [BLOCKING] findings; the author must address them.
- `**COMMENT**` — observations only; nothing blocks merge.
- `**BLOCKED**` — cannot proceed (missing information, open pre-impl gate, etc.).
- `**SIGNED_OFF**` — your gate/phase is signed off.

### Worked example — reproduce this pattern exactly

```
**🤖 Pavo — Ateles swarm, pm gate owner**
**APPROVE**

- [x] Acceptance criteria met
- [x] No open blockers

PM gate signed off. Ready to merge.

---
📎 Neotoma: [neotoma#1686](https://neotoma.markmhendrickson.com/entities/ent_abc123)
```

### Checklists

All definition-of-done checklists use GitHub task-list syntax:

```
- [ ] Not yet verified
- [x] Confirmed satisfied
```

### Blocking markers

Prefix each finding with its severity so the aggregator and humans can parse uniformly:

```
[BLOCKING] <category>: <summary>
[NON-BLOCKING] <category>: <summary>
```

### Cite standing rules

When a finding rests on a guardrail, decision, or doc, say so explicitly — that marks \
it as systemic, not opinion. Link the Neotoma record when it is publicly readable (see \
Neotoma backlinks below).

### Edit, don't duplicate

Update your prior comment in place rather than posting a new one when you are revisiting \
the same issue or PR. Use `gh api -X PATCH repos/<owner>/<repo>/issues/comments/<id> \
-f body='...'` to edit.

### Neotoma backlinks

Every comment that references or is sourced by canonical Neotoma data MUST link the \
relevant record(s) in a footer line:

```
📎 Neotoma: <label> · <label>
```

Using the URL form: `https://neotoma.markmhendrickson.com/entities/<id>`

**Visibility rule**: link only entity records whose schema allows public read \
(`guest_access_policy: read_only`). Until the Phase 3a-0 policy change ships, only \
`issue` entities are known to be guest-readable; link those. For all other entity types \
(harness_event, plan_contribution, gate_status, etc.) that are not yet public, reference \
the entity id in prose — e.g. "see harness_event `ent_abc123`" — WITHOUT a bare URL \
that would 401 for public readers. Once Phase 3a-0 sets `read_only` on the \
public-orchestration types, the full link form applies to all of them.

### Brevity

Keep comments checklist/structured. Avoid essay-style prose. The implementer and \
aggregator (Vanellus) parse these; treat them as structured data with a human-readable \
summary, not a narrative.\
"""

# ── Prior-art contract (check existing context before building) ───────────────
# Injected into every dispatched agent's system prompt by build_system_prompt,
# alongside SWARM_GITHUB_CONTRACT.  Lives in ONE place, exactly like that
# contract — the point is that no brief author has to remember to ask for it.
#
# Motivated by two wasted runs on 2026-09-01: an agent was dispatched to build
# provider load balancing that already existed in harness_router.py
# (provider_candidates), and another was sent after neotoma#2279 — an issue
# filed against a clone 139 commits behind main, describing problems already
# fixed — and spent its entire run refuting its own brief.  On the same day the
# check paid off twice where a brief happened to request it: workflow_definition
# and participation_record already existed (so no new entity types were needed),
# and the non-blocking DB worker pool had shipped in July and merely was not
# selected (so the fix was one config line rather than building a pool).
#
# Scoped deliberately to three checks with observed payoff rather than a general
# "be careful" instruction, because a check that fires noise on every dispatch
# gets ignored — which is the same failure as having no check at all.

SWARM_PRIOR_ART_CONTRACT = """\
## Prior-art contract — check before you build

This codebase's dominant failure mode is **correct code that nothing invokes**.
Several mechanisms here are fully written and tested but wired into nothing, so
work that looks unbuilt is often built-but-unreferenced. Assume the thing you
were asked to build may already exist until you have checked.

Before you write code or open a PR, run these three checks. They are fast, and
each one has caught a wasted run in this swarm.

1. **Existing issues and PRs** — is this already filed, in flight, or fixed?
   `gh issue list --search '<terms>' --state all` and
   `gh pr list --search '<terms>' --state all` in the repo you were pointed at.
   An issue can also be *stale*: check whether the clone or branch it describes
   is behind main before you trust its problem statement.

2. **The codebase** — does this mechanism already exist, perhaps unwired?
   Grep for the capability, not just the name you were given: the existing
   implementation almost certainly uses different vocabulary than your brief.
   Search for the function it would perform and the config it would read. If you
   find it, check whether anything *calls* it — an unwired implementation needs
   connecting, not rebuilding.

3. **Existing tasks and plans** — is another agent already on this? Many agents
   run concurrently with overlapping scopes. Check open Neotoma `task` entities
   and the relevant `plan` before starting.

**Report what you found, at the top of your output, before your work.** One or
two lines: what you searched, and whether anything already covers this. If the
checks turned up prior art, say what it is and how it changed your approach.
If they turned up nothing, say that — a stated negative result is what makes
this contract auditable.

**When the brief's premise is wrong, say so and stop.** If the thing already
exists, or the issue describes a problem already fixed, that finding IS the
deliverable. Report it and do not build the duplicate. Correcting a brief is a
successful outcome, not a failed one — do not treat "I was told to build it" as
a reason to build something the repository already has.\
"""


# ── System-prompt assembly ─────────────────────────────────────────────────────


def build_system_prompt(
    agent_def: AgentDefinition,
    skill_md: str,
    include_github_contract: bool = False,
) -> tuple[str, bool]:
    """
    Build the composite system prompt for a role dispatch.

    Returns (prompt, degraded) where degraded=True means the agent_definition
    did not contribute (empty prompt_markdown) and the subprocess will run with
    SKILL.md alone.

    The agent_definition's canonical instructions come FIRST so they establish
    identity, permissions, and behavioral constraints before the per-task skill
    instructions. Separated by a clear boundary so the model can parse both layers.

    When include_github_contract=True, SWARM_GITHUB_CONTRACT is inserted between
    the definition prompt and the skill_md so all GitHub-dispatched agents receive
    the shared comment convention in ONE place.  The contract is injected even in
    degraded mode (no definition_prompt) because it is useful guidance regardless.
    When include_github_contract=False (the default), behaviour is byte-identical
    to the pre-contract implementation — the SSE/non-GitHub task path is unchanged.

    SWARM_PRIOR_ART_CONTRACT is injected on the SAME condition, immediately after
    the GitHub contract.  It is deliberately not given its own flag: the whole
    point is that no brief author has to remember to ask for a prior-art check,
    and a second flag would just relocate the forgetting.  It is injected in
    degraded mode too, for the same reason the GitHub contract is — checking for
    existing work is useful regardless of which definition loaded.
    """
    contracts = (
        f"{SWARM_GITHUB_CONTRACT}\n\n---\n\n{SWARM_PRIOR_ART_CONTRACT}"
        if include_github_contract
        else ""
    )
    definition_prompt = (agent_def.prompt_markdown or "").strip()
    if definition_prompt:
        if include_github_contract:
            return (
                f"{definition_prompt}\n\n"
                "---\n\n"
                f"{contracts}\n\n"
                "---\n\n"
                f"{skill_md}",
                False,
            )
        return (
            f"{definition_prompt}\n\n---\n\n{skill_md}",
            False,
        )
    # Degraded: no definition_prompt.
    if include_github_contract:
        return f"{contracts}\n\n---\n\n{skill_md}", True
    return skill_md, True


# ── Neotoma harness_event writer ───────────────────────────────────────────────


def _write_harness_event(
    *,
    task_entity_id: str,
    role: str,
    agent_sub: str,
    event_type: str,
    tool_name: str,
    success: str,
    input_summary: str = "",
    output_summary: str = "",
    duration_ms: int | None = None,
    usage: "DispatchUsage | None" = None,
) -> None:
    """
    Best-effort write of a harness_event entity to Neotoma.

    Uses the same /store endpoint and pattern as lib/activity/_store_activity_log.
    Never raises — a harness_event failure must not crash dispatch.

    ``usage`` carries per-dispatch model/provider/token attribution (see
    dispatch_usage.py). Its fields are merged in only when actually reported —
    a harness that reports nothing adds no keys, so an absent field reads as
    "not reported" rather than as a measured zero.
    """
    base_url = _require_neotoma_base_url()
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        return

    event_at = datetime.now(timezone.utc).isoformat()
    # canonical_name_fields per schema: session_id, event_type, event_at
    # session_id is not available at dispatch time; use role+task+event_at as a
    # stable-enough dedup key without it.
    idempotency_key = f"harness-event-{role}-{task_entity_id}-{event_type}-{event_at}"

    entity: dict = {
        "entity_type": "harness_event",
        "event_type": event_type,
        "event_at": event_at,
        "tool_name": tool_name,
        "agent_sub": agent_sub,
        "success": success,
        "task_entity_id": task_entity_id,
    }
    if input_summary:
        entity["input_summary"] = input_summary[:500]
    if output_summary:
        entity["output_summary"] = output_summary[:500]
    if duration_ms is not None:
        entity["duration_ms"] = duration_ms
    if usage is not None:
        # Additive: only fields the harness actually reported. If the
        # harness_event schema has not yet declared these, Neotoma accepts the
        # write and drops the undeclared keys — the pre-existing fields still
        # land, so this can never regress what was already recorded.
        entity.update(usage.as_event_fields())

    payload = {
        "idempotency_key": idempotency_key,
        "observation_source": "workflow_state",
        "entities": [entity],
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url}/store",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    req.add_header("User-Agent", NEOTOMA_USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=5.0):
            pass
    except Exception as exc:
        log.debug(f"[apis] harness_event write failed (non-fatal): {exc}")


# ── Dispatch-failure diagnostics (ateles#257) ──────────────────────────────────
#
# On a failed dispatch the ONLY durable evidence used to be `stderr[:500]` in a
# log line and `stderr[:200]` in a harness_event — stdout was dropped entirely.
# That is why ateles#256's rc=1 root cause is unrecoverable: the leading bytes
# of stderr were an incidental `--allowedTools` warning and the real error sat
# past the cut, or in stdout.
#
# We now write the COMPLETE stdout+stderr of every failed dispatch to a
# per-dispatch file and put its path everywhere the failure surfaces.
# Everything here is best-effort: a diagnostics failure must NEVER break
# dispatch.

DISPATCH_FAILURE_LOG_DIR = Path(
    os.environ.get(
        "ATELES_DISPATCH_FAILURE_LOG_DIR",
        str(Path.home() / "Library" / "Logs" / "ateles" / "dispatch-failures"),
    )
)

# Rate-limit window for operator notifications about dispatch failures. A
# swarm-wide breakage should produce a signal, not 200 of them: identical
# (skill, returncode, stderr-shape) failures notify at most once per window.
DISPATCH_FAILURE_NOTIFY_WINDOW_SECONDS = int(
    os.environ.get("ATELES_DISPATCH_FAILURE_NOTIFY_WINDOW", "3600")
)

# signature -> monotonic seconds of last notification. Process-local; the daemon
# is long-lived, so this is the right lifetime for burst suppression.
_dispatch_failure_notified_at: dict[str, float] = {}

# Env vars whose values must never land in a diagnostics file, in case a failing
# child echoes its own environment or argv.
_REDACTED_ENV_VARS = (
    "NEOTOMA_BEARER_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
)

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str, *, limit: int = 60) -> str:
    """
    Filesystem-safe slug for one path component. Never raises.

    Dots are collapsed so no ``..`` survives: ``skill`` reaches this from a
    caller-supplied name, and the result is joined onto a directory path.
    """
    cleaned = _SLUG_RE.sub("-", (value or "").strip())
    cleaned = re.sub(r"\.+", ".", cleaned).strip("-.")
    return (cleaned or "unknown")[:limit]


def _redact_secrets(text: str) -> str:
    """Replace any known secret value appearing in child output."""
    out = text
    for var in _REDACTED_ENV_VARS:
        secret = os.environ.get(var, "")
        if secret and len(secret) >= 8:
            out = out.replace(secret, f"<redacted:{var}>")
    return out


def _summarize_command(cmd: list[str]) -> str:
    """
    Render the dispatch command for the diagnostics header.

    The ``--append-system-prompt`` payload is multi-KB and not diagnostic, so it
    is elided by length rather than inlined.
    """
    parts: list[str] = []
    elide_next = False
    for arg in cmd:
        if elide_next:
            parts.append(f"<{len(arg)}B system-prompt elided>")
            elide_next = False
            continue
        parts.append(arg)
        if arg == "--append-system-prompt":
            elide_next = True
    return _redact_secrets(" ".join(parts))


def write_dispatch_failure_log(
    *,
    skill: str,
    role: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
    task_entity_id: str = "",
    cmd: list[str] | None = None,
    cwd: str | None = None,
    duration_ms: int | None = None,
) -> str:
    """
    Persist the COMPLETE stdout and stderr of a failed dispatch to a file.

    Returns the absolute path written, or ``""`` when the write could not be
    made. NEVER raises — diagnostics must not be able to break a dispatch.

    The file is written mode-0600: child output can contain repository content
    and, in pathological cases, tokens echoed by a failing tool.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        DISPATCH_FAILURE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = DISPATCH_FAILURE_LOG_DIR / f"{_slug(skill)}-{ts}.log"

        header = [
            "# Ateles dispatch failure (ateles#257)",
            f"written_at: {datetime.now(timezone.utc).isoformat()}",
            f"skill: {skill}",
            f"role: {role}",
            f"returncode: {returncode}",
            f"task_entity_id: {task_entity_id or '(none)'}",
            f"cwd: {cwd or '(daemon default)'}",
            f"duration_ms: {duration_ms if duration_ms is not None else '(unknown)'}",
            f"stdout_bytes: {len(stdout)}",
            f"stderr_bytes: {len(stderr)}",
        ]
        if cmd:
            header.append(f"command: {_summarize_command(list(cmd))}")

        body = (
            "\n".join(header)
            + "\n\n===== STDOUT (complete) =====\n"
            + _redact_secrets(stdout)
            + "\n===== END STDOUT =====\n"
            + "\n===== STDERR (complete) =====\n"
            + _redact_secrets(stderr)
            + "\n===== END STDERR =====\n"
        )
        path.write_text(body, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return str(path)
    except Exception as exc:  # noqa: BLE001 — diagnostics must never break dispatch
        log.warning(f"[apis] could not write dispatch-failure log (non-fatal): {exc}")
        return ""


def _failure_signature(skill: str, returncode: int | None, stderr: str) -> str:
    """
    Stable dedup key for a dispatch failure.

    Keys on (skill, returncode, hash of the stderr *shape*) so a burst of the
    SAME systemic breakage collapses to one notification while a genuinely
    different failure still gets through. Long hex runs and digits are
    normalized out so per-run ids and timestamps don't defeat the dedup.
    """
    normalized = re.sub(r"[0-9a-f]{6,}", "<hex>", (stderr or "")[-2000:])
    normalized = re.sub(r"\d+", "<n>", normalized)
    digest = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{skill}:{returncode}:{digest}"


def _should_notify_dispatch_failure(
    signature: str, *, now: float | None = None
) -> bool:
    """
    True when this failure signature has not notified within the rate-limit
    window. Records the notification time as a side effect when it returns True.
    """
    current = time.monotonic() if now is None else now
    last = _dispatch_failure_notified_at.get(signature)
    if last is not None and (current - last) < DISPATCH_FAILURE_NOTIFY_WINDOW_SECONDS:
        return False
    _dispatch_failure_notified_at[signature] = current
    return True


def notify_dispatch_failure(
    notifier,
    *,
    skill: str,
    role: str,
    returncode: int | None,
    stderr: str,
    task_entity_id: str = "",
    log_path: str = "",
) -> bool:
    """
    Send a rate-limited operator notification for a failed dispatch.

    Returns True when a notification was actually delivered to the notifier,
    False when suppressed by dedup, when no notifier was supplied, or when
    delivery raised. Never raises.

    Priority is BLOCKER per the rubric: a failed dispatch is work that did not
    happen and will not retry itself — it must reach the operator promptly
    rather than wait for a digest. Dedup is what keeps that from becoming spam.
    """
    if notifier is None:
        return False
    try:
        signature = _failure_signature(skill, returncode, stderr)
        if not _should_notify_dispatch_failure(signature):
            log.debug(
                f"[apis] dispatch-failure notification suppressed (dedup): {signature}"
            )
            return False

        preview = _redact_secrets(" ".join((stderr or "").split()))[:300]
        message = (
            f"Dispatch FAILED: {skill} (role {role}, rc={returncode}) "
            f"for task {task_entity_id or '(unknown)'}. "
            f"Full output: "
            f"{log_path or '(diagnostics file unavailable — see daemon log)'}"
        )
        if preview:
            message += f" — stderr head: {preview}"

        from lib.notify import Priority

        notifier.send(message, priority=Priority.BLOCKER, handler="apis")
        return True
    except Exception as exc:  # noqa: BLE001 — notification must never break dispatch
        log.debug(f"[apis] dispatch-failure notifier.send failed: {exc}")
        return False


# ── SkillResult ────────────────────────────────────────────────────────────────


@dataclass
class SkillResult:
    skill: str
    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    error: str = ""  # non-process failure: missing binary / SKILL.md / timeout
    provider: str = ""
    attempted_providers: tuple[str, ...] = ()
    # Per-dispatch model + token attribution (dispatch_usage.py). None when the
    # dispatch never reached a harness (missing binary, unreadable SKILL.md).
    usage: DispatchUsage | None = None


# ── Harness adapters + capacity detection ─────────────────────────────────────

_CAPACITY_FAILURE_SIGNATURES = (
    "usage limit",
    "rate limit reached",
    "rate_limit_error",
    "you've hit your limit",
    "you have hit your limit",
    "weekly limit",
    "session limit",
    "maximum usage",
    "quota exceeded",
    "out of requests",
    "no requests remaining",
    "resets at",
    "resets in",
)

_AUTH_FAILURE_SIGNATURES = (
    "invalid authentication credentials",
    "could not resolve authentication",
    "oauth token has expired",
    "authentication_error",
    "authentication required",
    "please run /login",
    "please run `claude auth login`",
    "please run 'agent login'",
    "invalid api key",
    "not logged in",
    "login required",
)

_METERED_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CURSOR_API_KEY",
)


def _provider_binaries() -> dict[str, str | None]:
    return {
        "claude": CLAUDE_BIN,
        "codex": CODEX_BIN,
        "cursor": CURSOR_BIN,
    }


def _provider_failure_kind(*texts: str) -> str | None:
    """Classify failures that are safe to retry on another harness."""
    blob = " ".join(text for text in texts if text).lower()
    if any(signature in blob for signature in _CAPACITY_FAILURE_SIGNATURES):
        return "capacity"
    if any(signature in blob for signature in _AUTH_FAILURE_SIGNATURES):
        return "auth"
    return None



# ── Codex sandbox: writable git roots + network (ateles#590) ──────────────────
# `codex exec --sandbox workspace-write` grants write access to the working
# directory, /tmp, and $TMPDIR — and nothing else. That is fine for a plain
# clone, whose entire `.git` lives inside the workdir. It is not fine for a
# LINKED WORKTREE, which is the layout this swarm mandates: the repo-isolation
# guard and ateles#572 both push every dispatch into its own worktree so
# concurrent agents cannot collide. In a linked worktree `.git` is a FILE
# pointing at `<main clone>/.git/worktrees/<name>`, and the object database is
# further out still, in `<main clone>/.git`. Both are outside the sandbox, so
# every git write is denied:
#
#   fatal: Unable to create '.../worktrees/<name>/index.lock': Operation not permitted
#   error: unable to create temporary file: Operation not permitted     (git add)
#
# Note that BOTH roots are required, and this was verified rather than assumed:
# granting only the per-worktree gitdir still fails at `git add`, because loose
# objects are written under the COMMON dir. Granting only the common dir leaves
# index.lock denied. So the adapter grants exactly the two directories git
# actually needs, and nothing more.
#
# Network is the second, independent cause: workspace-write denies it by
# default, so `git push` and `gh` cannot resolve github.com ("Could not resolve
# host"). It is re-enabled through the documented config key rather than by
# dropping to --sandbox danger-full-access, which would also surrender
# filesystem confinement everywhere on the operator's machine — a far larger
# grant than the delivery path needs.


def _git_roots_for_sandbox(cwd: str | None) -> list[str]:
    """The directories git must be able to write to for a commit to succeed.

    Returns the resolved gitdir and common-dir for ``cwd``, de-duplicated and
    excluding anything already inside ``cwd`` (a plain clone needs no extra
    grant — its ``.git`` is under the workdir and workspace-write covers it).

    Returns ``[]`` when ``cwd`` is not a git repository or git is unavailable.
    A dispatch into a non-repo is legitimate; it simply needs no git roots.
    """
    if not cwd:
        return []
    try:
        workdir = Path(cwd).resolve()
    except OSError:
        return []

    roots: list[str] = []
    for flag in ("--git-dir", "--git-common-dir"):
        try:
            out = subprocess.run(
                ["git", "-C", str(workdir), "rev-parse", flag],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if out.returncode != 0:
            return []
        raw = out.stdout.strip()
        if not raw:
            continue
        # `rev-parse` may answer with a path relative to cwd (".git").
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workdir / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        # Already covered by the workdir grant — do not widen the sandbox for
        # a path the sandbox already contains.
        if resolved == workdir or workdir in resolved.parents:
            continue
        as_str = str(resolved)
        if as_str not in roots:
            roots.append(as_str)
    return roots


# Signatures of a child that did real work and then could not deliver it.
# Each is a sandbox or network denial, not a code defect: the agent wrote
# correct output and the harness refused to let it out. Matching any of these
# turns a `returncode == 0` run into an explicit failure (ateles#590), because
# the alternative — the pre-fix behaviour — was `ok: true` over an empty
# delivery, the same class of lie as ateles#585 (envelope never written),
# ateles#566 (401 reported ok) and ateles#560 (grant_checker failing open).
_DELIVERY_DENIAL_SIGNATURES: tuple[tuple[str, str], ...] = (
    (
        r"(?:fatal|error): unable to create '[^']*index\.lock': "
        r"operation not permitted",
        "sandbox denied the git index lock — the child could not commit",
    ),
    (
        r"(?:fatal|error): unable to create temporary file: "
        r"operation not permitted",
        "sandbox denied writes to the git object store — the child could not commit",
    ),
    (
        r"fatal: unable to access '[^']*': could not resolve host: "
        r"(?:github\.com|api\.github\.com)",
        "sandbox denied network access — the child could not push or reach the GitHub API",
    ),
    (
        r"fatal: could not read from remote repository",
        "the child could not reach the git remote — nothing was pushed",
    ),
)


def _delivery_failure_reason(*texts: str) -> str | None:
    """Name the delivery denial in a child's output, if there is one.

    Read-only over the child's own words: no assumption is made about what the
    task was meant to deliver, so a task that never intended to commit is not
    penalised — it simply never emits these lines.
    """
    # Matched per LINE, anchored at the start, because that is where git emits
    # these — "fatal: ..." and "error: ..." begin a line. Searching a joined
    # blob matched the strings wherever they appeared, including quoted inside
    # ordinary prose: this PR's own body and diff both trip three of the four
    # signatures, so any agent dispatched to read them was reported as a failed
    # delivery (ateles#601 pm lens, reproduced). Anchoring is what separates
    # "git said this" from "someone wrote this down".
    for text in texts:
        if not text:
            continue
        for line in text.lower().splitlines():
            stripped = line.strip()
            for pattern, reason in _DELIVERY_DENIAL_SIGNATURES:
                if re.match(pattern, stripped):
                    return reason
    return None


def _provider_command(
    provider: str,
    binary: str,
    system_prompt: str,
    work_prompt: str,
    *,
    cwd: str | None,
    network: bool = False,
) -> tuple[list[str], bytes | None]:
    """Build one provider's noninteractive command and initial stdin payload.

    ``network`` opens the codex sandbox's network access for THIS dispatch only.
    #590 asks for it "without granting blanket network access to every
    dispatch", so it is off by default and the caller turns it on for the
    dispatches whose task actually involves GitHub delivery.
    """
    if provider == "claude":
        return [binary, "--print", "--append-system-prompt", system_prompt], None

    composite_prompt = (
        f"{system_prompt}\n\n"
        "---\n\n"
        "## Dispatched task\n\n"
        f"{work_prompt}"
    )
    if provider == "codex":
        # See the ateles#590 note above _git_roots_for_sandbox: without these
        # two additions a codex child in a linked worktree writes correct code
        # and then cannot commit, push, or open a PR.
        git_roots = _git_roots_for_sandbox(cwd)
        add_dir_flags: list[str] = []
        for root in git_roots:
            add_dir_flags += ["--add-dir", root]
        if git_roots:
            log.info(
                "[apis] codex sandbox: granting git roots %s", ", ".join(git_roots)
            )
        # Delivery needs github.com — but only a delivery-bearing dispatch does.
        # Scoped to the workspace-write policy rather than dropping the sandbox,
        # and to the dispatches that need it rather than to all of them (#590:
        # "without granting blanket network access to every dispatch").
        network_flags = (
            ["-c", "sandbox_workspace_write.network_access=true"] if network else []
        )
        if network:
            log.info("[apis] codex sandbox: network enabled for this dispatch")
        return (
            [
                binary,
                "exec",
                "--sandbox",
                "workspace-write",
                *network_flags,
                *add_dir_flags,
                "--ephemeral",
                "--skip-git-repo-check",
                "--color",
                "never",
                *(["--cd", cwd] if cwd else []),
                "-",
            ],
            composite_prompt.encode(),
        )
    if provider == "cursor":
        return (
            [
                binary,
                "--print",
                "--force",
                "--trust",
                "--approve-mcps",
                "--output-format",
                "text",
                *(["--workspace", cwd] if cwd else []),
                composite_prompt,
            ],
            None,
        )
    raise ValueError(f"unsupported harness provider: {provider}")


def _requested_model(provider: str, cmd: list[str]) -> str | None:
    """Return the model this dispatch ASKED for, read off the built command.

    Deliberately derived from the argv actually being executed rather than from
    a parameter, so it stays correct no matter which layer decides the model —
    today nothing passes one (every dispatch takes the provider's ambient
    default), and the per-model fallback work in ateles#667 adds `--model` in
    the command builder. Reading argv means this keeps reporting the truth
    across that change instead of silently going stale.

    A requested model is NOT evidence of the model that ran; callers mark it
    ``model_source="requested"``. Returns None when no model was pinned.
    """
    flags = {"--model", "-m"}
    for i, arg in enumerate(cmd or []):
        if arg in flags and i + 1 < len(cmd):
            value = (cmd[i + 1] or "").strip()
            return value or None
        # Support the `--model=x` spelling too.
        for flag in ("--model=",):
            if arg.startswith(flag):
                value = arg[len(flag):].strip()
                return value or None
    return None


def _subscription_only_env(
    env_extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a child env that cannot silently spill into metered API billing."""
    child = {**os.environ, **(env_extra or {})}
    if child.get("APIS_ALLOW_METERED_HARNESS", "0") != "1":
        for key in _METERED_CREDENTIALS:
            child.pop(key, None)
    elif child.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        # Even under the explicit override, prefer Max-plan OAuth for Claude.
        child.pop("ANTHROPIC_API_KEY", None)
    return child


# ── Single-provider runner ─────────────────────────────────────────────────────


async def _run_skill_once(
    skill: str,
    prompt: str,
    *,
    provider: str,
    role: str | None = None,
    task_entity_id: str = "",
    timeout: int | None = None,
    env_extra: dict[str, str] | None = None,
    notifier=None,  # lib.notify.Notifier | None — kept optional to avoid hard dep
    github_token: str | None = None,
    include_github_contract: bool = False,
    cwd: str | None = None,
) -> SkillResult:
    """
    Run one T4 agent to completion and return its output.

    Stage 1: loads the role's agent_definition (role defaults to skill when not
    passed — skill name == role name in this codebase). Prepends the definition's
    prompt_markdown to SKILL.md; applies the tool allowlist when restricted.

    Stage 2: writes harness_event entities to Neotoma at start, completion, and
    failure.

    Stage 5: when agent_definition carries empty prompt_markdown, logs a WARN,
    sends a notifier alert (when a notifier is supplied), and records a
    degraded_generic_subagent harness_event. Dispatch still proceeds.

    ``github_token`` (#109 — per-agent GitHub identity): when supplied, the token
    is injected into subprocess_env as both ``GITHUB_TOKEN`` and ``GH_TOKEN`` so
    the spawned agent's ``gh`` calls authenticate as the correct identity.  When
    not supplied, the child inherits the daemon's ambient env unchanged (current
    behaviour for all callers that predate #109).  Only GitHub-triggered pipeline
    call sites pass this; SSE task-path dispatches leave it unset.

    ``include_github_contract`` (Phase 1 / Layer A): when True, SWARM_GITHUB_CONTRACT
    is injected into the system prompt between the agent_definition and the SKILL.md.
    Pass True ONLY from GitHub-trigger call sites in swarm_dispatch.py; leave as
    False (the default) for all SSE/non-GitHub task dispatches so the contract never
    appears in payment, health, finance, or other non-GitHub work.

    Claude's `--allowed-tools` and injected Neotoma MCP config remain specific
    to the Claude adapter. Codex and Cursor receive the same system + skill
    instructions as a composite prompt and use their ambient configured tools.

    ``cwd`` (QE3 — eval-authoring affordance): when supplied, the dispatched
    child subprocess runs with this working directory instead of inheriting the
    daemon's. This is how the qa lens (Phoenicurus) is given a writable checkout
    of a PR branch so it can author an eval fixture, run ``eval:tier1``, commit,
    and push. When None (every call site that predates QE3), the child inherits
    the daemon's directory unchanged — exact current behaviour, no regression.
    """
    _role = (role or skill).lower()
    timeout = timeout or DISPATCH_TIMEOUT_SECONDS

    # ── Load agent_definition (Stage 1) ───────────────────────────────────────
    agent_def = await asyncio.to_thread(_load_agent_def, _role)

    binary = _provider_binaries().get(provider)
    if binary is None:
        msg = (
            f"{provider} binary unavailable "
            f"(APIS_{provider.upper()}_BIN unset, not on PATH)"
        )
        log.warning(f"[apis] {skill} dispatch skipped — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg, provider=provider)

    skill_path = ATELES_REPO / ".claude" / "skills" / skill / "SKILL.md"
    if not skill_path.exists():
        msg = f"SKILL.md not found at {skill_path}"
        log.error(f"[apis] {skill} dispatch skipped — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg, provider=provider)

    try:
        skill_md = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        return SkillResult(
            skill,
            False,
            None,
            "",
            "",
            error=f"read failed: {exc}",
            provider=provider,
        )

    # ── Build system prompt (Stage 1 + Stage 5) ────────────────────────────────
    system_prompt, degraded = build_system_prompt(
        agent_def, skill_md, include_github_contract=include_github_contract
    )

    if degraded:
        _title_hint = prompt[:80].replace("\n", " ")
        warn_msg = (
            f"Role {_role!r} ran DEGRADED (no agent_definition loaded) "
            f"for task {task_entity_id or '(unknown)'!r}. "
            "Dispatching with SKILL.md only."
        )
        log.warning(f"[apis] {warn_msg}")
        # A role with no agent_definition is ONE fact about that ROLE, not one
        # per task that happens to route to it. Notifying per task paged the
        # operator N times for a single missing definition; dedup on the role so
        # it escalates once (and again after the ledger's re-assert window, so a
        # role that stays undefined does not go quiet forever).
        _should_notify = True
        try:
            from unroutable_ledger import shared_ledger

            # The SHARED instance — never a second UnroutableLedger() on the same
            # file. Two instances each save their own stale view and drop each
            # other's records (Loxia review, ateles#656).
            _should_notify = shared_ledger().note_undefined_role(str(_role))
        except Exception as exc:  # noqa: BLE001 — dedup must never block the warning
            log.debug(f"[apis] undefined-role dedup unavailable: {exc}")
        if notifier is not None and _should_notify:
            try:
                from lib.notify import Priority

                notifier.send(
                    f"Role {_role!r} has no agent_definition — every task routed "
                    "to it runs DEGRADED (SKILL.md only). Reported once per role.",
                    priority=Priority.WARN,
                    handler="apis",
                )
            except Exception as exc:
                log.debug(f"[apis] notifier.send failed: {exc}")

        # Stage 5: degraded harness_event
        try:
            await asyncio.to_thread(
                _write_harness_event,
                task_entity_id=task_entity_id,
                role=_role,
                agent_sub=agent_def.aauth_sub,
                event_type="subprocess",
                tool_name=f"{provider}:{skill}",
                success="partial",
                input_summary=_title_hint,
                output_summary="degraded_generic_subagent",
            )
        except Exception as exc:
            log.debug(f"[apis] degraded harness_event write failed: {exc}")

    # ── Build provider command ─────────────────────────────────────────────────
    # A dispatch carrying the GitHub contract is one whose task involves
    # commit/push/PR — the delivery path #590 is about. Everything else runs
    # with the sandbox's default network denial.
    cmd, stdin_payload = _provider_command(
        provider,
        binary,
        system_prompt,
        prompt,
        cwd=cwd,
        network=include_github_contract,
    )

    # ── Stage 6: inject Neotoma MCP config so dispatched child can reach Neotoma ─
    # Dispatched `claude --print` children inherit the ambient Claude MCP config,
    # but in the daemon's context (ateles project scope) there is no neotoma MCP
    # server entry. Without it, role agents (Lanius/Pavo) cannot load
    # workflow_definition, init gate_status, or store plan_contribution — they
    # exit rc=0 without completing their Neotoma-dependent protocols.
    #
    # We inject a --mcp-config pointing the child at the local Neotoma HTTP MCP
    # endpoint (NEOTOMA_BASE_URL/mcp + bearer auth). We do NOT use
    # --strict-mcp-config so any other MCP servers the agent legitimately has
    # (from its own ambient config) are preserved; we only ADD neotoma.
    #
    # MCP tool allowlist syntax (ateles#1687 finding):
    #   claude --print --allowed-tools accepts "mcp__<servername>__*" as a wildcard
    #   that permits all tools from the named MCP server. The double-underscore
    #   separator matches the mcp__<server>__<tool> naming convention Claude uses
    #   internally. The server name must exactly match the key in mcpServers.
    #   So for {"mcpServers": {"mcpsrv_neotoma": ...}} the entry is
    #   "mcp__mcpsrv_neotoma__*" — matching the convention used across all 31 agent
    #   SKILL.md files and 24 agent_definition tool_allowlists in this codebase.
    #
    # Security tradeoff:
    #   Passing the bearer token as an inline JSON string in --mcp-config would
    #   expose it in the child's argv (visible via `ps aux`). Instead, we write
    #   the config to a mode-0600 temp file and pass the file path to --mcp-config.
    #   The temp file is cleaned up in a try/finally after the subprocess exits.
    _mcp_tmp_path: str | None = None
    if provider == "claude":
        _neotoma_base = os.environ.get(
            "NEOTOMA_BASE_URL", ""
        ).rstrip("/")
        _neotoma_token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
        _mcp_cfg: dict = {
            "mcpServers": {
                "mcpsrv_neotoma": {
                    "type": "http",
                    "url": f"{_neotoma_base}/mcp",
                }
            }
        }
        if _neotoma_token:
            _mcp_cfg["mcpServers"]["mcpsrv_neotoma"]["headers"] = {
                "Authorization": f"Bearer {_neotoma_token}"
            }

        # Write the MCP config to a mode-0600 temp file to avoid argv exposure.
        try:
            fd, _mcp_tmp_path = tempfile.mkstemp(
                suffix=".json", prefix="apis_mcp_"
            )
            os.chmod(_mcp_tmp_path, 0o600)
            with os.fdopen(fd, "w") as _f:
                json.dump(_mcp_cfg, _f)
            cmd += ["--mcp-config", _mcp_tmp_path]
            log.debug(
                f"[apis] Injected --mcp-config {_mcp_tmp_path} "
                "(mcpsrv_neotoma HTTP MCP)"
            )
        except Exception as exc:
            # Non-fatal: proceed without injection rather than abort.
            log.warning(
                f"[apis] Could not write MCP config temp file (non-fatal): {exc}"
            )
            _mcp_tmp_path = None

    tools = agent_def.tools  # property: list[str]; ['*'] means all
    if provider == "claude" and tools != ["*"]:
        # --allowed-tools is confirmed present in `claude --print --help`
        # (alias: --allowedTools). Accepts comma- or space-separated tool names.
        # MCP server tools use the "mcp__<servername>__*" wildcard form, where the
        # server name matches the mcpServers key (here: "mcpsrv_neotoma" — the
        # universal convention across all 31 agent SKILLs and 24 agent_definitions).
        # This allows all tools from that MCP server without enumerating them individually.
        allowed_list = list(tools)
        if "mcp__mcpsrv_neotoma__*" not in allowed_list:
            allowed_list.append("mcp__mcpsrv_neotoma__*")
        allowed = ",".join(allowed_list)
        cmd += ["--allowed-tools", allowed]
        log.info(
            f"[apis] Spawning via {provider}: "
            f"<{_role}:agent_def+{skill}.SKILL.md> "
            f"--allowed-tools {allowed} timeout={timeout}s"
        )
    else:
        log.info(
            f"[apis] Spawning via {provider}: "
            f"<{_role}:{'agent_def+' if not degraded else 'degraded-'}{skill}.SKILL.md> "
            f"timeout={timeout}s"
        )

    # ── Stage 2: harness_event at dispatch start ───────────────────────────────
    try:
        await asyncio.to_thread(
            _write_harness_event,
            task_entity_id=task_entity_id,
            role=_role,
            agent_sub=agent_def.aauth_sub,
            event_type="subprocess",
            tool_name=f"{provider}:{skill}",
            success="partial",  # "partial" = in-flight / started
            input_summary=prompt[:200],
        )
    except Exception as exc:
        log.debug(f"[apis] start harness_event write failed (non-fatal): {exc}")

    # Hard boundary from the approved plan: all three adapters use bundled
    # subscription auth by default. API-key credentials are removed so a capped
    # plan queues/fails over instead of silently spending metered tokens.
    subprocess_env = _subscription_only_env(env_extra)

    # ateles#109 — per-agent GitHub identity: when the caller resolved a
    # per-agent token (e.g. via _token_for_agent_on_repo in swarm_dispatch),
    # override both GITHUB_TOKEN and GH_TOKEN so the child's `gh` calls
    # authenticate as that agent's own account.  When github_token is None
    # (all SSE task-path and non-GitHub call sites), this block is skipped and
    # the child inherits the daemon's ambient tokens unchanged — exact
    # current behaviour, no regression.
    if github_token:
        subprocess_env["GITHUB_TOKEN"] = github_token
        subprocess_env["GH_TOKEN"] = github_token

    # Stage 3 (ateles#94): inject the Neotoma AAuth client signer env vars so
    # the dispatched child can sign its own Neotoma writes as <role>@ateles-swarm.
    # The Neotoma client signer (aauth_client_signer.ts) reads three vars:
    #   NEOTOMA_AAUTH_PRIVATE_JWK_PATH — path to the EC/P-256 JWK keypair file
    #   NEOTOMA_AAUTH_SUB              — the signing subject (e.g. gryllus@ateles-swarm)
    #   NEOTOMA_AAUTH_ISS              — the issuer (https://markmhendrickson.com)
    # We only inject when the role JWK file actually exists at the expected path;
    # if it is absent the child proceeds unsigned (graceful degradation, as today).
    # When degraded (empty prompt_markdown) we inject nothing — child runs unsigned.
    if not degraded and agent_def.aauth_sub:
        keys_dir = os.environ.get("ATELES_PRIVATE_KEYS_DIR", "")
        if keys_dir:
            jwk_path = os.path.join(keys_dir, f"{_role}.jwk.json")
            if os.path.exists(jwk_path):
                subprocess_env["NEOTOMA_AAUTH_PRIVATE_JWK_PATH"] = jwk_path
                subprocess_env["NEOTOMA_AAUTH_SUB"] = agent_def.aauth_sub
                subprocess_env["NEOTOMA_AAUTH_ISS"] = os.environ.get(
                    "NEOTOMA_AAUTH_ISS", "https://markmhendrickson.com"
                )

    _start_ns = time.monotonic_ns()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=subprocess_env,
            cwd=cwd,  # QE3: qa lens runs in a PR-branch worktree
        )
    except OSError as exc:
        if _mcp_tmp_path is not None:
            try:
                os.unlink(_mcp_tmp_path)
            except OSError:
                pass
        msg = f"{provider} launch failed: {exc}"
        log.warning(f"[apis] {skill} dispatch skipped — {msg}")
        return SkillResult(
            skill,
            False,
            None,
            "",
            "",
            error=msg,
            provider=provider,
        )

    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(
                    input=stdin_payload if stdin_payload is not None else prompt.encode()
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            duration_ms = int((time.monotonic_ns() - _start_ns) / 1_000_000)
            msg = f"timed out after {timeout}s"
            log.error(f"[apis] {skill} dispatch {msg}")

            # Stage 2: harness_event on timeout (failure)
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id=task_entity_id,
                    role=_role,
                    agent_sub=agent_def.aauth_sub,
                    event_type="subprocess",
                    tool_name=f"{provider}:{skill}",
                    success="false",
                    output_summary=f"timeout after {timeout}s",
                    duration_ms=duration_ms,
                    # A killed child's stdout is discarded, so no token counts
                    # exist for a timeout — but the provider and the requested
                    # model still do, and a dispatch that ran to the full
                    # timeout is the most expensive kind there is. Recording
                    # provider+model here is what makes "which model keeps
                    # timing out" answerable at all.
                    usage=parse_dispatch_usage(
                        provider,
                        "",
                        requested_model=_requested_model(provider, cmd),
                    ),
                )
            except Exception as exc:
                log.debug(f"[apis] timeout harness_event write failed: {exc}")

            # ateles#257 — a timed-out dispatch is the same silent failure class;
            # route it through the same rate-limited operator notification.
            notify_dispatch_failure(
                notifier,
                skill=skill,
                role=_role,
                returncode=None,
                stderr=msg,
                task_entity_id=task_entity_id,
            )

            return SkillResult(
                skill,
                False,
                None,
                "",
                "",
                error=msg,
                provider=provider,
            )

        duration_ms = int((time.monotonic_ns() - _start_ns) / 1_000_000)
        _stdout_text = stdout.decode("utf-8", errors="replace")
        _stderr_text = stderr.decode("utf-8", errors="replace")

        # ── Delivery-failure detection (ateles#590) ──────────────────────────────
        # A child that could not commit or push exits 0: it did everything it
        # was permitted to do, and says so plainly in its own output. Reading
        # only the exit code turns that into `ok: true` over an undelivered
        # change — a dispatch that reports success while delivering nothing.
        # The exit code is therefore necessary but not sufficient: a run is ok
        # only if the process succeeded AND nothing in its output says the
        # sandbox refused the delivery.
        # STDERR ONLY. git writes these lines to stderr; an agent that merely
        # QUOTES one — reading this PR, an issue, or a transcript — emits it on
        # stdout as prose. Scanning both made "the child read about a denial"
        # indistinguishable from "the child was denied", and the reproduction
        # transcript in #601's own body is a verbatim instance of that.
        _delivery_denial = _delivery_failure_reason(_stderr_text)
        if _delivery_denial and proc.returncode == 0:
            log.error(
                f"[apis] {skill} dispatch via {provider} exited 0 but could not "
                f"deliver: {_delivery_denial}"
            )

        # ── Per-dispatch usage attribution ───────────────────────────────────────
        # Parsed from what the harness already emitted; never estimated. Under
        # the swarm's text-mode invocations most harnesses report no token
        # counts, in which case this records provider + model_source and leaves
        # the token fields absent rather than writing a fabricated zero.
        _usage = await asyncio.to_thread(
            parse_dispatch_usage,
            provider,
            _stdout_text,
            requested_model=_requested_model(provider, cmd),
        )

        result = SkillResult(
            skill=skill,
            ok=proc.returncode == 0 and _delivery_denial is None,
            returncode=proc.returncode,
            stdout=_stdout_text,
            stderr=_stderr_text,
            provider=provider,
            error=(
                _delivery_denial
                if (_delivery_denial and proc.returncode == 0)
                else ""
            ),
            usage=_usage,
        )

        # ── Dropped-allowlist-rule notification (ateles#255) ──────────────────────
        # Checked regardless of exit code: the CLI logs "Ignoring --allowedTools
        # rule" and continues, so a drop can coexist with rc=0. One batched alert
        # per dispatch, not one per rule. Off-loaded to a thread (like the
        # harness_event writes below) so an unusually large stderr blob can't
        # block the event loop for other concurrent dispatches.
        dropped_rules = (
            await asyncio.to_thread(_find_dropped_allowlist_rules, result.stderr)
            if provider == "claude"
            else []
        )
        if dropped_rules:
            _notify_dropped_allowlist_rules(
                notifier, role=_role, rules=dropped_rules, returncode=proc.returncode
            )

        # ── Stage 2: harness_event at completion ──────────────────────────────────
        if result.ok:
            log.info(
                f"[apis] {skill} dispatch via {provider} ok "
                f"({len(result.stdout)}B stdout) [{_usage.summary()}]"
            )
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id=task_entity_id,
                    role=_role,
                    agent_sub=agent_def.aauth_sub,
                    event_type="subprocess",
                    tool_name=f"{provider}:{skill}",
                    success="true",
                    output_summary=(
                        f"provider={provider} {len(result.stdout)}B stdout rc=0"
                        f" {_usage.summary()}"
                    ),
                    duration_ms=duration_ms,
                    usage=_usage,
                )
            except Exception as exc:
                log.debug(f"[apis] success harness_event write failed: {exc}")
        else:
            # ateles#257 — persist the COMPLETE stdout+stderr before anything
            # truncates them, then name that file everywhere the failure surfaces.
            failure_log_path = await asyncio.to_thread(
                write_dispatch_failure_log,
                skill=skill,
                role=_role,
                returncode=proc.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                task_entity_id=task_entity_id,
                cmd=cmd,
                cwd=cwd,
                duration_ms=duration_ms,
            )
            path_note = failure_log_path or "(diagnostics file unavailable)"

            log.error(
                f"[apis] {skill} dispatch via {provider} failed "
                f"(rc={proc.returncode}); "
                f"full output: {path_note} "
                f"(stdout {len(result.stdout)}B, stderr {len(result.stderr)}B); "
                f"stderr head: {result.stderr[:500]}"
            )
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id=task_entity_id,
                    role=_role,
                    agent_sub=agent_def.aauth_sub,
                    event_type="subprocess",
                    tool_name=f"{provider}:{skill}",
                    success="false",
                    output_summary=(
                        f"provider={provider} rc={proc.returncode} "
                        f"{_usage.summary()} "
                        f"full_output={path_note} "
                        f"{result.stderr[:200]}"
                    ),
                    duration_ms=duration_ms,
                    # A failed dispatch still spent tokens. Recording usage only
                    # on success would systematically under-count exactly the
                    # dispatches most likely to have burned a retry loop.
                    usage=_usage,
                )
            except Exception as exc:
                log.debug(f"[apis] failure harness_event write failed: {exc}")

            # ateles#257 — a dispatch failure must reach the operator, not just a
            # log file. Rate-limited so a swarm-wide breakage is one signal.
            if _provider_failure_kind(result.stdout, result.stderr) is None:
                notify_dispatch_failure(
                    notifier,
                    skill=skill,
                    role=_role,
                    returncode=proc.returncode,
                    stderr=result.stderr,
                    task_entity_id=task_entity_id,
                    log_path=failure_log_path,
                )

        return result

    finally:
        # Clean up the MCP config temp file (always, even on timeout/exception).
        if _mcp_tmp_path is not None:
            try:
                os.unlink(_mcp_tmp_path)
            except OSError:
                pass


def usable_providers() -> set[str]:
    """Providers that could actually serve a run right now.

    The same view `run_skill` routes over: configured order ∩ providers whose
    binary resolves, minus those cooling down after a capacity/auth failure.
    Callers use this to decide whether a per-lens provider PREFERENCE can be
    honored, so that pinning never turns into "the lens silently did not run"
    (review_panel.resolve_lens_provider).
    """
    binaries = _provider_binaries()
    cooling = cooling_providers()
    return {
        provider
        for provider in configured_providers()
        if binaries.get(provider) and provider not in cooling
    }


async def run_skill(
    skill: str,
    prompt: str,
    *,
    role: str | None = None,
    task_entity_id: str = "",
    timeout: int | None = None,
    env_extra: dict[str, str] | None = None,
    notifier=None,
    github_token: str | None = None,
    include_github_contract: bool = False,
    cwd: str | None = None,
    provider: str | None = None,
) -> SkillResult:
    """Route one skill run across subscription-backed harness providers.

    The first candidate is selected with smooth weighted round-robin using the
    operator-supplied headroom estimates. Capacity, authentication, and launch
    failures cool that provider down and immediately try the next eligible CLI.
    Ordinary task failures and timeouts do not fail over because replaying a
    side-effecting task on another provider could duplicate work.

    Passing ``provider`` pins the invocation to one adapter, primarily for
    diagnostics and focused tests.
    """
    binaries = _provider_binaries()
    candidates = provider_candidates(binaries, preferred=provider)
    if not candidates:
        configured = os.environ.get(
            "APIS_HARNESS_PROVIDERS", "claude,codex,cursor"
        )
        cooling = ",".join(sorted(cooling_providers())) or "none"
        msg = (
            "no subscription-backed harness provider has usable headroom "
            f"(configured={configured}; cooling={cooling})"
        )
        return SkillResult(skill, False, None, "", "", error=msg)

    attempted: list[str] = []
    last_result: SkillResult | None = None
    for selected in candidates:
        attempted.append(selected)
        result = await _run_skill_once(
            skill,
            prompt,
            provider=selected,
            role=role,
            task_entity_id=task_entity_id,
            timeout=timeout,
            env_extra=env_extra,
            notifier=notifier,
            github_token=github_token,
            include_github_contract=include_github_contract,
            cwd=cwd,
        )
        result.attempted_providers = tuple(attempted)
        # A successful agent may legitimately discuss "usage limits" in its
        # answer. Only inspect stdout when the process itself failed; stderr and
        # explicit runner errors remain diagnostic on every result.
        failure_kind = _provider_failure_kind(
            result.error,
            result.stderr,
            result.stdout if not result.ok else "",
        )
        launch_failure = result.error.startswith(f"{selected} launch failed:")

        if result.ok and failure_kind is None:
            return result
        if failure_kind is None and not launch_failure:
            return result

        cool_down(selected)
        last_result = result
        log.warning(
            f"[apis] {skill}: {selected} {failure_kind or 'launch'} failure; "
            "trying next subscription-backed provider"
        )

    assert last_result is not None  # candidates was non-empty
    last_result.ok = False
    last_result.error = (
        "all eligible subscription-backed harness providers were exhausted "
        f"after attempts: {', '.join(attempted)}"
    )
    last_result.attempted_providers = tuple(attempted)
    notify_dispatch_failure(
        notifier,
        skill=skill,
        role=(role or skill).lower(),
        returncode=last_result.returncode,
        stderr=last_result.error,
        task_entity_id=task_entity_id,
    )
    return last_result

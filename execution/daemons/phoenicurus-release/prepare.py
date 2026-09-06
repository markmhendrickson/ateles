#!/usr/bin/env python3
"""
Phoenicurus-Release — Prepare Daemon (scheduled, Mon–Thu)

The PREPARE half of the operator-approved Neotoma release automation. Runs on a
schedule. Two-phase design, mirroring Cotinga:

  Phase 1 (fast, this process): preflight gate. Is there anything to release?
    Are unreleased commits sitting on main? Is main's CI green? If not, log
    and exit quietly (optionally a one-line Telegram on a hard block).

  Phase 2 (delegated, async): if there IS something to release, spawn a headless
    `claude --print` agent whose prompt runs the /release skill UP TO the RC-PR
    stop point — supplement, openapi:bc-diff, security lane, /review coverage
    lane, RC PR — then stores a `release_result` entity as status=pending_approval
    and Telegrams the operator the FULL rendered notes + RC PR link + advisory
    flags. The agent sends its own Telegram; this daemon exits immediately.

This daemon NEVER tags, publishes, or deploys. That is publish.py's job, invoked
only after the operator approves on Telegram (routed by Ateles).

The schedule (Mon–Thu) is set in the launchd plist via four StartCalendarInterval
dicts with Weekday 1..4. That scheduled run is now a SAFETY NET: the primary
trigger is a merge to Neotoma's main, which reaches this daemon as a GitHub
`push` webhook -> the Apis gateway (github_gateway.parse_github_event ->
swarm_dispatch._handle_push_main) -> `prepare.py --on-merge`. Merge-mode runs are
rate-limited per main commit rather than per day, so several merges in one day
each get a prepare attempt; the two locks are independent, so a merge run never
suppresses the day's scheduled sweep.

Usage:
  python3 prepare.py            # normal scheduled run
  python3 prepare.py --dry-run  # preflight only; print what it WOULD do, no spawn
  python3 prepare.py --force    # skip the "already-ran-today" guard
  python3 prepare.py --on-merge # merge-triggered; rate-limit per commit, not per day

Exit codes:
  0  ran (prepared / spawned, or nothing to do)
  1  fatal error
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

# Cloudflare fronts the hosted Neotoma instance and blocks urllib's default
# User-Agent with a 1010 "browser signature" 403. Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"

# ---------------------------------------------------------------------------
# Bootstrap env (launchd does not source profiles)
# ---------------------------------------------------------------------------

_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # ateles root
LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "phoenicurus-release.log"
STATE_FILE = Path(__file__).parent / ".phoenicurus_prepare_last_run"
# On-merge mode keys idempotency off the main commit it last considered rather
# than the calendar day, so a merge can trigger a prepare run the same day an
# earlier one already ran (the scheduled path's daily lock would swallow it).
MERGE_STATE_FILE = Path(__file__).parent / ".phoenicurus_prepare_last_sha"
# How many times a single head may have an agent spawned for it before we stop
# and ask the operator. A crashed agent must be retried (see run_prepare), but
# "retry forever" is its own failure: if the agent dies for a reason retrying
# cannot fix — no API credits, a missing agent_grant, a bad prompt — an
# unbounded retry spawns an agent on every webhook and buries the real cause.
MAX_SPAWNS_PER_HEAD = int(os.environ.get("PHOENICURUS_MAX_SPAWNS_PER_HEAD", "3"))
SPAWN_COUNT_FILE = Path(__file__).parent / ".phoenicurus_prepare_spawn_count"
# PID of the agent currently preparing a head, so a later run can defer to a
# live peer WITHOUT spawning an agent just to have it stand down (which would
# spend a retry attempt on a non-attempt — see _live_peer_pid).
SPAWN_PID_FILE = Path(__file__).parent / ".phoenicurus_prepare_spawn_pid"
AGENT_LOG = LOG_DIR / "phoenicurus-prepare-agent.log"
# Which blocking release_result entity_id we last escalated to the operator
# for a stale block, so the on-merge path (rate-limited per COMMIT, not per
# day — several merges can land while the same block is still stale) does not
# re-notify on every merge. Cleared implicitly once a different entity_id (or
# none) is the blocker.
STALE_ESCALATION_FILE = Path(__file__).parent / ".phoenicurus_prepare_stale_escalated"

NEOTOMA_REPO_ROOT = Path(
    os.environ.get("NEOTOMA_REPO_ROOT", str(Path.home() / "repos" / "neotoma"))
)
GITHUB_REPO = os.environ.get("NEOTOMA_GITHUB_REPO", "markmhendrickson/neotoma")
TELEGRAM_TOPIC = os.environ.get("TELEGRAM_TOPIC_PHOENICURUS", "") or os.environ.get(
    "TELEGRAM_TOPIC_RELEASES", ""
)
# Minimum unreleased commits before a release is worth preparing (avoid churning
# a 1-commit patch every weekday). Override with PHOENICURUS_MIN_COMMITS.
MIN_COMMITS = int(os.environ.get("PHOENICURUS_MIN_COMMITS", "1"))

# A release_result blocking prep for longer than this is a stall, not a wait —
# escalate to the operator instead of repeating the same quiet INFO line
# forever. Default N=3 from the ateles#461 PM spec; change here if the number
# needs to move. Override with PHOENICURUS_STALE_RELEASE_BLOCK_DAYS.
STALE_RELEASE_BLOCK_DAYS = int(
    os.environ.get("PHOENICURUS_STALE_RELEASE_BLOCK_DAYS", "3")
)

# Email notification (release RCs also go to the operator's inbox, not just
# Telegram — mirrors the rest of the swarm, which emails via gws +send). The
# operator + swarm addresses are the same env vars the shared lib/notify
# Notifier reads, so release mail matches every other daemon's From/To.
OPERATOR_EMAIL = os.environ.get("OPERATOR_EMAIL", "").strip()
SWARM_EMAIL = os.environ.get("ATELES_SWARM_EMAIL", "").strip()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.logging_setup import configure_daemon_logging
from lib.notify.email_gate import email_enabled, record_suppressed  # noqa: E402

# Rotating + repeat-suppressing (lib/daemon_runtime/logging_setup.py):
# unbounded retry logging filled a 926 GB disk on 2026-08-18.
log = configure_daemon_logging("phoenicurus-release", also_stdout=True)


# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------


def _already_ran_today() -> bool:
    return STATE_FILE.exists() and STATE_FILE.read_text().strip() == date.today().isoformat()


def _mark_ran_today() -> None:
    STATE_FILE.write_text(date.today().isoformat())


def _head_sha() -> str:
    return _git(["rev-parse", "origin/main"])


def _already_ran_for_sha(sha: str) -> bool:
    return (
        bool(sha)
        and MERGE_STATE_FILE.exists()
        and MERGE_STATE_FILE.read_text().strip() == sha
    )


def _mark_ran_for_sha(sha: str) -> None:
    if sha:
        MERGE_STATE_FILE.write_text(sha)


def _already_escalated_stale_block(entity_id: str) -> bool:
    """True if we already notified the operator about THIS blocking entity."""
    return (
        bool(entity_id)
        and STALE_ESCALATION_FILE.exists()
        and STALE_ESCALATION_FILE.read_text().strip() == entity_id
    )


def _mark_escalated_stale_block(entity_id: str) -> None:
    if entity_id:
        try:
            STALE_ESCALATION_FILE.write_text(entity_id)
        except OSError as exc:
            # Losing this only costs us a repeat notification, never a missed
            # one — never let it block the run.
            log.warning(f"could not persist stale-escalation marker: {exc}")


def _spawn_count(head: str) -> int:
    """How many agents have already been spawned for this head."""
    if not head or not SPAWN_COUNT_FILE.exists():
        return 0
    try:
        sha, _, n = SPAWN_COUNT_FILE.read_text().strip().partition(" ")
        return int(n) if sha == head else 0
    except (ValueError, OSError):
        return 0


def _live_peer_pid(head: str) -> int | None:
    """
    PID of a prepare agent still running for this head, or None.

    A spawned agent lives for many minutes. When a later MERGE fires inside that
    window, the new agent discovers the peer and correctly stands down rather
    than racing it to open a competing RC PR. But standing down is not an
    attempt, and counting it as one is how a head burns its whole retry budget
    without a single agent ever trying: observed 2026-08-09, where attempts 2/3
    and 3/3 were both deferrals and the original then died, leaving the release
    stuck with the budget spent.

    Checking here means we never spawn the deferring agent at all — cheaper
    than spawning one to discover what the parent already knows.

    SCOPE: merge-triggered runs only. The scheduled sweep passes `head=""` (it
    is rate-limited per calendar day, not per commit), so it has no head to key
    a peer lookup on and does not consult this. A sweep firing while a
    merge-spawned agent is still working therefore relies on the daily lock
    alone, and can in principle still spawn a peer that stands down — costing a
    wasted spawn, though no longer a miscounted attempt, since `_record_spawn`
    is likewise merge-only. Closing that would mean keying peer state on
    something other than the head; deliberately out of scope here.
    """
    if not head or not SPAWN_PID_FILE.exists():
        return None
    try:
        sha, _, pid_s = SPAWN_PID_FILE.read_text().strip().partition(" ")
        if sha != head:
            return None
        pid = int(pid_s)
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)  # signal 0 = liveness probe, sends nothing
    except (ProcessLookupError, PermissionError, OSError):
        return None
    return pid


def _record_spawn_pid(head: str, pid: int) -> None:
    """Remember which process is preparing this head, so peers can defer to it."""
    if not head:
        return
    try:
        SPAWN_PID_FILE.write_text(f"{head} {pid}")
    except OSError as exc:
        # Losing this only costs us a redundant spawn, never a missed release.
        log.warning(f"could not persist spawn pid: {exc}")


def _record_spawn(head: str) -> int:
    """Increment and persist this head's spawn count; return the new value."""
    n = _spawn_count(head) + 1
    if head:
        try:
            SPAWN_COUNT_FILE.write_text(f"{head} {n}")
        except OSError as exc:
            # Losing the counter must not block a release; worst case we retry
            # more than MAX_SPAWNS_PER_HEAD times, which is still better than
            # never retrying at all.
            log.warning(f"could not persist spawn count: {exc}")
    return n


def _mark_ran(on_merge: bool, head: str, *, transient: bool = False) -> None:
    """
    Stamp whichever idempotency lock applies to this run's mode. On-merge runs
    stamp the SHA only, so they never suppress the day's scheduled safety-net
    run (and vice versa).

    ``transient`` marks a deferral on a state that is expected to change for the
    SAME head — CI still in progress, or CI red that may go green. In on-merge
    mode we must NOT stamp the SHA for these: the merge webhook fires before that
    merge's CI finishes, so stamping here would burn the per-commit lock on a run
    that did nothing and block the `check_suite`-completion retry from ever
    preparing this head (the immediacy the auto-release exists to provide would
    be lost until the next merge or the scheduled sweep). The SCHEDULED path
    still stamps — its once-a-day deferral is intentional, and a same-day retry
    there is not wanted.
    """
    if on_merge:
        if not transient:
            _mark_ran_for_sha(head)
    else:
        _mark_ran_today()


# ---------------------------------------------------------------------------
# Telegram (outbound; only used for hard-block notices — the spawned agent
# sends the rich prepared-release notification itself)
# ---------------------------------------------------------------------------


def telegram_send(text: str) -> None:
    import shutil

    node = shutil.which("node")
    send_script = PROJECT_ROOT / "execution" / "lib" / "telegram" / "send.mjs"
    if node and send_script.exists():
        try:
            args = [node, str(send_script), "--text", text]
            if TELEGRAM_TOPIC:
                args += ["--thread-id", TELEGRAM_TOPIC]
            subprocess.run(args, timeout=20, capture_output=True, env=os.environ)
        except Exception as exc:
            log.warning(f"telegram send failed: {exc}")


def email_send(subject: str, body: str) -> bool:
    """
    Send a release notification to the operator's inbox via `gws gmail +send`.

    Mirrors the shared lib/notify Notifier's email transport (same OPERATOR_EMAIL
    To / ATELES_SWARM_EMAIL From, same gws argv-list send) so release mail matches
    every other swarm daemon. Fail-open: any missing config or send error logs and
    returns False so the caller keeps Telegram as the guaranteed channel — release
    notification must never be blocked on email.

    Returns True only if gws reports a successful send.
    """
    import shutil

    if not OPERATOR_EMAIL:
        log.info("OPERATOR_EMAIL unset — skipping release email (Telegram only)")
        return False
    # Global kill-switch (ateles#645). Returning False keeps Telegram as the
    # guaranteed channel, so a release notification is never lost — only
    # de-emailed. The message is recorded either way.
    if not email_enabled():
        record_suppressed(
            channel="phoenicurus_release", subject=subject, body=body,
            to=OPERATOR_EMAIL,
        )
        return False
    gws = shutil.which("gws")
    if not gws:
        log.warning("gws CLI not found — cannot email release notification")
        return False
    cmd = [gws, "gmail", "+send", "--to", OPERATOR_EMAIL,
           "--subject", subject, "--body", body]
    if SWARM_EMAIL:
        cmd += ["--from", SWARM_EMAIL]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           env=os.environ)
        if r.returncode != 0:
            log.warning(f"gws +send failed (rc={r.returncode}): "
                        f"{(r.stderr or '').strip()[:200]}")
            return False
        log.info(f"release email sent to {OPERATOR_EMAIL}")
        return True
    except Exception as exc:  # noqa: BLE001 — never block the release on email
        log.warning(f"release email send error: {exc}")
        return False


def notify_operator(text: str, *, subject: str | None = None) -> None:
    """
    Send an operator notification on BOTH channels: Telegram always, email too
    when OPERATOR_EMAIL is configured. Used for the synchronous hard-block /
    error notices prepare.py sends directly (agent couldn't spawn, main CI red,
    crash) — the rich prepared-RC notification is sent by the spawned agent,
    which owns the rendered notes. Both sends are best-effort and independent;
    neither failure blocks the other.
    """
    telegram_send(text)
    email_send(subject or (text.strip().splitlines() or ["Phoenicurus"])[0][:80], text)


# ---------------------------------------------------------------------------
# Git / CI preflight (read-only — runs in the Neotoma repo)
# ---------------------------------------------------------------------------


def _git(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(NEOTOMA_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return (proc.stdout or "").strip()


def latest_tag() -> str:
    out = _git(["tag", "--sort=-v:refname"])
    for line in out.splitlines():
        if line.startswith("v") and line[1:2].isdigit():
            return line.strip()
    return ""


def unreleased_commit_count(tag: str) -> int:
    if not tag:
        return 0
    out = _git(["rev-list", "--count", f"{tag}..origin/main"])
    try:
        return int(out)
    except ValueError:
        return 0


def main_ci_green() -> bool | None:
    """
    True if the latest 'CI test lanes' run on main is success, False if not,
    None if it can't be determined (treated as a soft block — surfaced, not fatal).
    """
    try:
        proc = subprocess.run(
            [
                "gh", "run", "list", "--repo", GITHUB_REPO, "--branch", "main",
                "--workflow", "CI test lanes", "--limit", "1",
                "--json", "conclusion,status", "--jq", ".[0]",
            ],
            cwd=str(NEOTOMA_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        data = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        log.warning(f"could not read main CI status: {exc}")
        return None
    if not data:
        return None
    if data.get("status") != "completed":
        return None  # in progress — don't prepare against an unknown state
    return data.get("conclusion") == "success"


# ---------------------------------------------------------------------------
# Neotoma: is a release for this version already in flight?
# ---------------------------------------------------------------------------

# Statuses that represent a release genuinely in flight — anything else
# (superseded, published, skipped, failed, …) never blocks prep.
_BLOCKING_STATUSES = ("prepared", "pending_approval", "approved", "publishing")


def is_sentinel_version(version: str | None) -> bool:
    """
    True for versions that are not a real release and must never gate prep —
    test/CI artifacts like `v0.0.0-lint-check` (the ateles#461 incident: an
    11-day-old lint-check sentinel blocked every real release). Permissive but
    explicit: a class of value, not one hardcoded string, since more sentinels
    of this shape are expected (e.g. `v0.0.0-ci-smoke`).
    """
    if not version:
        return True
    return version.startswith("v0.0.0-") or version.endswith("-lint-check")


def _parse_ts(ts) -> float | None:
    """Parse an epoch number or ISO-8601 string (with or without trailing Z)."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _row_timestamp(row: dict) -> float | None:
    """
    Best available timestamp for a queried entity row, checked in priority
    order (matching the convention in apis/task_watchdog.py's `_age_seconds`).

    `release_result` has no declared status-transition field in its schema, so
    this reads the envelope Neotoma attaches to every queried row rather than
    a field this function would have to invent.
    """
    for key in (
        "updated_at",
        "last_observation_at",
        "updated_date",
        "computed_at",
        "created_at",
    ):
        parsed = _parse_ts(row.get(key))
        if parsed is not None:
            return parsed
    return None


def existing_release_status(
    next_version_hint: str = "",
) -> tuple[str, str, str, float | None] | None:
    """
    Return (status, entity_id, version, age_days) for the release_result — if
    any — that should block preparing `next_version_hint`, so we don't
    re-prepare on top of a release already in flight for the SAME version.
    `age_days` is None when no usable timestamp was found on the row.

    Two modes, chosen by whether a hint is given:
      - next_version_hint set: only a `release_result` whose version equals the
        hint can block. A blocking status on an unrelated or sentinel version
        (e.g. a stray `v0.0.0-lint-check` test artifact) must never gate prep
        of a different, real release — that silent mismatch is ateles#461.
      - next_version_hint empty/None: falls back to "any non-sentinel version
        with a blocking status", never "any version at all" — a sentinel is
        excluded even with no hint to compare against. `run_prepare` itself
        uses THIS mode: the next RC's version is chosen later, inside the
        spawned agent's semver bump, so it is not knowable at the call site
        (`latest_tag()` is the LAST released version, not the one being
        prepared — passing it as the hint would compare the wrong two
        versions and never match a real in-flight RC). Passing an explicit
        hint remains supported for any caller that DOES know the exact
        version it's asking about.

    Sentinel versions (see `is_sentinel_version`) are excluded in BOTH modes.
    Entities are considered newest-first so the most recent blocking match
    wins when several are present.
    """
    base = os.environ.get("NEOTOMA_BASE_URL", "").rstrip("/")
    is_loopback = "localhost" in base or "127.0.0.1" in base
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if bearer and not is_loopback:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        body = json.dumps(
            {"entity_type": "release_result", "limit": 50, "include_snapshots": True}
        ).encode()
        req = urllib.request.Request(
            f"{base}/entities/query", data=body, headers=headers, method="POST"
        )
        req.add_header("User-Agent", NEOTOMA_USER_AGENT)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        entities = data.get("entities") if isinstance(data, dict) else data
        now = datetime.now(timezone.utc).timestamp()
        # Single pass, tracking the newest qualifying match — do not rely on
        # incidental query order when several rows qualify. A row with no
        # parseable timestamp is kept distinct from one dated 1970: it never
        # LOSES to an actually-old timestamped row (which would silently bury
        # a just-written, envelope-not-yet-populated blocker), but a row WITH
        # a real timestamp always wins the comparison once one is found.
        best: tuple[str, str, str, float | None] | None = None
        best_ts: float | None = None
        best_has_ts = False
        for row in entities or []:
            snap = row.get("snapshot") or row.get("fields") or row
            if isinstance(snap, dict) and isinstance(snap.get("snapshot"), dict):
                snap = snap["snapshot"]
            status = str(snap.get("status") or "")
            if status not in _BLOCKING_STATUSES:
                continue
            version = str(snap.get("version") or "")
            if is_sentinel_version(version):
                continue
            if next_version_hint and version != next_version_hint:
                continue
            entity_id = str(row.get("entity_id") or row.get("id") or "")
            ts = _row_timestamp(row)
            age_days = max(0.0, (now - ts) / 86400.0) if ts is not None else None
            candidate = (status, entity_id, version, age_days)
            if best is None:
                best, best_ts, best_has_ts = candidate, ts, ts is not None
                continue
            if ts is not None and (not best_has_ts or ts > best_ts):
                best, best_ts, best_has_ts = candidate, ts, True
        return best
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning(f"could not check existing release_result: {exc}")
    return None


# ---------------------------------------------------------------------------
# Phase 2: spawn the headless /release-prep agent
# ---------------------------------------------------------------------------


def _build_agent_prompt(last_tag: str, commit_count: int) -> str:
    topic_note = (
        f"Use Telegram topic id {TELEGRAM_TOPIC} for the notification."
        if TELEGRAM_TOPIC
        else "Send the Telegram notification to the default chat."
    )
    from_flag = f' --from "{SWARM_EMAIL}"' if SWARM_EMAIL else ""
    email_note = (
        f"""12. ALSO email the operator the SAME notification (release goes to the
    inbox, not just Telegram — and the operator can approve BY EMAIL REPLY).

    SEND AS HTML, not raw markdown. The release notes are markdown; if sent as a
    plain --body they render as a literal wall of `##` and `-` characters in
    Gmail. Convert the notes to clean, well-formed HTML and pass `--html`:
      - Write the HTML to a temp file and send with:
        `gws gmail +send --to "{OPERATOR_EMAIL}"{from_flag} --subject "🚀 Release <TAG> ready to approve" --html --body "$(cat /tmp/release-<TAG>-email.html)"`
        (or pass the HTML string directly to --body). ALWAYS include `--html`.
      - Convert markdown → HTML properly: `##` headings → <h2>, `-` lists → <ul><li>,
        `**bold**` → <strong>, code/backticks → <code>, blank lines → paragraph
        breaks. Do NOT inline a tiny font-size; let the client default apply
        (normal size). Keep it simple and readable — headings, paragraphs, lists.
      - Put the RC PR URL as a real <a href> link.

    The email MUST still contain, as VISIBLE TEXT the operator (and their reply
    quote) will carry:
      (a) the subject starting 🚀 with the phrase "ready to approve" + the version;
      (b) an approve/skip instruction, e.g. a line: Reply <code>approve &lt;TAG&gt;</code>
          to publish, or <code>skip &lt;TAG&gt;</code> to discard;
      (c) a line reading exactly `release-approve: <TAG>` (real tag, e.g.
          `release-approve: v0.20.0`) — Turdus parses this token from the reply
          body to route an `approve <TAG>` reply to the publish gate, so it MUST be
          present, exact, and in a form that survives as plain text in a quoted
          reply (put it in its own <p> or <code> line, NOT only inside an href).

    If the gws send fails, log it and continue — Telegram (step 11) is the
    guaranteed channel; do NOT abort the run over an email failure."""
        if OPERATOR_EMAIL
        else "12. (Email notification skipped: OPERATOR_EMAIL is not configured.)"
    )
    return f"""You are Phoenicurus, the Neotoma release-preparation agent.

Run a release PREPARATION pass for the Neotoma repo at {NEOTOMA_REPO_ROOT}.
There are {commit_count} commit(s) on origin/main since the last tag {last_tag}.

CRITICAL CONSTRAINTS — read carefully:
- You PREPARE ONLY. You MUST NOT tag, push tags, run `npm publish`, create or
  publish a GitHub Release, or deploy the sandbox. Those are done later by
  publish.py after the operator approves. If you find yourself about to run any
  irreversible publish step, STOP.
- Work in an isolated git worktree off origin/main. Do NOT disturb the operator's
  main checkout or any unrelated uncommitted changes.

Your job — run the /release skill's PREPARE phase up to (and including) the
release-candidate PR, then HALT:
1. Preflight: confirm commits since {last_tag}, clean compare range.
2. Choose the next version (semver: minor for features, patch for fixes only).
3. Draft the release supplement (the human-readable notes), walking the commit
   range and grouping by theme. Include an explicit "Breaking changes" section.
4. Run `npm run openapi:bc-diff` and reconcile against the supplement.
5. Run the security review lane (npm run security:classify-diff / security:lint /
   security:manifest:check / test:security:auth-matrix) and fill
   docs/releases/in_progress/<TAG>/security_review.md.
6. Run the /review skill over <last_tag>..HEAD and write
   docs/releases/in_progress/<TAG>/test_coverage_review.md. RESOLVE any BLOCKING
   findings before opening the RC PR.
7. Bump the version and commit it together with the supplement — this commit
   is REQUIRED and MUST land in the RC PR (a missing bump commit caused the
   v0.18.8 incident: the RC PR merged without it and publish.py would have
   tagged/published the wrong version). Run exactly:
   `npm version <TAG-without-v-prefix> --no-git-tag-version`
   then commit package.json + package-lock.json together with the supplement,
   using exactly this commit message format (matching precedent commit
   01344fec9): `chore(release): bump version to <TAG> + supplement`.
8. Open the release-candidate PR (release/<TAG> -> main) with the supplement as
   the body. Do NOT post `@claude review` — that workflow
   (`claude_pr_review.yml`) was disabled on 2026-06-24 and the comment fires
   nothing. The Ateles swarm review panel picks the PR up on its own.
9. Render the exact GitHub Release notes with
   `npm run -s release-notes:render -- --tag <TAG> --head-ref HEAD --supplement <path>`.

Then record + notify:
10. Record the release by running THIS SCRIPT — do not POST /store yourself:

      python3 {Path(__file__).parent}/store_release_result.py \\
        --version <TAG> --status pending_approval \\
        --rc-branch release/<TAG> --rc-pr-url <RC_PR_URL>

    It signs the write with phoenicurus's AAuth key. A plain POST /store is
    denied: `release_result` has no configured access policy, so it defaults to
    `closed` (no guest access), and a bearer-token request resolves as a guest.
    That denial is why v0.21.4 prepared with no durable record (ateles#402).
    The script sets both the `rc_*` and plain field names, and the idempotency
    key, so re-running it is safe. If it exits non-zero, say so in your Telegram
    and email — do NOT report the release as recorded.
11. Send a Telegram notification with: the version, the FULL rendered release
    notes, the RC PR URL, and any advisory flags (security sensitive=true,
    /review findings, CI status). End with: "Reply `approve <TAG>` to publish, or
    `skip <TAG>` to discard." {topic_note}
{email_note}

If preflight shows nothing to release, send a one-line Telegram saying so and stop.
Be precise and terse in the Telegram/email messages. No motivational filler.
"""


def _agent_env() -> dict:
    """Environment for the headless `claude --print` prepare agent.

    Prefer the Claude Code Max-subscription OAuth token over a pay-per-token
    API key: when BOTH CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY are set,
    `claude --print` uses the API key — and if that account has no credits the
    agent dies immediately with "Credit balance is too low", producing no RC and
    no notification (observed 2026-07-27, the first live prepare run). Dropping
    ANTHROPIC_API_KEY from the CHILD env only (never the daemon's own) routes the
    agent through the subscription, so releases don't depend on a funded API
    account. If only the API key is present, we leave it untouched.
    """
    env = dict(os.environ)
    if env.get("CLAUDE_CODE_OAUTH_TOKEN") and env.get("ANTHROPIC_API_KEY"):
        env.pop("ANTHROPIC_API_KEY", None)
        log.info(
            "prepare agent: using CLAUDE_CODE_OAUTH_TOKEN (dropped ANTHROPIC_API_KEY "
            "from the child env so the agent bills the Max subscription, not the "
            "pay-per-token API account)"
        )
    return env


def spawn_prepare_agent(
    last_tag: str, commit_count: int, dry_run: bool, head: str = ""
) -> bool:
    import shutil

    claude = shutil.which("claude")
    if not claude:
        log.error("claude CLI not found — cannot spawn prepare agent")
        notify_operator(
            "🔴 Phoenicurus: claude CLI not found — cannot prepare release."
        )
        return False

    prompt = _build_agent_prompt(last_tag, commit_count)
    if dry_run:
        log.info("[dry-run] would spawn prepare agent with prompt:")
        log.info(prompt)
        return True

    try:
        proc = subprocess.Popen(
            [claude, "--print", "--dangerously-skip-permissions", prompt],
            cwd=str(NEOTOMA_REPO_ROOT),
            env=_agent_env(),
            stdout=open(AGENT_LOG, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Record the pid so a later run defers to this agent instead of
        # spawning one just to have it stand down.
        _record_spawn_pid(head, proc.pid)
        log.info(
            f"Prepare agent spawned (background, pid {proc.pid}). "
            "It will Telegram when ready."
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error(f"failed to spawn prepare agent: {exc}")
        notify_operator(f"🔴 Phoenicurus: failed to spawn prepare agent — {exc}")
        return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_prepare(dry_run: bool, force: bool, on_merge: bool = False) -> int:
    if not (NEOTOMA_REPO_ROOT / "package.json").exists():
        log.error(f"NEOTOMA_REPO_ROOT has no package.json: {NEOTOMA_REPO_ROOT}")
        return 1

    # The scheduled path is rate-limited to one run per calendar day. On-merge
    # runs are rate-limited per main commit instead (checked after the fetch
    # below), so several merges in a day each get a prepare attempt.
    if not on_merge and _already_ran_today() and not force and not dry_run:
        log.info("Already ran today — exiting.")
        return 0

    # Refresh main + tags (read-only).
    subprocess.run(
        ["git", "fetch", "origin", "main", "--tags", "--quiet"],
        cwd=str(NEOTOMA_REPO_ROOT),
        capture_output=True,
        timeout=120,
    )

    head = _head_sha() if on_merge else ""
    if on_merge and _already_ran_for_sha(head) and not force and not dry_run:
        log.info(f"Already ran for origin/main {head[:9]} — exiting.")
        return 0

    tag = latest_tag()
    if not tag:
        log.info("No release tag found — nothing to base a release on. Exiting.")
        return 0

    count = unreleased_commit_count(tag)
    log.info(f"{count} commit(s) on origin/main since {tag}")
    if count < MIN_COMMITS:
        log.info(
            f"Fewer than MIN_COMMITS ({MIN_COMMITS}) unreleased commits — "
            "nothing to prepare. Exiting."
        )
        if not dry_run:
            _mark_ran(on_merge, head)
        return 0

    # Don't re-prepare if a release is already in flight awaiting approval.
    #
    # Deliberately pass NO hint here rather than `tag`. `tag` is the LAST
    # RELEASED version (latest_tag()); the version actually being prepared is
    # chosen later, inside the spawned agent's semver bump, and is not known
    # at this call site. Passing `tag` as the hint would make this compare
    # the wrong two versions — a real in-flight release_result for the RC
    # (e.g. v0.20.0) would never equal the last-released tag (e.g. v0.19.0),
    # so the guard would never match and this daemon would spawn a second
    # prepare agent on top of a real pending release. The no-hint mode
    # ("any non-sentinel version blocks") is what this call site's actual
    # information supports; sentinel/test entities (ateles#461) are still
    # excluded in that mode by `is_sentinel_version`.
    inflight = existing_release_status()
    if inflight:
        status, entity_id, version, age_days = inflight
        log.info(
            f"[phoenicurus-release] INFO A release_result is already '{status}' "
            f"({version}, {entity_id}) — not preparing another. "
            "(Approve or skip the pending one first.)"
        )
        if age_days is not None and age_days > STALE_RELEASE_BLOCK_DAYS:
            log.warning(
                f"[phoenicurus-release] WARNING release_result {entity_id} "
                f"({version}) has blocked prep as '{status}' for "
                f"{age_days:.1f} day(s), past the {STALE_RELEASE_BLOCK_DAYS}-day "
                "stale threshold."
            )
            # Escalate once per blocking entity, not once per run. The
            # on-merge path is rate-limited per COMMIT, not per day (see
            # module docstring), so several merges can land while the same
            # release_result is still stuck — without this, each one would
            # re-page the operator about a block they already know about.
            if not dry_run and _already_escalated_stale_block(entity_id):
                log.info(
                    f"[phoenicurus-release] INFO already escalated {entity_id} "
                    "— not re-notifying the operator."
                )
            else:
                log.warning(
                    "[phoenicurus-release] WARNING escalating to the operator."
                )
                notify_operator(
                    f"⚠️ Phoenicurus: release_result {entity_id} ({version}) has "
                    f"been '{status}' for {age_days:.1f} day(s) — longer than the "
                    f"{STALE_RELEASE_BLOCK_DAYS}-day stale threshold. This is "
                    "blocking prep of every release since. Approve or skip it, or "
                    "correct its status if it's stale (e.g. a test/sentinel "
                    "artifact)."
                )
                if not dry_run:
                    _mark_escalated_stale_block(entity_id)
        if not dry_run:
            _mark_ran(on_merge, head)
        return 0

    # CI gate.
    ci = main_ci_green()
    if ci is False:
        log.warning("main CI is RED — refusing to prepare a release.")
        notify_operator(
            f"⚠️ Phoenicurus: {count} unreleased commit(s) since {tag}, but main "
            "CI is RED. Not preparing a release until CI is green."
        )
        if not dry_run:
            _mark_ran(on_merge, head, transient=True)
        return 0
    if ci is None:
        log.warning("main CI status unknown / in progress — deferring to next run.")
        if not dry_run:
            _mark_ran(on_merge, head, transient=True)
        return 0

    # Defer to a live peer BEFORE consulting the retry budget. An agent that
    # stands down for a peer never attempted anything, so charging it an
    # attempt is miscounting — and three such deferrals exhaust the budget
    # while zero agents have tried. Leave the lock unstamped so this head is
    # re-evaluated once the peer finishes or dies.
    peer = _live_peer_pid(head) if on_merge else None
    if peer and not force:
        log.info(
            f"prepare agent (pid {peer}) is already working {head[:9]} — deferring "
            "to it rather than racing a competing RC PR. Not counted as an attempt."
        )
        if not dry_run:
            _mark_ran(on_merge, head, transient=True)
        return 0

    # Bounded retry: a crashed agent gets another attempt, but a head that has
    # burned through its attempts stops and asks rather than spawning forever.
    prior = _spawn_count(head) if on_merge else 0
    if on_merge and prior >= MAX_SPAWNS_PER_HEAD and not force:
        log.error(
            f"{prior} prepare agent(s) already spawned for {head[:9]} with no "
            "release_result to show for it — not spawning another."
        )
        # Both side-effects stay behind `not dry_run`, like every other mutation
        # in this function. A dry run must be observable-only: it should never
        # page the operator, and it must never stamp the lock — doing so would
        # let a diagnostic invocation silently retire a head that still needs
        # preparing, which is the same class of bug this PR exists to fix.
        if not dry_run:
            notify_operator(
                f"🔴 Phoenicurus: {prior} prepare attempts for `{head[:9]}` produced "
                f"no release candidate. Not retrying automatically.\n\n"
                f"Check `{AGENT_LOG}` for why the agent is dying, then re-run with "
                "`--force` once the cause is fixed."
            )
            _mark_ran(on_merge, head)  # stop the loop; operator owns it now
        return 1

    log.info(
        f"Preconditions met: {count} commits since {tag}, main CI green. "
        f"Spawning prepare agent (attempt {prior + 1}/{MAX_SPAWNS_PER_HEAD})."
    )
    if not dry_run and on_merge:
        _record_spawn(head)
    ok = spawn_prepare_agent(tag, count, dry_run, head)

    # Do NOT stamp the per-commit lock here. `spawn_prepare_agent` returns as
    # soon as `Popen` hands back a process, so `ok` means "the agent launched",
    # never "the agent produced an RC". Stamping on launch is a claim about an
    # outcome that has not happened yet, and it is not recoverable: a stamped
    # SHA suppresses every later run for that head, so an agent that dies
    # mid-flight leaves the release permanently unprepared with nothing to
    # retry it.
    #
    # That is not hypothetical. On 2026-08-08 the agent spawned for neotoma
    # 78dbcbefe died on `API Error: Unable to connect to API (ENOTFOUND)`, and
    # every subsequent run logged "Already ran for origin/main 78dbcbefe" while
    # no RC PR existed. The fix that merged never reached a release.
    #
    # A crashed agent is exactly what `transient` describes — "a state expected
    # to change for the SAME head" — so leave the lock unstamped and let the
    # next run re-evaluate. Re-running is safe because the real idempotency
    # guard is the in-flight `release_result` check above: once the agent
    # succeeds, that gate short-circuits and stamps the SHA itself. The lock
    # therefore comes to mean "an RC exists for this head", which is the
    # condition we actually want to avoid repeating.
    if not dry_run:
        _mark_ran(on_merge, head, transient=True)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Phoenicurus release prepare daemon")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="preflight only; print the agent prompt, do not spawn",
    )
    ap.add_argument(
        "--force", action="store_true", help="skip the already-ran-today guard"
    )
    ap.add_argument(
        "--on-merge",
        action="store_true",
        help=(
            "merge-triggered run: rate-limit per origin/main commit instead of "
            "per calendar day (all other gates unchanged)"
        ),
    )
    args = ap.parse_args()

    # Report whether this daemon is running the code that is actually on main.
    # A daemon executes the checkout it was launched from, so a merged fix does
    # nothing until that checkout is updated — on 2026-08-09 this daemon ran a
    # 2026-07-28 local merge commit, and ateles#401 (the fix for the very stall
    # being debugged) never reached it. Advisory: logs and continues, because a
    # release daemon that refuses to start is worse than one running slightly
    # stale code. Set ATELES_ENFORCE_CHECKOUT_FRESHNESS=1 to make it fatal.
    # Only the SETUP of the check is advisory. `warn_on_drift` itself must run
    # outside this guard: it raises CheckoutDriftError under enforcement, and a
    # blanket `except Exception` here would catch that and downgrade the abort
    # to a warning — advertising a switch that can never fire. That is the same
    # silently-defeated-safety-mechanism failure this module exists to catch,
    # so the guard is scoped to import/path errors only (Loxia, ateles#405).
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "lib" / "daemon_runtime"))
        from checkout_drift import warn_on_drift  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        # A missing or broken check must never be the reason a release fails to
        # prepare — but its absence should be visible, not silent.
        log.warning(f"checkout freshness check unavailable: {exc}")
    else:
        # Judge this file's own directory — a daemon runs the checkout it was
        # launched from, which is exactly what we want to assess. The library
        # honours ATELES_CHECKOUT_DRIFT_ROOT to override the inspected path
        # (entrypoint tests; production leaves it unset), so the call site
        # needs no override of its own.
        #
        # Raises CheckoutDriftError when ATELES_ENFORCE_CHECKOUT_FRESHNESS=1;
        # deliberately uncaught so enforcement actually aborts the run.
        warn_on_drift("phoenicurus-prepare", Path(__file__).resolve().parent)

    try:
        return run_prepare(args.dry_run, args.force, on_merge=args.on_merge)
    except Exception as exc:  # noqa: BLE001
        log.exception(f"prepare fatal error: {exc}")
        notify_operator(f"🔴 Phoenicurus prepare crashed — {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

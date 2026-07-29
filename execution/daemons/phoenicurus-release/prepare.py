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

Spawning the agent is not the end of the run: `--check-agent-outcome` reconciles
what the spawned agent actually DID. The spawn is wrapped in a shell that appends
`PHOENICURUS_PREPARE_EXIT=<code>` to the agent log, so a later pass can tell a
successful prepare from a crash, a credit/usage-limit death, or a silent hang.
The daily/per-SHA idempotency stamp is only written once that outcome check
confirms success (stamp-on-success), so a failed prepare is retried instead of
being locked out for the day. Usage-limit deaths schedule a retry via
`--retry-if-due`.

Usage:
  python3 prepare.py                      # normal scheduled run
  python3 prepare.py --dry-run            # preflight only; print what it WOULD do
  python3 prepare.py --force              # skip the "already-ran-today" guard
  python3 prepare.py --on-merge           # merge-triggered; per-commit rate limit
  python3 prepare.py --check-agent-outcome  # reconcile the last spawned agent
  python3 prepare.py --retry-if-due       # re-run a usage-limit-deferred prepare

Exit codes:
  0  ran (prepared / spawned, or nothing to do)
  1  fatal error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
AGENT_LOG = LOG_DIR / "phoenicurus-prepare-agent.log"

# Supervision state. The spawn record is what makes the fire-and-forget agent
# auditable: --check-agent-outcome reads it to find the exit sentinel in
# AGENT_LOG and decide whether the prepare actually succeeded.
SPAWN_STATE_FILE = Path(__file__).parent / ".phoenicurus_prepare_last_spawn"
RETRY_STATE_FILE = Path(__file__).parent / ".phoenicurus_prepare_retry_after"
AUTH_NOTIFY_STATE_FILE = Path(__file__).parent / ".phoenicurus_prepare_auth_notify"
# A prepare agent that has neither exited nor produced a release_result within
# this window is treated as dead (hung / killed / OOM), not merely slow.
OUTCOME_WINDOW_SECONDS = 45 * 60
MAX_RETRY_ATTEMPTS = 3
# existing_release_status() sentinel: the in-flight question could NOT be
# answered safely (auth failure / no credentials), which is NOT the same as
# "no release in flight" (None). Fail closed — never prepare on top of it.
STATUS_UNSAFE = "__unsafe__"
EXIT_SENTINEL_PREFIX = "PHOENICURUS_PREPARE_EXIT="
# Re-notify about a persistent auth failure at most this often, so a broken
# token doesn't Telegram the operator on every scheduled run.
AUTH_NOTIFY_INTERVAL_SECONDS = 6 * 3600
# release_result statuses that mean a release is real work in progress or done.
RELEASE_RESULT_INFLIGHT_STATUSES = (
    "prepared",
    "pending_approval",
    "approved",
    "publishing",
)
RELEASE_RESULT_LIVE_STATUSES = RELEASE_RESULT_INFLIGHT_STATUSES + ("published",)

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


class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [phoenicurus-prepare] %(levelname)s %(message)s",
    handlers=[_FlushingFileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


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


def _clear_stamp(on_merge: bool, head: str = "") -> None:
    """
    Drop whichever idempotency lock this run's mode uses, unblocking a retry.

    Called when the spawned agent's outcome turns out to be a FAILURE. Under
    stamp-on-success the lock is normally only written after a confirmed good
    outcome, but a stamp can still be present from an earlier deferral (or a
    --force run), and leaving it would lock the release out for the rest of the
    day / for that head.
    """
    target = MERGE_STATE_FILE if on_merge else STATE_FILE
    try:
        if target.exists():
            target.unlink()
            log.info(f"cleared idempotency stamp {target.name} — retry unblocked")
    except OSError as exc:
        log.warning(f"could not clear stamp {target}: {exc}")


# ---------------------------------------------------------------------------
# Clock helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware datetime (UTC when naive)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(iso_timestamp: str) -> float | None:
    started = _parse_iso(iso_timestamp)
    if started is None:
        return None
    return (_now() - started).total_seconds()


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


def _neotoma_base() -> str:
    return os.environ.get("NEOTOMA_BASE_URL", "http://localhost:9180").rstrip("/")


def _neotoma_headers() -> dict:
    base = _neotoma_base()
    is_loopback = "localhost" in base or "127.0.0.1" in base
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if bearer and not is_loopback:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _snapshot(entity: dict) -> dict:
    return entity.get("snapshot") or entity.get("fields") or entity


def _notify_auth_failure_once(text: str) -> None:
    """
    Notify the operator about a Neotoma auth failure, at most once per
    AUTH_NOTIFY_INTERVAL_SECONDS. A misconfigured token blocks EVERY prepare run,
    so it must be surfaced — but not on every scheduled tick.
    """
    last = ""
    try:
        if AUTH_NOTIFY_STATE_FILE.exists():
            last = AUTH_NOTIFY_STATE_FILE.read_text().strip()
    except OSError:
        last = ""
    age = _age_seconds(last)
    if age is not None and age < AUTH_NOTIFY_INTERVAL_SECONDS:
        log.info("auth-failure notice already sent recently — not re-notifying")
        return
    notify_operator(text)
    try:
        AUTH_NOTIFY_STATE_FILE.write_text(_now_iso())
    except OSError as exc:
        log.warning(f"could not record auth-notify state: {exc}")


def _query_release_results(limit: int = 50) -> tuple[list[dict] | None, str | None]:
    """
    Query release_result entities from Neotoma.

    Returns ``(entities, error)`` where ``error`` is:
      - None       — the query succeeded; ``entities`` is authoritative
      - "auth"     — credentials are missing or rejected (401/403). The answer is
                     UNKNOWN and callers must fail closed; an absent
                     Authorization header made a real pending_approval release
                     look like "nothing in flight", which is how a duplicate RC
                     gets prepared on top of one awaiting approval.
      - "transient" — network/JSON/other HTTP failure; callers keep the existing
                     lenient behavior (a refused local connection is the common
                     case on a laptop and must not page the operator).
    """
    base = _neotoma_base()
    is_loopback = "localhost" in base or "127.0.0.1" in base
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    # Preflight: a remote Neotoma with no bearer token cannot answer the
    # in-flight question at all — every query comes back empty or 401. Say so
    # distinctly instead of issuing a request that looks like "no release".
    if not is_loopback and not bearer:
        log.error(
            f"Neotoma release_result query unsafe: no token configured "
            f"(NEOTOMA_BEARER_TOKEN unset) for non-loopback {base}"
        )
        _notify_auth_failure_once(
            "🔴 Phoenicurus: NEOTOMA_BEARER_TOKEN is not configured for "
            f"{base} — cannot check whether a release is already in flight, so "
            "prepare is blocked (fail-closed)."
        )
        return None, "auth"
    body = json.dumps(
        {"entity_type": "release_result", "limit": limit, "include_snapshots": True}
    ).encode()
    req = urllib.request.Request(
        f"{base}/entities/query", data=body, headers=_neotoma_headers(), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            log.error(
                f"Neotoma release_result query rejected: HTTP {exc.code} "
                f"({base}) — treating the in-flight check as UNSAFE"
            )
            _notify_auth_failure_once(
                f"🔴 Phoenicurus: Neotoma returned HTTP {exc.code} for the "
                f"release_result query at {base}. Cannot tell whether a release "
                "is already in flight — prepare is blocked (fail-closed) until "
                "the credentials are fixed."
            )
            return None, "auth"
        log.warning(f"could not check existing release_result: HTTP {exc.code}")
        return None, "transient"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning(f"could not check existing release_result: {exc}")
        return None, "transient"
    entities = data.get("entities") if isinstance(data, dict) else data
    return list(entities or []), None


def existing_release_status(next_version_hint: str) -> str | None:
    """
    Return the status of any release_result already tracking work since the last
    tag, so we don't re-prepare on top of a pending_approval release.

    Three-valued on purpose:
      - a status string — a release IS in flight; do not prepare another
      - None            — no release in flight (or a transient read failure)
      - STATUS_UNSAFE   — the question could not be answered safely (auth); the
                          caller must defer rather than assume "nothing in flight"

    A record stuck in `publishing` whose tag / GitHub Release already exists is
    auto-corrected to `published` and does NOT count as in flight: publish.py
    crashing between the tag push and the status write would otherwise wedge the
    daemon forever, since nothing else ever revisits that record.
    """
    entities, error = _query_release_results()
    if error == "auth":
        return STATUS_UNSAFE
    if error or entities is None:
        return None
    for e in entities:
        snap = _snapshot(e)
        status = str(snap.get("status") or "")
        if status not in RELEASE_RESULT_INFLIGHT_STATUSES:
            continue
        version = str(snap.get("version") or "")
        if status == "publishing" and version and _release_already_shipped(version):
            log.warning(
                f"release_result {version} is stuck in 'publishing' but its tag / "
                "GitHub Release already exists — auto-repairing status to "
                "'published' and not treating it as in flight"
            )
            _correct_release_status(
                version,
                "published",
                reason=(
                    "auto-repair by prepare.py: tag/GitHub Release present while "
                    "status was still 'publishing' (stale publish.py write)"
                ),
            )
            continue
        return status
    return None


def _release_already_shipped(version: str) -> bool:
    """True if this version's git tag or GitHub Release already exists."""
    tag = version if version.startswith("v") else f"v{version}"
    if _git(["tag", "--list", tag]).strip():
        log.info(f"git tag {tag} exists — {version} was already tagged")
        return True
    try:
        proc = subprocess.run(
            ["gh", "release", "view", tag, "--repo", GITHUB_REPO, "--json", "tagName"],
            cwd=str(NEOTOMA_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning(f"could not check GitHub Release for {tag}: {exc}")
        return False
    if proc.returncode == 0:
        log.info(f"GitHub Release {tag} exists — {version} was already published")
        return True
    return False


def _correct_release_status(version: str, status: str, *, reason: str = "") -> bool:
    """
    Append a release_result observation flipping status, mirroring publish.py's
    set_release_status (same POST /store shape and idempotency-key convention,
    so the correction coalesces onto the same version-keyed entity).
    """
    rec: dict = {"entity_type": "release_result", "version": version, "status": status}
    if reason:
        rec["reason"] = reason
    body = json.dumps(
        {
            "entities": [rec],
            "idempotency_key": f"release-{version}-{status}-{date.today().isoformat()}",
        }
    ).encode()
    req = urllib.request.Request(
        f"{_neotoma_base()}/store",
        data=body,
        headers=_neotoma_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except (urllib.error.URLError, OSError) as exc:
        log.warning(f"could not correct release_result {version} -> {status}: {exc}")
        return False
    log.info(f"release_result {version} status auto-corrected -> {status}")
    return True


def has_new_release_result_since(spawned_at: str) -> bool:
    """
    True if a release_result exists that this prepare cycle plausibly produced.

    Deliberately monkeypatchable as a single seam: the outcome check needs one
    yes/no answer ("did the agent actually leave a release behind?"), and tests
    should be able to answer it without a Neotoma. A record whose observation
    time can't be read counts as new — better to accept a real RC than to
    declare a successful prepare a failure and Telegram the operator.
    """
    entities, error = _query_release_results()
    if error or not entities:
        if error:
            log.warning(
                f"could not confirm a release_result after the prepare agent ran "
                f"({error}) — treating as no result"
            )
        return False
    since = _parse_iso(spawned_at)
    for e in entities:
        snap = _snapshot(e)
        status = str(snap.get("status") or "").lower()
        if status not in RELEASE_RESULT_LIVE_STATUSES:
            continue
        observed = _parse_iso(
            str(
                e.get("last_observation_at")
                or e.get("updated_at")
                or snap.get("updated_at")
                or ""
            )
        )
        if since is None or observed is None or observed >= since:
            return True
    return False


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
   the body. Post `@claude review` on it.
9. Render the exact GitHub Release notes with
   `npm run -s release-notes:render -- --tag <TAG> --head-ref HEAD --supplement <path>`.

Then record + notify:
10. Store a Neotoma `release_result` entity (POST {os.environ.get("NEOTOMA_BASE_URL", "http://localhost:9180")}/store)
    with fields: version=<TAG>, status="pending_approval". Set BOTH branch-name
    fields to "release/<TAG>": `rc_branch` AND `branch`. Set BOTH PR-URL fields
    to the RC PR URL: `rc_pr_url` AND `release_url`. (publish.py reads the `rc_*`
    names; the plain names are kept for continuity — write both so either reader
    resolves.) Use idempotency_key
    "release-<TAG>-pending_approval-{date.today().isoformat()}".
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


def _agent_shell_command(claude: str, prompt: str) -> str:
    """
    Wrap the agent invocation so its exit status ALWAYS lands in AGENT_LOG.

    The daemon backgrounds the agent and exits, so it never sees the child's
    return code — and `claude --print` dying on "Credit balance is too low" or a
    usage limit looks exactly like a successful spawn. The trailing `echo` runs
    unconditionally (`;`, not `&&`), so the sentinel is present for every
    terminal outcome and --check-agent-outcome can read it later.
    """
    inner = shlex.join([claude, "--print", "--dangerously-skip-permissions", prompt])
    return (
        f'{inner} ; echo "{EXIT_SENTINEL_PREFIX}$?" >> {shlex.quote(str(AGENT_LOG))}'
    )


def _write_spawn_state(
    *, tag: str, commit_count: int, on_merge: bool, head: str, log_offset: int
) -> None:
    """Record enough about this spawn for --check-agent-outcome to reconcile it."""
    record = {
        "sha_or_date": head if on_merge else date.today().isoformat(),
        "spawned_at": _now_iso(),
        "on_merge": on_merge,
        "tag": tag,
        "head": head,
        "commit_count": commit_count,
        # Byte offset into AGENT_LOG at spawn time — the exit sentinel search
        # starts here, so a sentinel from a PREVIOUS run can never be read as
        # this run's outcome.
        "log_offset": log_offset,
    }
    try:
        SPAWN_STATE_FILE.write_text(json.dumps(record, indent=2))
    except OSError as exc:
        log.warning(f"could not record prepare-agent spawn state: {exc}")


def _read_spawn_state() -> dict | None:
    if not SPAWN_STATE_FILE.exists():
        return None
    try:
        data = json.loads(SPAWN_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"could not read prepare-agent spawn state: {exc}")
        return None
    return data if isinstance(data, dict) else None


def _record_spawn_outcome(state: dict, outcome: str) -> None:
    """Mark the recorded spawn reconciled so later checks are no-ops."""
    state = dict(state)
    state["outcome"] = outcome
    state["checked_at"] = _now_iso()
    try:
        SPAWN_STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        log.warning(f"could not record prepare-agent outcome: {exc}")


def _agent_log_since(offset: int) -> str:
    """AGENT_LOG content written after ``offset`` bytes."""
    if not AGENT_LOG.exists():
        return ""
    try:
        return AGENT_LOG.read_bytes()[max(offset, 0):].decode("utf-8", "replace")
    except OSError as exc:
        log.warning(f"could not read agent log: {exc}")
        return ""


def _agent_exit_code(state: dict) -> int | None:
    """
    The exit code the sentinel recorded for this spawn, or None if the agent has
    not terminated (no sentinel written since the spawn offset).
    """
    text = _agent_log_since(int(state.get("log_offset") or 0))
    code: int | None = None
    for line in text.splitlines():
        if EXIT_SENTINEL_PREFIX not in line:
            continue
        raw = line.split(EXIT_SENTINEL_PREFIX, 1)[1].strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            code = int(digits)  # last sentinel wins
    return code


def _agent_log_tail(state: dict, lines: int = 30) -> str:
    text = _agent_log_since(int(state.get("log_offset") or 0))
    return "\n".join(text.splitlines()[-lines:])


def spawn_prepare_agent(
    last_tag: str,
    commit_count: int,
    dry_run: bool,
    *,
    on_merge: bool = False,
    head: str = "",
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

    log_offset = AGENT_LOG.stat().st_size if AGENT_LOG.exists() else 0
    try:
        # Open the agent log ourselves so we can close the fd in the parent after
        # Popen inherits it — leaving it open would leak a handle per spawn.
        agent_log_fh = open(AGENT_LOG, "a")
        try:
            subprocess.Popen(
                ["sh", "-c", _agent_shell_command(claude, prompt)],
                cwd=str(NEOTOMA_REPO_ROOT),
                env=_agent_env(),
                stdout=agent_log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            agent_log_fh.close()
    except Exception as exc:  # noqa: BLE001
        log.error(f"failed to spawn prepare agent: {exc}")
        notify_operator(f"🔴 Phoenicurus: failed to spawn prepare agent — {exc}")
        return False
    _write_spawn_state(
        tag=last_tag,
        commit_count=commit_count,
        on_merge=on_merge,
        head=head,
        log_offset=log_offset,
    )
    log.info(
        "Prepare agent spawned (background). It will Telegram when ready; "
        "--check-agent-outcome reconciles what it actually did."
    )
    return True


# ---------------------------------------------------------------------------
# Usage-limit backoff
# ---------------------------------------------------------------------------

# `claude` reports a hit subscription limit as e.g.
#   "5-hour limit reached ∙ resets 6:40pm (Europe/Madrid)"
# That is a WAIT, not a failure: retrying after the stated reset succeeds, while
# giving up loses the day's release.
USAGE_LIMIT_RESET_RE = re.compile(
    r"resets (\d{1,2}:\d{2}(am|pm)) \(([^)]+)\)", re.IGNORECASE
)


def _parse_usage_limit_reset(text: str) -> str | None:
    """
    ISO deadline for the usage-limit reset named in ``text``, or None if the text
    carries no usage-limit notice (i.e. it's an ordinary failure).
    """
    match = USAGE_LIMIT_RESET_RE.search(text or "")
    if not match:
        return None
    clock, _, tz_name = match.group(1), match.group(2), match.group(3)
    try:
        tz = ZoneInfo(tz_name.strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning(f"unknown timezone {tz_name!r} in usage-limit notice — using UTC")
        tz = timezone.utc
    try:
        reset_time = datetime.strptime(clock.lower(), "%I:%M%p").time()
    except ValueError:
        log.warning(f"could not parse usage-limit reset clock {clock!r}")
        return None
    now = datetime.now(tz)
    deadline = datetime.combine(now.date(), reset_time, tzinfo=tz)
    if deadline <= now:
        deadline += timedelta(days=1)  # the reset is tomorrow's clock time
    return deadline.isoformat()


def _read_retry_state() -> dict | None:
    if not RETRY_STATE_FILE.exists():
        return None
    try:
        data = json.loads(RETRY_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"could not read retry state: {exc}")
        return None
    return data if isinstance(data, dict) else None


def _clear_retry_state() -> None:
    try:
        if RETRY_STATE_FILE.exists():
            RETRY_STATE_FILE.unlink()
    except OSError as exc:
        log.warning(f"could not clear retry state: {exc}")


def _schedule_retry(deadline_iso: str, *, on_merge: bool, head: str, tag: str) -> None:
    previous = _read_retry_state() or {}
    attempts = int(previous.get("attempts") or 0) + 1
    record = {
        "retry_after": deadline_iso,
        "attempts": attempts,
        "on_merge": on_merge,
        "head": head,
        "tag": tag,
        "scheduled_at": _now_iso(),
    }
    try:
        RETRY_STATE_FILE.write_text(json.dumps(record, indent=2))
    except OSError as exc:
        log.warning(f"could not write retry state: {exc}")
        return
    log.info(
        f"usage limit hit — prepare retry {attempts}/{MAX_RETRY_ATTEMPTS} "
        f"scheduled for {deadline_iso}"
    )


def retry_if_due(dry_run: bool = False) -> int:
    """
    Re-run a prepare that was deferred by a usage limit, once the reset has
    passed. A no-op when nothing is scheduled or the deadline is still ahead.

    Attempt-count preservation: clearing the whole retry file before re-running
    would reset ``attempts`` to 0, so every subsequent usage-limit failure would
    re-arm as attempt 1 and the cap would never fire (infinite retry). After
    consuming a due schedule we leave an unarmed breadcrumb that keeps the
    attempt count; ``_schedule_retry`` increments from it, and a successful
    outcome clears it via ``_clear_retry_state``.
    """
    state = _read_retry_state()
    if not state:
        log.info("No prepare retry scheduled — nothing to do.")
        return 0
    raw_deadline = state.get("retry_after")
    if not raw_deadline:
        # Unarmed breadcrumb left by a prior re-run — not a live schedule.
        log.info("No prepare retry armed — nothing to do.")
        return 0
    deadline = _parse_iso(str(raw_deadline))
    if deadline is None:
        log.warning("retry state has no usable deadline — discarding it")
        _clear_retry_state()
        return 0
    if _now() < deadline:
        log.info(f"Prepare retry not due until {deadline.isoformat()} — exiting.")
        return 0
    attempts = int(state.get("attempts") or 0)
    if attempts >= MAX_RETRY_ATTEMPTS:
        log.error(
            f"prepare retry budget exhausted ({attempts}/{MAX_RETRY_ATTEMPTS}) — "
            "giving up and asking the operator to intervene"
        )
        notify_operator(
            f"🔴 Phoenicurus: prepare failed {attempts} times on usage limits "
            f"(tag {state.get('tag') or '?'}). Giving up — run "
            "`prepare.py --force` manually when capacity is available."
        )
        _clear_retry_state()
        return 0
    on_merge = bool(state.get("on_merge"))
    log.info(
        f"Prepare retry due (attempt {attempts}/{MAX_RETRY_ATTEMPTS}, "
        f"on_merge={on_merge}) — re-running prepare."
    )
    # Disarm the schedule but KEEP the attempt count so a repeated usage-limit
    # failure increments rather than resetting. Success clears this entirely.
    try:
        RETRY_STATE_FILE.write_text(
            json.dumps(
                {
                    "attempts": attempts,
                    "on_merge": on_merge,
                    "head": state.get("head") or "",
                    "tag": state.get("tag") or "",
                    "disarmed_at": _now_iso(),
                },
                indent=2,
            )
        )
    except OSError as exc:
        log.warning(f"could not disarm retry state: {exc}")
        _clear_retry_state()
    return run_prepare(dry_run, True, on_merge=on_merge)


# ---------------------------------------------------------------------------
# Agent-outcome supervision
# ---------------------------------------------------------------------------


def _handle_agent_failure(state: dict, headline: str, tail: str) -> None:
    """
    Common failure path: tell the operator with a log tail, unblock the lock, and
    schedule a retry when the failure was a usage limit rather than a real error.
    """
    on_merge = bool(state.get("on_merge"))
    head = str(state.get("head") or "")
    tag = str(state.get("tag") or "")
    reset = _parse_usage_limit_reset(tail)
    body = headline
    if tail.strip():
        body += f"\n\nLast {len(tail.splitlines())} log line(s):\n{tail}"
    if reset:
        body += f"\n\nUsage limit detected — retry scheduled for {reset}."
    notify_operator(body)
    _clear_stamp(on_merge, head)
    if reset:
        _schedule_retry(reset, on_merge=on_merge, head=head, tag=tag)
    else:
        # Not a capacity problem: retrying on a schedule would just re-fail.
        log.info("failure is not a usage limit — notifying only, no retry scheduled")


def check_agent_outcome() -> int:
    """
    Reconcile the last spawned prepare agent.

    Four outcomes:
      - still within OUTCOME_WINDOW_SECONDS with no exit sentinel → still running
      - exit 0 AND a release_result appeared → SUCCESS: stamp the idempotency lock
        (this is the only place the stamp is written for a spawning run)
      - exit non-zero, or exit 0 with no release_result, or no sentinel after the
        window → FAILURE: notify with a log tail, clear the stamp, schedule a
        retry if the log shows a usage limit
    """
    state = _read_spawn_state()
    if not state:
        log.info("No prepare-agent spawn recorded — nothing to reconcile.")
        return 0
    if state.get("outcome"):
        log.info(
            f"Last prepare-agent spawn already reconciled "
            f"({state['outcome']}) — nothing to do."
        )
        return 0
    spawned_at = str(state.get("spawned_at") or "")
    on_merge = bool(state.get("on_merge"))
    head = str(state.get("head") or "")
    tag = str(state.get("tag") or "?")
    age = _age_seconds(spawned_at)
    exit_code = _agent_exit_code(state)

    if exit_code is None:
        if age is not None and age < OUTCOME_WINDOW_SECONDS:
            log.info(
                f"Prepare agent for {tag} still running "
                f"({age / 60:.0f}m elapsed, no exit sentinel yet) — will check again."
            )
            return 0
        elapsed = f"{age / 60:.0f}m" if age is not None else "unknown time"
        log.error(f"prepare agent for {tag} never reported an exit after {elapsed}")
        _handle_agent_failure(
            state,
            f"🔴 Phoenicurus: the prepare agent for {tag} never reported an exit "
            f"({elapsed} elapsed, window {OUTCOME_WINDOW_SECONDS // 60}m). "
            "Assuming it died; no release was prepared.",
            _agent_log_tail(state),
        )
        _record_spawn_outcome(state, "no_exit")
        return 0

    if exit_code != 0:
        log.error(f"prepare agent for {tag} exited {exit_code}")
        _handle_agent_failure(
            state,
            f"🔴 Phoenicurus: the prepare agent for {tag} exited {exit_code} — "
            "no release was prepared.",
            _agent_log_tail(state),
        )
        _record_spawn_outcome(state, f"exit_{exit_code}")
        return 0

    if not has_new_release_result_since(spawned_at):
        log.error(
            f"prepare agent for {tag} exited 0 but left no release_result behind"
        )
        _handle_agent_failure(
            state,
            f"⚠️ Phoenicurus: the prepare agent for {tag} exited cleanly but no "
            "release_result was stored — the RC was NOT prepared.",
            _agent_log_tail(state),
        )
        _record_spawn_outcome(state, "exit_0_no_result")
        return 0

    log.info(
        f"Prepare agent for {tag} succeeded (exit 0, release_result present) — "
        "stamping the idempotency lock."
    )
    _mark_ran(on_merge, head)
    _clear_retry_state()
    _record_spawn_outcome(state, "success")
    return 0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _spawn_still_pending() -> bool:
    """
    True if a previously spawned agent is still working (or awaiting its outcome
    check). Under stamp-on-success the idempotency lock is not written at spawn
    time, so this is what stops a second run from spawning a duplicate agent
    while the first is mid-flight and has not yet stored its release_result.
    """
    state = _read_spawn_state()
    if not state or state.get("outcome"):
        return False
    if _agent_exit_code(state) is not None:
        return False  # it terminated; --check-agent-outcome owns it now
    age = _age_seconds(str(state.get("spawned_at") or ""))
    return age is not None and age < OUTCOME_WINDOW_SECONDS


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

    # Don't spawn a second agent on top of one that is still working. The
    # idempotency stamp is only written on a CONFIRMED good outcome now, so it
    # can't be what suppresses this.
    if _spawn_still_pending() and not force and not dry_run:
        log.info(
            "A prepare agent spawned recently has not finished — not spawning "
            "another. (--check-agent-outcome will reconcile it.)"
        )
        return 0

    # Don't re-prepare if a release is already in flight awaiting approval.
    inflight = existing_release_status(tag)
    if inflight == STATUS_UNSAFE:
        log.warning(
            "Could not determine whether a release is already in flight "
            "(Neotoma auth) — deferring rather than risking a duplicate RC."
        )
        if not dry_run:
            _mark_ran(on_merge, head, transient=True)
        return 0
    if inflight:
        log.info(
            f"A release_result is already {inflight!r} — not preparing another. "
            "(Approve or skip the pending one first.)"
        )
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

    log.info(
        f"Preconditions met: {count} commits since {tag}, main CI green. "
        "Spawning prepare agent."
    )
    # NOTE: no _mark_ran here. Stamping at spawn time locked the release out for
    # the day even when the agent died seconds later (credit exhausted, usage
    # limit, crash) — the failure was invisible AND unretryable. The stamp is now
    # written by check_agent_outcome() only after the agent is confirmed to have
    # produced a release_result.
    ok = spawn_prepare_agent(tag, count, dry_run, on_merge=on_merge, head=head)
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
    ap.add_argument(
        "--check-agent-outcome",
        action="store_true",
        help=(
            "reconcile the last spawned prepare agent: stamp on confirmed "
            "success, notify + unblock retry on failure"
        ),
    )
    ap.add_argument(
        "--retry-if-due",
        action="store_true",
        help="re-run a prepare that a usage limit deferred, if its reset has passed",
    )
    args = ap.parse_args()
    try:
        if args.check_agent_outcome:
            return check_agent_outcome()
        if args.retry_if_due:
            return retry_if_due(args.dry_run)
        return run_prepare(args.dry_run, args.force, on_merge=args.on_merge)
    except Exception as exc:  # noqa: BLE001
        log.exception(f"prepare fatal error: {exc}")
        notify_operator(f"🔴 Phoenicurus prepare crashed — {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

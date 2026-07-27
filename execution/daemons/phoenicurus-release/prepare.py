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
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

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


def existing_release_status(next_version_hint: str) -> str | None:
    """
    Return the status of any release_result already tracking work since the last
    tag, so we don't re-prepare on top of a pending_approval release.
    """
    base = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:9180").rstrip("/")
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        entities = data.get("entities") if isinstance(data, dict) else data
        for e in entities or []:
            snap = e.get("snapshot") or e.get("fields") or e
            status = str(snap.get("status") or "")
            if status in ("prepared", "pending_approval", "approved", "publishing"):
                return status
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
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
    inbox, not just Telegram — and the operator can approve BY EMAIL REPLY). Run
    exactly:
    `gws gmail +send --to "{OPERATOR_EMAIL}"{from_flag} --subject "🚀 Release <TAG> ready to approve" --body "<the full notification text: version, the FULL rendered release notes, the RC PR URL, advisory flags. Then, on their OWN lines: the exact line: Reply approve <TAG> to publish, or skip <TAG> to discard   AND the exact correlation token line: release-approve: <TAG>>"`
    The email subject MUST start with 🚀 and contain the phrase "ready to
    approve" and name the version. The body MUST contain BOTH (a) the
    approve/skip instruction verbatim and (b) a line reading exactly
    `release-approve: <TAG>` (with the real tag, e.g. `release-approve: v0.20.0`)
    — Turdus keys off that token to route an `approve <TAG>` reply to the publish
    gate, so it MUST be present and exact. If the gws send fails, log it and
    continue — Telegram (step 11) is the guaranteed channel; do NOT abort the run
    over an email failure."""
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


def spawn_prepare_agent(last_tag: str, commit_count: int, dry_run: bool) -> bool:
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
        subprocess.Popen(
            [claude, "--print", "--dangerously-skip-permissions", prompt],
            cwd=str(NEOTOMA_REPO_ROOT),
            env=os.environ,
            stdout=open(AGENT_LOG, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.info("Prepare agent spawned (background). It will Telegram when ready.")
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
    inflight = existing_release_status(tag)
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
    ok = spawn_prepare_agent(tag, count, dry_run)
    if not dry_run:
        _mark_ran(on_merge, head)
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
    try:
        return run_prepare(args.dry_run, args.force, on_merge=args.on_merge)
    except Exception as exc:  # noqa: BLE001
        log.exception(f"prepare fatal error: {exc}")
        notify_operator(f"🔴 Phoenicurus prepare crashed — {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

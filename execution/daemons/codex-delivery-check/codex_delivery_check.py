#!/usr/bin/env python3
"""Hourly read-only delivery check across Codex sessions.

Long-running Codex sessions stall silently: goals sit blocked with nothing to
wake them, watchers are paused while a session says it is watching, merged work
is not deployed, and dated follow-ups have no trigger. This script looks for
those four conditions once an hour and tells the operator, in plain language,
what is stuck, for how long, and what to do. It makes no model calls and
changes nothing outside its own small state file.

Checks (each a small independent function returning findings):

1. ``check_goals``       blocked / usage-limited / budget-limited Codex goals
                         (paused goals are deliberate and never alert).
2. ``check_automations`` recently created but PAUSED Codex automations, and
                         ACTIVE ones whose target thread has gone quiet.
3. ``check_deploys``     merged to the default branch but not deployed for
                         over a day, per ``deployment_configuration`` entity.
4. ``check_tasks``       operator tasks due soon and blocked, or past due.

Alerting reuses ``lib/notify/notifier.py`` (priority routing, silence windows,
``dedupe_key`` / ``clear_dedupe``). A finding alerts once; when it resolves its
key is cleared so a recurrence alerts again. A check that could not run is
reported as "could not check X", never as an all-clear.

Privacy: thread rollouts are never opened, and goal objectives are never read
into a finding or message (the thread name is used instead).

Usage:
    codex_delivery_check.py --once --dry-run   # print findings, send nothing;
                                               # exit 1 if any check failed
    codex_delivery_check.py --once             # alert on change
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HANDLER = "codex-delivery-check"
KEY_PREFIX = "codex-delivery"

BAD_GOAL_STATUSES = ("blocked", "usage_limited", "budget_limited")
RECENT_AUTOMATION_DAYS = 14
STALE_FACTOR = 3
MIN_STALE_SECONDS = 3600
UNDEPLOYED_HOURS = 24
TASK_HORIZON_DAYS = 7
TASK_PAGE = 25
TASK_MAX_PAGES = 8
# A task further past due than this is old backlog, not a change worth an
# alert. It is counted in the report so it stays visible in a dry run.
TASK_OVERDUE_ALERT_DAYS = 7
# Optional JSON file mapping a Fly app to its "owner/name" repo, for deployment
# records that do not name one. Private to the host, never committed.
REPO_MAP_ENV = "CODEX_DELIVERY_REPO_MAP"
TERMINAL_TASK_STATUSES = frozenset(
    {"done", "completed", "complete", "cancelled", "canceled", "closed", "dropped"}
)
MAX_NEW_ALERTS_PER_RUN = 10
BASELINE_LIST_PER_CHECK = 12
SUBPROCESS_TIMEOUT = 60

_RRULE_SECONDS = {
    "SECONDLY": 1,
    "MINUTELY": 60,
    "HOURLY": 3600,
    "DAILY": 86400,
    "WEEKLY": 7 * 86400,
}
_NON_PROD_LABELS = frozenset(
    {
        "dev",
        "staging",
        "stage",
        "test",
        "qa",
        "sandbox",
        "preview",
        "local",
        "localhost",
    }
)


@dataclass(frozen=True)
class Finding:
    key: str
    message: str


@dataclass
class CheckResult:
    name: str
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


# ── helpers ──────────────────────────────────────────────────────────────────


def humanize(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _ro_connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite file strictly read-only. Raises if it does not exist."""
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _thread_index(state_db: Path) -> dict[str, dict[str, Any]]:
    conn = _ro_connect(state_db)
    try:
        rows = conn.execute(
            "SELECT id, name, title, archived, updated_at_ms FROM threads"
        ).fetchall()
    finally:
        conn.close()
    return {
        r[0]: {
            "name": (r[1] or r[2] or "").strip() or "(unnamed session)",
            "archived": bool(r[3]),
            "updated_at_ms": r[4],
        }
        for r in rows
    }


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


# ── check 1: goals ───────────────────────────────────────────────────────────


def check_goals(goals_db: Path, state_db: Path, now_ms: int) -> list[Finding]:
    threads = _thread_index(state_db)
    conn = _ro_connect(goals_db)
    try:
        placeholders = ",".join("?" * len(BAD_GOAL_STATUSES))
        # The objective column is deliberately not selected: it may hold
        # private detail and no message needs it.
        rows = conn.execute(
            "SELECT thread_id, status, updated_at_ms FROM thread_goals "
            f"WHERE status IN ({placeholders})",
            BAD_GOAL_STATUSES,
        ).fetchall()
    finally:
        conn.close()
    findings: list[Finding] = []
    for thread_id, status, updated_ms in sorted(rows):
        thread = threads.get(thread_id)
        if thread and thread["archived"]:
            continue
        name = thread["name"] if thread else "(unnamed session)"
        age = humanize((now_ms - updated_ms) / 1000)
        label = status.replace("_", "-")
        findings.append(
            Finding(
                key=f"{KEY_PREFIX}:goal:{thread_id}:{status}",
                message=(
                    f'The goal in Codex session "{name}" has been {label} for '
                    f"{age}, and nothing will wake it. Open that session and run "
                    "the resume command."
                ),
            )
        )
    return findings


# ── check 2: automations ─────────────────────────────────────────────────────


def rrule_period_seconds(rrule: str) -> int | None:
    parts = dict(
        p.split("=", 1) for p in rrule.replace("RRULE:", "").split(";") if "=" in p
    )
    base = _RRULE_SECONDS.get(parts.get("FREQ", "").upper())
    if base is None:
        return None
    try:
        interval = int(parts.get("INTERVAL", "1"))
    except ValueError:
        return None
    return base * max(1, interval)


def check_automations(
    automations_dir: Path, state_db: Path, now_ms: int
) -> tuple[list[Finding], list[str]]:
    threads = _thread_index(state_db)
    findings: list[Finding] = []
    notes: list[str] = []
    recent_ms = RECENT_AUTOMATION_DAYS * 86400 * 1000
    for path in sorted(automations_dir.glob("*/automation.toml")):
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError(f"unreadable automation file ({type(exc).__name__})")
        auto_id = str(data.get("id") or path.parent.name)
        name = str(data.get("name") or auto_id)
        status = str(data.get("status", "")).upper()
        if status == "PAUSED":
            created = data.get("created_at")
            if isinstance(created, int) and now_ms - created <= recent_ms:
                since = humanize(
                    (now_ms - int(data.get("updated_at") or created)) / 1000
                )
                findings.append(
                    Finding(
                        key=f"{KEY_PREFIX}:automation-paused:{auto_id}",
                        message=(
                            f'The Codex watcher "{name}" was created in the last '
                            f"{RECENT_AUTOMATION_DAYS} days but has been paused for "
                            f"about {since}, so nothing is watching. Re-enable it in "
                            "Codex if the session is still waiting on something."
                        ),
                    )
                )
        elif status == "ACTIVE":
            period = rrule_period_seconds(str(data.get("rrule", "")))
            if period is None:
                notes.append(f"automation {auto_id}: schedule not understood, skipped")
                continue
            thread = threads.get(str(data.get("target_thread_id", "")))
            threshold = max(STALE_FACTOR * period, MIN_STALE_SECONDS)
            if thread is None:
                findings.append(
                    Finding(
                        key=f"{KEY_PREFIX}:automation-stale:{auto_id}",
                        message=(
                            f'The active Codex watcher "{name}" points at a session '
                            "that no longer exists, so its checks reach nobody. "
                            "Remove or retarget it in Codex."
                        ),
                    )
                )
                continue
            idle = (now_ms - int(thread["updated_at_ms"])) / 1000
            if idle > threshold:
                findings.append(
                    Finding(
                        key=f"{KEY_PREFIX}:automation-stale:{auto_id}",
                        message=(
                            f'The active Codex watcher "{name}" runs every '
                            f'{humanize(period)}, but its session "{thread["name"]}" '
                            f"has shown no activity for {humanize(idle)}. The watcher "
                            "may not be firing; open the session and check it."
                        ),
                    )
                )
    return findings, notes


# ── check 3: merged but not live ─────────────────────────────────────────────

_REPO_RE = re.compile(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?(?=[\s/'\"]|$)")


def _snapshot(entity: dict[str, Any]) -> dict[str, Any]:
    snap = entity.get("snapshot", entity)
    if isinstance(snap, dict) and isinstance(snap.get("snapshot"), dict):
        snap = snap["snapshot"]
    return snap if isinstance(snap, dict) else {}


def load_repo_map(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Read the optional app-to-repo map. A named file that cannot be read fails."""
    env = os.environ if environ is None else environ
    path = env.get(REPO_MAP_ENV, "").strip()
    if not path:
        return {}
    data = json.loads(Path(path).expanduser().read_text())
    if not isinstance(data, dict) or not all(
        isinstance(v, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", v)
        for v in data.values()
    ):
        raise ValueError(f"{REPO_MAP_ENV} must map app names to owner/name repos")
    return data


def _repo_for(
    snap: dict[str, Any], repo_map: dict[str, str] | None = None
) -> str | None:
    mapped = (repo_map or {}).get(str(snap.get("fly_app") or ""))
    if mapped:
        return mapped
    for fld in ("repo", "github_repo", "source_repo"):
        value = snap.get(fld)
        if isinstance(value, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", value.strip()):
            return value.strip()
    match = _REPO_RE.search(str(snap.get("deploy_command") or ""))
    return match.group(1) if match else None


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _latest_deploy(releases: Any) -> datetime | None:
    if not isinstance(releases, list):
        raise ValueError("unexpected release listing shape")
    best: datetime | None = None
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        status = str(rel.get("Status") or rel.get("status") or "").lower()
        if status and status not in ("complete", "succeeded", "success"):
            continue
        stamp = rel.get("CreatedAt") or rel.get("created_at") or rel.get("createdAt")
        if not stamp:
            continue
        ts = _parse_ts(str(stamp))
        if best is None or ts > best:
            best = ts
    return best


def _check_one_deploy(
    ent: dict[str, Any],
    app: str,
    repo: str,
    target: str,
    run: Callable[[list[str]], str],
    now: datetime,
) -> Finding | bool | None:
    """Finding if undeployed too long, False if fine, None if no release exists."""
    deployed = _latest_deploy(
        json.loads(run(["fly", "releases", "--app", app, "--json"]))
    )
    if deployed is None:
        return None
    branch = run(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"]).strip()
    since = deployed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = json.loads(
        run(
            [
                "gh",
                "api",
                f"repos/{repo}/commits?sha={urllib.parse.quote(branch)}"
                f"&since={since}&per_page=100",
            ]
        )
    )
    dates = [
        d
        for d in (_parse_ts(c["commit"]["committer"]["date"]) for c in commits)
        if d > deployed
    ]
    if not dates:
        return False
    waiting = (now - min(dates)).total_seconds()
    if waiting <= UNDEPLOYED_HOURS * 3600:
        return False
    return Finding(
        key=f"{KEY_PREFIX}:undeployed:{ent.get('entity_id') or app}",
        message=(
            f"{len(dates)} change(s) merged for {target} have not been deployed; "
            f"the oldest has waited {humanize(waiting)}. Deploy it using the "
            "instance's deployment configuration."
        ),
    )


def check_deploys(
    configs: list[dict[str, Any]],
    run: Callable[[list[str]], str],
    now: datetime,
    repo_map: dict[str, str] | None = None,
) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    notes: list[str] = []
    checkable = 0
    for ent in configs:
        snap = _snapshot(ent)
        label = str(snap.get("project") or snap.get("environment") or "a target")
        env = snap.get("environment")
        target = f"{label} ({env})" if env and snap.get("project") else label
        app = snap.get("fly_app")
        repo = _repo_for(snap, repo_map)
        if not app or not repo:
            notes.append(f"{target}: not checkable (needs a Fly app and a repo)")
            continue
        try:
            outcome = _check_one_deploy(ent, app, repo, target, run, now)
        except Exception as exc:  # noqa: BLE001 — one target must not hide the rest
            # An alert, not a note: a target that could not be read must never
            # look the same as a target with nothing waiting.
            findings.append(
                Finding(
                    key=f"{KEY_PREFIX}:deploy-unreadable:{ent.get('entity_id') or app}",
                    message=(
                        f"Could not check whether {target} is deployed "
                        f"({type(exc).__name__}: {str(exc)[:80]}). This is not an "
                        "all-clear."
                    ),
                )
            )
            continue
        if outcome is None:
            notes.append(f"{target}: not checkable (no completed release found)")
            continue
        checkable += 1
        if outcome is not False:
            findings.append(outcome)
    if configs and checkable == 0:
        raise RuntimeError(
            f"none of {len(configs)} deployment targets could be checked "
            "(each needs a Fly app and a readable repo)"
        )
    return findings, notes


# ── check 4: tasks ───────────────────────────────────────────────────────────


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def check_tasks(
    fetch_page: Callable[[int], tuple[list[dict[str, Any]], bool]], now: datetime
) -> tuple[list[Finding], list[str]]:
    """``fetch_page(offset) -> (rows, more)``; reads are sequential and bounded."""
    findings: list[Finding] = []
    notes: list[str] = []
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = today + timedelta(days=TASK_HORIZON_DAYS)
    offset = 0
    more = False
    old_overdue = 0
    for _ in range(TASK_MAX_PAGES):
        rows, more = fetch_page(offset)
        offset += len(rows)
        for ent in rows:
            snap = _snapshot(ent)
            status = str(snap.get("status") or "").strip().lower()
            if status in TERMINAL_TASK_STATUSES:
                continue
            due = _parse_date(snap.get("due_date"))
            if due is None:
                continue
            ident = ent.get("entity_id") or snap.get("entity_id") or snap.get("title")
            title = str(
                snap.get("title") or ent.get("canonical_name") or "an untitled task"
            )
            blocked = status == "blocked" or bool(
                str(snap.get("blocked_reason") or "").strip()
            )
            if due < today:
                days = (today - due).days
                if days > TASK_OVERDUE_ALERT_DAYS:
                    old_overdue += 1
                    continue
                findings.append(
                    Finding(
                        key=f"{KEY_PREFIX}:task-overdue:{ident}",
                        message=(
                            f'Task "{title}" is {days} day(s) past its due date and '
                            "not done. Finish it, move the date, or close it."
                        ),
                    )
                )
            elif due <= horizon and blocked:
                findings.append(
                    Finding(
                        key=f"{KEY_PREFIX}:task-blocked:{ident}",
                        message=(
                            f'Task "{title}" is due {due.date().isoformat()} and is '
                            "blocked. Clear the blocker or move the date."
                        ),
                    )
                )
        if not more or not rows:
            more = False
            break
    if old_overdue:
        notes.append(
            f"tasks: {old_overdue} more than {TASK_OVERDUE_ALERT_DAYS} days past due "
            "(backlog, not alerted)"
        )
    if more:
        notes.append(
            f"tasks: stopped after {TASK_MAX_PAGES * TASK_PAGE} rows; older entries not examined"
        )
    return findings, notes


def assert_prod_target(base_url: str) -> str:
    """Return the normalized base URL, or raise if it is not a prod target."""
    parsed = urllib.parse.urlparse(base_url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise RuntimeError(
            "Neotoma target is not an https URL; refusing a non-prod target"
        )
    labels = set(host.split("."))
    if labels & _NON_PROD_LABELS or host in ("127.0.0.1", "::1"):
        raise RuntimeError("Neotoma target looks like a non-prod instance; refusing")
    return f"{parsed.scheme}://{parsed.netloc}"


class NeotomaReader:
    """Minimal read-only prod client. Credentials come only from the environment."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        base = (env.get("NEOTOMA_BASE_URL") or "").strip()
        token = (env.get("NEOTOMA_BEARER_TOKEN") or "").strip()
        if not base or not token:
            raise RuntimeError("NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN not set")
        self._base = assert_prod_target(base)
        self._token = token

    def query(self, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self._base}/entities/query",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "ateles-neotoma-sync/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Neotoma read failed: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f"Neotoma read failed: {type(exc).__name__}") from None

    def deployment_configs(self) -> list[dict[str, Any]]:
        res = self.query(
            {
                "entity_type": "deployment_configuration",
                "include_snapshots": True,
                "limit": 50,
            }
        )
        return list(res.get("entities") or [])

    def task_page_fetcher(
        self, now: datetime
    ) -> Callable[[int], tuple[list[dict[str, Any]], bool]]:
        horizon = (now + timedelta(days=TASK_HORIZON_DAYS)).strftime("%Y-%m-%d")

        def fetch(offset: int) -> tuple[list[dict[str, Any]], bool]:
            res = self.query(
                {
                    "entity_type": "task",
                    "include_snapshots": True,
                    "limit": TASK_PAGE,
                    "offset": offset,
                    "sort_by": "snapshot.due_date",
                    "sort_order": "desc",
                    "snapshot_filters": {
                        "due_date": {"op": "lte", "value": horizon + "T23:59:59"}
                    },
                }
            )
            rows = list(res.get("entities") or [])
            total = res.get("total")
            more = bool(rows) and (total is None or offset + len(rows) < total)
            return rows, more

        return fetch


# ── orchestration ────────────────────────────────────────────────────────────


def _child_env(argv: list[str]) -> dict[str, str]:
    """The environment for a child read: no App keys, plus a token where needed.

    A ``gh api repos/<owner>/<name>`` read gets a short-lived App token. The
    ambient token from the daemon's dotenv cannot see private repos, and a
    private repo answers it with an error that used to read as a failed check.
    """
    from lib.github_app_token import token_env, without_app_keys

    env = without_app_keys(dict(os.environ))
    if argv[0] != "gh":
        return env
    for arg in argv:
        match = re.match(r"repos/([\w.-]+/[\w.-]+)", arg)
        if match:
            return {**env, **token_env(match.group(1))}
    return env


def run_command(argv: list[str]) -> str:
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
        env=_child_env(argv),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {proc.returncode}")
    return proc.stdout


def _guard(name: str, fn: Callable[[], tuple[list[Finding], list[str]]]) -> CheckResult:
    try:
        findings, notes = fn()
        return CheckResult(name, findings, None, notes)
    except Exception as exc:  # noqa: BLE001 — a failed read must never look like all-clear
        return CheckResult(name, error=f"{type(exc).__name__}: {str(exc)[:160]}")


def run_checks(
    *,
    now: datetime,
    home: Path,
    reader_factory: Callable[[], NeotomaReader] = NeotomaReader,
    run: Callable[[list[str]], str] = run_command,
) -> list[CheckResult]:
    now_ms = int(now.timestamp() * 1000)
    goals_db = home / "goals_1.sqlite"
    state_db = home / "state_5.sqlite"
    results = [
        _guard("codex goals", lambda: (check_goals(goals_db, state_db, now_ms), [])),
        _guard(
            "codex automations",
            lambda: check_automations(home / "automations", state_db, now_ms),
        ),
    ]
    reader_box: list[NeotomaReader] = []

    def reader() -> NeotomaReader:
        if not reader_box:
            reader_box.append(reader_factory())
        return reader_box[0]

    results.append(
        _guard(
            "merged-but-not-deployed",
            lambda: check_deploys(
                reader().deployment_configs(), run, now, load_repo_map()
            ),
        )
    )
    results.append(
        _guard(
            "due and blocked tasks",
            lambda: check_tasks(reader().task_page_fetcher(now), now),
        )
    )
    return results


def failure_key(name: str) -> str:
    return f"{KEY_PREFIX}:check-failed:{name.replace(' ', '-')}"


def failure_finding(result: CheckResult) -> Finding:
    return Finding(
        key=failure_key(result.name),
        message=(
            f"Could not check {result.name} ({result.error}). This is not an "
            "all-clear; the hourly delivery check will retry."
        ),
    )


def default_state_path() -> Path:
    override = os.environ.get("CODEX_DELIVERY_STATE_PATH", "").strip()
    return (
        Path(override)
        if override
        else Path.home() / ".local/state/ateles/codex-delivery-check.json"
    )


def load_state(path: Path) -> dict[str, list[str]]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: list(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def save_state(path: Path, state: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.replace(path)


def _alert(notifier: Any, message: str, priority: Any, key: str | None = None) -> bool:
    """Send one alert. False means the operator did not get it and will not.

    ``send()`` records a dedupe key before routing and returns False for a
    notice held through the silence window and for a failed delivery alike. A
    held notice goes out later, so it counts as told. A failed one has its key
    cleared, and the caller keeps it out of state so the next run tries again.
    """
    if notifier.send(message, priority, handler=HANDLER, dedupe_key=key):
        return True
    if notifier.in_silence_window():
        return True
    if key:
        notifier.clear_dedupe(key)
    print(f"ALERT NOT DELIVERED, will retry: {message[:200]}", file=sys.stderr)
    return False


def isolate_notifier_env(state_path: Path) -> Path:
    """Pin this job's notifier settings; return the job's own held-queue path.

    Assigned directly, never ``setdefault``: the shared dotenv or a global
    setting must not win. The held queue defaults to a file every daemon's
    notifier shares and drains on each send, so this job would otherwise email
    other daemons' held notices. ``ATELES_SWARM_EMAIL`` is blanked so no
    ``--from`` is passed: the dotenv alias is the operator's own account, whose
    mail to itself lands in Sent, and without ``--from`` gws sends as the
    swarm mailbox it is signed in as.
    """
    queue = state_path.with_name("codex-delivery-held.json")
    os.environ["ATELES_DIGEST_QUEUE_PATH"] = str(queue)
    os.environ["ATELES_SWARM_EMAIL"] = ""
    return queue


def release_held_notices(notifier: Any) -> bool:
    """Send notices held overnight, once the silence window is over.

    The notifier drains its queue only on a send, and a notice held in silence
    counts as told, so with no later alert it would never go out. A failed
    delivery stays queued (``flush_digest`` keeps it).
    """
    if notifier.in_silence_window():
        return False
    return bool(notifier.flush_digest())


def apply_results(
    results: list[CheckResult],
    state: dict[str, list[str]],
    notifier: Any,
    priority: Any,
    max_new: int = MAX_NEW_ALERTS_PER_RUN,
    baseline: bool = False,
) -> dict[str, list[str]]:
    """Send alerts for new findings, clear keys that resolved, return new state.

    A check that failed keeps its previous keys (its true state is unknown, so
    nothing is cleared) and raises a "could not check" alert in their place.

    On the first run (``baseline``) everything already open is reported in one
    message and recorded, so later runs alert only on change. A failed check
    still alerts: a failure is never folded into a baseline. If that message
    cannot be delivered the returned state is empty, so the next run is a first
    run again.

    An alert that could not be delivered is left out of the returned state, so
    it is retried on the next run.
    """
    new_state: dict[str, list[str]] = {}
    sent = 0
    overflow: dict[str, set[str]] = {}
    summary: list[str] = []
    for result in results:
        previous = set(state.get(result.name, []))
        fail_key = failure_key(result.name)
        if not result.ok:
            told = fail_key in previous or _alert(
                notifier, failure_finding(result).message, priority, fail_key
            )
            new_state[result.name] = sorted(previous | ({fail_key} if told else set()))
            continue
        if baseline:
            new_state[result.name] = sorted(f.key for f in result.findings)
            summary.append(f"{result.name}: {len(result.findings)}")
            summary.extend(
                f"- {f.message}" for f in result.findings[:BASELINE_LIST_PER_CHECK]
            )
            hidden = len(result.findings) - BASELINE_LIST_PER_CHECK
            if hidden > 0:
                summary.append(f"- and {hidden} more")
            continue
        if fail_key in previous:
            notifier.clear_dedupe(fail_key)
            previous.discard(fail_key)
        current = {f.key: f for f in result.findings}
        kept: set[str] = set()
        for key in sorted(current):
            if key in previous:
                kept.add(key)
            elif sent < max_new:
                sent += 1
                if _alert(notifier, current[key].message, priority, key):
                    kept.add(key)
            else:
                overflow.setdefault(result.name, set()).add(key)
        for resolved in previous - set(current):
            notifier.clear_dedupe(resolved)
        new_state[result.name] = sorted(kept)
    if summary and not _alert(
        notifier,
        "Delivery check started. Already open:\n"
        + "\n".join(summary)
        + "\nYou will hear again only when something changes.",
        priority,
    ):
        return {}
    if overflow and _alert(
        notifier,
        f"{sum(len(keys) for keys in overflow.values())} more delivery findings "
        f"were found this run; only the first {max_new} are listed individually. "
        "Run the delivery check with --once --dry-run to see them all.",
        priority,
    ):
        # Told by the summary, so these are not sent again on later runs.
        for name, keys in overflow.items():
            new_state[name] = sorted(set(new_state[name]) | keys)
    return new_state


def render_report(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        if not r.ok:
            lines.append(f"[{r.name}] COULD NOT CHECK: {r.error}")
            continue
        lines.append(f"[{r.name}] {len(r.findings)} finding(s)")
        lines.extend(f"  - {f.message}" for f in r.findings)
        lines.extend(f"  note: {n}" for n in r.notes)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    # Importing lib.daemon_runtime loads the materialized dotenv into the
    # environment (the repo's existing credential path). Nothing is printed.
    repo_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo_root))
    import lib.daemon_runtime  # noqa: F401,E402

    results = run_checks(now=datetime.now(timezone.utc), home=codex_home())
    print(render_report(results))
    failed = any(not r.ok for r in results)
    if args.dry_run:
        return 1 if failed else 0

    state_path = default_state_path()
    os.environ.setdefault(
        "ATELES_DEDUPE_JOURNAL_PATH",
        str(state_path.with_name("codex-delivery-dedupe.json")),
    )
    isolate_notifier_env(state_path)
    from lib.notify.notifier import Notifier, Priority

    state = load_state(state_path)
    notifier = Notifier.from_neotoma()
    release_held_notices(notifier)
    new_state = apply_results(
        results,
        state,
        notifier,
        Priority.WARN,
        baseline=not state,
    )
    save_state(state_path, new_state)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

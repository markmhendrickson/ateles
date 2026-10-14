#!/usr/bin/env python3
"""Swarm-operations baseline: derived reads over existing records (build-plan step 4).

Computes weekly readings for S1 time_to_completion, S2 dispatch_failure_rate,
S4 review_rounds_per_pr, S5 cost_per_task (dispatch minutes, a proxy) and the
interim S3 operator-intervention proxies, from records the swarm already
writes: ``harness_event`` (subprocess dispatches), ``task`` (status changes),
``checkpoint_brief`` (resolved checkpoints), ``decision_shape_check`` events and
GitHub pull-request reviews. Nothing here writes to those records.

Why observations, not snapshots: ``harness_event`` rows of one agent resolve to
ONE entity (identity falls back to the agent), so a snapshot shows only the
latest dispatch while the history lives in that entity's observations. Every
read below goes through observations.

    python3 execution/scripts/metrics/swarm_ops_baseline.py fetch      # refresh local caches (slow, sequential)
    python3 execution/scripts/metrics/swarm_ops_baseline.py compute --swarm-artifact swarm:<name>
    python3 execution/scripts/metrics/swarm_ops_baseline.py store   --swarm-artifact swarm:<name> [--dry-run]

The swarm label (``--swarm-artifact`` or METRICS_SWARM_ARTIFACT) names the artifact the swarm readings are
stored under; it is an argument so another operator's swarm is not mislabelled. The first week defaults to
the earliest observation in the caches (``--first-week YYYY-MM-DD`` overrides).

Absence is not zero. A week with no source rows has no reading. The caches carry a fetch time
(``meta.json``): windows end at the last complete week before the OLDEST fetch, readings are stamped with
the fetch time of their own stream, and a cache older than ``--max-cache-age-days`` is refused unless
``--allow-stale``, which stores every reading as partial. A clean rerun after such a run rewrites status
and coverage but does not clear its ``coverage_note`` (an absent field is not written, so the old note
would stay on the stored reading); treat a note beginning "cache older than" on a reading with status ok as
left over from that run.

Needs NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN in the process for ``fetch`` and
``store`` (never printed). ``fetch --only reviews`` reads pull-request reviews
from the public GitHub API (GITHUB_TOKEN from the process if set; never
printed). Reads are sequential: hosted Neotoma is fragile. Cost is reported in
dispatch wall-clock minutes (a proxy); tokens only where a harness reported
them, and a missing token field is "not metered", never zero.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from execution.scripts.metrics import neotoma_io, repo_baseline, weekly  # noqa: E402

COLLECTOR = "swarm_ops_baseline.py"
# Only the fields a derivation reads are cached (the rest, such as task titles, is personal data
# the metrics do not need).
SUBPROCESS_KEEP = ("event_type", "event_at", "tool_name", "success", "task_entity_id", "duration_ms",
                   "total_tokens")
TASK_KEEP = ("status", "action_type", "assigned_to")
CHECKPOINT_KEEP = ("status",)
SHAPE_KEEP = ("event_type", "event_at")

# Weeks with fewer than this many completed dispatches (or done tasks) are stored
# with coverage 0.5 so they stay visible but stay out of baseline summaries.
MIN_DISPATCHES = 20
MIN_DONE_TASKS = 5
MIN_REVIEWED_PRS = 3
CACHE_MAX_AGE_DAYS = 7
# Checkpoint statuses that record a decision. The record carries no resolver identity, so this is the
# closest honest proxy for "an operator decided": machine approvals (approved_via_auto_build,
# approved_by_dispatch), pending_* states, superseded, resolved and prepared are deliberately absent.
CHECKPOINT_DECIDED_STATUSES = {"approved", "approved_no_release", "rejected", "redirected"}
# A task is swarm work when any observation carries swarm routing metadata.
SWARM_TASK_MARKERS = ("assigned_to", "action_type")
DONE_STATUSES = {"done", "completed"}
STREAMS = ("subprocess", "tasks", "checkpoints", "shape_checks", "reviews")


# --- fetch -------------------------------------------------------------------

def _entities_of_event_type(event_type: str, requester) -> list[dict[str, Any]]:
    ents = neotoma_io.query_all("harness_event", page=100, requester=requester,
                                snapshot_filters={"event_type": {"op": "eq", "value": event_type}})
    print(f"  harness_event entities with event_type {event_type}: {len(ents)}", file=sys.stderr)
    if not ents:
        # A known-positive check: an empty source is far more likely a broken query than an idle swarm.
        raise RuntimeError(f"no harness_event entities with event_type {event_type!r}; refusing to cache an empty history")
    return ents


def fetch_subprocess(requester=neotoma_io.request) -> list[dict[str, Any]]:
    """All subprocess dispatch observations, one entity at a time (sequential)."""
    rows: list[dict[str, Any]] = []
    for ent in _entities_of_event_type("subprocess", requester):
        rows += neotoma_io.fetch_entity_observations(ent["entity_id"], SUBPROCESS_KEEP,
                                                     requester=requester)
        print(f"  subprocess {ent['entity_id']}: {len(rows)} rows so far", file=sys.stderr)
    return rows


def fetch_shape_checks(requester=neotoma_io.request) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ent in _entities_of_event_type("decision_shape_check", requester):
        rows += neotoma_io.fetch_entity_observations(ent["entity_id"], SHAPE_KEEP, requester=requester)
    return rows


def cache_paths(cache_dir: Path) -> dict[str, Path]:
    return {name: cache_dir / f"{name}.jsonl" for name in STREAMS}


def fetch_all(cache_dir: Path, *, only: set[str] | None = None) -> None:
    paths = cache_paths(cache_dir)
    plan = {
        "subprocess": fetch_subprocess,
        "tasks": lambda: neotoma_io.fetch_observation_stream("task", TASK_KEEP),
        "checkpoints": lambda: neotoma_io.fetch_observation_stream("checkpoint_brief", CHECKPOINT_KEEP),  # vocab-ok: live entity type until the checkpoint rename migrates
        "shape_checks": fetch_shape_checks,
    }
    for name, fn in plan.items():
        if only and name not in only:
            continue
        print(f"fetching {name} ...", file=sys.stderr)
        rows = fn()
        neotoma_io.write_cache(paths[name], rows)
        print(f"  {name}: {len(rows)} rows -> {paths[name]}", file=sys.stderr)


def stream_fetched_at(cache_dir: Path) -> dict[str, datetime]:
    """When each cache file was fetched: the sidecar's stamp, else the file's modification time."""
    meta = neotoma_io.read_meta(cache_dir)
    out: dict[str, datetime] = {}
    for name, path in cache_paths(cache_dir).items():
        stamp = (meta.get(name) or {}).get("fetched_at")
        if stamp:
            out[name] = weekly.parse_ts(stamp)
        elif path.exists():
            out[name] = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return out


def stale_streams(fetched_at: dict[str, datetime], now: datetime, max_age_days: float) -> list[str]:
    """Names of caches older than the freshness limit."""
    return sorted(n for n, t in fetched_at.items() if (now - t) > timedelta(days=max_age_days))


# --- GitHub review fetch (S4) -------------------------------------------------------

def fetch_reviews(repos: list[str], cutoff: datetime, existing: list[dict[str, Any]],
                  fetch=None) -> list[dict[str, Any]]:
    """Merged PRs since ``cutoff`` with the commit ids their formal reviews attach to.

    Incremental: PRs already in ``existing`` are not re-read. Public GitHub API.
    """
    getter = fetch or repo_baseline.http_get
    have = {(r["repo"], r["number"]) for r in existing}
    out = list(existing)
    for repo in repos:
        repo_baseline.validate_repo(repo)
        url = (f"{repo_baseline.GITHUB_API}/repos/{repo}/pulls?state=closed&sort=updated"
               f"&direction=desc&per_page=100")
        for pr in repo_baseline.paginate(url, fetch=getter):
            if weekly.parse_ts(pr["updated_at"]) < cutoff:
                break
            if not pr.get("merged_at") or (repo, pr["number"]) in have:
                continue
            reviews = list(repo_baseline.paginate(
                f"{repo_baseline.GITHUB_API}/repos/{repo}/pulls/{pr['number']}/reviews?per_page=100",
                fetch=getter))
            out.append({"repo": repo, "number": pr["number"], "merged_at": pr["merged_at"],
                        "review_commits": parse_review_commits(reviews)})
    return out


def parse_review_commits(reviews: list[dict[str, Any]]) -> list[str]:
    """Distinct head commits that received a formal review (PENDING drafts excluded)."""
    return sorted({r["commit_id"] for r in reviews
                   if r.get("commit_id") and (r.get("state") or "").upper() != "PENDING"})


# --- derived reads (pure) ---------------------------------------------------------------

def completions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Completed dispatches from subprocess observations.

    A dispatch writes a start row (success ``partial``, no duration) and a
    completion row (success ``true``/``false``). Only completions are counted;
    a failure with no duration (launch failure) is still a completed, failed
    dispatch with unknown duration.
    """
    out = []
    for r in rows:
        f = r["fields"]
        if f.get("event_type", "subprocess") != "subprocess" or f.get("success") not in ("true", "false"):
            continue
        out.append({
            "at": weekly.parse_ts(f.get("event_at") or r["observed_at"]),
            "role": (f.get("tool_name") or "unknown").split(":")[-1],
            "provider": (f.get("tool_name") or "unknown:").split(":")[0],
            "ok": f["success"] == "true",
            "minutes": f["duration_ms"] / 60000 if f.get("duration_ms") is not None else None,
            "task": f.get("task_entity_id") or None,
            "tokens": f.get("total_tokens"),
        })
    return out


def _bucket(items: list[dict[str, Any]], key: str, windows) -> list[list[dict[str, Any]]]:
    return [[i for i in items if s <= i[key] < e] for s, e in windows]


def weekly_dispatch_rows(comps: list[dict[str, Any]], windows) -> list[dict[str, Any]]:
    """S2 and S5 inputs per window; windows with no dispatch at all are skipped (no data, not zero)."""
    out = []
    for (start, end), items in zip(windows, _bucket(comps, "at", windows)):
        if not items:
            continue
        failed = [i for i in items if not i["ok"]]
        timed = [i for i in items if i["minutes"] is not None]
        by_role: dict[str, dict[str, Any]] = defaultdict(lambda: {"dispatches": 0, "failed": 0})
        for i in items:
            by_role[i["role"]]["dispatches"] += 1
            by_role[i["role"]]["failed"] += 0 if i["ok"] else 1
        tasks = {i["task"] for i in items if i["task"]}
        task_minutes = [i["minutes"] for i in items if i["task"] and i["minutes"] is not None]
        tokens = [i["tokens"] for i in items if i["tokens"] is not None]
        out.append({
            "start": start, "end": end, "n": len(items),
            "failure_rate": len(failed) / len(items),
            "minutes_per_dispatch": (sum(i["minutes"] for i in timed) / len(timed)) if timed else None,
            "total_minutes": sum(i["minutes"] for i in timed),
            "timed": len(timed),
            "tasks_identified": len(tasks),
            "minutes_per_identified_task": (sum(task_minutes) / len(tasks)) if tasks and task_minutes else None,
            "failed": len(failed),
            "by_role": dict(sorted(by_role.items())),
            "tokens_reported_dispatches": len(tokens),
            "total_tokens_reported": sum(tokens) if tokens else None,
        })
    return out


def normalize_kind(value: Any) -> str:
    """action_type is a string on most tasks but a list on some; make it one hashable label."""
    if isinstance(value, (list, tuple)):
        parts = sorted({str(v) for v in value if v})
        return "+".join(parts) or "unknown"
    return str(value) if value else "unknown"


def task_lifecycles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Swarm tasks that reached done: intake time, done time and kind.

    Intake is the task's first observation; done is the first observation whose
    status is a done status. A task first recorded already done has no measurable
    duration: it is flagged ``created_done`` and ``weekly_task_rows`` counts it per week.
    """
    by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_entity[r["entity_id"]].append(r)
    out = []
    for obs in by_entity.values():
        obs.sort(key=lambda r: r["observed_at"])
        if not any(any(m in o["fields"] for m in SWARM_TASK_MARKERS) for o in obs):
            continue
        first = weekly.parse_ts(obs[0]["observed_at"])
        done = next((o for o in obs if str(o["fields"].get("status") or "").lower() in DONE_STATUSES), None)
        if done is None:
            continue
        done_at = weekly.parse_ts(done["observed_at"])
        kind = normalize_kind(next((o["fields"]["action_type"] for o in reversed(obs)
                                    if o["fields"].get("action_type")), None))
        out.append({"intake": first, "done": done_at, "kind": kind,
                    "hours": (done_at - first).total_seconds() / 3600,
                    "created_done": (done["observed_at"] == obs[0]["observed_at"])})
    return out


def weekly_task_rows(lifecycles: list[dict[str, Any]], windows) -> list[dict[str, Any]]:
    """S1 inputs per window. Tasks first recorded already done have no measurable duration; they are
    left out of the median and counted per window so the reading's coverage is visible."""
    measurable = [t for t in lifecycles if not t["created_done"]]
    unmeasurable = [t for t in lifecycles if t["created_done"]]
    out = []
    for (start, end), items, skipped in zip(windows, _bucket(measurable, "done", windows),
                                            _bucket(unmeasurable, "done", windows)):
        if not items:
            continue
        kinds: dict[str, list[float]] = defaultdict(list)
        for t in items:
            kinds[t["kind"]].append(t["hours"])
        hours = sorted(t["hours"] for t in items)
        p90 = hours[min(len(hours) - 1, int(0.9 * len(hours)))]
        out.append({"start": start, "end": end, "n": len(items), "median_hours": weekly.median(hours),
                    "p90_hours": p90, "first_recorded_done": len(skipped),
                    "by_kind": {k: {"n": len(v), "median_hours": weekly.median(v)} for k, v in sorted(kinds.items())}})
    return out


def resolved_checkpoints(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per checkpoint, at the time its status first became a recorded decision.

    Only ``CHECKPOINT_DECIDED_STATUSES`` count. Machine approvals and pending
    states do not: they are not an operator intervening.
    """
    by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_entity[r["entity_id"]].append(r)
    out = []
    for obs in by_entity.values():
        obs.sort(key=lambda r: r["observed_at"])
        for o in obs:
            status = str(o["fields"].get("status") or "").lower()
            if status in CHECKPOINT_DECIDED_STATUSES:
                out.append({"at": weekly.parse_ts(o["observed_at"]), "status": status})
                break
    return out


def weekly_intervention_rows(decided: list[dict[str, Any]], checkpoint_rows: list[dict[str, Any]],
                             shape_rows: list[dict[str, Any]], windows) -> list[dict[str, Any]]:
    """S3p inputs per window, for windows where the checkpoint source has any record at all.

    A window with decisions is counted; a window whose source shows activity but
    no decision is a measured zero; a window with no checkpoint record (before the
    source existed, or after the cache's last row) has no reading. Absence is not zero.
    """
    activity = [{"at": weekly.parse_ts(r["observed_at"])} for r in checkpoint_rows]
    shape = [{"at": weekly.parse_ts(r["fields"].get("event_at") or r["observed_at"])} for r in shape_rows
             if r["fields"].get("event_type", "decision_shape_check") == "decision_shape_check"]
    first_shape = min((s["at"] for s in shape), default=None)
    out = []
    for (start, end), cps, act, sh in zip(windows, _bucket(decided, "at", windows),
                                          _bucket(activity, "at", windows), _bucket(shape, "at", windows)):
        if not act:
            continue
        out.append({"start": start, "end": end, "resolved_checkpoints": len(cps),
                    "checkpoint_records": len(act),
                    "decision_shape_checks": len(sh) if first_shape is not None and end > first_shape
                    else "not recorded"})
    return out


def weekly_review_rows(prs: list[dict[str, Any]], windows) -> list[dict[str, Any]]:
    keyed = [{"at": weekly.parse_ts(p["merged_at"]), **p} for p in prs]
    out = []
    for (start, end), items in zip(windows, _bucket(keyed, "at", windows)):
        if not items:
            continue
        reviewed = [p for p in items if p["review_commits"]]
        out.append({"start": start, "end": end, "merged": len(items), "reviewed": len(reviewed),
                    "unreviewed": len(items) - len(reviewed),
                    "rounds_mean": (sum(len(p["review_commits"]) for p in reviewed) / len(reviewed)) if reviewed else None,
                    "rounds_max": max((len(p["review_commits"]) for p in reviewed), default=0)})
    return out


# --- readings ---------------------------------------------------------------------------------

def first_observed(*row_sets: list[dict[str, Any]], review_prs: list[dict[str, Any]] | None = None) -> datetime | None:
    """Earliest timestamp across the source rows; the default start of the weekly series."""
    stamps = []
    for rows in row_sets:
        stamps += [weekly.parse_ts(r["observed_at"]) for r in rows if r.get("observed_at")]
    stamps += [weekly.parse_ts(p["merged_at"]) for p in review_prs or [] if p.get("merged_at")]
    return min(stamps) if stamps else None


def mark_stale(readings: list[dict[str, Any]], note: str) -> list[dict[str, Any]]:
    """Downgrade every reading to partial (coverage 0.5) with a note, for a deliberately stale cache."""
    out = []
    for r in readings:
        r = dict(r)
        r["coverage_ratio"] = min(r.get("coverage_ratio", 1.0), 0.5)
        r["status"] = "partial"
        r["coverage_note"] = "; ".join(x for x in (r.get("coverage_note"), note) if x)
        out.append(r)
    return out


def build_readings(*, subprocess_rows, task_rows, checkpoint_rows, shape_rows, review_prs, now: datetime,
                   swarm_artifact: str, fetched_at: dict[str, datetime] | None = None,
                   first_week: datetime | None = None) -> list[dict[str, Any]]:
    """Weekly readings for the swarm metrics.

    ``fetched_at`` maps a stream name to when its cache was fetched. Windows stop at
    the last complete week before the OLDEST fetch (a stale cache cannot reach later
    weeks), and each reading's ``sourced_at`` is its own stream's fetch time. Both
    default to ``now`` for a live read.
    """
    fetched = {name: (fetched_at or {}).get(name, now) for name in STREAMS}
    as_of = min(fetched.values())
    first = first_week or first_observed(subprocess_rows, task_rows, checkpoint_rows, shape_rows,
                                         review_prs=review_prs)
    if first is None:
        return []
    windows = weekly.weeks_between(first, as_of)
    mk = weekly.make_reading
    readings: list[dict[str, Any]] = []

    comps = completions(subprocess_rows)
    dispatch_rows = weekly_dispatch_rows(comps, windows)
    for r in dispatch_rows:
        thin = r["n"] < MIN_DISPATCHES
        cov, note = (0.5, f"fewer than {MIN_DISPATCHES} completed dispatches; rate unreliable") if thin else (1.0, "")
        readings.append(mk(
            metric_id="S2_dispatch_failure_rate", artifact=swarm_artifact, start=r["start"], end=r["end"],
            value=r["failure_rate"], unit="ratio", collector=COLLECTOR, sourced_at=fetched["subprocess"],
            coverage_ratio=cov, coverage_note=note, probe_ok=len(comps) > 0,
            detail={"completed_dispatches": r["n"], "failed": r["failed"], "by_role": r["by_role"]}))
        if r["minutes_per_dispatch"] is not None:
            detail = {"completed_dispatches": r["n"], "timed_dispatches": r["timed"],
                      "total_minutes": round(r["total_minutes"], 2),
                      "tasks_identified": r["tasks_identified"],
                      "tokens_reported_dispatches": r["tokens_reported_dispatches"],
                      # A partial sum: only the dispatches counted just above reported tokens at all.
                      "tokens_reported_sum": ("not metered" if r["total_tokens_reported"] is None
                                              else r["total_tokens_reported"])}
            if r["minutes_per_identified_task"] is not None:
                detail["minutes_per_identified_task"] = round(r["minutes_per_identified_task"], 2)
            readings.append(mk(
                metric_id="S5_cost_per_task", artifact=swarm_artifact, start=r["start"], end=r["end"],
                value=r["minutes_per_dispatch"], unit="minutes_per_dispatch", collector=COLLECTOR,
                sourced_at=fetched["subprocess"], coverage_ratio=cov, coverage_note=note,
                probe_ok=len(comps) > 0, detail=detail))

    lifecycles = task_lifecycles(task_rows)
    for r in weekly_task_rows(lifecycles, windows):
        thin = r["n"] < MIN_DONE_TASKS
        readings.append(mk(
            metric_id="S1_time_to_completion", artifact=swarm_artifact, start=r["start"], end=r["end"],
            value=r["median_hours"], unit="hours", collector=COLLECTOR, sourced_at=fetched["tasks"],
            coverage_ratio=0.5 if thin else 1.0,
            coverage_note=f"fewer than {MIN_DONE_TASKS} tasks done in the week" if thin else "",
            probe_ok=len(lifecycles) > 0,
            detail={"tasks_done": r["n"], "first_recorded_done_excluded": r["first_recorded_done"],
                    "p90_hours": round(r["p90_hours"], 2), "by_kind": r["by_kind"]}))

    decided = resolved_checkpoints(checkpoint_rows)
    for r in weekly_intervention_rows(decided, checkpoint_rows, shape_rows, windows):
        readings.append(mk(
            metric_id="S3p_operator_intervention_proxy", artifact=swarm_artifact, start=r["start"], end=r["end"],
            value=r["resolved_checkpoints"], unit="events", collector=COLLECTOR,
            sourced_at=fetched["checkpoints"], probe_ok=len(decided) > 0,
            detail={"checkpoint_records_in_week": r["checkpoint_records"],
                    "decision_shape_checks": r["decision_shape_checks"],
                    "counts_statuses": sorted(CHECKPOINT_DECIDED_STATUSES),
                    "resolver_identity_recorded": False,
                    "method": "decided_status_allowlist",
                    "note": "proxy: checkpoints whose status records a decision; S3 proper needs principal attribution on writes"}))

    for repo in sorted({p["repo"] for p in review_prs}):
        for r in weekly_review_rows([p for p in review_prs if p["repo"] == repo], windows):
            if r["rounds_mean"] is None:
                continue
            thin = r["reviewed"] < MIN_REVIEWED_PRS
            readings.append(mk(
                metric_id="S4_review_rounds_per_pr", artifact=f"github:{repo}", start=r["start"], end=r["end"],
                value=r["rounds_mean"], unit="rounds", collector=COLLECTOR, sourced_at=fetched["reviews"],
                coverage_ratio=0.5 if thin else 1.0,
                coverage_note=f"fewer than {MIN_REVIEWED_PRS} reviewed PRs merged in the week" if thin else "",
                probe_ok=True,
                detail={"merged_prs": r["merged"], "reviewed_prs": r["reviewed"],
                        "unreviewed_prs": r["unreviewed"], "rounds_max": r["rounds_max"],
                        "counts_formal_reviews_only": True}))
    return readings


UNITS = {"S1_time_to_completion": "hours", "S2_dispatch_failure_rate": "ratio",
         "S3p_operator_intervention_proxy": "decided checkpoints/wk", "S4_review_rounds_per_pr": "rounds",
         "S5_cost_per_task": "min/dispatch"}


def load_cache(cache_dir: Path) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for name, path in cache_paths(cache_dir).items():
        rows = neotoma_io.read_cache(path)
        if rows is None and name != "reviews":
            raise SystemExit(f"missing cache {path}; run the fetch step first")
        out[name] = rows or []
    return out


def _parse_day(text: str) -> datetime:
    return weekly.parse_ts(text)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["fetch", "compute", "store"])
    parser.add_argument("--cache-dir", type=Path, default=neotoma_io.default_cache_dir())
    parser.add_argument("--only", nargs="*", help="fetch: subset of subprocess tasks checkpoints shape_checks reviews")
    parser.add_argument("--repo", action="append", help="S4: owner/name; repeatable (or METRICS_REPOS)")
    parser.add_argument("--weeks", type=int, default=26, help="S4 review lookback in weeks")
    parser.add_argument("--swarm-artifact", default=os.environ.get("METRICS_SWARM_ARTIFACT", ""),
                        help="artifact label the swarm readings are stored under, e.g. swarm:<name> (or METRICS_SWARM_ARTIFACT)")
    parser.add_argument("--first-week", type=_parse_day, default=None,
                        help="first week to read, YYYY-MM-DD (default: the earliest observation in the caches)")
    parser.add_argument("--max-cache-age-days", type=float, default=CACHE_MAX_AGE_DAYS,
                        help="refuse a cache fetched longer ago than this")
    parser.add_argument("--allow-stale", action="store_true",
                        help="use a stale cache anyway; every reading is then stored as partial")
    parser.add_argument("--metric", action="append", default=[],
                        help="compute/store only these metric ids (repeatable), e.g. S3p_operator_intervention_proxy")
    parser.add_argument("--dry-run", action="store_true",
                        help="store: compute and print the target instance and record counts, write nothing")
    args = parser.parse_args(argv)
    only = set(args.only) if args.only else None
    now = utcnow()
    if args.command == "fetch":
        neotoma_part = (only - {"reviews"}) if only else None
        if only is None or neotoma_part:
            fetch_all(args.cache_dir, only=neotoma_part)
        if only is None or "reviews" in only:
            paths = cache_paths(args.cache_dir)
            cutoff = weekly.week_start(now) - timedelta(weeks=args.weeks)
            existing = neotoma_io.read_cache(paths["reviews"]) or []
            repos = args.repo or repo_baseline.env_list("METRICS_REPOS")
            if not repos:
                parser.error("name at least one repository with --repo owner/name or METRICS_REPOS")
            try:
                rows = fetch_reviews(repos, cutoff, existing)
            except (ValueError, repo_baseline.PaginationError) as exc:
                print(f"Refusing to continue: {exc}", file=sys.stderr)
                return 2
            neotoma_io.write_cache(paths["reviews"], rows)
            print(f"  reviews: {len(rows)} merged PRs -> {paths['reviews']}", file=sys.stderr)
        return 0
    if not args.swarm_artifact:
        parser.error("name the swarm artifact with --swarm-artifact swarm:<name> (or METRICS_SWARM_ARTIFACT)")
    cache = load_cache(args.cache_dir)
    fetched = stream_fetched_at(args.cache_dir)
    stale = stale_streams(fetched, now, args.max_cache_age_days)
    if stale and not args.allow_stale:
        print(f"Refusing to read stale caches ({', '.join(stale)}; limit {args.max_cache_age_days:g} days): "
              "run fetch again, or pass --allow-stale to store every reading as partial", file=sys.stderr)
        return 2
    readings = build_readings(subprocess_rows=cache["subprocess"], task_rows=cache["tasks"],
                              checkpoint_rows=cache["checkpoints"], shape_rows=cache["shape_checks"],
                              review_prs=cache["reviews"], now=now, swarm_artifact=args.swarm_artifact,
                              fetched_at=fetched, first_week=args.first_week)
    if stale:
        readings = mark_stale(readings, f"cache older than {args.max_cache_age_days:g} days: {', '.join(stale)}")
    if args.metric:
        readings = [r for r in readings if r["metric_ref"] in set(args.metric)]
    if not readings:
        print("no readings: the caches hold no rows in any complete week", file=sys.stderr)
        return 1
    print(f"{len(readings)} readings; window ends {min(r['window_end'] for r in readings)} .. "
          f"{max(r['window_end'] for r in readings)}")
    print(weekly.format_summary(weekly.summarize_readings(readings), UNITS))
    if args.command == "compute":
        return 0
    return neotoma_io.store_command(readings, "swarm-ops-baseline", "reading_key", dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

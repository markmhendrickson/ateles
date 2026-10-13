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
    python3 execution/scripts/metrics/swarm_ops_baseline.py compute    # print weekly readings + baseline summary
    python3 execution/scripts/metrics/swarm_ops_baseline.py store      # compute, then store metric_reading rows

Needs NEOTOMA_BASE_URL / NEOTOMA_BEARER_TOKEN in the process for ``fetch`` and
``store`` (never printed). ``fetch --only reviews`` reads pull-request reviews
from the public GitHub API (GITHUB_TOKEN from the process if set; never
printed). Reads are sequential: hosted Neotoma is fragile. Cost is reported in
dispatch wall-clock minutes (a proxy); tokens only where a harness reported
them, and a missing token field is "not metered", never zero.
"""

from __future__ import annotations

import argparse
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
SUBPROCESS_KEEP = ("event_type", "event_at", "tool_name", "agent_sub", "success", "task_entity_id",
                   "duration_ms", "provider", "model", "model_source", "total_tokens",
                   "input_tokens", "output_tokens", "total_cost_usd")
TASK_KEEP = ("status", "action_type", "assigned_to", "title", "task_type", "completed_at",
             "completed_date")
CHECKPOINT_KEEP = ("status", "task_entity_id", "blast_radius")
SHAPE_KEEP = ("event_type", "event_at", "session_id", "harness", "summary", "status")

SWARM_ARTIFACT = "swarm:ateles"
# Weeks with fewer than this many completed dispatches (or done tasks) are stored
# with coverage 0.5 so they stay visible but stay out of baseline summaries.
MIN_DISPATCHES = 20
MIN_DONE_TASKS = 5
# Checkpoint statuses that mean nobody has decided yet.
CHECKPOINT_OPEN_STATUSES = {"awaiting_operator", "open", "pending", "superseded", "draft"}
# A task is swarm work when any observation carries swarm routing metadata.
SWARM_TASK_MARKERS = ("assigned_to", "action_type")
DONE_STATUSES = {"done", "completed"}


# --- fetch -------------------------------------------------------------------

def fetch_subprocess(requester=neotoma_io.request) -> list[dict[str, Any]]:
    """All subprocess dispatch observations, one entity at a time (sequential)."""
    ents = requester("POST", "/entities/query", {
        "entity_type": "harness_event", "limit": 500, "include_snapshots": True,
        "snapshot_filters": {"event_type": {"op": "eq", "value": "subprocess"}}})
    rows: list[dict[str, Any]] = []
    for ent in ents.get("entities", []):
        rows += neotoma_io.fetch_entity_observations(ent["entity_id"], SUBPROCESS_KEEP,
                                                     requester=requester)
        print(f"  subprocess {ent['entity_id']}: {len(rows)} rows so far", file=sys.stderr)
    return rows


def fetch_shape_checks(requester=neotoma_io.request) -> list[dict[str, Any]]:
    ents = requester("POST", "/entities/query", {
        "entity_type": "harness_event", "limit": 100, "include_snapshots": True,
        "snapshot_filters": {"event_type": {"op": "eq", "value": "decision_shape_check"}}})
    rows: list[dict[str, Any]] = []
    for ent in ents.get("entities", []):
        rows += neotoma_io.fetch_entity_observations(ent["entity_id"], SHAPE_KEEP, requester=requester)
    return rows


def cache_paths(cache_dir: Path) -> dict[str, Path]:
    return {name: cache_dir / f"{name}.jsonl"
            for name in ("subprocess", "tasks", "checkpoints", "shape_checks", "reviews")}


def fetch_all(cache_dir: Path, *, only: set[str] | None = None) -> None:
    paths = cache_paths(cache_dir)
    plan = {
        "subprocess": fetch_subprocess,
        "tasks": lambda: neotoma_io.fetch_observation_stream("task", TASK_KEEP),
        "checkpoints": lambda: neotoma_io.fetch_observation_stream("checkpoint_brief", CHECKPOINT_KEEP),
        "shape_checks": fetch_shape_checks,
    }
    for name, fn in plan.items():
        if only and name not in only:
            continue
        print(f"fetching {name} ...", file=sys.stderr)
        rows = fn()
        neotoma_io.write_cache(paths[name], rows)
        print(f"  {name}: {len(rows)} rows -> {paths[name]}", file=sys.stderr)




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
    duration and is skipped (counted by the caller via ``len`` difference).
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
    measurable = [t for t in lifecycles if not t["created_done"]]
    out = []
    for (start, end), items in zip(windows, _bucket(measurable, "done", windows)):
        if not items:
            continue
        kinds: dict[str, list[float]] = defaultdict(list)
        for t in items:
            kinds[t["kind"]].append(t["hours"])
        hours = sorted(t["hours"] for t in items)
        p90 = hours[min(len(hours) - 1, int(0.9 * len(hours)))]
        out.append({"start": start, "end": end, "n": len(items), "median_hours": weekly.median(hours),
                    "p90_hours": p90,
                    "by_kind": {k: {"n": len(v), "median_hours": weekly.median(v)} for k, v in sorted(kinds.items())}})
    return out


def resolved_checkpoints(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per checkpoint at the time it first left the undecided statuses."""
    by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_entity[r["entity_id"]].append(r)
    out = []
    for obs in by_entity.values():
        obs.sort(key=lambda r: r["observed_at"])
        for o in obs:
            status = str(o["fields"].get("status") or "").lower()
            if status and status not in CHECKPOINT_OPEN_STATUSES:
                out.append({"at": weekly.parse_ts(o["observed_at"]), "status": status})
                break
    return out


def weekly_intervention_rows(checkpoints: list[dict[str, Any]], shape_rows: list[dict[str, Any]],
                             windows) -> list[dict[str, Any]]:
    shape = [{"at": weekly.parse_ts(r["fields"].get("event_at") or r["observed_at"])} for r in shape_rows
             if r["fields"].get("event_type", "decision_shape_check") == "decision_shape_check"]
    out = []
    for (start, end), cps, sh in zip(windows, _bucket(checkpoints, "at", windows), _bucket(shape, "at", windows)):
        out.append({"start": start, "end": end, "resolved_checkpoints": len(cps),
                    "decision_shape_checks": len(sh)})
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

def build_readings(*, subprocess_rows, task_rows, checkpoint_rows, shape_rows, review_prs, now: datetime,
                   first_week: datetime | None = None) -> list[dict[str, Any]]:
    first = first_week or datetime(2026, 6, 15, tzinfo=timezone.utc)
    windows = weekly.weeks_between(first, now)
    mk = weekly.make_reading
    readings: list[dict[str, Any]] = []

    comps = completions(subprocess_rows)
    dispatch_rows = weekly_dispatch_rows(comps, windows)
    for r in dispatch_rows:
        thin = r["n"] < MIN_DISPATCHES
        cov, note = (0.5, f"fewer than {MIN_DISPATCHES} completed dispatches; rate unreliable") if thin else (1.0, "")
        readings.append(mk(
            metric_id="S2_dispatch_failure_rate", artifact=SWARM_ARTIFACT, start=r["start"], end=r["end"],
            value=r["failure_rate"], unit="ratio", collector=COLLECTOR, sourced_at=now,
            coverage_ratio=cov, coverage_note=note, probe_ok=len(comps) > 0,
            detail={"completed_dispatches": r["n"], "failed": r["failed"], "by_role": r["by_role"]}))
        if r["minutes_per_dispatch"] is not None:
            detail = {"completed_dispatches": r["n"], "timed_dispatches": r["timed"],
                      "total_minutes": round(r["total_minutes"], 2),
                      "tasks_identified": r["tasks_identified"],
                      "tokens_reported_dispatches": r["tokens_reported_dispatches"],
                      "tokens": "not metered" if r["total_tokens_reported"] is None else r["total_tokens_reported"]}
            if r["minutes_per_identified_task"] is not None:
                detail["minutes_per_identified_task"] = round(r["minutes_per_identified_task"], 2)
            readings.append(mk(
                metric_id="S5_cost_per_task", artifact=SWARM_ARTIFACT, start=r["start"], end=r["end"],
                value=r["minutes_per_dispatch"], unit="minutes_per_dispatch", collector=COLLECTOR,
                sourced_at=now, coverage_ratio=cov, coverage_note=note, probe_ok=len(comps) > 0, detail=detail))

    lifecycles = task_lifecycles(task_rows)
    for r in weekly_task_rows(lifecycles, windows):
        thin = r["n"] < MIN_DONE_TASKS
        readings.append(mk(
            metric_id="S1_time_to_completion", artifact=SWARM_ARTIFACT, start=r["start"], end=r["end"],
            value=r["median_hours"], unit="hours", collector=COLLECTOR, sourced_at=now,
            coverage_ratio=0.5 if thin else 1.0,
            coverage_note=f"fewer than {MIN_DONE_TASKS} tasks done in the week" if thin else "",
            probe_ok=len(lifecycles) > 0,
            detail={"tasks_done": r["n"], "p90_hours": round(r["p90_hours"], 2), "by_kind": r["by_kind"]}))

    cps = resolved_checkpoints(checkpoint_rows)
    for r in weekly_intervention_rows(cps, shape_rows, windows):
        readings.append(mk(
            metric_id="S3p_operator_intervention_proxy", artifact=SWARM_ARTIFACT, start=r["start"], end=r["end"],
            value=r["resolved_checkpoints"], unit="events", collector=COLLECTOR, sourced_at=now,
            probe_ok=len(cps) > 0,
            detail={"decision_shape_checks": r["decision_shape_checks"],
                    "note": "proxy; S3 proper needs principal attribution on writes"}))

    for repo in sorted({p["repo"] for p in review_prs}):
        for r in weekly_review_rows([p for p in review_prs if p["repo"] == repo], windows):
            if r["rounds_mean"] is None:
                continue
            readings.append(mk(
                metric_id="S4_review_rounds_per_pr", artifact=f"github:{repo}", start=r["start"], end=r["end"],
                value=r["rounds_mean"], unit="rounds", collector=COLLECTOR, sourced_at=now,
                coverage_ratio=1.0 if r["reviewed"] >= 3 else 0.5,
                coverage_note="" if r["reviewed"] >= 3 else "fewer than 3 reviewed PRs merged in the week",
                probe_ok=True,
                detail={"merged_prs": r["merged"], "reviewed_prs": r["reviewed"],
                        "unreviewed_prs": r["unreviewed"], "rounds_max": r["rounds_max"],
                        "counts_formal_reviews_only": True}))
    return readings


UNITS = {"S1_time_to_completion": "hours", "S2_dispatch_failure_rate": "ratio",
         "S3p_operator_intervention_proxy": "checkpoints/wk", "S4_review_rounds_per_pr": "rounds",
         "S5_cost_per_task": "min/dispatch"}


def load_cache(cache_dir: Path) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for name, path in cache_paths(cache_dir).items():
        rows = neotoma_io.read_cache(path)
        if rows is None and name != "reviews":
            raise SystemExit(f"missing cache {path}; run the fetch step first")
        out[name] = rows or []
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["fetch", "compute", "store"])
    parser.add_argument("--cache-dir", type=Path, default=neotoma_io.default_cache_dir())
    parser.add_argument("--only", nargs="*", help="fetch: subset of subprocess tasks checkpoints shape_checks reviews")
    parser.add_argument("--repo", action="append", help="S4: owner/name; repeatable (or METRICS_REPOS)")
    parser.add_argument("--weeks", type=int, default=26, help="S4 review lookback in weeks")
    args = parser.parse_args(argv)
    only = set(args.only) if args.only else None
    now = datetime.now(timezone.utc)
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
            rows = fetch_reviews(repos, cutoff, existing)
            neotoma_io.write_cache(paths["reviews"], rows)
            print(f"  reviews: {len(rows)} merged PRs -> {paths['reviews']}", file=sys.stderr)
        return 0
    cache = load_cache(args.cache_dir)
    readings = build_readings(subprocess_rows=cache["subprocess"], task_rows=cache["tasks"],
                              checkpoint_rows=cache["checkpoints"], shape_rows=cache["shape_checks"],
                              review_prs=cache["reviews"], now=now)
    print(f"{len(readings)} readings; window ends {min(r['window_end'] for r in readings)} .. "
          f"{max(r['window_end'] for r in readings)}")
    print(weekly.format_summary(weekly.summarize_readings(readings), UNITS))
    if args.command == "compute":
        return 0
    problems = neotoma_io.store_and_verify(readings, "swarm-ops-baseline", "reading_key")
    if problems:
        print("PROBLEMS:", *problems[:40], sep="\n  ", file=sys.stderr)
        return 1
    print(f"stored and read back {len(readings)} metric_reading records")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Replay a labelled set of PR heads through the real review-lens path.

For each selected case this checks out the case's head (with its planted patch
where it has one) in a throwaway checkout, runs ONE review lens with the chosen
candidate through ``harness_lens_runner.run_one`` (same sandbox profile, lens
prompt, shared brief and verdict reader as a live review), and appends one JSON
line to a results file. It never posts anything: there is no ``--post`` flag and
the runner refuses replay together with posting.

    python3 execution/scripts/review_replay/review_replay.py \\
        --set-dir <labelled-set> --brief <lens-brief.md> \\
        --candidate codex:<model> --codex-effort medium \\
        --case planted-01,planted-02,planted-03 --lens security \\
        --results out/results.jsonl

Candidates (``--candidate``): ``claude[:model]``, ``codex[:model]``,
``openrouter:<model>`` (hosted, real per-run cost from the provider's usage
field) and ``ollama:<model>`` (local, thinking off unless ``--ollama-thinking``).

Safety and limits
* ``--spend-cap`` (USD, default 5) is a hard stop on real provider-reported
  cost across everything already in the results file; see ``spend_guard.py``.
* Re-running the same command skips every (case, lens, candidate, run) already
  in the results file; a run that could not be launched is retried.
* ``--timeout-minutes`` bounds each run (default 30).
* The candidate's environment holds no GitHub, Neotoma or endpoint credential.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "review_replay"

from . import REPO_ROOT
from .candidate_runner import (
    ChildLaunch,
    DEFAULT_OPENROUTER_KEY_REF,
    run_candidate_child,
)
from .dataset import DEFAULT_KINDS, RunUnit, build_units, load_dataset
from .findings import parse_findings, verdict_token
from .replay_worktree import ReplayWorktree, ReplayWorktreeError, retarget
from .spend_guard import SpendGuard

import harness_lens_runner as hlr  # noqa: E402
from review_panel import lens_by_name  # noqa: E402

SCHEMA_VERSION = 1
OWNER = "markmhendrickson"
FOCUS_NOTES = (
    "The `gh` CLI and network publication are not available in this review. "
    "The PR description, if any, is in ./PR_DESCRIPTION.md."
)


@dataclass
class ReplayConfig:
    set_dir: Path
    brief: Path
    results: Path
    timeout_seconds: int
    source_repo_root: Path | None
    codex_effort: str | None
    ollama_thinking: bool
    ollama_context_tokens: int | None
    openrouter_key_ref: str
    ollama_num_ctx: int | None = None


def run_key(rec: dict) -> tuple:
    return (rec["case_id"], rec["lens"], rec["candidate"], rec["run_index"])


def load_done(results: Path) -> set[tuple]:
    """Keys of runs that finished and need not be repeated."""
    done: set[tuple] = set()
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not rec.get("incomplete"):
                done.add(run_key(rec))
    return done


def verdict_file(results: Path, rec_key: tuple) -> Path:
    safe = "__".join(re.sub(r"[^A-Za-z0-9._-]+", "_", str(p)) for p in rec_key)
    return results.parent / (results.name + ".verdicts") / f"{safe}.md"


async def replay_one(
    unit: RunUnit,
    *,
    provider: str,
    candidate: str,
    run_index: int,
    cfg: ReplayConfig,
    run_cost_limit: float | None,
) -> dict:
    """Run one case once and return its result record (never raises)."""
    metrics: dict = {}
    captured: dict = {}
    t0 = time.time()
    source = cfg.source_repo_root or (Path.home() / "repos" / unit.repo)
    own_repo = unit.repo == "ateles"
    description = unit.pr_description or "(no description is available in this replay)"
    patches = [cfg.set_dir / p for p in unit.patches]

    def factory(_repo_name: str, path: Path) -> ReplayWorktree:
        wt = ReplayWorktree(
            source=source,
            path=path,
            base_sha=unit.base_sha,
            pr=unit.pr,
            patches=patches,
            pr_description=description,
            may_fetch=own_repo,
        )
        captured["worktree"] = wt
        return wt

    async def child_runner(
        *, provider, task_text, worktree, sandbox, verdict_path, agent, timeout
    ):
        metrics["child_ran"] = True
        launch = ChildLaunch(
            provider=provider,
            task_text=task_text,
            worktree=worktree,
            sandbox=sandbox,
            verdict_path=verdict_path,
            timeout=timeout or cfg.timeout_seconds,
            agent=agent,
            run_cost_limit_usd=run_cost_limit,
            codex_effort=cfg.codex_effort,
            ollama_thinking=cfg.ollama_thinking,
            ollama_context_tokens=cfg.ollama_context_tokens,
            ollama_num_ctx=cfg.ollama_num_ctx,
            openrouter_key_ref=cfg.openrouter_key_ref,
        )
        return await run_candidate_child(launch, metrics)

    def on_verdict(text, check, _target):
        captured["verdict_text"] = text
        captured["check"] = check

    hooks = hlr.ReplayHooks(
        worktree_factory=factory,
        child_runner=child_runner,
        on_verdict=on_verdict,
        after_create=retarget,
        agent_prompt_root=REPO_ROOT,
    )
    lens = lens_by_name(unit.lens)
    rec: dict = {
        "schema_version": SCHEMA_VERSION,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "case_id": unit.case_id,
        "unit_kind": unit.kind,
        "candidate": candidate,
        "provider": provider,
        "run_index": run_index,
        "lens": unit.lens,
        "repo": unit.repo,
        "pr": unit.pr,
        "head_sha": unit.head_sha,
        "patches": unit.patches,
    }
    if lens is None:
        rec.update(incomplete=True, incomplete_reason=f"unknown lens {unit.lens!r}")
        return rec
    target = hlr.LensTarget(
        repo=f"{OWNER}/{unit.repo}",
        pr=unit.pr,
        head=unit.head_sha,
        lens=unit.lens,
        agent=lens.agent,
        focus_notes=FOCUS_NOTES,
    )
    scratch = Path(tempfile.mkdtemp(prefix="review-replay-"))
    report: dict = {}
    error = ""
    try:
        report = await hlr.run_one(
            target,
            provider=provider,
            post=False,
            dry_run=False,
            repo_worktree_name=unit.repo,
            scratch_root=scratch,
            brief_path=cfg.brief,
            timeout=cfg.timeout_seconds,
            replay=hooks,
        )
    except (
        ReplayWorktreeError,
        FileNotFoundError,
        ValueError,
        RuntimeError,
        hlr.HeadroomExhausted,
    ) as exc:
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    except Exception as exc:  # keep one bad run from ending a long replay
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        wt = captured.get("worktree")
        if wt is not None:
            wt.remove()
        shutil.rmtree(scratch, ignore_errors=True)

    check = captured.get("check")
    text = captured.get("verdict_text")
    wt = captured.get("worktree")
    child_ran = bool(metrics.get("child_ran")) and not metrics.get("launch_error")
    rec["replay_head_sha"] = getattr(wt, "head_sha", "") or None
    rec["wall_seconds_total"] = round(time.time() - t0, 1)
    if not child_ran:
        reason = (
            error
            or metrics.get("launch_error")
            or str(
                report.get("reason")
                or report.get("refusal_reason")
                or "child did not run"
            )
        )
        rec.update(incomplete=True, incomplete_reason=reason[:400])
        return rec
    for key in (
        "wall_seconds",
        "turns",
        "tool_calls",
        "tool_errors",
        "tokens_in",
        "tokens_out",
        "tokens_cache_read",
        "tokens_cache_write",
        "cost_usd",
        "cost_source",
        "cost_api_equivalent_usd",
        "timeout",
        "budget_abort",
        "exit_status",
        "model_reported",
        "endpoint_requests",
        "endpoint_errors",
        "env_names",
        "ollama_context_length",
        "ollama_num_ctx",
        "stderr_tail",
        "stdout_tail",
    ):
        if key in metrics:
            rec[key] = metrics[key]
    rec["verdict_valid"] = bool(check.ok) if check else False
    rec["validity_reason"] = (
        None
        if (check and check.ok)
        else (
            check.reason
            if check
            else (report.get("reason") or error or "no verdict produced")
        )[:300]
    )
    rec["verdict_token"] = verdict_token(
        text or "", check.lens_verdict if check else None
    )
    findings = parse_findings(text or "")
    rec["findings"] = [f.to_dict() for f in findings]
    rec["blocking_count"] = len(findings)
    rec["completed"] = bool(report.get("ok")) or bool(check and check.ok)
    rec["run_error"] = (error or str(report.get("reason") or ""))[:300] or None
    if text:
        vf = verdict_file(cfg.results, run_key(rec))
        vf.parent.mkdir(parents=True, exist_ok=True)
        vf.write_text(text, encoding="utf-8")
        rec["verdict_file"] = vf.name
    return rec


def append_line(results: Path, rec: dict) -> None:
    results.parent.mkdir(parents=True, exist_ok=True)
    with results.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def spread(units: list[RunUnit]) -> list[RunUnit]:
    """Interleave units by kind so a small ``--limit`` still covers every kind."""
    buckets: dict[str, list[RunUnit]] = {}
    for u in units:
        buckets.setdefault(u.kind, []).append(u)
    out: list[RunUnit] = []
    while any(buckets.values()):
        for kind in sorted(buckets):
            if buckets[kind]:
                out.append(buckets[kind].pop(0))
    return out


async def run_all(
    units: list[RunUnit], args, cfg: ReplayConfig, guard: SpendGuard
) -> int:
    done = load_done(cfg.results)
    kind, _model = hlr.parse_provider(args.candidate)
    label = args.label or args.candidate
    work = [
        (u, i)
        for u in units
        for i in range(args.runs)
        if (u.case_id, u.lens, label, i) not in done
    ]
    print(
        f"{len(units)} units x {args.runs} runs: {len(work)} to do, "
        f"{len(units) * args.runs - len(work)} already done",
        file=sys.stderr,
    )
    sem = asyncio.Semaphore(args.parallel)
    write_lock = asyncio.Lock()
    stop = {"reason": ""}

    async def one(unit: RunUnit, idx: int) -> None:
        async with sem:
            if stop["reason"]:
                return
            ok, reason, limit = guard.reserve(label)
            if not ok:
                stop["reason"] = reason
                return
            rec = await replay_one(
                unit,
                provider=args.candidate,
                candidate=label,
                run_index=idx,
                cfg=cfg,
                run_cost_limit=limit if kind in ("openrouter",) else None,
            )
            guard.finish(label, kind, rec.get("cost_usd"))
            async with write_lock:
                append_line(cfg.results, rec)
            tag = (
                "INCOMPLETE " + str(rec.get("incomplete_reason"))[:90]
                if rec.get("incomplete")
                else (
                    f"{rec.get('verdict_token')} valid={rec.get('verdict_valid')} findings={rec.get('blocking_count')} "
                    f"cost={rec.get('cost_usd')} wall={rec.get('wall_seconds')}s"
                )
            )
            print(f"[{unit.case_id}/{unit.lens}/#{idx}] {tag}", file=sys.stderr)
            if guard.tripped:
                stop["reason"] = guard.tripped

    await asyncio.gather(*(one(u, i) for u, i in work))
    if stop["reason"]:
        print(f"STOPPED: {stop['reason']}", file=sys.stderr)
        return 3
    return 0


def csv(value: str | None) -> tuple[str, ...] | None:
    return tuple(v.strip() for v in value.split(",") if v.strip()) if value else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="review_replay", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument(
        "--candidate",
        help="provider spec, e.g. codex:gpt-5.6-sol, openrouter:z-ai/glm-5.2, ollama:qwen3.6:35b",
    )
    ap.add_argument(
        "--label", help="name recorded for the candidate (default: the provider spec)"
    )
    ap.add_argument(
        "--set-dir",
        type=Path,
        required=True,
        help="directory holding dataset.json, planted/ and probes/",
    )
    ap.add_argument(
        "--results",
        type=Path,
        help="results .jsonl (appended; makes the run resumable)",
    )
    ap.add_argument(
        "--brief", type=Path, help="shared lens brief (required unless --list)"
    )
    ap.add_argument("--lens", help="comma list: pm,qa,ux,arch,security")
    ap.add_argument("--kind", help=f"comma list; default {','.join(DEFAULT_KINDS)}")
    ap.add_argument(
        "--repo", default="ateles", help="comma list of repos (default ateles)"
    )
    ap.add_argument("--case", help="comma list of case ids")
    ap.add_argument(
        "--limit",
        type=int,
        help="run at most this many units (after spreading across kinds)",
    )
    ap.add_argument(
        "--runs", type=int, default=1, help="runs per unit (run_index 0..runs-1)"
    )
    ap.add_argument(
        "--spend-cap",
        type=float,
        default=5.0,
        help="hard cap on real provider cost in USD",
    )
    ap.add_argument("--timeout-minutes", type=float, default=30.0)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument(
        "--source-repo-root",
        type=Path,
        help="clone to take objects from (default ~/repos/<repo>)",
    )
    ap.add_argument(
        "--codex-effort", help="reasoning effort for codex candidates, e.g. medium"
    )
    ap.add_argument(
        "--ollama-thinking",
        action="store_true",
        help="leave a local model's thinking channel on",
    )
    ap.add_argument(
        "--ollama-context-tokens",
        type=int,
        default=32768,
        help="window the CLI assumes for a local model; must match the server (OLLAMA_CONTEXT_LENGTH)",
    )
    ap.add_argument("--openrouter-key-ref", default=DEFAULT_OPENROUTER_KEY_REF)
    ap.add_argument(
        "--num-ctx",
        type=int,
        help="local model window in tokens; builds a model tag that carries it "
        "(weights shared) and tells the CLI the same window",
    )
    ap.add_argument(
        "--list", action="store_true", help="print the selected units and exit"
    )
    args = ap.parse_args(argv)

    records = load_dataset(args.set_dir)
    units = build_units(
        records,
        kinds=csv(args.kind) or DEFAULT_KINDS,
        lenses=csv(args.lens),
        repos=csv(args.repo),
        case_ids=csv(args.case),
    )
    units = spread(units) if args.limit and not args.case else units
    if args.limit:
        units = units[: args.limit]
    if args.list:
        for u in units:
            print(
                f"{u.case_id}\t{u.lens}\t{u.kind}\t{u.repo}#{u.pr}\t{u.head_sha[:10]}\tpatches={len(u.patches)}\tlabels={len(u.labels)}"
            )
        print(f"{len(units)} units", file=sys.stderr)
        return 0
    if not args.candidate or not args.results or not args.brief:
        ap.error("--candidate, --results and --brief are required (unless --list)")
    try:
        hlr.parse_provider(args.candidate)
    except ValueError as exc:
        ap.error(str(exc))
    cfg = ReplayConfig(
        set_dir=args.set_dir,
        brief=args.brief,
        results=args.results,
        timeout_seconds=int(args.timeout_minutes * 60),
        source_repo_root=args.source_repo_root,
        codex_effort=args.codex_effort,
        ollama_thinking=args.ollama_thinking,
        ollama_context_tokens=args.ollama_context_tokens,
        ollama_num_ctx=args.num_ctx,
        openrouter_key_ref=args.openrouter_key_ref,
    )
    guard = SpendGuard.from_results(args.spend_cap, args.results)
    return asyncio.run(run_all(units, args, cfg, guard))


if __name__ == "__main__":
    raise SystemExit(main())

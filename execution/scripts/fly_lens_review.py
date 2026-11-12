#!/usr/bin/env python3
"""
execution/scripts/fly_lens_review.py - run a lens review of a PR on Fly.

The supported command for "lens reviews run on Fly first" (agent_policy
ent_161873dfe2a269554d60187f). For each requested lens it:

  1. resolves the PR's CURRENT head and pins the review to it (the brief names
     the head; a head that moved while the lens ran marks the verdict STALE);
  2. dispatches the lens on Fly through ``dispatch_role.py --runner fly
     --github-read --action-class lens_review:<lens>``, one job at a time
     (the Fly runner admits one job per provider; the 45 minute per-job limit
     is passed as ``--timeout``);
  3. retries ONCE on a mechanical failure (unknown outcome, dropped result,
     unreadable verdict). A refusal is deterministic and is not retried;
  4. writes the verdict body in ``post_lens_verdict.py``'s form (attribution
     header line, bold verdict token line, findings) to ``<out>/<lens>.verdict.md``;
  5. records provider, duration, Fly run id and the reviewed head in
     ``<out>/summary.json``;
  6. when Fly cannot deliver (refused, failed twice, queue wait over the
     limit) prints ``FALL BACK TO LOCAL LENS: <lens> (<reason>)`` and exits 3.

It posts NOTHING to GitHub unless ``--post`` is given; ``--post`` then calls
``post_lens_verdict.py`` for each verdict (which itself refuses a head that is
no longer current).

Run it from the Ateles deployment checkout with the Neotoma env pair sourced
into the program (never print or read that file):

    cd ~/ateles-rc-src && set -a; source ~/.config/neotoma/.env; set +a
    .venv/bin/python execution/scripts/fly_lens_review.py \\
        --repo markmhendrickson/ateles --pr 1724 --lens all \\
        --task-entity-id ent_<task> [--extra-brief round2.md] [--post]

Exit codes: 0 every lens delivered (and posted, with --post); 3 at least one
lens needs the local fallback (failed, refused, queue wait, stale); 4 verdicts
delivered but a --post failed (a POST FAILED line names the lens); 2 usage /
head unresolvable. 3 wins when both apply.
Standard library only. See docs/runbooks/fly-lens-review.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# lens -> swarm role (its prompt mirror is .claude/skills/<role>/SKILL.md).
LENS_ROLES: dict[str, str] = {
    "pm": "pavo",
    "arch": "waxwing",
    "security": "falco",
    "qa": "phoenicurus",
    "ux": "accipiter",
}
LENS_ORDER = ("pm", "arch", "security", "qa", "ux")

JOB_TIMEOUT_SECONDS = 45 * 60  # approved per-job limit
QUEUE_MAX_WAIT_SECONDS = 60 * 60  # approved queue limit
MAX_ATTEMPTS = 2  # one try plus one retry

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DISPATCH = SCRIPT_DIR.parent / "daemons" / "apis" / "dispatch_role.py"
DEFAULT_POST = SCRIPT_DIR / "post_lens_verdict.py"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERDICT_RE = re.compile(r"^\*\*(SIGNED_OFF|APPROVE|BLOCKED|REQUEST_CHANGES)\*\*\s*$")
# A deterministic refusal at the router: do not retry, fall back at once.
REFUSAL_HINTS = ("refus", "not eligible", "not pinnable", "no provider", "headroom")
QUEUE_HINTS = ("queue", "waited", "wait ")
# A failure a second Fly job cannot cure: the qa/ux result-write 403 grant gap
# (shows as "no result recorded", exit 95). Only recognised when the dispatch
# output says so; otherwise it reads as mechanical (see the runbook).
DETERMINISTIC_HINTS = ("403", "exit 95", "exit code 95", "exit=95", "rc=95")


class FallBack(Exception):
    """Fly cannot deliver this lens; the caller runs it locally."""

    def __init__(self, reason: str, record: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.record = record or {}


def log(msg: str) -> None:
    print(f"fly_lens_review: {msg}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# GitHub head resolution
# --------------------------------------------------------------------------
def resolve_head(repo: str, pr: int) -> str:
    """The PR's current head sha, or raise RuntimeError."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha"],
        capture_output=True, text=True, timeout=60,
    )
    sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or not SHA_RE.match(sha):
        raise RuntimeError(
            f"could not resolve the head of {repo}#{pr} "
            f"(rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}"
        )
    return sha


# --------------------------------------------------------------------------
# Brief
# --------------------------------------------------------------------------
def compose_brief(*, repo: str, pr: int, head: str, lens: str, role: str,
                  extra: str = "") -> str:
    brief = f"""# Lens review brief: {repo} PR #{pr} (head {head})

You are ONE independent lens ({lens}) on a bootstrap review panel. You did not
write this change. Review ONLY head `{head}`. Do not post anything to GitHub;
the parent session posts your verdict.

## Your lens definition
This job cannot read Neotoma. Read your lens prompt from the repository's
prompt mirror: `.claude/skills/{role}/SKILL.md` (read it from the base branch
of the repository, not from the PR). Apply its review criteria.

## The change
- Repo: {repo}. PR: https://github.com/{repo}/pull/{pr}
- Read it with `gh pr view {pr} --repo {repo}` and `gh pr diff {pr} --repo {repo}`.
- State in your verdict that you reviewed head `{head}`. If the PR head is no
  longer `{head}` when you start, say so in one line and review `{head}` anyway.

## Rules
- Read-only. Never `git stash`, never `--no-verify`, never commit or push,
  never read credential files. No writes of any kind to GitHub or Neotoma.
- Report EVERY finding of a class at once, not one variant.
- If you find a security weakness OUTSIDE this PR's scope, put no details in
  the verdict body; write only "One out-of-scope hardening observation was
  recorded privately." and give the details only in your final reply.

## Output (your entire reply is the verdict body)
- Line 1: your attribution header, e.g. `**🤖 {role.capitalize()} — Ateles swarm, {lens} lens panelist**`
- Line 2: one verdict token in bold: `**SIGNED_OFF**` (`**APPROVE**` for the
  security lens) if you approve, or `**BLOCKED**` if any finding must be fixed
  before merge.
- Then a blank line and your findings. Prefix each must-fix finding line with
  `[BLOCKING]`; non-blocking notes are plain.
- If you sign off, the body must contain NO occurrence of the words BLOCKED,
  BLOCKING, REQUEST_CHANGES or CHANGES_REQUESTED in any decoration, no
  `[BLOCKING]` line, no `Verdict:` line and no line starting `review:` or `[`.
  Describe resolved concerns as "fixed" or "not a problem".
"""
    if extra.strip():
        brief += f"\n## Additional notes from the requesting session\n\n{extra.strip()}\n"
    return brief


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
def dispatch_once(*, python: str, dispatch_script: Path, role: str, lens: str,
                  repo: str, task_entity_id: str, task_file: Path) -> dict[str, Any]:
    """Run dispatch_role.py once; return its JSON envelope (or a synthetic one)."""
    cmd = [
        python, str(dispatch_script),
        "--role", role,
        "--task-file", str(task_file),
        "--runner", "fly",
        "--repository", repo,
        "--github-read",
        "--action-class", f"lens_review:{lens}",
        "--task-entity-id", task_entity_id,
        "--timeout", str(JOB_TIMEOUT_SECONDS),
        "--json",
    ]
    try:
        # State the approved queue limit to the Fly runner explicitly; a value
        # the caller already set wins.
        child_env = {**os.environ}
        child_env.setdefault("APIS_FLY_QUEUE_MAX_WAIT_SECONDS", str(QUEUE_MAX_WAIT_SECONDS))
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=child_env,
            # the queue wait plus one job, so a stuck child cannot hang us
            timeout=QUEUE_MAX_WAIT_SECONDS + JOB_TIMEOUT_SECONDS + 300,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "dispatch exceeded the queue plus job limit",
                "stderr": "", "remote_outcome_unknown": True, "timed_out": True}
    try:
        env = json.loads(proc.stdout)
        if not isinstance(env, dict):
            raise ValueError("envelope is not an object")
    except ValueError:
        return {"ok": False, "reason": "dispatch printed no readable JSON envelope",
                "stderr": (proc.stderr or "")[-500:], "returncode": proc.returncode}
    return env


def extract_verdict(stdout: str) -> str | None:
    """The verdict body in post_lens_verdict.py's form, or None.

    Header line, then the bold verdict token line. Any preamble before the
    header is dropped.
    """
    lines = (stdout or "").strip().splitlines()
    for i, line in enumerate(lines):
        if i >= 1 and VERDICT_RE.match(line.strip()) and lines[i - 1].strip():
            return "\n".join(lines[i - 1:]).strip() + "\n"
    return None


def classify_failure(env: dict[str, Any]) -> str:
    """'deterministic', 'refusal', 'queue', or 'mechanical' (retry once)."""
    text = " ".join(str(env.get(k, "")) for k in ("reason", "error", "stderr")).lower()
    if any(h in text for h in DETERMINISTIC_HINTS):
        return "deterministic"
    if env.get("remote_outcome_unknown") or "no result recorded" in text:
        return "mechanical"
    if any(h in text for h in QUEUE_HINTS) and ("queue" in text):
        return "queue"
    if env.get("returncode") is None and any(h in text for h in REFUSAL_HINTS):
        return "refusal"
    if any(h in text for h in REFUSAL_HINTS) and not env.get("remote_run_id"):
        return "refusal"
    return "mechanical"


def run_lens(*, lens: str, repo: str, pr: int, head: str, task_entity_id: str,
             out_dir: Path, extra: str, python: str, dispatch_script: Path,
             dispatch: Callable[..., dict[str, Any]] = dispatch_once,
             clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Run one lens to a delivered verdict or raise FallBack. Returns its record."""
    role = LENS_ROLES[lens]
    task_file = out_dir / f"{lens}.brief.md"
    task_file.write_text(compose_brief(repo=repo, pr=pr, head=head, lens=lens,
                                       role=role, extra=extra))
    record: dict[str, Any] = {"lens": lens, "role": role, "reviewed_head": head,
                              "attempts": []}
    t_lens = clock()
    last_reason = "unknown"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        t0 = clock()
        env = dispatch(python=python, dispatch_script=dispatch_script, role=role,
                       lens=lens, repo=repo, task_entity_id=task_entity_id,
                       task_file=task_file)
        dur = round(clock() - t0, 1)
        att = {"attempt": attempt, "duration_s": dur, "ok": bool(env.get("ok")),
               "provider": env.get("provider"),
               "run_id": env.get("remote_run_id") or "",
               "outcome_unknown": bool(env.get("remote_outcome_unknown"))}
        record["attempts"].append(att)
        (out_dir / f"{lens}.attempt{attempt}.json").write_text(
            json.dumps({k: v for k, v in env.items() if k != "stdout"}, indent=2))
        if env.get("ok"):
            body = extract_verdict(str(env.get("stdout", "")))
            if body is not None:
                path = out_dir / f"{lens}.verdict.md"
                path.write_text(body)
                token = VERDICT_RE.search(body.splitlines()[1]).group(1)  # type: ignore[union-attr]
                record.update(
                    status="delivered", verdict=token, verdict_file=str(path),
                    provider=att["provider"], run_id=att["run_id"],
                    duration_s=round(clock() - t_lens, 1))
                return record
            last_reason = "the reply had no header line plus verdict token line"
            att["problem"] = last_reason
            log(f"{lens}: attempt {attempt}: {last_reason}")
            continue  # an unreadable reply is mechanical: retry once
        kind = classify_failure(env)
        last_reason = str(env.get("reason") or env.get("error") or "dispatch failed")
        att["problem"] = f"{kind}: {last_reason}"
        log(f"{lens}: attempt {attempt} failed ({kind}): {last_reason}")
        if kind in ("refusal", "queue", "deterministic"):
            label = "queue wait" if kind == "queue" else kind
            record.update(status="fallback", reason=f"{label}: {last_reason}",
                          duration_s=round(clock() - t_lens, 1))
            raise FallBack(f"{label}: {last_reason}", record)
    record.update(status="fallback",
                  reason=f"failed {MAX_ATTEMPTS} times: {last_reason}",
                  duration_s=round(clock() - t_lens, 1))
    raise FallBack(f"failed {MAX_ATTEMPTS} times: {last_reason}", record)


# --------------------------------------------------------------------------
# Posting (explicit)
# --------------------------------------------------------------------------
def post_verdict(*, python: str, post_script: Path, repo: str, pr: int, lens: str,
                 body_file: str, head: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [python, str(post_script), "--repo", repo, "--pr", str(pr), "--lens", lens,
         "--body-file", body_file, "--head", head],
        capture_output=True, text=True, timeout=300,
    )
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()[-400:]


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None, *,
         dispatch: Callable[..., dict[str, Any]] = dispatch_once,
         head_resolver: Callable[[str, int], str] = resolve_head,
         poster: Callable[..., tuple[bool, str]] = post_verdict) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--pr", required=True, type=int)
    p.add_argument("--lens", required=True, choices=[*LENS_ROLES, "all"])
    p.add_argument("--task-entity-id", required=True,
                   help="Neotoma task id (dispatch_role requires one)")
    p.add_argument("--out-dir",
                   help="default: ~/.ateles-work/fly-lens-review/<repo>-<pr>-<head8>-<time>"
                   " (never the current directory); the path is printed")
    p.add_argument("--extra-brief", help="file passed through to each lens (round notes)")
    p.add_argument("--post", action="store_true",
                   help="post each delivered verdict via post_lens_verdict.py (default: no)")
    p.add_argument("--dispatch-script", default=str(DEFAULT_DISPATCH))
    p.add_argument("--post-script", default=str(DEFAULT_POST))
    args = p.parse_args(argv)

    if not re.match(r"^[\w.-]+/[\w.-]+$", args.repo):
        log("--repo must be owner/name")
        return 2
    if not args.task_entity_id.startswith("ent_"):
        log("--task-entity-id must be a Neotoma entity id (ent_...)")
        return 2
    extra = ""
    if args.extra_brief:
        try:
            extra = Path(args.extra_brief).read_text()
        except OSError as exc:
            log(f"cannot read --extra-brief: {exc}")
            return 2
    try:
        head = head_resolver(args.repo, args.pr)
    except RuntimeError as exc:
        log(str(exc))
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else (
        Path.home() / ".ateles-work" / "fly-lens-review"
        / f"{args.repo.replace('/', '_')}-{args.pr}-{head[:8]}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"out-dir: {out_dir}", flush=True)
    lenses = list(LENS_ORDER) if args.lens == "all" else [args.lens]
    summary: dict[str, Any] = {
        "repo": args.repo, "pr": args.pr, "head": head, "started": now_iso(),
        "task_entity_id": args.task_entity_id, "posted": False, "complete": False,
        "lenses": [],
    }

    def save() -> None:
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    fallbacks = 0
    post_failures = 0
    try:
        for n, lens in enumerate(lenses):  # sequential: one job per provider at a time
            log(f"{lens}: dispatching on Fly for head {head[:12]}")
            record: dict[str, Any]
            try:
                record = run_lens(
                    lens=lens, repo=args.repo, pr=args.pr, head=head,
                    task_entity_id=args.task_entity_id, out_dir=out_dir, extra=extra,
                    python=sys.executable, dispatch_script=Path(args.dispatch_script),
                    dispatch=dispatch)
            except FallBack as exc:
                fallbacks += 1
                record = {"lens": lens, "role": LENS_ROLES[lens], "reviewed_head": head,
                          "status": "fallback", "reason": str(exc), **exc.record}
                print(f"FALL BACK TO LOCAL LENS: {lens} ({exc})", flush=True)
                summary["lenses"].append(record)
                save()
                continue
            summary["lenses"].append(record)
            # the head moved while the lens ran: its verdict is for an older head
            try:
                current = head_resolver(args.repo, args.pr)
            except RuntimeError:
                current = ""
            if current != head:
                stale = Path(record["verdict_file"])
                stale = stale.rename(stale.with_name(stale.name + ".stale"))
                record["verdict_file"] = str(stale)
                record["status"] = "stale"
                record["reason"] = f"PR head moved to {current or 'unreadable'} during review"
                fallbacks += 1
                print(f"FALL BACK TO LOCAL LENS: {lens} (STALE: {record['reason']})",
                      flush=True)
                rest = lenses[n + 1:]
                for other in rest:  # their results would be stale too
                    fallbacks += 1
                    summary["lenses"].append({
                        "lens": other, "role": LENS_ROLES[other], "status": "not_run",
                        "reason": "stopped: PR head moved"})
                    print(f"FALL BACK TO LOCAL LENS: {other} (not run: PR head moved)",
                          flush=True)
                if rest:
                    print("stopped dispatching: the PR head moved, so the remaining "
                          "lenses would review a stale head", flush=True)
                save()
                break
            print(f"{lens}: {record['verdict']} via {record['provider']} "
                  f"in {record['duration_s']}s -> {record['verdict_file']}", flush=True)
            if args.post:
                ok, detail = poster(python=sys.executable,
                                    post_script=Path(args.post_script), repo=args.repo,
                                    pr=args.pr, lens=lens,
                                    body_file=record["verdict_file"], head=head)
                record["posted"] = ok
                record["post_detail"] = detail
                if ok:
                    summary["posted"] = True
                else:
                    post_failures += 1
                    record["status"] = "post_failed"
                    short = " ".join(detail.split())[:200] or "no detail"
                    print(f"POST FAILED: {lens} ({short}); the verdict is NOT on the PR. "
                          f"Post by hand: python3 execution/scripts/post_lens_verdict.py "
                          f"--repo {args.repo} --pr {args.pr} --lens {lens} "
                          f"--body-file {record['verdict_file']} --head {head}",
                          flush=True)
            save()
        summary["complete"] = True
    finally:  # also on Ctrl-C: keep the partial results
        summary["finished"] = now_iso()
        save()
    print(f"summary: {out_dir / 'summary.json'}", flush=True)
    if fallbacks:
        return 3
    return 4 if post_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Operator command: list, inspect, approve or reject waiting checkpoints.

The swarm's gate holds a task until the operator approves or rejects its signed
checkpoint.  An approval is only accepted when it carries RFC 9421 headers signed
by the operator's pinned resolver key over the exact ``POST /correct`` body.
This command is the operator-facing way to produce and submit those.

    resolve_checkpoints.py list [--bucket SAFE]
    resolve_checkpoints.py show ent_...
    resolve_checkpoints.py approve --bucket SAFE --limit 10            # preview only
    resolve_checkpoints.py approve --bucket SAFE --limit 10 --execute  # asks, then signs
    resolve_checkpoints.py reject  --checkpoint ent_... --execute

Rejecting a checkpoint DECLINES its task.  It is not a way to close work.

Safety, all enforced in this file:

* ``approve`` / ``reject`` are a preview unless ``--execute`` is given.
* ``--execute`` refuses unless stdin AND stdout are a terminal and a human types
  the confirmation phrase (it contains the count).  No flag skips the phrase, so
  a script or an agent cannot approve on the operator's behalf.
* At most 25 checkpoints per run by default (``--limit``, never more than 100).
* Before an approval batch the provider pace status is shown, and the run is
  refused when no provider can take a new frontier dispatch, unless
  ``--ignore-pace`` (approving releases tasks that spend capacity).
* A checkpoint whose tier is ``never``, or whose summary says approving will not
  release the task, is never approved.
* A checkpoint that is no longer ``awaiting_operator`` is a no-op (a replay does
  nothing); a task no longer held at the gate is not touched.
* The resolver key is read only to sign, from ``ATELES_CHECKPOINT_RESOLVER_JWK``
  (or ``<keys dir>/<subject name>.jwk.json`` under ``ATELES_PRIVATE_KEYS_DIR``),
  and neither its content nor its location is ever printed.

Submission goes through ``server._resolve_checkpoint``, the same implementation
the MCP ``resolve_checkpoint`` tool runs, and every result is read back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _path in (
    _REPO,
    _REPO / "execution" / "mcp" / "ateles",
    _REPO / "execution" / "scripts",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from lib.daemon_runtime import gating  # noqa: E402
from lib.daemon_runtime.aauth_httpsig import AAuthSigningError  # noqa: E402
from lib.daemon_runtime.checkpoint_resolution import (  # noqa: E402
    sign_checkpoint_resolution,
)

CHECKPOINT_TYPE = "checkpoint_" + "brief"
PENDING = "awaiting_operator"
HELD_TASK_STATUS = "awaiting_approval"
BUCKETS = ("SAFE", "OPERATOR", "STALE", "UNSORTED")
DEFAULT_MAX_PER_RUN = 25
HARD_MAX_PER_RUN = 100
DEFAULT_TRIAGE_FILE = "~/Library/Logs/ateles/checkpoint_triage/triage.json"
# Set by the harnesses agents run in; a tripwire for an agent that has somehow
# been handed a terminal, not a boundary (an agent can unset it).
AGENT_ENV_MARKERS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CODEX_SANDBOX",
    "CODEX_HOME_SESSION",
    "CURSOR_AGENT",
    "ATELES_AGENT_SESSION",
)
_NO_RELEASE = re.compile(
    r"will\s+not\s+release|can\s+never\s+release|cannot\s+release", re.IGNORECASE
)
_TITLE = re.compile(r"^PLAN checkpoint:\s*(?P<title>.*?)\s*\[ent_\w+\](?:\s*#[0-9a-f]+)?\s*$")


class Fatal(Exception):
    """A condition the command must stop on, shown to the operator as one line."""


@dataclass
class Entry:
    checkpoint_id: str
    task_id: str
    title: str
    reason: str
    blast: str
    gate_action: str
    confidence: str
    summary: str
    status: str
    bucket: str
    bucket_reason: str
    authority: dict = field(default_factory=dict)

    @property
    def cannot_release(self) -> str | None:
        """Why approving this checkpoint can never release its task, if so."""
        if self.blast == "never" or str(self.authority.get("blast_radius")) == "never":
            return "never tier: approval is recorded but never releases the task"
        if _NO_RELEASE.search(self.summary):
            return "its summary says approving will not release the task"
        return None


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _require_connection() -> None:
    if not gating.NEOTOMA_BEARER_TOKEN or not gating.NEOTOMA_BASE_URL:
        raise Fatal("NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN must be set")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", gating.CHECKPOINT_PRODUCER_JKT or ""):
        raise Fatal(
            "APIS_CHECKPOINT_PRODUCER_JKT is not set to a key thumbprint, so no "
            "checkpoint's signed authority can be validated"
        )


def load_triage(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """(by task id, by old checkpoint id).  A missing file means all UNSORTED."""
    path = Path(path).expanduser()
    if not path.is_file():
        print(
            "note: no triage file found; every checkpoint is shown as UNSORTED",
            file=sys.stderr,
        )
        return {}, {}
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise Fatal(f"triage file is unreadable: {type(exc).__name__}") from exc
    if not isinstance(rows, list):
        raise Fatal("triage file must be a JSON list")
    by_task: dict[str, dict] = {}
    by_cp: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("bucket") not in BUCKETS[:3]:
            continue
        if row.get("task_entity_id"):
            by_task[str(row["task_entity_id"])] = row
        if row.get("checkpoint_id"):
            by_cp[str(row["checkpoint_id"])] = row
    return by_task, by_cp


def _snapshot(record: dict) -> dict:
    return gating._snapshot_with_tenant(record)


def _title_of(snap: dict, triage_row: dict | None, task_id: str) -> str:
    match = _TITLE.match(str(snap.get("title") or ""))
    if match and match.group("title"):
        return match.group("title")
    if triage_row and triage_row.get("task_title"):
        return str(triage_row["task_title"])
    return str(snap.get("title") or task_id)


def build_entry(record: dict, triage: tuple[dict, dict], authority: dict) -> Entry:
    snap = _snapshot(record)
    checkpoint_id = str(record.get("entity_id") or snap.get("entity_id") or "")
    task_id = str(snap.get("task_entity_id") or authority.get("task_entity_id") or "")
    by_task, by_cp = triage
    row = by_task.get(task_id) or by_cp.get(checkpoint_id)
    return Entry(
        checkpoint_id=checkpoint_id,
        task_id=task_id,
        title=_title_of(snap, row, task_id),
        reason=str(snap.get("reason") or "").strip(),
        blast=str(snap.get("blast_radius") or "").strip().lower(),
        gate_action=str(snap.get("gate_action") or "").strip().lower(),
        confidence=str(snap.get("confidence") or ""),
        summary=str(snap.get("plan_summary") or ""),
        status=str(snap.get("status") or "").strip().lower(),
        bucket=str(row["bucket"]) if row else "UNSORTED",
        bucket_reason=str(row.get("reason") or "") if row else "",
        authority=authority,
    )


def validated_authority(checkpoint_id: str, record: dict) -> dict | None:
    """The checkpoint's authority envelope when it validates, else None.

    A failed observation read is an error, never "invalid": an outage must not
    read as a checkpoint that cannot be resolved.
    """
    snap = _snapshot(record)
    if not isinstance(snap.get("body"), str):
        return None  # a pre-signing brief: no envelope, cannot be resolved
    observations = gating.fetch_entity_observations_strict(checkpoint_id)
    if observations is None:
        raise Fatal(f"could not read the signing record of {checkpoint_id}")
    return gating.read_authenticated_checkpoint_authorization(
        checkpoint_id, record, observations=observations
    )


def _bucket_of(snap: dict, triage: tuple[dict, dict], checkpoint_id: str) -> str:
    by_task, by_cp = triage
    row = by_task.get(str(snap.get("task_entity_id") or "")) or by_cp.get(checkpoint_id)
    return str(row["bucket"]) if row else "UNSORTED"


def fetch_pending(
    triage: tuple[dict, dict],
    *,
    buckets: list[str] | None = None,
    exclude: frozenset[str] = frozenset(),
    stop_after: int | None = None,
    progress=None,
) -> tuple[list[Entry], int]:
    """(resolvable pending checkpoints, how many pending ones are not resolvable).

    ``buckets`` and ``stop_after`` narrow the work before the per-checkpoint
    signature reads (two requests each), so a batch of 25 never reads the queue.
    The listing itself is always complete or an error.
    """
    _require_connection()
    found = gating.query_all_entities(
        CHECKPOINT_TYPE,
        snapshot_filters={"status": {"op": "eq", "value": PENDING}},
    )
    if found is None:
        raise Fatal(
            "could not list the pending checkpoints (a page failed, or the "
            f"queue is deeper than the server's {gating.MAX_QUERY_OFFSET}-offset "
            "ceiling); nothing is shown rather than a partial list"
        )
    entries: list[Entry] = []
    unresolvable = 0
    for n, item in enumerate(found, 1):
        if stop_after is not None and len(entries) >= stop_after:
            break
        checkpoint_id = str(item.get("entity_id") or "")
        if checkpoint_id in exclude:
            continue
        snap = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
        if buckets and _bucket_of(snap, triage, checkpoint_id) not in buckets:
            continue
        if progress:
            progress(n, len(found))
        record = gating.fetch_checkpoint_record(checkpoint_id)
        if record is None:
            raise Fatal(f"could not read checkpoint {checkpoint_id}")
        authority = validated_authority(checkpoint_id, record)
        if not authority:
            unresolvable += 1
            continue
        entries.append(build_entry(record, triage, authority))
    order = {b: i for i, b in enumerate(BUCKETS)}
    entries.sort(key=lambda e: (order[e.bucket], e.title.lower(), e.checkpoint_id))
    return entries, unresolvable


def fetch_entry(checkpoint_id: str, triage: tuple[dict, dict]) -> Entry | None:
    """One checkpoint, freshly read; None when it has no valid authority."""
    record = gating.fetch_checkpoint_record(checkpoint_id)
    if record is None:
        raise Fatal(f"could not read checkpoint {checkpoint_id}")
    authority = validated_authority(checkpoint_id, record)
    return build_entry(record, triage, authority) if authority else None


def task_view(task_id: str) -> tuple[str, str]:
    """(status, title) of a task, read now; ('unreadable', '') on a failed read."""
    snap = gating.fetch_task_snapshot(task_id) if task_id else None
    if snap is None:
        return "unreadable", ""
    return str(snap.get("status") or "").strip().lower(), str(snap.get("title") or "")


# --------------------------------------------------------------------------
# Wording
# --------------------------------------------------------------------------


def approve_effect(entry: Entry) -> str:
    why = entry.cannot_release
    if why:
        return f"will NOT release the task ({why}); this tool refuses it"
    return "releases the task to an agent, which will spend capacity"


def line_for(entry: Entry) -> str:
    held = entry.reason or "held at the gate"
    return (
        f"{entry.title}\n"
        f"      held: {held}. approve: {approve_effect(entry)}. "
        f"reject: DECLINES the task."
    )


def print_entries(entries: list[Entry], out=None) -> None:
    out = out or sys.stdout
    for bucket in BUCKETS:
        group = [e for e in entries if e.bucket == bucket]
        if not group:
            continue
        print(f"\n== {bucket} ({len(group)}) ==", file=out)
        for entry in group:
            print(f"  {entry.checkpoint_id}  {line_for(entry)}", file=out)
            if entry.bucket_reason:
                print(f"      triage: {entry.bucket_reason}", file=out)


# --------------------------------------------------------------------------
# Eligibility, pace
# --------------------------------------------------------------------------


def refusals(entry: Entry, action: str, task_status: str) -> list[str]:
    """Reasons this checkpoint must not be acted on now (empty: eligible)."""
    problems: list[str] = []
    if entry.status != PENDING:
        problems.append(f"already resolved ({entry.status or 'unknown'}); nothing to do")
    if action == "approve" and entry.cannot_release:
        problems.append(f"approve refused: {entry.cannot_release}")
    if task_status != HELD_TASK_STATUS:
        problems.append(
            f"its task is '{task_status}', not '{HELD_TASK_STATUS}'; "
            "resolve it by hand"
        )
    return problems


def pace_status() -> tuple[bool, list[str]]:
    """(frontier dispatch possible, one line per provider).  Unreadable: not possible."""
    try:
        import harness_usage

        report = harness_usage.usage_report()
    except Exception as exc:  # noqa: BLE001 - fail closed
        return False, [f"pace status unreadable ({type(exc).__name__}); treating as paced"]
    lines: list[str] = []
    allowed = False
    for provider, view in sorted(report.items()):
        gate = view.get("usage_gate") if isinstance(view, dict) else None
        if not isinstance(gate, dict):
            lines.append(f"{provider}: no pace verdict")
            continue
        lines.append(str(gate.get("summary") or provider))
        allowed = allowed or bool(gate.get("dispatch_allowed"))
    if not report:
        return False, ["no provider is configured"]
    return allowed, lines


# --------------------------------------------------------------------------
# Signing and submission
# --------------------------------------------------------------------------


def resolver_key_path(subject: str) -> Path:
    """Where the operator's resolver key is configured; never printed."""
    configured = os.environ.get("ATELES_CHECKPOINT_RESOLVER_JWK", "").strip()
    if configured:
        return Path(configured).expanduser()
    keys_dir = Path(
        os.environ.get(
            "ATELES_PRIVATE_KEYS_DIR",
            str(Path(__file__).resolve().parents[3] / "ateles-private" / "keys"),
        )
    )
    return keys_dir / f"{subject.split('@', 1)[0]}.jwk.json"


def load_server():
    try:
        import server
    except ImportError as exc:  # the MCP SDK the server module imports
        raise Fatal(
            f"cannot load the resolve implementation ({exc.name} is not installed)"
        ) from exc
    return server


def sign(server, entry: Entry, action: str) -> dict[str, str]:
    sub = str(entry.authority["required_approver_sub"])
    path = resolver_key_path(sub)
    if not path.is_file():
        raise Fatal("resolver key is not configured or not found (see --help)")
    try:
        headers = sign_checkpoint_resolution(
            entry.checkpoint_id,
            action,
            private_jwk_path=path,
            expected_sub=sub,
            expected_jkt=str(entry.authority["required_approver_jkt"]),
            issuer=server.CHECKPOINT_RESOLVER_ISSUER,
            neotoma_base_url=server.NEOTOMA_BASE_URL,
        )
    except AAuthSigningError as exc:
        raise Fatal(f"signing refused: {exc}") from exc
    return {**headers, "content-type": headers.get("content-type", "application/json")}


@dataclass
class Outcome:
    entry: Entry
    action: str
    result: str
    checkpoint_status: str = "unreadable"
    task_status: str = "unreadable"
    confirmed: bool = False


def read_back(entry: Entry, action: str, result: str, server) -> Outcome:
    cp = gating.fetch_checkpoint_snapshot(entry.checkpoint_id)
    cp_status = str((cp or {}).get("status") or "unreadable").strip().lower()
    task_status, _ = task_view(entry.task_id)
    if action == "approve":
        confirmed = (
            cp_status == "approved"
            and task_status in server._RELEASE_CONFIRMED_TASK_STATUSES
        )
    else:
        confirmed = cp_status == "rejected" and task_status == "declined"
    return Outcome(entry, action, result, cp_status, task_status, confirmed)


async def submit_batch(
    entries: list[Entry], action: str, triage: tuple[dict, dict], server
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for entry in entries:
        # Re-read right before signing: state can have moved since the preview,
        # and a replay (already resolved) must be a no-op, not a second write.
        fresh = fetch_entry(entry.checkpoint_id, triage)
        task_status, _ = task_view(entry.task_id)
        if fresh is None:
            outcomes.append(Outcome(entry, action, "skipped: no longer resolvable"))
            continue
        problems = refusals(fresh, action, task_status)
        if problems:
            outcomes.append(Outcome(fresh, action, "skipped: " + "; ".join(problems)))
            continue
        try:
            headers = sign(server, fresh, action)
        except Fatal as exc:
            # Nothing was written for this one; stop here and still report the
            # earlier results rather than losing them to an exception.
            outcomes.append(Outcome(fresh, action, f"stopped, not signed: {exc}"))
            break
        reply = await server._resolve_checkpoint(
            fresh.checkpoint_id, action, resolver_aauth_headers=headers
        )
        text = str(reply.get("action_taken") or reply.get("error") or "no reply")
        outcomes.append(read_back(fresh, action, text, server))
    return outcomes


def print_outcomes(outcomes: list[Outcome]) -> None:
    print("\nResult (checkpoint and task statuses are read back after each write):")
    print(f"{'checkpoint':<28} {'ask':<8} {'checkpoint':<18} {'task':<18} ok  result")
    for o in outcomes:
        print(
            f"{o.entry.checkpoint_id:<28} {o.action:<8} {o.checkpoint_status:<18} "
            f"{o.task_status:<18} {'yes' if o.confirmed else 'NO':<3} {o.result[:120]}"
        )
    done = sum(1 for o in outcomes if o.confirmed)
    print(f"\n{done} of {len(outcomes)} confirmed by read-back.")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _progress(n: int, total: int) -> None:
    if n == 1 or n % 25 == 0 or n == total:
        print(f"  reading checkpoint {n}/{total}", file=sys.stderr)


def cmd_list(args, triage) -> int:
    entries, unresolvable = fetch_pending(
        triage, buckets=args.bucket, progress=_progress
    )
    print_entries(entries)
    counts = {b: sum(1 for e in entries if e.bucket == b) for b in BUCKETS}
    print(
        "\n"
        + ", ".join(f"{b} {n}" for b, n in counts.items())
        + f"  (total {len(entries)} resolvable)"
    )
    if unresolvable:
        print(
            f"{unresolvable} more pending checkpoints have no valid signed "
            "authority and cannot be resolved here (re-issue them first)."
        )
    return 0


def cmd_show(args, triage) -> int:
    _require_connection()
    record = gating.fetch_checkpoint_record(args.checkpoint_id)
    if record is None:
        raise Fatal(f"could not read checkpoint {args.checkpoint_id}")
    snap = _snapshot(record)
    authority = validated_authority(args.checkpoint_id, record)
    entry = build_entry(record, triage, authority or {})
    task_status, task_title = task_view(entry.task_id)
    print(f"checkpoint   {entry.checkpoint_id}  ({entry.status or 'unknown'})")
    print(f"task         {entry.task_id}  now '{task_status}'")
    print(f"title        {task_title or entry.title}")
    print(f"bucket       {entry.bucket}" + (f"  ({entry.bucket_reason})" if entry.bucket_reason else ""))
    print(f"held because {entry.reason or '(none recorded)'}")
    print(
        f"gate         {entry.gate_action or '?'}; blast {entry.blast or '?'}; "
        f"confidence {entry.confidence or '?'} vs threshold "
        f"{snap.get('confidence_threshold', '?')}"
    )
    print(f"policy       {snap.get('policy_entity_id') or '(none)'}")
    if authority:
        print(
            "authority    valid; approver "
            f"{authority.get('required_approver_sub')} "
            f"(key {authority.get('required_approver_jkt')}); "
            f"action {authority.get('action_type')}; "
            f"policy revision {str(authority.get('policy_revision'))[:16]}"
        )
    else:
        print("authority    NOT valid: this checkpoint cannot be resolved here")
    print(f"approve      {approve_effect(entry)}")
    print("reject       DECLINES the task")
    print("\nsummary:\n" + (entry.summary or "(none)"))
    return 0


def select(args, triage) -> list[Entry]:
    """Named checkpoints first, then bucket members, at most ``--limit`` in all."""
    ids = list(dict.fromkeys(args.checkpoint or []))
    chosen: list[Entry] = []
    for checkpoint_id in ids:
        entry = fetch_entry(checkpoint_id, triage)
        if entry is None:
            raise Fatal(
                f"{checkpoint_id} has no valid signed authority and cannot be "
                "resolved here"
            )
        chosen.append(entry)
    room = args.limit - len(chosen)
    if args.bucket and room > 0:
        more, _ = fetch_pending(
            triage,
            buckets=args.bucket,
            exclude=frozenset(ids),
            stop_after=room,
            progress=_progress,
        )
        chosen.extend(more)
    return chosen[: args.limit]


def agent_marker() -> str | None:
    return next((name for name in AGENT_ENV_MARKERS if os.environ.get(name)), None)


def cmd_resolve(args, triage) -> int:
    action = args.command
    if not args.bucket and not args.checkpoint:
        raise Fatal("name what to resolve: --bucket B and/or --checkpoint ID")
    if args.execute:
        marker = agent_marker()
        if marker:
            raise Fatal(f"refusing: this looks like an agent session ({marker})")
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise Fatal("refusing: --execute needs an interactive terminal")
    chosen = select(args, triage)
    if not chosen:
        print("nothing selected; no changes made")
        return 0

    batch: list[Entry] = []
    print(f"\n{action.upper()} batch (max {args.limit}):")
    for entry in chosen:
        task_status, _ = task_view(entry.task_id)
        problems = refusals(entry, action, task_status)
        mark = "SKIP" if problems else action
        print(f"  [{mark}] {entry.checkpoint_id}  {entry.title}  [{entry.bucket}]")
        print(f"        held: {entry.reason or 'held at the gate'}")
        for problem in problems:
            print(f"        - {problem}")
        if not problems:
            batch.append(entry)
            odd = (action == "approve" and entry.bucket == "STALE") or (
                action == "reject" and entry.bucket == "SAFE"
            )
            if odd:
                print(f"        note: triage recommended the opposite for {entry.bucket}")
    print(f"\n{len(batch)} of {len(chosen)} would be {"approved" if action == "approve" else "rejected"}.")

    if action == "approve":
        allowed, lines = pace_status()
        print("\nProvider pace:")
        for line in lines:
            print(f"  {line}")
        if not allowed and not args.ignore_pace:
            print(
                "\nrefusing to approve: no provider can take a new frontier "
                "dispatch right now, and approving releases tasks that spend "
                "capacity.  Wait, or pass --ignore-pace.",
                file=sys.stderr,
            )
            return 1
    if not batch:
        print("nothing eligible; no changes made")
        return 0
    if not args.execute:
        print("\nDRY RUN: nothing was signed or written.  Add --execute to proceed.")
        return 0

    phrase = f"{action} {len(batch)} checkpoints"
    try:
        typed = input(f"\nType exactly '{phrase}' to proceed: ")
    except EOFError:
        typed = ""
    if typed.strip() != phrase:
        print("confirmation phrase not matched; no changes made", file=sys.stderr)
        return 1

    server = load_server()
    outcomes = asyncio.run(submit_batch(batch, action, triage, server))
    print_outcomes(outcomes)
    return 0 if all(o.confirmed or o.result.startswith("skipped") for o in outcomes) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--triage",
        default=os.environ.get("ATELES_CHECKPOINT_TRIAGE_FILE", DEFAULT_TRIAGE_FILE),
        help="triage file mapping tasks to SAFE / OPERATOR / STALE (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="pending signed checkpoints, grouped by bucket")
    p.add_argument("--bucket", action="append", choices=BUCKETS)
    p = sub.add_parser("show", help="one checkpoint in full")
    p.add_argument("checkpoint_id")
    for name, text in (
        ("approve", "approve checkpoints (releases their tasks)"),
        ("reject", "reject checkpoints (DECLINES their tasks)"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--bucket", action="append", choices=BUCKETS)
        p.add_argument("--checkpoint", action="append", metavar="ID")
        p.add_argument(
            "--limit",
            type=_limit,
            default=DEFAULT_MAX_PER_RUN,
            help=f"at most N per run (default {DEFAULT_MAX_PER_RUN}, ceiling {HARD_MAX_PER_RUN})",
        )
        p.add_argument(
            "--execute",
            action="store_true",
            help="actually sign and submit, after a typed confirmation (default: preview)",
        )
        if name == "approve":
            p.add_argument(
                "--ignore-pace",
                action="store_true",
                help="approve even though no provider can take a frontier dispatch",
            )
    return parser


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if not 1 <= number <= HARD_MAX_PER_RUN:
        raise argparse.ArgumentTypeError(f"must be between 1 and {HARD_MAX_PER_RUN}")
    return number


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "approve" or args.command == "reject":
        args.ignore_pace = getattr(args, "ignore_pace", False)
    try:
        triage = load_triage(Path(args.triage))
        if args.command == "list":
            return cmd_list(args, triage)
        if args.command == "show":
            return cmd_show(args, triage)
        return cmd_resolve(args, triage)
    except Fatal as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

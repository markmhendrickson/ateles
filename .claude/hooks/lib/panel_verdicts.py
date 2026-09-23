"""Panel verdict helpers for the approve-on-operator's-behalf PreToolUse guard.

Pure evaluation plus thin `gh api` I/O. Seated lenses come from
`review_panel.select_panel` (principles #9). Marker/verdict parsers come from
`review_markers` (same source Apis uses). Fail closed on unreadable data.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Repo root on sys.path so we can import Apis modules the same way daemons do.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_APIS_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(_REPO_ROOT), str(_APIS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from review_markers import (  # noqa: E402
    LENS_MARKER_RE,
    body_has_blocking_findings,
    parse_review_verdict,
)
from review_panel import select_panel  # noqa: E402

SCOPED_REPOS = frozenset({"markmhendrickson/ateles", "markmhendrickson/neotoma"})
DEFAULT_MAX_PANEL = 6

# Same keyword sets as swarm_dispatch parent-link resolution (ateles#300/#434).
_CLOSING_KEYWORDS = r"clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed)"
_PARENTAGE_KEYWORDS = r"part\s+of|refs?|references?|parent|related\s+to"
_ISSUE_REF = r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<number>\d+)"
_PARENT_LINK = re.compile(
    rf"\b(?:{_CLOSING_KEYWORDS}|{_PARENTAGE_KEYWORDS})\s*:?\s+{_ISSUE_REF}", re.I
)

EXPECTATION_MARKER = "review_expectation"
_EXPECTATION_RE = re.compile(
    rf"\*\*{EXPECTATION_MARKER} \((?P<lens>[\w-]+)\)\*\* — what "
    r"(?P<agent>\w+) will verify",
    re.I,
)
_GATE_PENDING = re.compile(r"GATE_PENDING:\s*([a-z0-9_,\s]+)", re.I)
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

SEGMENT_SPLIT = re.compile(r"&&|[;\n|]")
TEXT_BEARING_LEADERS = re.compile(
    r"^(?:git\s+(?:commit|tag|notes)|echo|printf|grep|rg)\b"
)

# `gh pr review` approve shapes.
_PR_REVIEW_APPROVE = re.compile(
    r"\bgh\b(?:\s+[^\s]+)*\s+pr\s+review\b",
    re.IGNORECASE,
)
_API_REVIEWS_PATH = re.compile(
    r"(?:^|[\s\"'/])repos/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pulls/"
    r"(?P<pr>\d+)/reviews(?:\b|[\"'?])",
    re.IGNORECASE,
)
_REPO_FLAG = re.compile(
    r"(?:--repo|-R)\s+(?P<repo>[\w.-]+/[\w.-]+)", re.IGNORECASE
)
_PR_NUMBER = re.compile(r"\bpr\s+review\s+(?P<pr>\d+)\b", re.IGNORECASE)
_EVENT_APPROVE_FIELD = re.compile(
    r"(?:-f|-F|--field)\s+event=APPROVE\b", re.IGNORECASE
)
_EVENT_APPROVE_JSON = re.compile(
    r'["\']?event["\']?\s*:\s*["\']APPROVE["\']', re.IGNORECASE
)


@dataclass(frozen=True)
class ApproveTarget:
    owner: str
    repo: str
    pr_number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass
class PanelInputs:
    changed_files: list[str]
    gate_contributors: set[str]
    pending_gates: set[str]
    max_panel: int = DEFAULT_MAX_PANEL


@dataclass
class VerdictRecord:
    lens: str
    commit_sha: str
    verdict: str | None
    has_blocking: bool
    updated_at: str
    comment_id: int | None = None


@dataclass
class Offender:
    lens: str
    kind: str  # "missing" | "blocking"
    last_reviewed: str  # sha or "none"
    verdict_label: str | None = None  # REQUEST_CHANGES | [BLOCKING]


@dataclass
class Decision:
    allowed: bool
    reason_kind: str | None = None  # missing | blocking | unreadable | None
    unreadable_cause: str | None = None
    missing: list[Offender] = field(default_factory=list)
    blocking: list[Offender] = field(default_factory=list)


class VerdictDataUnreadable(Exception):
    """Raised when head/panel/marker data cannot be trusted — fail closed."""

    def __init__(self, cause: str):
        super().__init__(cause)
        self.cause = cause


def _join_line_continuations(command: str) -> str:
    return re.sub(r"\\[ \t]*\n", " ", command)


def _normalise_sha(value: str) -> str:
    sha = (value or "").strip().lower()
    return sha if _FULL_SHA_RE.fullmatch(sha) else ""


def _run_gh(args: list[str], *, timeout: int = 30) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerdictDataUnreadable(f"gh invocation failed: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "gh failed").strip().splitlines()
        cause = err[0] if err else f"gh exit {proc.returncode}"
        raise VerdictDataUnreadable(cause)
    return proc.stdout


def _gh_api_json(path: str, *, paginate: bool = False) -> Any:
    args = ["api", path, "--method", "GET"]
    if paginate:
        args.append("--paginate")
    raw = _run_gh(args)
    # --paginate concatenates JSON arrays; wrap if needed.
    text = raw.strip()
    if not text:
        return [] if paginate else {}
    try:
        if paginate and text.startswith("["):
            # Multiple array pages concatenated: ][
            if "\n" in text and "][" in text.replace("\n", ""):
                chunks = re.split(r"\]\s*\[", text)
                items: list[Any] = []
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        chunk = chunk if chunk.endswith("]") else chunk + "]"
                    elif i == len(chunks) - 1:
                        chunk = "[" + chunk if not chunk.startswith("[") else chunk
                    else:
                        chunk = "[" + chunk + "]"
                    items.extend(json.loads(chunk))
                return items
            return json.loads(text)
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise VerdictDataUnreadable(f"unparseable gh api JSON: {exc}") from exc


def _infer_repo_from_cwd() -> str | None:
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    name = (proc.stdout or "").strip()
    return name if name in SCOPED_REPOS else None


def _repo_from_segment(segment: str) -> str | None:
    m = _REPO_FLAG.search(segment)
    if m:
        return m.group("repo")
    api = _API_REVIEWS_PATH.search(segment)
    if api:
        return f"{api.group('owner')}/{api.group('repo')}"
    return None


def _segment_has_approve_event(segment: str) -> bool:
    if _EVENT_APPROVE_FIELD.search(segment):
        return True
    if _EVENT_APPROVE_JSON.search(segment):
        return True
    # JSON body via --input / stdin often embeds event in the same segment.
    if re.search(r"\bevent=APPROVE\b", segment, re.I):
        return True
    return False


def _is_pr_review_approve(segment: str) -> bool:
    if not _PR_REVIEW_APPROVE.search(segment):
        return False
    # Approve flags; reject comment / request-changes.
    if re.search(r"(?:^|\s)(?:--approve|-a)(?:\s|$)", segment):
        if re.search(r"--(?:comment|request-changes|body)\b", segment):
            # --approve with --body is still approve; --comment alone is not.
            if re.search(r"--request-changes\b", segment):
                return False
            if re.search(r"(?:^|\s)--comment(?:\s|$)", segment) and not re.search(
                r"(?:^|\s)(?:--approve|-a)(?:\s|$)", segment
            ):
                return False
        return True
    return False


def matches_approve_call(command: str) -> ApproveTarget | None:
    """Detect a scoped operator-behalf approve; None when out of scope / non-match."""
    if not isinstance(command, str) or "gh" not in command:
        return None
    cwd_repo = None
    for segment in SEGMENT_SPLIT.split(_join_line_continuations(command)):
        normalized = " ".join(segment.split())
        if not normalized or "gh" not in normalized:
            continue
        if TEXT_BEARING_LEADERS.match(normalized):
            # Commit messages / echo / grep carry the pattern as prose.
            continue
        owner_repo = _repo_from_segment(normalized)
        if owner_repo is None:
            if cwd_repo is None:
                cwd_repo = _infer_repo_from_cwd()
            owner_repo = cwd_repo
        if owner_repo is None or owner_repo not in SCOPED_REPOS:
            # Out of scope: do not match (silent allow at hook layer).
            if _is_pr_review_approve(normalized) or (
                _API_REVIEWS_PATH.search(normalized)
                and _segment_has_approve_event(normalized)
            ):
                continue
            continue

        owner, repo = owner_repo.split("/", 1)
        pr_number: int | None = None

        if _is_pr_review_approve(normalized):
            m = _PR_NUMBER.search(normalized)
            if not m:
                continue
            pr_number = int(m.group("pr"))
            return ApproveTarget(owner=owner, repo=repo, pr_number=pr_number)

        api = _API_REVIEWS_PATH.search(normalized)
        if api and _segment_has_approve_event(normalized):
            return ApproveTarget(
                owner=api.group("owner"),
                repo=api.group("repo"),
                pr_number=int(api.group("pr")),
            )
    return None


def resolve_head_sha(owner: str, repo: str, pr: int) -> str:
    raw = _run_gh(
        ["api", f"repos/{owner}/{repo}/pulls/{pr}", "--jq", ".head.sha"]
    ).strip()
    sha = _normalise_sha(raw)
    if not sha:
        raise VerdictDataUnreadable(
            f"head.sha not a full 40-hex digest (got {raw!r})"
        )
    return sha


def parent_issue_number(pr_body: str, repository: str) -> int | None:
    for m in _PARENT_LINK.finditer(pr_body or ""):
        qualifier = m.group("repo")
        if qualifier and qualifier.lower() != (repository or "").lower():
            continue
        return int(m.group("number"))
    return None


def resolve_changed_files(owner: str, repo: str, pr: int) -> list[str]:
    """Paginate PR files. API failure → raise (fail closed; diverge from Apis)."""
    data = _gh_api_json(
        f"repos/{owner}/{repo}/pulls/{pr}/files?per_page=100", paginate=True
    )
    if not isinstance(data, list):
        raise VerdictDataUnreadable("changed-files response was not a list")
    return [f["filename"] for f in data if isinstance(f, dict) and "filename" in f]


def resolve_gate_contributors(owner: str, repo: str, parent: int | None) -> set[str]:
    if parent is None:
        return set()
    data = _gh_api_json(
        f"repos/{owner}/{repo}/issues/{parent}/comments?per_page=100",
        paginate=True,
    )
    if not isinstance(data, list):
        raise VerdictDataUnreadable("parent-issue comments response was not a list")
    agents: set[str] = set()
    for comment in data:
        body = (comment or {}).get("body") or ""
        for m in _EXPECTATION_RE.finditer(body):
            agents.add(m.group("agent").lower())
    return agents


def resolve_pending_gates(owner: str, repo: str, pr: int, parent: int | None) -> set[str]:
    """Best-effort pending-gate set.

    Fallback order (documented in PR #1198):
    1. Parse `GATE_PENDING:` from recent PR + parent-issue comments.
    2. Else empty set.

    An empty set does not shrink the seated panel below what
    `changed_files` + `gate_contributors` + always-on imply — it only
    withholds the pending-gate boost. A hard API failure here raises
    (fail closed) rather than silently under-seating.
    """
    texts: list[str] = []
    pr_comments = _gh_api_json(
        f"repos/{owner}/{repo}/issues/{pr}/comments?per_page=100",
        paginate=True,
    )
    if not isinstance(pr_comments, list):
        raise VerdictDataUnreadable("PR comments response was not a list")
    for c in pr_comments:
        texts.append((c or {}).get("body") or "")
    if parent is not None:
        issue_comments = _gh_api_json(
            f"repos/{owner}/{repo}/issues/{parent}/comments?per_page=100",
            paginate=True,
        )
        if not isinstance(issue_comments, list):
            raise VerdictDataUnreadable("parent comments response was not a list")
        for c in issue_comments:
            texts.append((c or {}).get("body") or "")
    pending: set[str] = set()
    for text in texts:
        m = _GATE_PENDING.search(text)
        if m:
            pending |= {g.strip().lower() for g in m.group(1).split(",") if g.strip()}
    return pending


def resolve_panel_inputs(owner: str, repo: str, pr: int) -> PanelInputs:
    pr_data = _gh_api_json(f"repos/{owner}/{repo}/pulls/{pr}")
    if not isinstance(pr_data, dict):
        raise VerdictDataUnreadable("pull response was not an object")
    body = pr_data.get("body") or ""
    repository = f"{owner}/{repo}"
    parent = parent_issue_number(body, repository)
    changed = resolve_changed_files(owner, repo, pr)
    contributors = resolve_gate_contributors(owner, repo, parent)
    pending = resolve_pending_gates(owner, repo, pr, parent)
    return PanelInputs(
        changed_files=changed,
        gate_contributors=contributors,
        pending_gates=pending,
        max_panel=DEFAULT_MAX_PANEL,
    )


def get_seated_panel(owner: str, repo: str, pr: int) -> list[str]:
    inputs = resolve_panel_inputs(owner, repo, pr)
    lenses = select_panel(
        gate_contributors=inputs.gate_contributors,
        changed_files=inputs.changed_files,
        max_panel=inputs.max_panel,
        pending_gates=inputs.pending_gates,
    )
    seated = [lens.lens for lens in lenses]
    if not seated:
        raise VerdictDataUnreadable("seated panel unresolved (empty after select_panel)")
    return seated


def get_seated_panel_from_inputs(inputs: PanelInputs) -> list[str]:
    lenses = select_panel(
        gate_contributors=inputs.gate_contributors,
        changed_files=inputs.changed_files,
        max_panel=inputs.max_panel,
        pending_gates=inputs.pending_gates,
    )
    seated = [lens.lens for lens in lenses]
    if not seated:
        raise VerdictDataUnreadable("seated panel unresolved (empty after select_panel)")
    return seated


def tabulate_verdicts(owner: str, repo: str, pr: int) -> dict[str, VerdictRecord]:
    """Latest-edited (`updated_at`) marker wins per lens."""
    comments = _gh_api_json(
        f"repos/{owner}/{repo}/issues/{pr}/comments?per_page=100",
        paginate=True,
    )
    if not isinstance(comments, list):
        raise VerdictDataUnreadable("PR comments response was not a list")

    # lens -> list of (updated_at, record) for ambiguity detection
    by_lens: dict[str, list[tuple[str, VerdictRecord]]] = {}
    for comment in comments:
        body = (comment or {}).get("body") or ""
        updated = (comment or {}).get("updated_at") or ""
        cid = (comment or {}).get("id")
        for m in LENS_MARKER_RE.finditer(body):
            lens = m.group("lens").lower()
            sha = _normalise_sha(m.group("sha"))
            if not sha:
                raise VerdictDataUnreadable(
                    f"malformed marker for lens {lens}: commit not 40-hex"
                )
            verdict = parse_review_verdict(body)
            rec = VerdictRecord(
                lens=lens,
                commit_sha=sha,
                verdict=verdict,
                has_blocking=body_has_blocking_findings(body),
                updated_at=updated,
                comment_id=int(cid) if cid is not None else None,
            )
            by_lens.setdefault(lens, []).append((updated, rec))

    out: dict[str, VerdictRecord] = {}
    for lens, entries in by_lens.items():
        entries.sort(key=lambda t: t[0])
        latest_ts = entries[-1][0]
        at_latest = [rec for ts, rec in entries if ts == latest_ts]
        shas = {r.commit_sha for r in at_latest}
        if len(shas) > 1:
            raise VerdictDataUnreadable(
                f"ambiguous dual markers for {lens} at updated_at={latest_ts}"
            )
        out[lens] = at_latest[-1]
    return out


def evaluate(
    seated: list[str],
    verdicts: dict[str, VerdictRecord],
    head_sha: str,
    *,
    unreadable_cause: str | None = None,
) -> Decision:
    """Pure decision: current-head all-clear, or don't approve."""
    if unreadable_cause:
        return Decision(
            allowed=False,
            reason_kind="unreadable",
            unreadable_cause=unreadable_cause,
        )
    head = _normalise_sha(head_sha)
    if not head:
        return Decision(
            allowed=False,
            reason_kind="unreadable",
            unreadable_cause="head sha missing or not 40-hex",
        )
    if not seated:
        return Decision(
            allowed=False,
            reason_kind="unreadable",
            unreadable_cause="seated panel unresolved",
        )

    missing: list[Offender] = []
    blocking: list[Offender] = []
    for lens in seated:
        rec = verdicts.get(lens)
        if rec is None:
            missing.append(
                Offender(lens=lens, kind="missing", last_reviewed="none")
            )
            continue

        is_blocking_verdict = (
            rec.verdict == "request_changes" or rec.has_blocking
        )
        if is_blocking_verdict:
            # Stale REQUEST_CHANGES / [BLOCKING] still surfaces as blocking
            # (ateles#1181 worked deny: security on earlier SHA is `blocking:`,
            # not `missing:`).
            if rec.verdict == "request_changes":
                label = "REQUEST_CHANGES"
            else:
                label = "[BLOCKING]"
            blocking.append(
                Offender(
                    lens=lens,
                    kind="blocking",
                    last_reviewed=rec.commit_sha,
                    verdict_label=label,
                )
            )
            continue

        if rec.commit_sha != head:
            missing.append(
                Offender(
                    lens=lens,
                    kind="missing",
                    last_reviewed=rec.commit_sha,
                )
            )
            continue
        # At head, clean token (APPROVE / COMMENT / BLOCKED / SIGNED_OFF)
        # without [BLOCKING] — OK.

    if missing or blocking:
        kind = "blocking" if blocking and not missing else (
            "missing" if missing and not blocking else "missing"
        )
        # Prefer "blocking" when any blocking present alongside missing? UX
        # example shows both. reason_kind is the primary why — use the more
        # severe when both.
        if blocking and missing:
            kind = "blocking"
        elif blocking:
            kind = "blocking"
        else:
            kind = "missing"
        return Decision(
            allowed=False,
            reason_kind=kind,
            missing=missing,
            blocking=blocking,
        )
    return Decision(allowed=True)


def format_deny_reason(
    owner: str,
    repo: str,
    pr: int,
    head_sha: str,
    decision: Decision,
) -> str:
    head = head_sha or "unknown"
    lines = [
        f"Refused: approve blocked for {owner}/{repo}#{pr} at head {head}"
    ]
    if decision.reason_kind == "unreadable":
        cause = decision.unreadable_cause or "unknown"
        lines.append(f"  verdict data unreadable: {cause}")
    else:
        for off in decision.missing:
            lines.append(
                f"  missing: {off.lens} last_reviewed={off.last_reviewed}"
            )
        for off in decision.blocking:
            label = off.verdict_label or "REQUEST_CHANGES"
            lines.append(
                f"  blocking: {off.lens} verdict={label} "
                f"last_reviewed={off.last_reviewed}"
            )
    lines.append(
        "Next: re-drive seated lenses (or /swarm-run) until each has "
        f"<!-- review:<lens> commit={head} --> and none is REQUEST_CHANGES / "
        "[BLOCKING]; then retry approve."
    )
    lines.append(
        "Background: ateles#1181 (2026-09-23 stale-marker false approve)."
    )
    return "\n".join(lines)

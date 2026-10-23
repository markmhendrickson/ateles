"""Shared declarative artifact contracts for Apis + Anthus (ateles#1155).

One source of truth for role → artifact_kind (+ gate aliases, body shapes,
resolver policy). Apis direct-task completion and Anthus gate satisfaction
both derive from ``ARTIFACT_CONTRACTS`` — a comment claiming parity is not
parity (principles.md#9).

Pure functions only — no I/O. Call sites inject ``resolve_artifact_ref``.
"""

from __future__ import annotations

import re
import shlex
import unicodedata
from dataclasses import dataclass
from typing import Callable, Literal

CauseCode = Literal[
    "missing_header",
    "empty_body",
    "blocked",
    "unresolvable_ref",
    "invalid_ref_shape",
    "wrong_body_for_dispatch",
    "record_not_saved",
    "ref_unverifiable",
    "ref_check_unavailable",
    "gate_error",
]

# What the resolver can say about a PR or commit reference. "unavailable" is
# NOT "absent": a rate limit, 5xx, timeout or OSError says nothing about whether
# the ref exists, and treating it as absent would send a task with a real PR into
# the watchdog's automatic re-run lane (a second PR).
RefCheck = Literal["exists", "absent", "unavailable"]

# Causes whose task status is FAILED, which the stall watchdog re-runs
# automatically with backoff (TaskWatchdog.classify("failed") -> RETRY). Every
# other cause is BLOCKED, which nothing retries.
WATCHDOG_RETRIED: frozenset[str] = frozenset(
    {
        "missing_header",
        "empty_body",
        "wrong_body_for_dispatch",
        "invalid_ref_shape",
        "unresolvable_ref",
    }
)

BodyShape = Literal["pr_or_commit", "eng_spec_section", "prose"]
ResolverPolicy = Literal["github_ref", "none"]
BodyClass = Literal["blocked", "valid", "empty"]

ARTIFACT_GATE_PREFIX = "[ARTIFACT_GATE]"

# Shell / injection metacharacters never allowed in a ref body.
_SHELL_METACHAR_RE = re.compile(r"[;&|`$()<>\\]")

# The PR number is bounded to 9 digits: `int()` on thousands of digits raises
# ValueError (Python's 4300-digit limit), and no repository has a billion PRs.
# ASCII-only: with Unicode semantics `\d` matches other scripts' digits (int() then
# accepts them) and IGNORECASE folds a few non-ASCII letters onto ASCII ones.
_PR_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[0-9]{1,9})\s*$",
    re.IGNORECASE | re.ASCII,
)
_PR_SHORTHAND_RE = re.compile(
    r"^(?:PR\s*)?#(?P<number>[0-9]{1,9})\s*$",
    re.IGNORECASE | re.ASCII,
)
_SHA_RE = re.compile(r"^(?P<sha>[0-9a-f]{40})\s*$", re.IGNORECASE | re.ASCII)
# A GitHub owner/repo as it may reach `gh` argv: the same character class the URL
# regex allows, with no leading dash (would read as an option) and no dot-only
# component.
_REPO_PART_RE = re.compile(r"^(?![-.])[A-Za-z0-9_.-]+$", re.ASCII)
# An abbreviated SHA (7-39 hex) is recognized as a ref SHAPE so it is refused as
# `ref_unverifiable` (BLOCKED: a real commit may exist) rather than mis-read as
# prose (`wrong_body_for_dispatch`): it answered the right question, but a
# prefix can be ambiguous, so this gate never resolves one. Only a full 40-char
# SHA is accepted as a commit ref.
# A GitHub PR / issue / commit link anywhere in the text. A body that contains one
# but is not exactly one PR URL is a near miss (a wrapper sentence, a `/files` or
# `#fragment` or `?query` suffix, an issue link): the agent very probably DID
# open something, so it is BLOCKED rather than retried into a duplicate.
_GH_URL_IN_TEXT_RE = re.compile(
    r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:pull|issues|commit)/[A-Za-z0-9]+",
    re.IGNORECASE | re.ASCII,
)
_SHORT_SHA_RE = re.compile(r"^(?P<sha>[0-9a-f]{7,39})\s*$", re.IGNORECASE | re.ASCII)
_ENG_SPEC_RE = re.compile(r"^ENG_SPEC_SECTION\b", re.IGNORECASE)

# Plain-language wording for each cause: what happened, and what the operator
# should do next. Kept beside the codes so the runbook table cannot drift from
# what the alert says (test_artifact_contract.py binds every code to both).
CAUSE_HINTS: dict[CauseCode, str] = {
    "missing_header": (
        "The agent finished but did not end with the required header line, so no "
        "deliverable was recorded."
    ),
    "empty_body": "The header line was present but named nothing after the colon.",
    "blocked": (
        "The agent stopped and asked for something instead of delivering (its words "
        "are quoted below)."
    ),
    "unresolvable_ref": (
        "The PR or commit reference is well-formed and GitHub answered that it does "
        "not exist."
    ),
    "invalid_ref_shape": (
        "The agent offered a reference this gate will not accept: a link to another "
        "host or a non-PR page, or malformed text."
    ),
    "wrong_body_for_dispatch": (
        "The agent answered a different question than this dispatch asked."
    ),
    "ref_unverifiable": (
        "The reference cannot be checked as offered, so the agent's work is neither "
        "accepted nor judged wrong. A PR or commit may already exist."
    ),
    "ref_check_unavailable": (
        "GitHub could not be asked right now (rate limit, outage, timeout or network "
        "error), so the reference is neither confirmed nor refuted. A PR or commit "
        "very likely exists."
    ),
    "record_not_saved": (
        "The agent's deliverable was accepted, but the task record did not save "
        "(the result or status write did not read back)."
    ),
    "gate_error": (
        "The artifact gate itself raised an error while judging the deliverable, so "
        "the deliverable is neither accepted nor judged wrong. The error is in the "
        "detail field of this line."
    ),
}

# `ref_unverifiable` is one code but several situations; each has its own wording.
UNVERIFIABLE_HINTS: dict[str, str] = {
    "no_repo": (
        "The task records no repo, so the offered reference cannot be checked against "
        "one. A PR or commit may already exist."
    ),
    "bad_repo": (
        "The repo recorded on the task is malformed, so the offered reference cannot "
        "be checked against it. A PR or commit may already exist."
    ),
    "other_repo": (
        "The offered PR link names a different repo than the task's. That PR exists "
        "in the wrong place; a retry would open another."
    ),
    "short_sha": (
        "The reference is an abbreviated SHA; only a full 40-character SHA can be "
        "checked. The commit may already exist."
    ),
    "near_miss": (
        "The body contains a GitHub link but is not exactly one PR URL (a wrapper "
        "sentence, an issue or commit link, or a suffix such as /files, #fragment or "
        "?query). The agent very likely opened something."
    ),
}

# Deliverable-shape wording for `record_not_saved`: the PR wording is false for a
# prose deliverable or an eng-spec section.
RECORD_NOT_SAVED_TEXT_HINT = (
    "The agent's deliverable text was accepted, but the task record did not save "
    "(the result or status write did not read back)."
)

_RETRY_NOTE = (
    "The task is FAILED, so the stall watchdog re-runs it automatically with "
    "backoff (up to APIS_MAX_TASK_ATTEMPTS attempts, default 3); you do not need "
    "to re-dispatch it."
)
_RECORD_BY_HAND = (
    "If a real deliverable already exists, record it as the task result with status "
    "done before the retries run, or a retry may duplicate it (procedure: "
    "docs/agent_execution_runbook.md, 'Recording a deliverable by hand')."
)
_BLOCKED_NOTE = "The task is BLOCKED and is NOT retried automatically."

# Words only. The recording commands (when there is a real value to record) are a
# separate block added by `artifact_gate_reason`.
CAUSE_NEXT_STEPS: dict[CauseCode, str] = {
    "missing_header": _RETRY_NOTE + " " + _RECORD_BY_HAND,
    "empty_body": _RETRY_NOTE + " " + _RECORD_BY_HAND,
    "blocked": (
        "Give the agent what it says it needs, then reopen the task by setting its "
        "status back to routed (below); a BLOCKED task is not retried automatically, "
        "and the stall watchdog re-dispatches a routed task after its stall window."
    ),
    "unresolvable_ref": _RETRY_NOTE + " " + _RECORD_BY_HAND,
    "invalid_ref_shape": _RETRY_NOTE + " " + _RECORD_BY_HAND,
    "wrong_body_for_dispatch": _RETRY_NOTE + " " + _RECORD_BY_HAND,
    "ref_unverifiable": (
        _BLOCKED_NOTE + " Do not re-dispatch (it could open a second PR). Check the "
        "offered reference by hand, then record it and mark the task done."
    ),
    "ref_check_unavailable": (
        _BLOCKED_NOTE + " Do not re-dispatch. Once GitHub is reachable, check the "
        "reference by hand, then record it and mark the task done."
    ),
    "record_not_saved": (
        _BLOCKED_NOTE + " Do not re-dispatch (it would redo the agent's work). "
        "Record the deliverable and mark the task done."
    ),
    "gate_error": (
        _BLOCKED_NOTE + " Do not re-dispatch. Read the detail field above and the "
        "dispatcher log for this task, then record or resolve the deliverable by hand."
    ),
}

RECORD_NOT_SAVED_PR_EXTRA = " The PR or commit exists; a retry would open a second one."
RECORD_NOT_SAVED_TEXT_EXTRA = " The deliverable is not a PR; a retry would redo the work."

# The longest value the alert will put into a copy-paste command. A longer
# deliverable is never truncated into the command (a truncated value would be
# recorded as the result); the words say where the full text is.
MAX_RECORD_VALUE_CHARS = 500


def trim_on_word(text: str, limit: int = 160) -> str:
    """Trim to *limit* characters on a word boundary, marking the cut with an ellipsis."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if not text[limit].isspace() and " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:") + "…"


def sanitize_text(text: str | None, *, keep_newlines: bool = False) -> str:
    """Make agent-controlled text safe to embed in a one-line operator message.

    Format characters (bidi overrides, zero-width) are removed; control characters
    and the Unicode line / paragraph separators (U+2028, U+2029, U+0085, vertical
    tab, form feed, ...) become spaces so agent text cannot start a new line or
    spoof surrounding markup. ``keep_newlines`` keeps `\\n` and `\\t` for multi-line
    output such as a stdout tail (everything else is still neutralized).
    """
    out = []
    for ch in text or "":
        if keep_newlines and ch in "\n\t":
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat == "Cf":
            continue
        out.append(" " if cat in {"Cc", "Zl", "Zp"} else ch)
    return "".join(out)


def pr_url(parsed: "ParsedRef") -> str | None:
    """The full https URL of a parsed PR / commit reference, or None if not derivable."""
    if not (parsed.owner and parsed.repo):
        return None
    if parsed.kind == "pr" and parsed.number is not None:
        return f"https://github.com/{parsed.owner}/{parsed.repo}/pull/{parsed.number}"
    if parsed.kind == "sha" and parsed.sha:
        return f"https://github.com/{parsed.owner}/{parsed.repo}/commit/{parsed.sha}"
    return None


def complete_reference_url(text: str | None) -> str | None:
    """*text* if it is exactly one full PR URL (self-sufficient to record), else None."""
    t = (text or "").strip()
    return t if _PR_URL_RE.match(t) else None


def record_commands(task_id: str, value: str) -> list[str]:
    """The copy-ready commands to record a deliverable by hand, one command per line.

    Three SEPARATE commands (never a prose sentence): the result correction, the
    status correction, and the read-back. Every command carries the global
    `--api-only`: without it the CLI defaults data commands to the in-process
    LOCAL store, so the correction and the read-back would both hit the wrong store
    and give false confirmation while the hosted task stays stuck. Arguments are
    shell-quoted, so the printed line splits back into exactly this argv.
    """
    base = ["neotoma", "--api-only", "corrections", "create",
            "--entity-id", task_id, "--entity-type", "task"]
    return [
        shlex.join(base + ["--field-name", "result", "--corrected-value", value]),
        shlex.join(base + ["--field-name", "status", "--corrected-value", "done"]),
        shlex.join(["neotoma", "--api-only", "entities", "get", task_id]),
    ]


def reopen_command(task_id: str) -> str:
    """Set a BLOCKED task back to routed (the documented operator remediation)."""
    return shlex.join(
        ["neotoma", "--api-only", "corrections", "create", "--entity-id", task_id,
         "--entity-type", "task", "--field-name", "status", "--corrected-value", "routed"]
    )


_CANONICAL_PR_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>[0-9]{1,9})$", re.ASCII
)


def gh_check_command(offered: str | None, expected_repo: str | None) -> str | None:
    """A real `gh` command the operator can run to check a reference, or None.

    Never a placeholder: with no known repo a bare `#N` or SHA has no command (the
    words say to find the PR first), consistent with the recording commands.
    """
    text = (offered or "").strip()
    m = _PR_URL_RE.match(text) or _CANONICAL_PR_RE.match(text)
    if m:
        return shlex.join(["gh", "pr", "view", m.group("number"), "--repo",
                           f"{m.group('owner')}/{m.group('repo')}"])
    repo = _split_dispatch_repo(expected_repo)
    if repo is None:
        return None
    short = _PR_SHORTHAND_RE.match(text)
    if short:
        return shlex.join(["gh", "pr", "view", short.group("number"), "--repo", "/".join(repo)])
    if _SHORT_SHA_RE.match(text) or _SHA_RE.match(text):
        return shlex.join(["gh", "api", f"repos/{repo[0]}/{repo[1]}/commits/{text}", "--jq", ".sha"])
    return None


def alert_text(reason: str, limit: int = 1800) -> str:
    """Fit *reason* into an operator alert without ever cutting a command mid-line.

    The recovery commands (the block that starts at the "Record it by hand" or
    "Reopen" label) are kept WHOLE or dropped whole: a command cut mid-line is
    worse than none. If the text fits it is returned unchanged; otherwise the head
    is trimmed to leave room for the whole command block, and if even that does not
    leave a usable head the commands are dropped with a pointer to the reason.
    """
    if len(reason) <= limit:
        return reason
    idx = min(
        (i for i in (reason.find("\nRecord it by hand"), reason.find("\nReopen")) if i >= 0),
        default=-1,
    )
    if idx < 0:
        return reason[:limit] + "…"
    head, commands = reason[:idx], reason[idx:]
    room = limit - len(commands)
    if room >= 600:
        return (head if len(head) <= room else head[:room] + "…") + commands
    return head[: limit - 120] + "…\n(recovery commands omitted here; see the task's reason and the runbook)"


_GH_UNAVAILABLE_MARKERS = (
    "rate limit",
    "secondary rate",
    "abuse",
    "http 403",
    "http 429",
    "http 401",
    "bad credentials",
    "http 5",  # 500 / 502 / 503 / 504
    "timed out",
    "timeout",
    "connection",
    "could not resolve host",
    "tls",
    "eof",
    "could not resolve to a repository",  # unknown repo OR no access: cannot tell
)
_GH_ABSENT_MARKERS = (
    "could not resolve to a pullrequest",
    "no pull requests found",
    "no commit found for sha",
    "http 422",
)
# NOT here: "http 404". The only path that can emit it is the commit lookup
# (`gh api repos/o/r/commits/<sha>`), where GitHub answers 422 for a missing
# commit and 404 ONLY when the repository is missing or not visible to the token,
# byte-identical to a private repo this identity cannot see. A real commit there
# must not be called absent (that would be FAILED, i.e. re-run, i.e. duplicate
# work), so a 404 falls through to "unavailable".


def classify_gh_ref_failure(returncode: int, stderr: str) -> RefCheck:
    """Map a finished `gh` invocation to a tri-state answer.

    Only a clear not-found is "absent". Anything else that failed (rate limit,
    auth, 5xx, unknown non-zero) is "unavailable": it says nothing about whether
    the reference exists, and calling it absent would push a task that has a real
    PR into the watchdog's re-run lane. Unavailable markers win over absent ones.
    """
    if returncode == 0:
        return "exists"
    text = (stderr or "").lower()
    if any(m in text for m in _GH_UNAVAILABLE_MARKERS):
        return "unavailable"
    if any(m in text for m in _GH_ABSENT_MARKERS):
        return "absent"
    return "unavailable"


@dataclass(frozen=True)
class ArtifactContract:
    """Declarative artifact requirement for one swarm role."""

    role: str
    artifact_kind: str
    gates: tuple[str, ...]
    accepted_body_shapes: frozenset[str]
    resolver_policy: ResolverPolicy
    # Shapes in `accepted_body_shapes` that are accepted ONLY on a dispatch that
    # asked for them (`ordered_spec` / `eng_lens`), not on a generic one. The
    # table is the enforced source: `body_shape_for_dispatch` reads this field
    # and carries no per-role special case.
    mode_gated_shapes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ArtifactHeader:
    """One parsed ``[role] kind: body`` line."""

    agent: str
    kind: str
    body: str
    matched_line: str


@dataclass(frozen=True)
class ParsedRef:
    """Structured GitHub PR or commit reference."""

    kind: Literal["pr", "sha"]
    owner: str | None = None
    repo: str | None = None
    number: int | None = None
    sha: str | None = None
    canonical: str = ""


@dataclass(frozen=True)
class InvalidRef:
    """Ref body rejected before any external resolve."""

    code: CauseCode
    reason: str
    body: str = ""
    # For `ref_unverifiable`: which situation, so the hint and check command fit.
    situation: str = ""


# Seeded from the pre-#1155 Anthus GATE_SATISFACTION_RULES map (short + verbose
# gate vocabularies). Every gate name that map carried MUST appear here, or the
# workflow owning it stalls at its phase forever and does so silently
# (ateles#568) — `test_artifact_contract_parity.py` binds that.
#
# `role` is the swarm role that produces the artifact, as written in
# `agent_definition.name` / `task.assigned_to`, because Apis looks the contract
# up by the role it just dispatched. Owners are taken from the expected-agent
# table in `docs/smoke_test_runbook.md`. Two gates need a producer no roster
# agent uniquely owns — `draft_lint` (a deterministic lint runner,
# `execution/scripts/draft_lint.py`) and `post` (the publication surface, whose
# author role already carries `social_post_draft` for `draft`). Those two carry
# function-named roles so one role never claims two artifact kinds; Apis cannot
# dispatch to them, which is correct — neither is an agent.
#
# Roles absent from this table keep Apis's legacy process-ok→done behaviour.
ARTIFACT_CONTRACTS: tuple[ArtifactContract, ...] = (
    ArtifactContract(
        role="pavo",
        artifact_kind="acceptance_criteria",
        gates=("pm", "pm_scope"),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="accipiter",
        artifact_kind="copy_and_ux_flow",
        gates=("ux", "ux_design"),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="manucode",
        artifact_kind="copy_and_ux_flow",
        gates=("copy",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="waxwing",
        artifact_kind="schema_or_api_proposal",
        gates=("arch",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    # The only contract with an external resolve today: a pull_request_link that
    # names no reachable PR or commit is the false completion this exists to
    # catch, and prose cannot stand in for it.
    ArtifactContract(
        role="cicada",
        artifact_kind="pull_request_link",
        gates=("impl",),
        accepted_body_shapes=frozenset({"pr_or_commit", "eng_spec_section"}),
        resolver_policy="github_ref",
        # An eng-spec section answers the ordered-spec / eng-lens question; on a
        # generic direct implementation it is how a task closes without one.
        mode_gated_shapes=frozenset({"eng_spec_section"}),
    ),
    ArtifactContract(
        role="phoenicurus",
        artifact_kind="test_plan",
        gates=("qa",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="buteo",
        artifact_kind="compliance_review",
        gates=("legal",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="robin",
        artifact_kind="compliance_verdict",
        gates=("compliance_supervisor",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="vanellus",
        artifact_kind="merge_decision",
        gates=("pr_review",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="struthio",
        artifact_kind="release_note",
        gates=("release",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    # The smoke-test table names Accipiter here, who already carries
    # `copy_and_ux_flow` above; Ciconia is the go-to-market owner in
    # `execution/daemons/apis/routing.py` DOMAIN_ROUTES, and the derived
    # gate→kind map is identical either way.
    ArtifactContract(
        role="ciconia",
        artifact_kind="launch_brief",
        gates=("growth_announce",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="corvus",
        artifact_kind="social_post_draft",
        gates=("social_draft", "draft"),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="regulus",
        artifact_kind="docs_diff_or_no_change_note",
        gates=("devrel_docs",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="draft_lint_runner",
        artifact_kind="lint_report",
        gates=("draft_lint",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
    ArtifactContract(
        role="social_publisher",
        artifact_kind="published_post_link",
        gates=("post",),
        accepted_body_shapes=frozenset({"prose"}),
        resolver_policy="none",
    ),
)


def role_required_artifact() -> dict[str, ArtifactContract]:
    """Role/skill name → contract. Roles absent keep legacy Apis done-on-ok."""
    return {c.role: c for c in ARTIFACT_CONTRACTS}


def gate_satisfaction_rules() -> dict[str, str]:
    """Gate name → required artifact_kind (Anthus consumer view)."""
    out: dict[str, str] = {}
    for contract in ARTIFACT_CONTRACTS:
        for gate in contract.gates:
            out[gate] = contract.artifact_kind
    return out


# Only the tail of very large output is scanned: the LAST match wins and a
# deliverable header is the agent's closing line, so an earlier megabyte of
# noise cannot change the answer but could cost time.
_MAX_SCAN_CHARS = 1_000_000


def parse_artifact_header(
    text: str,
    *,
    agent: str,
    artifact_kind: str,
) -> ArtifactHeader | None:
    """Last MULTILINE match of ``[agent] kind: body``; exact matched line returned.

    Linear in the scanned text: every gap is horizontal whitespace and the body is
    `[^\\n]*`, so a run of newlines or spaces cannot make the engine retry the
    same span from every line start.

    Grammar vs the pre-#1155 Anthus regex (which used `\\s` everywhere): the gaps
    still accept every Unicode whitespace character that is not a line break
    (NBSP, ideographic space, form feed, ...), so those headers still match. The
    one deliberate difference is that a newline between the `[agent]` tag and the
    kind (or before the colon) no longer matches: a header is one line.
    """
    if not text:
        return None
    if len(text) > _MAX_SCAN_CHARS:
        text = text[-_MAX_SCAN_CHARS:]
        text = text.split("\n", 1)[1] if "\n" in text else text
    hs = r"[^\S\r\n]"  # horizontal whitespace: any \s except a line break
    pattern = re.compile(
        rf"^(?P<line>{hs}*\[(?P<agent>{re.escape(agent)})\]{hs}+"
        rf"(?P<kind>{re.escape(artifact_kind)}){hs}*:{hs}*(?P<body>[^\n]*))$",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    return ArtifactHeader(
        agent=m.group("agent"),
        kind=m.group("kind"),
        body=(m.group("body") or "").strip(),
        matched_line=m.group("line").strip(),
    )


# The stop-and-ask body: the word BLOCKED followed by nothing, or by any
# separator an agent might use (colon, em/en dash, hyphen, comma, full stop,
# parenthetical, or just whitespace and then its reason). `\\b` keeps a longer
# word such as BLOCKED_BY_X or BLOCKEDNESS from matching.
_BLOCKED_RE = re.compile(r"^BLOCKED\b", re.IGNORECASE | re.ASCII)


def classify_artifact_body(body: str) -> BodyClass:
    """Classify header body: blocked / empty / valid."""
    stripped = (body or "").strip()
    if not stripped:
        return "empty"
    if _BLOCKED_RE.match(stripped):
        return "blocked"
    return "valid"


def looks_like_pr_or_commit_ref(body: str) -> bool:
    """True when body looks like a PR/commit *attempt* (incl. invalid shapes).

    Foreign hosts and shell metacharacters still count as attempts so the
    dispatcher can emit ``invalid_ref_shape`` rather than collapsing them into
    generic ``wrong_body_for_dispatch``.
    """
    stripped = (body or "").strip()
    if not stripped:
        return False
    if _ENG_SPEC_RE.match(stripped):
        return False
    if re.match(r"^https?://", stripped, re.IGNORECASE):
        return True
    if _GH_URL_IN_TEXT_RE.search(stripped):
        return True  # a sentence around a GitHub link: a (near-miss) ref attempt
    if _SHA_RE.match(stripped) or _SHORT_SHA_RE.match(stripped):
        return True
    if _PR_SHORTHAND_RE.match(stripped):
        return True
    # Metachar-bearing PR-ish text (e.g. ``PR #1; curl``) — still a ref attempt.
    if _SHELL_METACHAR_RE.search(stripped) and (
        "#" in stripped or stripped.upper().startswith("PR")
    ):
        return True
    return False


def eng_spec_has_content(body: str) -> bool:
    """False for a bare ``ENG_SPEC_SECTION`` token with nothing authored after it."""
    rest = _ENG_SPEC_RE.sub("", (body or "").strip(), count=1)
    return bool(rest.strip(" \t:—–-"))


def infer_body_shape(body: str) -> BodyShape:
    """Label a valid body's shape so the caller can test it against a contract.

    `prose` is the floor, not a failure: for most roles prose IS the artifact,
    and only a contract that accepts narrower shapes turns it into a refusal.
    """
    stripped = (body or "").strip()
    if _ENG_SPEC_RE.match(stripped):
        return "eng_spec_section"
    if looks_like_pr_or_commit_ref(stripped):
        return "pr_or_commit"
    return "prose"


def body_shape_for_dispatch(*, role: str, dispatch_mode: str | None) -> frozenset[str]:
    """Accepted shapes for this role under the given dispatch mode.

    Read from the contract table, with no per-role special case:

    * `mode_gated_shapes` (Cicada's `eng_spec_section`) are accepted only on an
      `ordered_spec` / `eng_lens` dispatch, the ones that actually asked for the
      section; on a generic direct implementation they are how a task closes
      without an implementation.
    * A contract that accepts `prose` accepts any shape: prose is the floor, and a
      docs note that links its PR is still a valid note. Only a contract that
      NARROWS the shapes turns a shape into a refusal.
    """
    contract = role_required_artifact().get(role)
    if contract is None:
        return frozenset()
    mode = (dispatch_mode or "").strip().lower()
    accepted = set(contract.accepted_body_shapes)
    if mode not in {"ordered_spec", "eng_lens"}:
        accepted -= contract.mode_gated_shapes
    if "prose" in accepted:
        return frozenset({"prose", "pr_or_commit", "eng_spec_section"})
    return frozenset(accepted)


def _split_dispatch_repo(dispatch_repo: str | None) -> tuple[str, str] | None:
    """`(owner, name)`, or None unless both parts are safe to hand to `gh` argv.

    Validated against the same character class the URL regex allows, so a value
    such as `--flag/x`, `a/b/c`, or one with spaces or metacharacters never
    reaches a subprocess argument.
    """
    if not dispatch_repo:
        return None
    parts = dispatch_repo.strip().split("/")
    if len(parts) != 2:
        return None
    owner, name = parts
    if not (_REPO_PART_RE.match(owner) and _REPO_PART_RE.match(name)):
        return None
    return owner, name


def parse_github_ref(
    body: str,
    *,
    dispatch_repo: str | None,
) -> ParsedRef | InvalidRef:
    """Canonicalize a PR/commit body against ``dispatch_repo`` context."""
    stripped = (body or "").strip()
    if not stripped:
        return InvalidRef(code="invalid_ref_shape", reason="empty ref", body=stripped)
    if _ENG_SPEC_RE.match(stripped):
        return InvalidRef(
            code="invalid_ref_shape",
            reason="ENG_SPEC_SECTION is not a github ref",
            body=stripped,
        )
    if _GH_URL_IN_TEXT_RE.search(stripped) and not _PR_URL_RE.match(stripped):
        # Never handed to `gh`: only classified.
        return InvalidRef(
            code="ref_unverifiable",
            reason=(
                "the body contains a GitHub link but is not exactly one PR URL "
                "(a wrapper sentence, an issue or commit link, or a suffix such as "
                "/files, #fragment or ?query)"
            ),
            body=stripped,
            situation="near_miss",
        )
    if _SHELL_METACHAR_RE.search(stripped):
        return InvalidRef(
            code="invalid_ref_shape",
            reason="shell metacharacters in ref",
            body=stripped,
        )

    # Reject non-github absolute URLs (foreign host / SSRF surface).
    if re.match(r"^https?://", stripped, re.IGNORECASE):
        m = _PR_URL_RE.match(stripped)
        if not m:
            return InvalidRef(
                code="invalid_ref_shape",
                reason="foreign host or non-PR URL",
                body=stripped,
            )
        owner, repo, number = m.group("owner"), m.group("repo"), int(m.group("number"))
        expected = _split_dispatch_repo(dispatch_repo)
        if expected is None:
            # Same rule as a bare #N or SHA: with no expected repo there is
            # nothing to check the URL against, and a full PR URL for ANY
            # public repo would resolve and close the task.
            return InvalidRef(
                code="ref_unverifiable",
                reason=(
                    "PR URL, and the task records no repo to check it against"
                    if not dispatch_repo
                    else "PR URL, and the repo recorded on the task is malformed"
                ),
                body=stripped,
                situation="no_repo" if not dispatch_repo else "bad_repo",
            )
        if (owner.lower(), repo.lower()) != (
            expected[0].lower(),
            expected[1].lower(),
        ):
            # A real PR exists, in the wrong repo: BLOCKED, so a retry does not
            # open a second one.
            return InvalidRef(
                code="ref_unverifiable",
                reason="cross-repo PR URL vs the repo recorded on the task",
                body=stripped,
                situation="other_repo",
            )
        return ParsedRef(
            kind="pr",
            owner=owner,
            repo=repo,
            number=number,
            canonical=f"{owner}/{repo}#{number}",
        )

    if _SHORT_SHA_RE.match(stripped) and not _SHA_RE.match(stripped):
        return InvalidRef(
            code="ref_unverifiable",
            reason="abbreviated SHA; only a full 40-character SHA can be checked",
            body=stripped,
            situation="short_sha",
        )

    sha_m = _SHA_RE.match(stripped)
    if sha_m:
        owner_repo = _split_dispatch_repo(dispatch_repo)
        if not owner_repo:
            return InvalidRef(
                code="ref_unverifiable",
                reason=(
                    "SHA, and the task records no repo to check it against"
                    if not dispatch_repo
                    else "SHA, and the repo recorded on the task is malformed"
                ),
                body=stripped,
                situation="no_repo" if not dispatch_repo else "bad_repo",
            )
        sha = sha_m.group("sha").lower()
        return ParsedRef(
            kind="sha",
            owner=owner_repo[0],
            repo=owner_repo[1],
            sha=sha,
            canonical=sha,
        )

    short_m = _PR_SHORTHAND_RE.match(stripped)
    if short_m:
        owner_repo = _split_dispatch_repo(dispatch_repo)
        if not owner_repo:
            return InvalidRef(
                code="ref_unverifiable",
                reason=(
                    "bare #N, and the task records no repo to say which repository it means"
                    if not dispatch_repo
                    else "bare #N, and the repo recorded on the task is malformed"
                ),
                body=stripped,
                situation="no_repo" if not dispatch_repo else "bad_repo",
            )
        number = int(short_m.group("number"))
        return ParsedRef(
            kind="pr",
            owner=owner_repo[0],
            repo=owner_repo[1],
            number=number,
            canonical=f"{owner_repo[0]}/{owner_repo[1]}#{number}",
        )

    return InvalidRef(
        code="invalid_ref_shape",
        reason="unrecognized ref shape",
        body=stripped,
    )


def artifact_gate_reason(
    code: CauseCode,
    *,
    role: str,
    kind: str | None = None,
    extra: str = "",
    verbatim_body: str | None = None,
    task_id: str | None = None,
    offered: str | None = None,
    expected_repo: str | None = None,
    ref_based: bool = True,
    record_value: str | None = None,
    situation: str | None = None,
    redact: Callable[[str], str] | None = None,
) -> str:
    """Build a grep-stable ``[ARTIFACT_GATE] <code> …`` reason string.

    Layout: a grep-stable head (`[ARTIFACT_GATE] <code> role=… kind=… offered=…
    expected_repo=… detail=…`), then what happened, then the agent's own words on
    their own line, then a `Next:` line saying whether the watchdog will retry and
    what the operator can do, then (only when there is a real value to record) the
    copy-ready commands, one per line, indented two spaces.

    Every piece of agent-controlled text (``offered``, ``verbatim_body``,
    ``extra``, ``record_value``) goes through ``redact`` when given, and then
    through `sanitize_text`, so a secret in an agent's header never reaches the
    task reason, the operator alert, the run thread or a printed command.

    ``record_value`` is the FULL value a command would record; there is no
    placeholder and no truncation: if it is longer than `MAX_RECORD_VALUE_CHARS`
    the commands are withheld and the words say to copy it from the run's stdout.
    ``ref_based`` is False for a deliverable that is text rather than a PR / commit
    (only `record_not_saved` changes). ``situation`` picks the `ref_unverifiable`
    wording and the `gh` check command.
    """
    def clean(text: str | None) -> str:
        # Sanitize BEFORE redacting: one invisible character (zero-width space,
        # soft hyphen, word joiner, a bidi mark) inside a token defeats a plain
        # substring redaction, so the invisible characters go first.
        t = sanitize_text(text or "")
        return sanitize_text(redact(t) if redact else t)

    parts = [f"{ARTIFACT_GATE_PREFIX} {code}", f"role={role}"]
    if kind is not None:
        parts.append(f"kind={kind}")
    if offered is not None:
        parts.append(f'offered="{trim_on_word(clean(offered), 160)}"')
    if expected_repo is not None or offered is not None:
        shown = clean(expected_repo) if expected_repo else "(none recorded on the task)"
        parts.append(f"expected_repo={shown}")
    if extra:
        parts.append(f'detail="{trim_on_word(clean(extra), 300)}"')
    head = " ".join(parts)

    if code == "ref_unverifiable" and situation in UNVERIFIABLE_HINTS:
        hint = UNVERIFIABLE_HINTS[situation]
    elif code == "record_not_saved" and not ref_based:
        hint = RECORD_NOT_SAVED_TEXT_HINT
    else:
        hint = CAUSE_HINTS[code]
    nxt = CAUSE_NEXT_STEPS[code]
    if code == "record_not_saved":
        nxt += RECORD_NOT_SAVED_PR_EXTRA if ref_based else RECORD_NOT_SAVED_TEXT_EXTRA

    lines = [f"{head} — {hint}"]
    if verbatim_body is not None:
        lines.append(f'Agent said: "{clean(verbatim_body)}"')

    check = gh_check_command(clean(offered), expected_repo) if code in {
        "ref_unverifiable", "ref_check_unavailable"
    } else None
    value = clean(record_value) if record_value else None
    commands: list[str] = []
    if code == "blocked" and task_id:
        commands = [reopen_command(task_id)]
    elif code in {"ref_unverifiable", "ref_check_unavailable", "record_not_saved"}:
        if value and task_id and len(value) <= MAX_RECORD_VALUE_CHARS:
            commands = record_commands(task_id, value)
        elif value and task_id:
            nxt += (
                " The deliverable is too long to embed in a command: copy the full "
                "`[role] kind:` header line from the run's stdout as the --corrected-value "
                "(see the runbook, 'Recording a deliverable by hand')."
            )
        elif code != "record_not_saved":
            nxt += (
                " The offered reference is not complete enough to record as it is: "
                "find the real PR or commit, then record its full URL (see the "
                "runbook, 'Recording a deliverable by hand')."
            )
    lines.append(f"Next: {nxt}")
    if check:
        lines.append(f"Check it: {check}")
    if commands:
        label = (
            "Reopen (then the watchdog re-dispatches after its stall window):"
            if code == "blocked"
            else "Record it by hand, then read it back (each line is one command):"
        )
        lines.append(label)
        lines.extend(f"  {c}" for c in commands)
    return "\n".join(lines)


def stdout_tail_for_reason(text: str, *, limit: int = 2048) -> str:
    """Bounded tail for diagnostics (caller must redact secrets first)."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]

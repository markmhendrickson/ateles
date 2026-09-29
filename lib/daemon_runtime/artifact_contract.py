"""Shared declarative artifact contracts for Apis + Anthus (ateles#1155).

One source of truth for role → artifact_kind (+ gate aliases, body shapes,
resolver policy). Apis direct-task completion and Anthus gate satisfaction
both derive from ``ARTIFACT_CONTRACTS`` — a comment claiming parity is not
parity (principles.md#9).

Pure functions only — no I/O. Call sites inject ``resolve_artifact_ref``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CauseCode = Literal[
    "missing_header",
    "empty_body",
    "blocked",
    "unresolvable_ref",
    "invalid_ref_shape",
    "wrong_body_for_dispatch",
    "record_not_saved",
]

BodyShape = Literal["pr_or_commit", "eng_spec_section", "prose"]
ResolverPolicy = Literal["github_ref", "none"]
BodyClass = Literal["blocked", "valid", "empty"]

ARTIFACT_GATE_PREFIX = "[ARTIFACT_GATE]"

# Shell / injection metacharacters never allowed in a ref body.
_SHELL_METACHAR_RE = re.compile(r"[;&|`$()<>\\]")

_PR_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)\s*$",
    re.IGNORECASE,
)
_PR_SHORTHAND_RE = re.compile(
    r"^(?:PR\s*)?#(?P<number>\d+)\s*$",
    re.IGNORECASE,
)
_SHA_RE = re.compile(r"^(?P<sha>[0-9a-f]{40})\s*$", re.IGNORECASE)
# An abbreviated SHA (7-39 hex) is recognized as a ref SHAPE so it is refused as
# `invalid_ref_shape` ("give the full 40-character SHA") rather than mis-read as
# prose (`wrong_body_for_dispatch`): it answered the right question, but a
# prefix can be ambiguous, so this gate never resolves one. Only a full 40-char
# SHA is accepted as a commit ref.
_SHORT_SHA_RE = re.compile(r"^(?P<sha>[0-9a-f]{7,39})\s*$", re.IGNORECASE)
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
        "The agent reported that it is blocked, in its own words below. This is the "
        "agent asking for something, not a gate failure."
    ),
    "unresolvable_ref": (
        "The PR or commit reference is well-formed but GitHub could not find it."
    ),
    "invalid_ref_shape": (
        "The reference is not a PR or commit link this gate can check: a link to "
        "another repo or host, a bare #N or SHA with no repo on the task, an "
        "abbreviated SHA, or malformed text."
    ),
    "wrong_body_for_dispatch": (
        "The agent answered a different question than this dispatch asked."
    ),
    "record_not_saved": (
        "The agent's deliverable was accepted, but the task record did not save "
        "(the result or status write did not read back)."
    ),
}

CAUSE_NEXT_STEPS: dict[CauseCode, str] = {
    "missing_header": (
        "Read the stdout tail; re-dispatch only if the work was not done."
    ),
    "empty_body": "Re-dispatch, or ask the agent to state its deliverable.",
    "blocked": "Give the agent what it says it needs, then reopen the task.",
    "unresolvable_ref": (
        "Check the reference exists; re-dispatch only if it does not."
    ),
    "invalid_ref_shape": (
        "Fix the reference (full PR URL for this repo, or a full 40-character "
        "SHA), and make sure the task carries its repo, then re-dispatch."
    ),
    "wrong_body_for_dispatch": (
        "Re-dispatch in the right mode, or correct the body by hand."
    ),
    "record_not_saved": (
        "The PR exists; the task record did not save. Do not re-dispatch (that "
        "would open a second PR). Record the reference on the task and mark it "
        "done by hand."
    ),
}


@dataclass(frozen=True)
class ArtifactContract:
    """Declarative artifact requirement for one swarm role."""

    role: str
    artifact_kind: str
    gates: tuple[str, ...]
    accepted_body_shapes: frozenset[str]
    resolver_policy: ResolverPolicy


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

    Linear in the scanned text: every gap is `[ \\t]*` and the body is `[^\\n]*`,
    never `\\s`, so a run of newlines or spaces cannot make the engine retry the
    same span from every line start.
    """
    if not text:
        return None
    if len(text) > _MAX_SCAN_CHARS:
        text = text[-_MAX_SCAN_CHARS:]
        text = text.split("\n", 1)[1] if "\n" in text else text
    pattern = re.compile(
        rf"^(?P<line>[ \t]*\[(?P<agent>{re.escape(agent)})\][ \t]+"
        rf"(?P<kind>{re.escape(artifact_kind)})[ \t]*:[ \t]*(?P<body>[^\n]*))$",
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


def classify_artifact_body(body: str) -> BodyClass:
    """Classify header body: blocked / empty / valid."""
    stripped = (body or "").strip()
    if not stripped:
        return "empty"
    if stripped.upper() == "BLOCKED" or stripped.upper().startswith("BLOCKED —"):
        return "blocked"
    if stripped.upper().startswith("BLOCKED -"):  # ASCII hyphen variant
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

    A generic direct-implementation dispatch of Cicada accepts ONLY
    ``pr_or_commit``: an eng-spec section is the deliverable of a different
    question, and accepting it there is how an implementation task closes
    without an implementation. ``ordered_spec`` / ``eng_lens`` are the
    dispatches that actually asked for the section, so they accept both.
    """
    contract = role_required_artifact().get(role)
    if contract is None:
        return frozenset()
    mode = (dispatch_mode or "").strip().lower()
    if role == "cicada":
        if mode in {"ordered_spec", "eng_lens"}:
            return frozenset({"pr_or_commit", "eng_spec_section"})
        return frozenset({"pr_or_commit"})
    accepted = frozenset(contract.accepted_body_shapes)
    if "prose" in accepted:
        # Prose is the floor: a contract that accepts prose accepts a URL, a
        # ref-looking token, or a spec section too, because all of those are
        # text and the role's deliverable may legitimately contain any of them
        # (e.g. a docs note that links its PR). Only a contract that NARROWS
        # the shapes (Cicada, above) turns a shape into a refusal.
        return frozenset({"prose", "pr_or_commit", "eng_spec_section"})
    return accepted


def _split_dispatch_repo(dispatch_repo: str | None) -> tuple[str, str] | None:
    if not dispatch_repo:
        return None
    parts = dispatch_repo.strip().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def parse_github_ref(
    body: str,
    *,
    dispatch_repo: str | None,
) -> ParsedRef | InvalidRef:
    """Canonicalize a PR/commit body against ``dispatch_repo`` context."""
    stripped = (body or "").strip()
    if not stripped:
        return InvalidRef(code="invalid_ref_shape", reason="empty ref", body=stripped)
    if _SHELL_METACHAR_RE.search(stripped):
        return InvalidRef(
            code="invalid_ref_shape",
            reason="shell metacharacters in ref",
            body=stripped,
        )
    if _ENG_SPEC_RE.match(stripped):
        return InvalidRef(
            code="invalid_ref_shape",
            reason="ENG_SPEC_SECTION is not a github ref",
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
                code="invalid_ref_shape",
                reason=(
                    "PR URL without a known dispatch_repo to check it against"
                    if not dispatch_repo
                    else "malformed dispatch_repo for PR URL"
                ),
                body=stripped,
            )
        if (owner.lower(), repo.lower()) != (
            expected[0].lower(),
            expected[1].lower(),
        ):
            return InvalidRef(
                code="invalid_ref_shape",
                reason="cross-repo PR URL vs dispatch_repo",
                body=stripped,
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
            code="invalid_ref_shape",
            reason="abbreviated SHA; give the full 40-character SHA",
            body=stripped,
        )

    sha_m = _SHA_RE.match(stripped)
    if sha_m:
        if not dispatch_repo:
            return InvalidRef(
                code="invalid_ref_shape",
                reason="ambiguous SHA without dispatch_repo",
                body=stripped,
            )
        owner_repo = _split_dispatch_repo(dispatch_repo)
        if not owner_repo:
            return InvalidRef(
                code="invalid_ref_shape",
                reason="malformed dispatch_repo for SHA",
                body=stripped,
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
        if not dispatch_repo:
            return InvalidRef(
                code="invalid_ref_shape",
                reason="ambiguous bare #N without dispatch_repo",
                body=stripped,
            )
        owner_repo = _split_dispatch_repo(dispatch_repo)
        if not owner_repo:
            return InvalidRef(
                code="invalid_ref_shape",
                reason="malformed dispatch_repo for bare #N",
                body=stripped,
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
) -> str:
    """Build a grep-stable ``[ARTIFACT_GATE] <code> …`` reason string.

    The head (`[ARTIFACT_GATE] <code> role=… kind=… extra`) is grep-stable; what
    follows is plain wording for the operator: what happened, any verbatim agent
    text, and a `Next:` line saying what to do.
    """
    parts = [f"{ARTIFACT_GATE_PREFIX} {code}", f"role={role}"]
    if kind is not None:
        parts.append(f"kind={kind}")
    if extra:
        parts.append(extra)
    head = " ".join(parts)
    tail_bits = [CAUSE_HINTS[code]]
    if verbatim_body is not None:
        tail_bits.append(verbatim_body)
    tail_bits.append(f"Next: {CAUSE_NEXT_STEPS[code]}")
    return f"{head} — " + " ".join(tail_bits)


def stdout_tail_for_reason(text: str, *, limit: int = 2048) -> str:
    """Bounded tail for diagnostics (caller must redact secrets first)."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]

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
# An abbreviated SHA is recognized as a ref SHAPE so it is rejected as an
# ambiguous ref rather than mis-read as prose. Distinguishing the two matters:
# prose from Cicada is `wrong_body_for_dispatch` (the agent answered the wrong
# question), while a short SHA is `invalid_ref_shape` (it answered the right
# one, unverifiably).
_SHORT_SHA_RE = re.compile(r"^(?P<sha>[0-9a-f]{7,39})\s*$", re.IGNORECASE)
_ENG_SPEC_RE = re.compile(r"^ENG_SPEC_SECTION\b", re.IGNORECASE)

_COPY_HINTS: dict[CauseCode, str] = {
    "missing_header": "[COPY: hint missing_header]",
    "empty_body": "[COPY: hint empty_body]",
    "blocked": "[COPY: hint blocked]",
    "unresolvable_ref": "[COPY: hint unresolvable_ref]",
    "invalid_ref_shape": "[COPY: hint invalid_ref_shape]",
    "wrong_body_for_dispatch": "[COPY: hint wrong_body_for_dispatch]",
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


def parse_artifact_header(
    text: str,
    *,
    agent: str,
    artifact_kind: str,
) -> ArtifactHeader | None:
    """Last MULTILINE match of ``[agent] kind: body``; exact matched line returned."""
    if not text:
        return None
    pattern = re.compile(
        rf"^(\s*\[(?P<agent>{re.escape(agent)})\]\s+"
        rf"(?P<kind>{re.escape(artifact_kind)})\s*:\s*(?P<body>.*?)\s*)$",
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
        matched_line=m.group(1).strip(),
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
    return frozenset(contract.accepted_body_shapes)


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
        if expected and (owner.lower(), repo.lower()) != (
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

    sha_m = _SHA_RE.match(stripped) or _SHORT_SHA_RE.match(stripped)
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
    """Build a grep-stable ``[ARTIFACT_GATE] <code> …`` reason string."""
    parts = [f"{ARTIFACT_GATE_PREFIX} {code}", f"role={role}"]
    if kind is not None:
        parts.append(f"kind={kind}")
    if extra:
        parts.append(extra)
    head = " ".join(parts)
    hint = _COPY_HINTS.get(code, "")
    tail_bits = [hint] if hint else []
    if verbatim_body is not None:
        tail_bits.append(verbatim_body)
    if not tail_bits:
        return head
    return f"{head} — " + " ".join(tail_bits)


def stdout_tail_for_reason(text: str, *, limit: int = 2048) -> str:
    """Bounded tail for diagnostics (caller must redact secrets first)."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]

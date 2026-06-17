"""
execution/daemons/apis/routing.py — Apis domain routing tables.

Single source of truth for the task-domain → T4-skill routing used by both:

  - the SSE dispatch path (apis.py handle_event → dispatch_task), and
  - the A2A gateway (a2a_executor.py), which infers a task's domain before
    creating the Neotoma `task` entity that the SSE path later dispatches.

Extracting these here resolves the long-standing "kept in sync manually until a
shared lib is extracted" note that previously lived in apis.py. Keep all
domain-routing knowledge in this module; importers should not redefine it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Domain tags → T4 skill mappings. First matching tag wins in _resolve_skill.
DOMAIN_ROUTES: dict[str, str] = {
    "finance": "monedula",  # payment EXECUTION (concrete amount + payee) → Monedula
    "finance_analysis": "fringilla",  # review/reconcile/audit/report → Fringilla
    "health": "gorilla",  # workout logging / fitness tasks → Gorilla
    "ops": "cicada",  # ops/deploy tasks → issue worker
    "engineering": "cicada",  # engineering tasks → issue worker
    "agents": "cicada",  # agent/swarm tasks → issue worker
    "neotoma": "cicada",  # neotoma-repo tasks → issue worker
    "product": "cicada",  # product/design tasks → issue worker
    "comms": "cicada",  # comms tasks → issue worker
}

# An explicit task.assigned_to value always wins over tag inference. Maps an
# agent name (as written in agent_definition.name / task.assigned_to) to the
# skill Apis dispatches. Keep in sync with the active swarm roster.
ASSIGNED_TO_ROUTES: dict[str, str] = {
    "monedula": "monedula",
    "fringilla": "fringilla",
    "gorilla": "gorilla",
    "cicada": "cicada",
    "sturnus": "sturnus",
}

# Domain keyword patterns. Order matters: earlier patterns take precedence when
# multiple match (see _resolve_skill, which walks tags in insertion order).
DOMAIN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(financial review|reconcile|reconciliation|audit|portfolio|"
            r"fixed costs?|subscription review|quarterly review)\b",
            re.I,
        ),
        "finance_analysis",
    ),
    (
        re.compile(
            r"\b(payment|invoice|transfer|wage|salary|rent|yoga|therapy|pay)\b", re.I
        ),
        "finance",
    ),
    (
        re.compile(
            r"\b(workout|gym|fitness|lift|squat|bench|deadlift|training|"
            r"reps|sets|cardio|gorilla)\b",
            re.I,
        ),
        "health",
    ),
    (
        re.compile(r"\b(deploy|release|build|ci|pipeline|docker|kubernetes)\b", re.I),
        "ops",
    ),
    (
        re.compile(r"\b(bug|fix|error|crash|exception|regression|test)\b", re.I),
        "engineering",
    ),
    (
        re.compile(r"\b(design|ux|ui|figma|wireframe|mockup|copy|content)\b", re.I),
        "product",
    ),
    (
        re.compile(r"\b(neotoma|schema|entity|migration|api|endpoint)\b", re.I),
        "neotoma",
    ),
    (
        re.compile(r"\b(agent|daemon|skill|swarm|formica|apus|tyto|anthus)\b", re.I),
        "agents",
    ),
    (re.compile(r"\b(email|newsletter|telegram|social|post|draft)\b", re.I), "comms"),
]

# Domains advertised on the A2A Agent Card's delegate-task skill. Derived from
# the routing table so the external contract tracks internal capability.
SUPPORTED_DOMAINS: list[str] = list(DOMAIN_ROUTES.keys())

# File-path → domain patterns for PR-review routing (Loxia per-domain fan-out).
# Distinct from DOMAIN_PATTERNS above, which classify a task's TITLE/BODY text:
# these match the PATHS of files changed in a pull request. A single PR may
# touch several domains, so all matches are collected (no first-match-wins).
#
# Only specialist domains with a non-generalist owner are listed. The
# cicada-owned domains (ops/engineering/agents/neotoma/product/comms) route to
# the same generalist Loxia already covers as the baseline reviewer, so adding a
# second cicada pass buys nothing — they are intentionally omitted here.
DOMAIN_PATH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(^|/)(monedula|payment|invoice|wage|payroll|rent)", re.I),
        "finance",
    ),
    (
        re.compile(r"(^|/)(fringilla|reconcil|finance[_-]?analysis)", re.I),
        "finance_analysis",
    ),
    (
        re.compile(r"(^|/)(gorilla|workout|fitness)", re.I),
        "health",
    ),
]


def infer_domains_from_paths(paths: Iterable[str]) -> list[str]:
    """Distinct domains touched anywhere in a PR's changeset, in first-seen order.

    Dedup is global across the whole changeset (not per-path): the question this
    answers is coverage — which specialists should look at the PR — not which
    single domain owns a given file. A path may match several patterns, and
    every distinct domain across all paths is collected once. Contrast
    infer_tags_from_text, which classifies a single text blob.
    """
    domains: list[str] = []
    for path in paths:
        for pattern, domain in DOMAIN_PATH_PATTERNS:
            if pattern.search(path) and domain not in domains:
                domains.append(domain)
    return domains


def resolve_reviewers(paths: Iterable[str]) -> list[str]:
    """T4 skills of the domain-owning agents that should review a PR touching
    these paths, *in addition to* the universal baseline reviewer (Loxia).

    Deduplicated and order-stable; returns [] when no specialist domain is
    touched. Loxia is intentionally excluded — callers always run the baseline
    reviewer plus whatever this returns.
    """
    reviewers: list[str] = []
    for domain in infer_domains_from_paths(paths):
        skill = DOMAIN_ROUTES.get(domain)
        if skill and skill not in reviewers:
            reviewers.append(skill)
    return reviewers


def infer_tags_from_text(title: str, body: str = "") -> list[str]:
    """Infer domain tags from task title + body (fallback when tags unset)."""
    text = f"{title} {body}"
    tags: list[str] = []
    for pattern, tag in DOMAIN_PATTERNS:
        if pattern.search(text) and tag not in tags:
            tags.append(tag)
    return tags


def resolve_skill(tags: list[str], assigned_to: str | None = None) -> str | None:
    """
    Pick the T4 skill for a task.

    An explicit `assigned_to` (set by Sylvia/Turdus when they create or route a
    task) always wins over tag inference — the creating agent already decided
    the owner. Only when `assigned_to` is unset, unknown, or the dispatcher
    itself ("apis") do we fall back to domain-tag routing. First matching tag
    wins; returns None if nothing maps to a route.
    """
    if assigned_to:
        key = assigned_to.strip().lower()
        if key and key != "apis":
            skill = ASSIGNED_TO_ROUTES.get(key)
            if skill:
                return skill
            # Unknown assignee: don't silently misroute — let tag inference try,
            # but the caller can log the miss.
    for tag in tags:
        skill = DOMAIN_ROUTES.get(tag)
        if skill:
            return skill
    return None


def resolve_role(tags: list[str], assigned_to: str | None = None) -> str | None:
    """
    Return the role name for a task (i.e. the agent_definition name to load).

    In this codebase the role is the same string as the resolved skill — the
    skill name IS the agent name stored in Neotoma's agent_definition entities.
    Exposing this as a named function gives callers a stable "ask for the role"
    interface: if the role/skill mapping ever diverges (e.g. a single agent
    handles multiple skill entry-points), only this function changes.

    Returns None when no route matches (mirrors resolve_skill).
    """
    return resolve_skill(tags, assigned_to=assigned_to)

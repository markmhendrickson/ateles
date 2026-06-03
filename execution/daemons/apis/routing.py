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

# Domain tags → T4 skill mappings. First matching tag wins in _resolve_skill.
DOMAIN_ROUTES: dict[str, str] = {
    "finance": "monedula",  # payment EXECUTION (concrete amount + payee) → Monedula
    "finance_analysis": "fringilla",  # review/reconcile/audit/report → Fringilla
    "health": "gorilla",  # workout logging / fitness tasks → Gorilla
    "ops": "gryllus",  # ops/deploy tasks → issue worker
    "engineering": "gryllus",  # engineering tasks → issue worker
    "agents": "gryllus",  # agent/swarm tasks → issue worker
    "neotoma": "gryllus",  # neotoma-repo tasks → issue worker
    "product": "gryllus",  # product/design tasks → issue worker
    "comms": "gryllus",  # comms tasks → issue worker
}

# An explicit task.assigned_to value always wins over tag inference. Maps an
# agent name (as written in agent_definition.name / task.assigned_to) to the
# skill Apis dispatches. Keep in sync with the active swarm roster.
ASSIGNED_TO_ROUTES: dict[str, str] = {
    "monedula": "monedula",
    "fringilla": "fringilla",
    "gorilla": "gorilla",
    "gryllus": "gryllus",
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

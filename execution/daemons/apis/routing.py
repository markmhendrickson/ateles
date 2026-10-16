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

import os
import re
from collections.abc import Iterable

# Domain tags → T4 skill mappings. First matching tag wins in _resolve_skill.
#
# Ordering is load-bearing: DOMAIN_PATTERNS below is walked in list order and
# resolve_skill walks the resulting tags in that same order, so a SPECIALIST
# domain must be declared ahead of the broad generalist (cicada-owned) domains,
# which would otherwise swallow copy, GTM, content, compliance and release work.
DOMAIN_ROUTES: dict[str, str] = {
    # ── Specialist domains (each has a dedicated T4 owner) ───────────────────
    "finance": "monedula",  # payment EXECUTION (concrete amount + payee) → Monedula
    "finance_analysis": "fringilla",  # review/reconcile/report → Fringilla
    "tax": "picus",  # tax preparation / filings → Picus
    "health": "gorilla",  # workout logging / fitness tasks → Gorilla
    "crm": "sturnus",  # relationship / contact management → Sturnus
    "customer_intel": "hirundo",  # ICP / customer research → Hirundo
    "gtm": "ciconia",  # launch / go-to-market → Ciconia
    "copy": "manucode",  # positioning / messaging copy → Manucode
    "content": "corvus",  # blog / social / editorial writing → Corvus
    "design": "aythya",  # visual & brand design → Aythya
    "devrel": "regulus",  # developer docs / onboarding → Regulus
    "release": "struthio",  # release execution → Struthio
    "compliance": "robin",  # session / process compliance audit → Robin
    "policy": "columba",  # constitution & policy escalation → Columba
    "personal": "nucifraga",  # personal / household enrichment → Nucifraga
    "anchor_delivery": "ploceus",  # anchor-customer delivery → Ploceus
    # ── Generalist domains (all → the issue worker) ──────────────────────────
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
#
# Derived from DOMAIN_ROUTES so an agent that owns a domain is, by construction,
# also addressable by name — the two tables cannot drift apart (guarded by
# test_routing.test_every_domain_owner_is_addressable_by_name).
ASSIGNED_TO_ROUTES: dict[str, str] = {skill: skill for skill in DOMAIN_ROUTES.values()}

# ── Negative-context guards ───────────────────────────────────────────────────
#
# Some domain keywords are a genuine domain signal in one context and ordinary
# English in another ("audit the README", "insurance agent", "training
# documentation"). A bare \b<word>\b pattern for these produces a SILENT
# MISROUTE: the task resolves to a confident specialist who is the wrong owner.
# That reads as covered and is strictly worse than resolving to no owner —
# apis.dispatch_task escalates an unrouted task to BLOCKED and pages the
# operator, so "no route" is visibly unowned, while a wrong route is not.
#
# Each entry is (tag, pattern): when the pattern matches the text the tag is
# suppressed, even though its DOMAIN_PATTERNS entry matched. Guards are
# deliberately narrow — they encode the specific collision observed, not a veto.
# (tag, negative-context pattern, trigger) — the trigger names the ambiguous
# word the guard qualifies, so the negative context only counts when it appears
# NEAR that word rather than anywhere in the task. A guard with no trigger
# applies to the whole text (its pattern is already self-contained).
DOMAIN_ANTIPATTERNS: list[
    tuple[str, re.Pattern[str]] | tuple[str, re.Pattern[str], re.Pattern[str]]
] = [
    # "audit" is finance analysis only when it is not auditing something else.
    (
        "finance_analysis",
        re.compile(
            r"\b(readme|docs?|documentation|onboarding|accessibility|security|"
            r"a11y|seo|content|copy|session|sessions|compliance|code|schema|"
            r"test|tests|performance|dependency|dependencies|log|logs|"
            r"permission|permissions|prompt|prompts|routing|coverage|"
            r"accessibility)\b",
            re.I,
        ),
        # Bound to "audit": that is the only over-broad token in the
        # finance_analysis pattern. "Reconcile the portfolio" must keep its
        # route even when the body happens to mention docs or tests.
        re.compile(r"\baudits?\b", re.I),
    ),
    # "agent" also means a human intermediary (insurance/estate/travel agent).
    (
        "agents",
        re.compile(
            r"\b(insurance|estate|travel|booking|rental|customs|literary)\s+agent\b",
            re.I,
        ),
    ),
    # "training" is health only outside a docs / machine-learning context.
    (
        "health",
        re.compile(
            r"\b(training\s+(doc|docs|documentation|material|materials|manual|"
            r"guide|video|videos|course|deck)|"
            r"(model|ml|llm|pipeline|data)\s+training|"
            r"training\s+(data|run|loop|pipeline|job))\b",
            re.I,
        ),
        re.compile(r"\btraining\b", re.I),
    ),
]

# Domain keyword patterns. Order matters: earlier patterns take precedence when
# multiple match (see _resolve_skill, which walks tags in insertion order).
# Specialist patterns come FIRST so the broad generalist patterns
# (ops/engineering/product/neotoma/agents/comms) cannot swallow copy, GTM,
# content, compliance, release or devrel work into cicada.
#
# Patterns must be SPECIFIC. A bare single word that also occurs in ordinary
# English ("audit", "pay", "design", "post", "release", "build", "agent",
# "content", "api") is how a silent misroute is born: prefer a multi-word
# phrase, a qualified noun, or a DOMAIN_ANTIPATTERNS guard. Every pattern here
# has a negative test in test_routing.py asserting what it must NOT claim.
DOMAIN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ── Specialist domains ───────────────────────────────────────────────────
    (
        re.compile(
            r"\b(tax returns?|tax filings?|tax prep|tax preparation|"
            r"tax declaration|tax year|tax residency|tax deductions?|"
            r"modelo\s*\d+|irpf|vat return|withholding|hacienda|"
            r"income tax|capital gains|tax advisor|tax preparer|"
            r"quarterly filing|fiscal report)\b",
            re.I,
        ),
        "tax",
    ),
    (
        re.compile(
            r"\b(financial review|reconcile|reconciliation|financial audit|"
            r"spend audit|expense audit|portfolio|fixed costs?|"
            r"subscription review|quarterly review|burn rate|cash flow|"
            r"profit and loss|balance sheet|audit)\b",
            re.I,
        ),
        "finance_analysis",
    ),
    (
        re.compile(
            r"\b(payments?|invoices?|transfer|wages?|salary|rent|yoga|therapy|"
            r"payout|remittance|reimburse|reimbursement)\b"
            r"|\bpay\s+(the|a|an|out|off|for|back)\b",
            re.I,
        ),
        "finance",
    ),
    (
        re.compile(
            r"\b(workout|gym|fitness|squat|bench press|deadlift|"
            r"reps|sets|cardio|gorilla|strength training|lift session|"
            r"training)\b",
            re.I,
        ),
        "health",
    ),
    (
        re.compile(
            r"\b(crm|contact records?|relationship hub|warm intro|"
            r"follow[- ]up with|introduction to|contact enrichment|"
            r"address book|reconnect with|relationship graph)\b",
            re.I,
        ),
        "crm",
    ),
    (
        re.compile(
            r"\b(icp|ideal customer|customer interviews?|customer research|"
            r"customer intelligence|user research|win[- ]loss|personas?|"
            r"jobs[- ]to[- ]be[- ]done|market segment|segmentation|"
            r"churn analysis|voice of customer)\b",
            re.I,
        ),
        "customer_intel",
    ),
    (
        re.compile(
            r"\b(go[- ]to[- ]market|gtm|launch (plan|campaign|strategy)|"
            r"product launch|campaign plan|pricing strategy|"
            r"acquisition channel|growth experiment|funnel strategy|"
            r"marketing plan|distribution strategy)\b",
            re.I,
        ),
        "gtm",
    ),
    (
        re.compile(
            r"\b(positioning|value prop|value proposition|messaging|tagline|"
            r"headline copy|landing page copy|marketing copy|copywriting|"
            r"category narrative|brand voice|page copy|ad copy)\b"
            r"|\brewrite the (headline|copy|tagline|landing page)\b",
            re.I,
        ),
        "copy",
    ),
    (
        re.compile(
            r"\b(blog posts?|essay|newsletter issue|editorial|social posts?|"
            r"thread draft|substack|article draft|content calendar)\b"
            r"|\bwrite (a|an|the) (post|article|essay)\b"
            r"|\bpublish (a|an|the) (post|article|essay)\b",
            re.I,
        ),
        "content",
    ),
    (
        re.compile(
            r"\b(brand (colou?r|palette|identity|guidelines?)|"
            r"colou?r palette|logo|typography|visual design|figma|wireframe|"
            r"mockup|design system|style guide|illustration|iconography|"
            r"ui design|ux design|visual identity)\b",
            re.I,
        ),
        "design",
    ),
    (
        re.compile(
            r"\b(devrel|developer relations|developer docs|"
            r"developer experience|getting started guide|quickstart|"
            r"onboarding (path|docs|guide|flow|experience)|readme|"
            r"api reference|tutorial|sample app|code examples?|"
            r"contributor guide)\b",
            re.I,
        ),
        "devrel",
    ),
    (
        re.compile(
            r"\b(release candidate|release notes|changelog|version bump|"
            r"semver|release checklist|release process)\b"
            r"|\b(cut|ship|tag)\s+(a|the)?\s*(v?\d+\.\d+\S*\s+)?release\b"
            r"|\bpublish the (package|release)\b",
            re.I,
        ),
        "release",
    ),
    (
        re.compile(
            r"\b(compliance|session audit|audit trail|rgpd|gdpr|"
            r"retention policy|data protection|process adherence|"
            r"governance review)\b",
            re.I,
        ),
        "compliance",
    ),
    (
        re.compile(
            r"\b(constitution|policy (escalation|question|exception)|charter|"
            r"escalation protocol|governing principle|principles document|"
            r"ethical review)\b",
            re.I,
        ),
        "policy",
    ),
    (
        re.compile(
            r"\b(dentist|doctor|appointment|household|groceries|grocery|"
            r"personal errand|birthday|vacation|holiday booking|restaurant|"
            r"haircut|dry cleaning|veterinar|school run)\b",
            re.I,
        ),
        "personal",
    ),
    (
        re.compile(
            r"\b(anchor (customer|client|account)|client delivery|"
            r"customer delivery|delivery milestone|design partner|"
            r"pilot customer)\b",
            re.I,
        ),
        "anchor_delivery",
    ),
    # ── Generalist domains (all → cicada) ────────────────────────────────────
    (
        re.compile(
            r"\b(deploy|deployment|ci|pipeline|docker|kubernetes|"
            r"build failure|provision|infrastructure|disk space|"
            r"host maintenance|uptime)\b"
            r"|\b(build|restart) the\b",
            re.I,
        ),
        "ops",
    ),
    (
        re.compile(
            r"\b(bug|fix|error|crash|exception|regression|refactor|"
            r"unit tests?|test suite|failing test|flaky test|stack trace)\b",
            re.I,
        ),
        "engineering",
    ),
    (
        re.compile(
            r"\b(product spec|feature request|roadmap|user story|"
            r"acceptance criteria|product requirements?)\b",
            re.I,
        ),
        "product",
    ),
    (
        re.compile(
            r"\b(neotoma|entity schema|schema migration|db migration|"
            r"entity type|mcp tool|api endpoint)\b",
            re.I,
        ),
        "neotoma",
    ),
    (
        re.compile(
            r"\b(daemon|skill|swarm|formica|apus|tyto|anthus|subagent)\b"
            r"|\bagents?\b"
            # Swarm-mechanics vocabulary. Added from measured misses, not from
            # guesswork: tasks about the dispatcher itself ("Add dispatch gate
            # sequencing…", "Route tasks by claim predicate…") inferred NO tags
            # at all and blocked. Each term is qualified by a swarm noun rather
            # than left bare — "gate", "dispatch", "route" and "claim" are all
            # ordinary English ("gate the release", "claim expenses"), and a
            # bare token here is how the silent misroutes above were born.
            r"|\b(dispatch|routing|route)\s+(gate|gates|task|tasks|pipeline|"
            r"sequence|sequences|predicate|table|health)\b"
            r"|\b(gate|gates|checkpoint|checkpoints)\s+(sequence|sequences|"
            r"status|dispatch|observability)\b"
            r"|\b(claim|lease)\s+(predicate|primitive|protocol)\b",
            re.I,
        ),
        "agents",
    ),
    (
        re.compile(
            r"\b(email|newsletter|telegram|reply to)\b"
            r"|\bdraft (a|an|the) (email|reply|message|note)\b",
            re.I,
        ),
        "comms",
    ),
]

# Domains advertised on the A2A Agent Card's delegate-task skill. Derived from
# the routing table so the external contract tracks internal capability.
SUPPORTED_DOMAINS: list[str] = list(DOMAIN_ROUTES.keys())

# Paths that are agent METADATA (docs/skill mirrors), not domain work. A change
# to docs/agents/monedula.md is a docs edit, not finance code — it must not
# summon the finance specialist as a reviewer (Loxia covers it as baseline).
# Similarly, .claude/skills/<agent>/SKILL.md files are generated mirrors of the
# skill definition and carry no domain-work signal.
#
# Pattern matches repo-relative paths like:
#   docs/agents/monedula.md
#   docs/agents/README.md
#   .claude/skills/lanius/SKILL.md
# The (^|/) anchor handles paths with or without a leading slash.
_AGENT_METADATA_PATH: re.Pattern[str] = re.compile(
    r"(^|/)(docs/agents/|\.claude/skills/)", re.I
)

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
        # Skip agent-metadata paths (docs/agents/ and .claude/skills/). A PR
        # that touches docs/agents/monedula.md is regenerating documentation,
        # not changing finance code — it must not summon Monedula as a reviewer.
        # Loxia covers these as the universal baseline reviewer.
        if _AGENT_METADATA_PATH.search(path):
            continue
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


# A guard qualifies the ambiguous keyword it sits next to — not the whole task.
# Anything wider is its own silent misroute: an incidental "docs" in a body
# suppressed a strong title signal, and dropping `finance_analysis` let the
# broader `finance` pattern claim the task and route it to the PAYMENT
# EXECUTOR. Measured before this bound existed (ateles#607 qa lens):
#
#   ("Reconcile the portfolio statements", "Numbers are in the shared docs
#    folder.")                                   -> [] (unrouted)
#   ("Reconcile the Q3 portfolio", "Cross-check against the invoice test
#    data.")                                     -> ['finance'] -> monedula
#
# so the guard window is the ambiguous word plus a few tokens either side.
_GUARD_WINDOW = 40


def _guarded_windows(text: str, trigger: re.Pattern[str]) -> list[str]:
    """The spans of `text` close enough to a trigger word for a guard to apply."""
    return [
        text[max(0, m.start() - _GUARD_WINDOW) : m.end() + _GUARD_WINDOW]
        for m in trigger.finditer(text)
    ]


def _suppressed_tags(text: str) -> set[str]:
    """Tags whose negative-context guard fires for this text.

    See DOMAIN_ANTIPATTERNS: a keyword that is a domain signal in one context and
    ordinary English in another is suppressed rather than allowed to produce a
    confident wrong owner.

    A guard fires only when its negative context sits NEAR the ambiguous word it
    qualifies. "Audit the README" suppresses `finance_analysis`; "Reconcile the
    portfolio" with a passing mention of docs elsewhere does not.
    """
    suppressed: set[str] = set()
    for entry in DOMAIN_ANTIPATTERNS:
        tag, pattern = entry[0], entry[1]
        trigger = entry[2] if len(entry) > 2 else None
        if trigger is None:
            if pattern.search(text):
                suppressed.add(tag)
            continue
        if any(pattern.search(w) for w in _guarded_windows(text, trigger)):
            suppressed.add(tag)
    return suppressed


def infer_tags_from_text(title: str, body: str = "") -> list[str]:
    """Infer domain tags from task title + body (fallback when tags unset).

    A tag whose DOMAIN_ANTIPATTERNS guard fires is dropped: an over-broad match
    is a silent misroute, and no route (which apis.dispatch_task escalates to
    BLOCKED and pages the operator about) is better than a wrong one.
    """
    text = f"{title} {body}"
    suppressed = _suppressed_tags(text)
    tags: list[str] = []
    for pattern, tag in DOMAIN_PATTERNS:
        if tag in suppressed:
            continue
        if pattern.search(text) and tag not in tags:
            tags.append(tag)
    return tags


# Values that encode "nobody owns this" as a STRING rather than as absence.
# They are worse than an empty field: `bool(assigned_to)` is True for them, so
# they satisfy an owner check (`readiness.assess_readiness(has_owner=...)`) and
# an `eq` filter without naming an owner any dispatcher can resolve.
# `canonical_assignee` maps them to None so absence is spelled exactly one way.
SENTINEL_ASSIGNEES = frozenset({"unassigned", "none", "nobody", "tbd", "n/a", "-"})

# AAuth subjects are written `<agent>@ateles-swarm` (lib/daemon_runtime/
# aauth_signer.py builds them as f"{name}@ateles-swarm"). The local part IS the
# agent name by construction, so an assignee carrying the suffix names the same
# owner as the bare form and must resolve identically.
_AAUTH_SUFFIXES = ("@ateles-swarm", "@ateles")

# ── Human ownership is a routing ANSWER, not a routing miss ──────────────────
#
# `resolve_skill` returns this when `assigned_to` names a person rather than an
# agent. It is deliberately NOT a key in ASSIGNED_TO_ROUTES and never names a
# skill: `dispatch_task` branches on it before any spawn path, parks the task in
# AWAITING_INPUT, and notifies at OPERATOR_DECISION. Correctly-assigned human
# work is not a defect to page about, so it must not travel the BLOCKED +
# BLOCKER unroutable path either.
HUMAN_OWNED = "HUMAN_OWNED"


def _human_assignee_keys() -> frozenset[str]:
    """Assignee values that mean "a person owns this", in canonical form.

    Always includes the generic `operator`. The operator's own login is read
    from `APIS_OPERATOR_LOGIN` at CALL time (not import time) so a test can
    monkeypatch it, and so a fork supplies its own identity — this file must
    never hardcode a personal login (the repo is public, and operator identity
    is env-sourced per the operator-config rule).
    """
    keys = {"operator"}
    login = os.environ.get("APIS_OPERATOR_LOGIN", "").strip().lower()
    if login:
        keys.add(login)
    return frozenset(keys)


def canonical_assignee(assigned_to: str | None) -> str | None:
    """
    Reduce an ``assigned_to`` value to the one form the dispatcher resolves.

    The canonical form is not a preference — it is whatever
    ``ASSIGNED_TO_ROUTES`` is keyed by: a bare, lowercase, stripped agent name
    (the keys are derived from ``DOMAIN_ROUTES.values()``). This normalizes the
    three mechanical variations observed in stored tasks — surrounding
    whitespace, capitalization, and the AAuth-subject suffix — and maps the
    "unassigned" sentinel family to ``None`` so absence has a single spelling.

    Returns ``None`` for absence/sentinels, otherwise the reduced string. A
    value that is not a known agent is returned reduced but unchanged: this
    function normalizes *form*, and never invents or guesses an owner (a human
    name, or prose, is left for the caller to treat as unroutable).
    """
    if not assigned_to:
        return None
    key = assigned_to.strip().lower()
    if not key or key in SENTINEL_ASSIGNEES:
        return None
    for suffix in _AAUTH_SUFFIXES:
        if key.endswith(suffix):
            key = key[: -len(suffix)].strip()
            break
    return key or None


def resolve_skill(tags: list[str], assigned_to: str | None = None) -> str | None:
    """
    Pick the T4 skill for a task.

    Returns one of three things — the contract is three-way, not two-way:

      * an agent skill name — a resolved owner the dispatcher may spawn;
      * ``HUMAN_OWNED``    — a person owns this; park it, never spawn;
      * ``None``           — nobody owns this; escalate it loudly.

    An explicit `assigned_to` (set by Sylvia/Turdus when they create or route a
    task) always wins over tag inference — the creating agent already decided
    the owner. Tag inference runs ONLY when the field is genuinely absent: unset,
    a sentinel ("unassigned"), or the dispatcher itself ("apis").

    An `assigned_to` naming someone who is not a routable agent is a HARD STOP,
    never a fallthrough. This used to fall through to keyword tag inference,
    which is the silent misroute the old comment here disclaimed while
    performing it: with 17 domain owners against a ~38-name roster, every other
    agent name — `pavo`, `apus`, `turdus`, `waxwing`, … — was treated as absent,
    so a finance-tagged task assigned to ANY of them reached `monedula`, the
    payment executor, and a product task assigned to `pavo` became a `cicada`
    code change. A named owner nobody can spawn must block visibly (#702).
    """
    key = canonical_assignee(assigned_to)
    if key and key != "apis":
        skill = ASSIGNED_TO_ROUTES.get(key)
        if skill:
            return skill
        if key in _human_assignee_keys():
            # Deliberate human ownership — terminal, and not an escalation.
            return HUMAN_OWNED
        # A named owner that is neither an agent nor the operator. Stop here:
        # tag inference would reassign work its creator explicitly gave to
        # someone else, and a confident wrong owner reads as covered while a
        # missing one is visibly unowned.
        return None
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

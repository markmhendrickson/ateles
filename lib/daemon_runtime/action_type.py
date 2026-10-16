"""
lib/daemon_runtime/action_type.py — resolve the action_type the execution gate
classifies blast radius from, using what a task SAYS it does as well as who
would handle it (ateles#682).

Why this exists
---------------
``apis._AGENT_ACTION_TYPE["cicada"] = "open_or_merge_pr"`` makes blast radius a
property of the assignee. Cicada is the generalist, so a task whose entire
output is a written report is gated as if it merged a PR, and a task routed to
an agent with a LOW prior that says "send the email to the client" is gated as
if it were a local edit.

What this module does — and, deliberately, does not do
------------------------------------------------------
It resolves one value for the GATE and the MCP ``route_task`` preview. It is
NOT used for model-tier selection or for the build-delivery grant: those keep
``apis._infer_action_type`` exactly as it was, because they key on the agent's
class and must not move when the gate's reading of a task's text does.

Resolution order (``resolve_gate_action_type``):

1. **Declared beats everything, and is passed through UNCHANGED** (lowercased
   and stripped, nothing else). An unrecognized spelling reaches the gate, which
   treats it as never-auto-executable and says so by name (ateles#715). This
   module never discards, normalizes, or aliases a declared value.
2. Otherwise the per-agent prior (``AGENT_ACTION_TYPE_PRIOR``), exactly as before.
3. Text inference may then change that prior in exactly two ways, and no other:

   * **Escalate.** A high-blast signal in the task text ("open a PR", "send the
     email to the client", "pay the invoice") replaces an absent or LOW prior
     with the high-blast type the text names. It never replaces a HIGH prior
     and never touches an unrecognized one.
   * **Lower, narrowly.** For the agents in ``LOWERABLE_PRIOR_AGENTS`` (the
     generalist whose single prior mislabels most of its work), an explicit
     report-only statement ("Write NOTHING", "pure reporting", "the only output
     is a report") replaces the HIGH prior with ``compute_only_analysis`` — and
     only when the text carries NO high-blast signal and no implementation verb,
     and the task carries no ``source`` marker. A task that arrived from outside
     the swarm (the A2A gateway stamps ``source: "a2a"``) is written by someone
     who can put any sentence in it, so its prose may tighten its own gate but
     never loosen it.

   Text never supplies a LOW value where there is no prior: with nothing to
   lower, a LOW reading would only be able to move a task from the policy
   default to LOW, which is exactly the "silence is a licence" step this module
   refuses to take. A task with no signal resolves exactly as it does today.

Every value this module can emit is a member of the policy's action-type
vocabulary (``test_action_type.py`` holds that against ``gating``); it emits no
aliases and no ``operator_only``.

This module is dependency-free (stdlib only) so the MCP server can import it
without pulling in the daemon runtime's HTTP stack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

# ── Vocabulary ───────────────────────────────────────────────────────────────
# A stdlib-only mirror of the classification in ``lib.daemon_runtime.gating``
# (``_FALLBACK_LOW_BLAST`` / ``_FALLBACK_HIGH_BLAST`` / ``NEVER_AUTO_EXECUTE``),
# kept honest by ``test_action_type.py::test_vocabulary_mirrors_gating``. It
# exists only so this module can say which tier a PRIOR sits in; the policy
# entity, evaluated by ``ExecutionPolicy.blast_radius_for``, stays authoritative
# for the actual verdict.
LOW_BLAST_ACTION_TYPES = frozenset(
    {
        "local_edit",
        "draft",
        "neotoma_read",
        "neotoma_internal_entity_update",
        "compute_only_analysis",
    }
)
HIGH_BLAST_ACTION_TYPES = frozenset(
    {
        "git_push",
        "open_or_merge_pr",
        "open_pr",
        "merge_pr",
        "payment",
        "transfer",
        "wage",
        "invoice_pay",
        "send_external_comms",
        "publish",
        "release",
        "delete_entity_or_data",
        "external_api_write",
    }
)
NEVER_ACTION_TYPES = frozenset({"operator_only"})

# Every spelling the gate can classify. Includes the never-auto-executable set
# on purpose: `test_gating.py::test_any_action_type_vocabulary_must_contain_the_
# never_set` fails if a module of this name admits a vocabulary that omits it.
# This is a description of the vocabulary, NOT an acceptance filter — nothing
# below drops a declared value for being outside it.
KNOWN_ACTION_TYPES = LOW_BLAST_ACTION_TYPES | HIGH_BLAST_ACTION_TYPES | NEVER_ACTION_TYPES

# Coarse action_type per agent — feeds the gate's blast-radius classification.
# MOVED here from ``apis._AGENT_ACTION_TYPE`` (which now aliases this very dict)
# so the MCP ``route_task`` preview reads the same prior the gate does, instead
# of a copy that can drift. Contents are unchanged: nothing about what any agent
# resolves to without a text signal changes.
#
# Values MUST match the execution_policy's high/low_blast_action_types
# vocabulary (default policy ent_dfce6edecefe3eb7fc9e0337) or the gate
# mis-classifies blast radius; ``test_action_type_vocabulary.py`` enforces it.
AGENT_ACTION_TYPE_PRIOR: dict[str, str] = {
    "cicada": "open_or_merge_pr",
    "vanellus": "open_or_merge_pr",
    "struthio": "publish",
    "monedula": "payment",
    "fringilla": "compute_only_analysis",
    "corvus": "send_external_comms",
}

# Agents whose prior may be LOWERED by an explicit report-only statement.
# Cicada only: it is the generalist whose one prior mislabels the majority of
# its work. Every specialist's prior is a statement about what it is for
# (Monedula moves money, Struthio publishes), so text can add to it but never
# take it away. Adding an agent here loosens the gate for that agent.
LOWERABLE_PRIOR_AGENTS = frozenset({"cicada"})

SOURCE_DECLARED = "declared"
SOURCE_CONTENT = "content"
SOURCE_AGENT_PRIOR = "agent_prior"
SOURCE_NONE = "none"

_TIER_LOW = "low"
_TIER_HIGH = "high"
_TIER_NEVER = "never"


def normalize_action_type(raw: object) -> str | None:
    """A declared action_type, stripped and lowercased — and nothing else.

    Deliberately NOT a validator. An unrecognized spelling (a typo, an alias, a
    value no policy classifies) is returned as written, because the gate treats
    it as never-auto-executable and logs it by name (ateles#715); discarding it
    here would hide that and let the task fall through to inference. Returns
    ``None`` only for "nothing was declared": absent, empty, or whitespace.
    """
    if raw is None or raw is False:
        return None
    value = str(raw).strip().lower()
    return value or None


def tier_of(action_type: str | None) -> str | None:
    """Coarse tier of an action type, ``None`` when nothing was declared.

    Anything classified by neither set — including ``operator_only`` — is
    ``never``, the same fail-closed reading ``ExecutionPolicy.blast_radius_for``
    gives it.
    """
    if not action_type:
        return None
    at = action_type.strip().lower()
    if at in NEVER_ACTION_TYPES:
        return _TIER_NEVER
    if at in LOW_BLAST_ACTION_TYPES:
        return _TIER_LOW
    if at in HIGH_BLAST_ACTION_TYPES:
        return _TIER_HIGH
    return _TIER_NEVER


@dataclass(frozen=True)
class ResolvedActionType:
    """The resolved value and where it came from.

    ``source`` is what lets a log line or the MCP preview say whether a verdict
    rests on something the task declared, something its text said, or only on
    who would handle it.
    """

    value: str | None
    source: str


# ── HIGH-blast signals ───────────────────────────────────────────────────────
# Scanned first; the first match wins, ordered most-consequential first so a
# task that both pays someone and opens a PR is classified by the payment.
# Narrow multi-word phrases on purpose (see execution/daemons/apis/routing.py):
# a bare common word is how a silent misclassification is born.
_HIGH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(send (a |the )?payment|make (a |the )?payment|pay (the |a |an )"
            r"(invoice|rent|wage|salary|bill|instructor|teacher|therapist)|"
            r"transfer (funds|money|eur|€|\d)|issue (a |the )?refund|"
            r"execute (the |a )?payout|remit(tance)? to|wire transfer)\b",
            re.I,
        ),
        "payment",
    ),
    (
        re.compile(
            r"\b(delete (the |all |these |those |any |orphaned )*"
            r"(entit|record|row|file|branch|draft|message|edge)|"
            r"permanently (delete|remove|purge)|purge (the |all )|"
            r"hard[- ]delete|drop (the )?table|empty the trash|"
            r"delete[- ]after[- ]\w+ path)\w*",
            re.I,
        ),
        "delete_entity_or_data",
    ),
    (
        re.compile(
            r"\b(send (the |an |a )?(email|reply|message|note) to|"
            r"email (the |a )?(client|customer|partner|investor|contact|operator's)|"
            r"reply to (the |his |her |their )?(client|customer|partner|thread)|"
            r"send (the |a )?outreach|notify the (client|customer|partner)|"
            r"post (a |the )?(comment|reply) on (github|the issue|the pr))\b",
            re.I,
        ),
        "send_external_comms",
    ),
    (
        re.compile(
            r"\b(publish (the |a |an )|ship (the )?release|cut (a |the )?release|"
            r"tag (a |the )?release|deploy to production|go live|"
            r"post to (x|twitter|linkedin|substack|bluesky)|"
            r"publish(ing)? (the )?(post|page|article|site|website))\b",
            re.I,
        ),
        "publish",
    ),
    (
        re.compile(
            r"\b(open (a |the )?pr|open (a |the )?pull request|merge (the |a )?pr|"
            r"merge (the )?pull request|submit (a |the )?pr|"
            r"raise (a |the )?pull request|land (the )?(pr|change|fix) (on|to) main|"
            r"push to (main|origin|remote)|force[- ]push|"
            # A concrete PR reference is PR work however it is phrased.
            r"pr #\d+|pull request #\d+)\b",
            re.I,
        ),
        "open_or_merge_pr",
    ),
    (
        re.compile(
            r"\b(create (a |the )?(github )?(issue|milestone|label) (in|on)|"
            r"close (the |a )?(github )?issue|write to the .{0,20}api|"
            r"call the .{0,20}api to (create|update|delete)|"
            r"update (the )?(calendar|gmail|asana|linear) (event|draft|task))\b",
            re.I,
        ),
        "external_api_write",
    ),
]

# ── Report-only signals ──────────────────────────────────────────────────────
# Each is an explicit statement that the work produces a report and writes
# nothing — never the mere absence of a high-blast verb, and never a bare
# "read-only" / "do not modify X" (those usually constrain one part of a task
# that still changes something else).
_REPORT_ONLY = re.compile(
    r"\bwrites? nothing\b|\bpure reporting\b|\breporting only\b|\breport[- ]only\b|"
    r"\bonly output is (a |an |the )?([\w-]+ ){0,2}report\b|"
    r"\bnothing gated on the result\b",
    re.I,
)
# A task that also says it will change something is not report-only, whatever
# else it says. Conservative by construction: a false hit only keeps the task
# at its prior, which is today's behaviour.
_IMPLEMENTATION = re.compile(
    r"\b(implement(s|ed|ing)?|refactor(s|ed|ing)?|migrat(e|es|ed|ing)|rewrite|"
    r"patch(es|ed|ing)?|fix(es|ed|ing)?|build(s|ing)?|deploy(s|ed|ing)?)\b",
    re.I,
)

REPORT_ONLY_ACTION_TYPE = "compute_only_analysis"


def infer_action_type_from_text(title: object, body: object = None) -> str | None:
    """What the task's own text says it does, or ``None`` for no decisive signal.

    Returns a high-blast type when any high-blast pattern matches (the whole
    text is scanned — nothing is truncated, because a signal missed late in a
    long body would be a missed escalation), ``compute_only_analysis`` only for
    an explicit report-only statement with no implementation verb, else
    ``None``. Never returns an alias or ``operator_only``.
    """
    text = " ".join(str(p) for p in (title, body) if p).strip()
    if not text:
        return None
    for pattern, action in _HIGH_PATTERNS:
        if pattern.search(text):
            return action
    if _REPORT_ONLY.search(text) and not _IMPLEMENTATION.search(text):
        return REPORT_ONLY_ACTION_TYPE
    return None


def _has_source_marker(snapshot: Mapping[str, object]) -> bool:
    """True when the task names where it came from. Unknown markers count: a
    provenance this module has not vetted is not one whose prose may loosen a
    gate."""
    return bool(str(snapshot.get("source") or "").strip())


def resolve_gate_action_type(
    skill: str | None,
    snapshot: Mapping[str, object],
    agent_priors: Mapping[str, str] | None = None,
) -> ResolvedActionType:
    """Resolve the action_type for the gate and the MCP preview.

    ``agent_priors`` defaults to ``AGENT_ACTION_TYPE_PRIOR``; ``apis`` passes its
    own alias so a test that patches the prior is seen by both.
    """
    priors = AGENT_ACTION_TYPE_PRIOR if agent_priors is None else agent_priors

    declared = normalize_action_type(snapshot.get("action_type"))
    if declared:
        return ResolvedActionType(declared, SOURCE_DECLARED)

    skill_key = skill.lower() if skill else ""
    prior = priors.get(skill_key) if skill_key else None
    prior_tier = tier_of(prior)

    text_type = infer_action_type_from_text(
        snapshot.get("title"), snapshot.get("body") or snapshot.get("description")
    )

    if text_type is not None:
        if text_type != REPORT_ONLY_ACTION_TYPE:
            # Escalate: only where the prior is absent or LOW.
            if prior is None or prior_tier == _TIER_LOW:
                return ResolvedActionType(text_type, SOURCE_CONTENT)
        elif (
            prior_tier == _TIER_HIGH
            and skill_key in LOWERABLE_PRIOR_AGENTS
            and not _has_source_marker(snapshot)
        ):
            # Lower: only a HIGH prior, only a lowerable agent, only a task with
            # no provenance marker (see module docstring).
            return ResolvedActionType(text_type, SOURCE_CONTENT)

    if prior is not None:
        return ResolvedActionType(prior, SOURCE_AGENT_PRIOR)
    return ResolvedActionType(None, SOURCE_NONE)

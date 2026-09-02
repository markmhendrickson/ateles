"""
lib/daemon_runtime/action_type.py — infer a task's action_type from what the
task DOES, not from who would handle it.

Motivation (ateles#682)
-----------------------
Before this module, ``apis._infer_action_type`` fell back to a per-agent map::

    _AGENT_ACTION_TYPE["cicada"] = "open_or_merge_pr"   # HIGH blast

Cicada is the generalist and owns six of nine routing domains, so *any*
engineering-flavoured task inherited "open_or_merge_pr" and scored as high
blast — including tasks whose entire output is a written report. Blast radius
was a property of the assignee, never of the work. Measured on 2026-09-01/02:
27 consecutive PLAN checkpoints, 23 of them ``open_or_merge_pr`` via Cicada.

The gate's own vocabulary already distinguishes these (default execution_policy
ent_dfce6edecefe3eb7fc9e0337)::

    LOW  : local_edit, draft, neotoma_read,
           neotoma_internal_entity_update, compute_only_analysis
    HIGH : git_push, open_or_merge_pr, payment, send_external_comms,
           delete_entity_or_data, external_api_write, publish

Nothing populated ``task.action_type``, so the vocabulary went unused. This
module supplies it from the task's own text.

Design rules
------------
1. **Never widen.** An inferred value may only be LOW when the text carries a
   positive, specific signal that the work is read/analysis/draft-only. Absent
   such a signal the result is ``None`` and the caller keeps its existing
   conservative behaviour (agent map → policy default). Silence is not a
   licence to auto-execute.
2. **HIGH signals beat LOW signals.** A task that says "analyze X and open a PR"
   is a PR task. The high-blast scan runs first and wins outright.
3. **Explicit beats inferred.** A task that declares its own ``action_type``
   is taken at its word; this module is only consulted when the field is unset.
4. **Composable with routing.** This reads task text, exactly as the domain
   classifier does, but answers a different question ("what does it do?" vs
   "who owns it?"). If a future LLM classifier emits ``action_type`` directly,
   it writes the task field and this inference is skipped by rule 3 — no
   conflict, no rework.

The patterns are deliberately narrow multi-word phrases, following the
discipline established in ``execution/daemons/apis/routing.py``: a bare common
word ("build", "update", "report") is how a silent misclassification is born.
"""

from __future__ import annotations

import re

# ── Vocabulary ───────────────────────────────────────────────────────────────
# Mirrors the default execution_policy's action-type sets. Kept here as the
# canonical spelling used by inference; the policy entity remains authoritative
# for which of these are high vs low blast.
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
        "delete_entity_or_data",
        "external_api_write",
        "publish",
        "release",
    }
)

KNOWN_ACTION_TYPES = LOW_BLAST_ACTION_TYPES | HIGH_BLAST_ACTION_TYPES


# ── HIGH-blast signals ───────────────────────────────────────────────────────
# Scanned FIRST; the first match wins. Order is load-bearing: the most
# consequential and most specific actions come first, so a task that both pays
# someone and opens a PR is classified by the payment.
_HIGH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Money moves.
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
    # Destructive data operations.
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
    # Outbound communication to humans outside the swarm.
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
    # Public publication.
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
    # Repository writes that leave the local machine.
    (
        re.compile(
            r"\b(open (a |the )?pr|open (a |the )?pull request|merge (the |a )?pr|"
            r"merge (the )?pull request|submit (a |the )?pr|"
            r"raise (a |the )?pull request|land (the )?(pr|change|fix) (on|to) main|"
            r"push to (main|origin|remote)|force[- ]push|"
            # A task that cites a concrete PR number is PR work, whichever way
            # it is phrased: "PR #643 open", "open PR #656", "merged PR #666".
            # This is what keeps the config-migration control case HIGH.
            r"\bpr #\d+|\bpull request #\d+)\b",
            re.I,
        ),
        "open_or_merge_pr",
    ),
    # Writes against third-party APIs.
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


# ── LOW-blast signals ────────────────────────────────────────────────────────
# Only consulted when NO high-blast pattern matched. Each requires an explicit
# statement that the work produces analysis, a report, a read, or a draft —
# never a mere absence of high-blast verbs.
_LOW_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # The strongest signal there is: the task says it writes nothing.
    (
        re.compile(
            r"\bwrite nothing\b|\bwrites? nothing\b|"
            r"\bno writes?\b(?!.*\bexcept\b)|"
            r"\bmake no changes\b|\bchange nothing\b|"
            r"\bread[- ]only\b|\bdo not (modify|change|write|edit)\b|"
            r"\bwithout (modifying|changing|writing)\b|"
            r"\bpure reporting\b|\breporting only\b|"
            r"\bno behaviou?r change\b|\bnothing gated on the result\b",
            re.I,
        ),
        "compute_only_analysis",
    ),
    # Report/analysis-only output.
    (
        re.compile(
            r"\b(only output is (a |an |the )?(divergence )?report|"
            r"produce (a |an )?(written )?(report|analysis|assessment|inventory|"
            r"summary|comparison|audit report)|"
            r"report (the )?(findings|divergence|drift|discrepanc)|"
            r"analy[sz]e (and report|then report)|"
            r"investigate and (report|document)|"
            r"diagnos(e|is) (only|and report))\b",
            re.I,
        ),
        "compute_only_analysis",
    ),
    # Retrieval from Neotoma.
    (
        re.compile(
            r"\b(retrieve (the |all |every )*(entit|task|record|snapshot)|"
            r"query neotoma|search neotoma|"
            r"list (the |all )?(entit|task|plan)s? (in|from) neotoma|"
            r"count (the )?(entit|record|row)s)\w*",
            re.I,
        ),
        "neotoma_read",
    ),
    # Bookkeeping writes that stay inside Neotoma.
    (
        re.compile(
            r"\b(correct (the )?(plan|task|entity) field|"
            r"update (the )?(plan|task) (entity|status|todos|decisions)|"
            r"store (the )?(finding|analysis|report|digest|entit)|"
            r"file (the )?(finding|task)s? in neotoma|"
            r"record (the )?(decision|outcome) (in|on) (neotoma|the plan))\b",
            re.I,
        ),
        "neotoma_internal_entity_update",
    ),
    # Drafts that are staged, never sent.
    (
        re.compile(
            r"\b(draft (a |an |the )?(reply|email|response|message|post|note)|"
            r"stage (a |the )?(draft|reply)|"
            r"prepare (a |an |the )?draft|"
            r"draft(ed)? but (do )?not send|without sending)\b",
            re.I,
        ),
        "draft",
    ),
    # Edits confined to the working tree.
    (
        re.compile(
            r"\b(edit (the )?(file|local|working)|"
            r"local (edit|change)s? only|"
            r"in (the )?working tree only|"
            r"do not (open a pr|push|commit))\b",
            re.I,
        ),
        "local_edit",
    ),
]


def infer_action_type(title: str | None, body: str | None = None) -> str | None:
    """
    Infer what a task DOES from its own text.

    Returns a member of ``KNOWN_ACTION_TYPES``, or ``None`` when the text
    carries no decisive signal. ``None`` explicitly means "unknown" — callers
    MUST keep their existing conservative fallback rather than treating it as
    low blast.

    High-blast signals are scanned first and win outright: a task that analyses
    something *and* opens a PR is a PR task.
    """
    text = " ".join(p for p in (title, body) if p).strip()
    if not text:
        return None

    for pattern, action in _HIGH_PATTERNS:
        if pattern.search(text):
            return action

    for pattern, action in _LOW_PATTERNS:
        if pattern.search(text):
            return action

    return None


def normalize_action_type(raw: object) -> str | None:
    """
    Normalize an explicitly-declared ``action_type`` to the canonical spelling.

    An unrecognized value returns ``None`` rather than being passed through, so
    a typo ("open_pull_request", "analysis") cannot resolve to the policy's
    ``blast_radius_default`` of LOW and quietly earn auto-execution. Unknown
    spelling → treated as undeclared → conservative fallback.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if not value:
        return None
    return value if value in KNOWN_ACTION_TYPES else None

"""
lib/daemon_runtime/action_type.py — resolve the action_type the execution gate
classifies blast radius from, using what a task SAYS it does as well as who
would handle it (ateles#682).

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
3. Task text may then **escalate** that prior, and nothing else:

   A high-blast phrase in the task's text ("open a PR", "send the email to the
   client", "pay the invoice") replaces the prior with the high-blast type it
   names — but ONLY when that is strictly stricter than the prior under the
   EFFECTIVE execution policy, which the caller supplies as ``verdict``. The
   comparison is made on the policy's own classification, never on this
   module's idea of the vocabulary, so an escalation cannot make the gate
   less strict under a policy whose sets differ from the defaults, and a
   never-auto-executable prior is never replaced.

   Text NEVER lowers anything. There is no path from a task's prose to a
   less-strict reading than the prior gives. A report-only statement in a task
   ("write nothing", "pure reporting") is deliberately ignored here: telling an
   affirmative report-only contract from negated, scoped, mixed or spoofed
   language needs independently labelled negative cases, and until that exists
   the safe reading of silence and of ambiguity is the prior. (So #682's
   report-only symptom is NOT fixed by this module.)

   A task with no high-blast phrase resolves exactly as it does today.

Every value this module can emit is a member of the policy's action-type
vocabulary (``test_action_type.py`` holds that against ``gating``); it emits no
aliases and no ``operator_only``.

This module is dependency-free (stdlib only) so the MCP server can import it
without pulling in the daemon runtime's HTTP stack.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Mapping

# ── Vocabulary ───────────────────────────────────────────────────────────────
# A stdlib-only mirror of the classification in ``lib.daemon_runtime.gating``
# (``_FALLBACK_LOW_BLAST`` / ``_FALLBACK_HIGH_BLAST`` / ``NEVER_AUTO_EXECUTE``),
# kept honest by ``test_action_type.py::test_vocabulary_mirrors_gating``. It
# describes the vocabulary; it is NOT consulted to decide a verdict. Every
# comparison of tiers is made under the effective policy the caller supplies.
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

SOURCE_DECLARED = "declared"
SOURCE_CONTENT = "content"
SOURCE_AGENT_PRIOR = "agent_prior"
SOURCE_NONE = "none"

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
# Pattern order is the stable tie-breaker. The gate compares every match under
# its effective policy; callers without a policy retain the first-match result.
# Narrow multi-word phrases on purpose (see execution/daemons/apis/routing.py):
# a bare common word is how a silent misclassification is born.
_HIGH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(send (a |the )?payment|make (a |the )?payment|pay (the |a |an )"
            r"(invoice|rent|wage|salary|bill|instructor|teacher|therapist)|"
            r"transfer (funds|money|eur|\d)|issue (a |the )?refund|"
            r"execute (the |a )?payout|remit(tance)? to|wire transfer)\b|"
            r"\btransfer €(?=\W|$|\d)",
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

# Zero-width and other format characters are removed and every run of
# whitespace (NBSP and Unicode spaces included) becomes one space before the
# patterns run, so a phrase is not missed because of how it is spaced.
_FORMAT_CHARS = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")
_WHITESPACE = re.compile(r"\s+")


def _normalize_text(*parts: object) -> str:
    joined = " ".join(str(p) for p in parts if p)
    folded = unicodedata.normalize("NFKC", joined)
    return _WHITESPACE.sub(" ", _FORMAT_CHARS.sub("", folded)).strip()


def infer_action_type_from_text(*fields: object, verdict: Verdict | None = None) -> str | None:
    """The high-blast type the task's own text names, or ``None``.

    Every text field the caller passes (title, body AND description) is read, in
    full — nothing is truncated and no field is ignored because another is
    present, because a phrase missed in a late or secondary field is a missed
    escalation. With an effective ``verdict``, choose the strictest recognized
    action; ties preserve pattern order. Without it, keep the first match for
    compatibility. Returns ``None`` when no phrase matches. Only the established
    action vocabulary is emitted, never an alias or ``operator_only``.
    """
    text = _normalize_text(*fields)
    if not text:
        return None
    matches = [action for pattern, action in _HIGH_PATTERNS if pattern.search(text)]
    if not matches:
        return None
    if verdict is None:
        return matches[0]
    return max(matches, key=lambda action: _RANK[verdict(action)])


#: ``action_type | None -> "low" | "high" | "never"`` under the EFFECTIVE policy;
#: ``None`` maps to the policy's ``blast_radius_default``.
Verdict = Callable[["str | None"], str]
_RANK = {"low": 0, "high": 1, "never": 2}


def resolve_gate_action_type(
    skill: str | None,
    snapshot: Mapping[str, object],
    agent_priors: Mapping[str, str] | None = None,
    *,
    verdict: Verdict | None = None,
) -> ResolvedActionType:
    """Resolve the action_type for the gate and the MCP preview.

    ``verdict`` is the effective policy's classification (see ``Verdict``). With
    no ``verdict`` there is no escalation: without a policy to compare under, the
    only safe move is to keep the prior. ``agent_priors`` defaults to
    ``AGENT_ACTION_TYPE_PRIOR``; ``apis`` passes its own alias.
    """
    priors = AGENT_ACTION_TYPE_PRIOR if agent_priors is None else agent_priors

    declared = normalize_action_type(snapshot.get("action_type"))
    if declared:
        return ResolvedActionType(declared, SOURCE_DECLARED)

    skill_key = skill.lower() if skill else ""
    prior = priors.get(skill_key) if skill_key else None

    if verdict is not None:
        text_type = infer_action_type_from_text(
            snapshot.get("title"), snapshot.get("body"), snapshot.get("description"),
            verdict=verdict,
        )
        if text_type is not None:
            current = verdict(prior)
            candidate = verdict(text_type)
            # Strictly stricter under the effective policy, and never from NEVER.
            if current != "never" and _RANK[candidate] > _RANK[current]:
                return ResolvedActionType(text_type, SOURCE_CONTENT)

    if prior is not None:
        return ResolvedActionType(prior, SOURCE_AGENT_PRIOR)
    return ResolvedActionType(None, SOURCE_NONE)

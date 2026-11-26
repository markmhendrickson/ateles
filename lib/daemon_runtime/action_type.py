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

1. **A declared value is passed through UNCHANGED** (lowercased and stripped,
   nothing else) -- with ONE exception, below. An unrecognized spelling reaches
   the gate, which treats it as never-auto-executable and says so by name
   (ateles#715). This module never discards, normalizes, or aliases a declared
   value.

   The exception: when the effective policy classifies the declared type LOW
   and the task's own title / body / description names an outward action
   ("Send Alex the reply", "Push and deploy the corrections", "Finish and
   submit the tax questionnaire"), the declared type is ESCALATED to the
   high-blast type the text names (source ``declared_escalated``, with a ``note``
   the gate shows the operator). A declared type is a label somebody typed; the
   text is what the task asks for, and a low label on an outward task is how an
   outward task ran unattended once the policy listed most work types as LOW.
   A declared HIGH or NEVER type, or any declared type when the caller supplies
   no ``verdict`` to compare under, is never touched, and nothing here ever
   lowers anything.
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
   less-strict reading than the prior gives. A statement that does nothing but
   forbid an outward action ("Do not merge, deploy or release.", "Do not cut a
   release.") is not read as asking for it, so it does not escalate; every other
   mention in the task still does (see "Pure prohibitions" below).
   A report-only statement in a task
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
KNOWN_ACTION_TYPES = (
    LOW_BLAST_ACTION_TYPES | HIGH_BLAST_ACTION_TYPES | NEVER_ACTION_TYPES
)

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
SOURCE_DECLARED_ESCALATED = "declared_escalated"
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
    #: Why the text changed the verdict, e.g. "declared docs, but the task text
    #: names an outward action (send)". Empty when it did not. Only fixed verb
    #: labels or a sanitized, truncated phrase -- never raw task prose.
    note: str = ""


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
_HSPACE = re.compile(r"[^\S\n]+")

# \u2500\u2500 Outward-verb detection (imperative-style) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# The phrase patterns above need a fixed shape ("send the reply to ..."). Real
# task titles are looser: "Send Alex the reply", "Push and deploy the four
# corrections", "Finish and submit the tax questionnaire". This second detector
# reads those, and is deliberately conservative about WHERE a verb counts:
#
#   * the text is cut into clauses at sentence punctuation, newlines, commas,
#     colons, spaced dashes and brackets, and each clause into segments at
#     "and" / "then" / "but" / "&" / "plus";
#   * a segment counts only if it STARTS with the outward verb, after optional
#     list markers and softeners ("please", "then", "finish", "need to", "we
#     will", ...). That is the imperative shape: a verb being asked for, not a
#     word that merely appears in the prose;
#   * base form and gerund only ("send", "sending"). Past tense ("sent",
#     "deployed") reports finished work and is not an action;
#   * a clause that opens with a negation ("do not push", "never send",
#     "without deploying") is skipped whole, so "Do not push or deploy" does
#     not hold a task that forbids it. A comma ends a clause, so this alone does
#     not cover a negated list; "Pure prohibitions" below does, narrowly.
#
# Not matched, on purpose: "post-merge cleanup" (hyphenated), "pushback", "push
# notification handler", "deployment docs", "release notes", "send-gate tests",
# "message queue", "email parser", "merge conflict", "Refactor the send gate"
# (the verb is not leading), "Write deployment docs", "Review PR feedback". The
# per-verb stop nouns below are the noun-compound readings of a verb word.
#
# This fails closed: where a title is ambiguous ("Delete the stale user
# records", "Merge main into the branch") it escalates. A false positive costs
# one approval prompt; a false negative lets an outward task run unattended.
_BASE_STOP = (
    r"gates?|queues?|paths?|buttons?|commands?|hooks?|helpers?|handlers?|"
    r"functions?|logic|flows?|scripts?|tests?|templates?|parsers?|schemas?|"
    r"pipelines?|modules?|mechanisms?|wrappers?"
)

# (label, action_type, regex for the verb and any required object, extra stop nouns)
_VERBS: list[tuple[str, str, str, str]] = [
    ("send", "send_external_comms", r"send(?:ing)?", ""),
    (
        "email",
        "send_external_comms",
        r"e-?mail(?:ing)?",
        r"address(?:es)?|parsing|body|bod(?:y|ies)|threads?|subjects?|digests?|"
        r"triage|sync|import|ingest\w*|validation|fields?|ids?|client",
    ),
    (
        "message",
        "send_external_comms",
        r"messag(?:e|ing)",
        r"bus|format|bod(?:y|ies)|fields?|ids?|threads?|types?|logs?|broker|"
        r"passing|ingest\w*",
    ),
    ("dm", "send_external_comms", r"dms?", ""),
    ("reply", "send_external_comms", r"repl(?:y|ying)", r"threads?|all|chains?"),
    ("respond", "send_external_comms", r"respond(?:ing)?", ""),
    ("notify", "send_external_comms", r"notify|notifying", ""),
    ("tweet", "publish", r"tweet(?:ing)?", ""),
    ("post", "publish", r"post(?:ing)?", r"mortems?|install|processing"),
    ("publish", "publish", r"publish(?:ing)?", ""),
    (
        "push",
        "git_push",
        r"push(?:ing)?",
        r"back|notifications?|tokens?|rules?|events?",
    ),
    ("deploy", "release", r"deploy(?:ing)?", ""),
    (
        "release",
        "release",
        r"releas(?:e|ing)",
        r"notes?|checklists?|plans?|process|trains?|management|candidates?|"
        r"engineering|automation|workflows?|branch(?:es)?",
    ),
    ("ship", "release", r"ship(?:ping)?", ""),
    (
        "merge",
        "open_or_merge_pr",
        r"merg(?:e|ing)",
        r"conflicts?|strateg(?:y|ies)|drivers?|sort|commits?|policy|policies",
    ),
    ("submit", "external_api_write", r"submit(?:ting)?", ""),
    (
        "pay",
        "payment",
        r"pay(?:ing)?",
        r"attention|down|rate|cycle|period|structure|scale|grade|gap",
    ),
    (
        "transfer",
        "payment",
        r"transfer(?:ring)?",
        r"learning|function|rate|protocol|matrix",
    ),
    (
        "invoice",
        "send_external_comms",
        r"invoic(?:e|ing)",
        r"templates?|numbers?|generation|flow|pipeline|fields?|data|import|export|"
        r"processing|parsing|ingest\w*",
    ),
    # Verbs that name an outward effect only with an object; the object is part
    # of the match so "wire up the hook" and "file listing" do not qualify.
    (
        "wire",
        "payment",
        # The object is consumed whole (a named thing, or an amount with an
        # optional currency symbol and word) because the trailing word-boundary
        # check in _compile_verbs would otherwise reject "$500" and "200 euros".
        r"wir(?:e|ing)\s+(?:the\s+|a\s+|an\s+)?(?:(?:money|funds?|payment|transfer|"
        r"deposit)|[\u20ac$\u00a3]?\s?\d[\d.,]*\s?(?:k|m|eur|euros?|usd|dollars?|"
        r"gbp|pounds?)?)",
        "",
    ),
    (
        "file",
        "external_api_write",
        r"fil(?:e|ing)\s+(?:the\s+|a\s+|an\s+|my\s+|our\s+|your\s+|his\s+|her\s+|their\s+)?"
        r"(?:[\w-]+\s+){0,3}?(?:forms?|returns?|taxes|tax|claims?|applications?|"
        r"declarations?|paperwork|modelos?|appeals?|complaints?|questionnaires?|"
        r"petitions?|disputes?|extensions?)\b",
        "",
    ),
    (
        "delete",
        "delete_entity_or_data",
        r"(?:delet(?:e|ing)|drop(?:ping)?|purg(?:e|ing)|wip(?:e|ing)|eras(?:e|ing)|"
        r"truncat(?:e|ing))\s+(?:the\s+|all\s+|any\s+|these\s+|those\s+|a\s+|an\s+)?"
        r"(?:[\w-]+\s+){0,3}?(?:entit(?:y|ies)|records?|rows?|tables?|databases?|"
        r"dbs?|data|backups?|accounts?|mailbox(?:es)?|inbox(?:es)?|emails?|messages?|"
        r"drafts?|buckets?|volumes?|snapshots?|datasets?|collections?|users?|"
        r"contacts?|orphans?)\b",
        r"parsers?|parsing|code|handlers?|templates?|modules?|logic|helpers?|"
        r"functions?|tests?|fixtures?",
    ),
    (
        "payment",
        "payment",
        r"(?:process|approve|execute|settle|schedule|authori[sz]e)\s+"
        r"(?:the\s+|an?\s+)?(?:[\w-]+\s+){0,2}?(?:payments?|payouts?|invoices?)\b",
        "",
    ),
    (
        "external api write",
        "external_api_write",
        r"(?:close|reopen|comment\s+on|create|open)\s+(?:the\s+|an?\s+)?(?:github\s+)?"
        r"(?:issues?|tickets?)\b",
        "",
    ),
]


def _compile_verbs() -> list[tuple[str, str, re.Pattern[str]]]:
    out: list[tuple[str, str, re.Pattern[str]]] = []
    for label, action, verb, extra in _VERBS:
        stop = _BASE_STOP + (("|" + extra) if extra else "")
        out.append(
            (
                label,
                action,
                re.compile(rf"^(?:{verb})(?![\w'\u2019-])(?!\s+(?:{stop})\b)", re.I),
            )
        )
    return out


_VERB_PATTERNS = _compile_verbs()

# A hyphen inside a word is NOT a boundary; only a spaced dash is.
_CLAUSE_SPLIT = re.compile(
    r"[\n;!?()\[\]]+|[.:,]\s+|[.:,]$|\s[-\u2013\u2014]+\s|\s*[\u2022*]\s+"
)
_SEGMENT_SPLIT = re.compile(r"\s+(?:and then|and|then|but|plus|&)\s+|\s*&\s*", re.I)
_LEADING_MARKERS = re.compile(
    r"^(?:\s|[-*\u2022>#\"'`_]|\d+[.)]|\[[ xX]\]|(?:todo|task|action|next step)s?\b)+",
    re.I,
)
_SOFTENERS = re.compile(
    r"^(?:(?:please|kindly|now|then|also|first|next|finally|just|still|finish|"
    r"complete|and|go ahead and|need(?:s)? to|have to|has to|must|should|"
    r"ready to|time to|will|can you|could you|let'?s|remember to|"
    r"don'?t forget to|you|we|i)\s+)+",
    re.I,
)
_NEGATED = re.compile(
    r"^(?:(?:please|just|you|we|i)\s+)*(?:do not|don'?t|never|must not|mustn'?t|"
    r"should not|shouldn'?t|cannot|can'?t|won'?t|will not|without|avoid|no|not)\b",
    re.I,
)


def _sanitize_label(raw: str) -> str:
    """A label safe to show an operator: lowercase, no markup, bounded."""
    return re.sub(r"[^a-z0-9_ #\u20ac$'-]", "", raw.lower()).strip()[:40]


def _normalize_lines(*parts: object) -> str:
    """Like ``_normalize_text`` but keeps line breaks (they are clause breaks)."""
    joined = "\n".join(str(p) for p in parts if p)
    folded = _FORMAT_CHARS.sub("", unicodedata.normalize("NFKC", joined))
    return "\n".join(_HSPACE.sub(" ", line).strip() for line in folded.splitlines())


def _outward_verb_hits(*fields: object) -> list[tuple[str, str]]:
    """``(action_type, label)`` for every imperative-style outward verb found."""
    text = _normalize_lines(*fields)
    hits: list[tuple[str, str]] = []
    for clause in _CLAUSE_SPLIT.split(text):
        clause = _LEADING_MARKERS.sub("", clause or "").strip()
        if not clause or _NEGATED.match(_SOFTENERS.sub("", clause)):
            continue
        for segment in _SEGMENT_SPLIT.split(clause):
            segment = _SOFTENERS.sub(
                "", _LEADING_MARKERS.sub("", segment or "")
            ).strip()
            if not segment:
                continue
            for label, action, pattern in _VERB_PATTERNS:
                if pattern.search(segment):
                    hits.append((action, label))
    return hits


# ── Pure prohibitions ────────────────────────────────────────────────────────
# "Do not merge, deploy or release." forbids an action, yet the clause split
# above cuts it at the comma (leaving "deploy or release" as a request), and the
# phrase patterns ignore negation ("Do not cut a release."). Before either
# detector runs, a statement that does NOTHING BUT forbid is removed. The test
# is deliberately far narrower than English negation, because a miss costs one
# approval prompt and a wrong removal lets an outward task run unattended.
#
# A statement is a whole sentence (ending ".", "!", a line break that really
# ends it, or the end of the field) and has exactly one of two shapes, after an
# optional "please" / "also" / "you" and an optional list marker:
#
#   * list:   NEGATOR VERB[, VERB...][,] or|nor VERB [anything else | anything |
#             it | this | them]      -- bare verbs only, "or"/"nor" required;
#   * phrase: NEGATOR [ever | directly | manually | yourself] PHRASE
#             -- one complete release/publish/PR/push phrase, nothing after it.
#
# So nothing carries a negation across "and", "then", "but", ",", ";", ":", a
# dash, a sentence end or an item with its own object. Even a matching statement
# stays held when the text around it may qualify or revoke it:
#
#   * the next sentence, line or field opens with a condition, a time, a
#     reversal or a back-reference ("Until QA signs off.", "Yet.", "Instead...",
#     "Do it Friday."), or mentions "until", "unless", "yet", "anyway", an
#     approval or a sign-off anywhere;
#   * the line is wrapped into the one before or after it, or has an indented
#     line under it;
#   * a heading or list introduction above it carries a condition or negation;
#   * it sits inside an unclosed quote, bracket or code span, or its field uses
#     markup that can quote or strike text out (HTML comments and quote or
#     strike tags, "~~").
#
# Payment, deletion, messaging and API phrases are not on the phrase list: a
# negated one stays held.
#
# Known limit: only the text next to a prohibition is checked. A later sentence
# that revokes it in other words ("Do not cut a release. Cut it Friday."), or
# earlier prose that calls it obsolete, is not understood.
_LIST_NEGATOR = r"(?:do not|don['’]?t|never|must not|mustn['’]?t|no)"
_PHRASE_NEGATOR = r"(?:do not|don['’]?t|never|must not|mustn['’]?t)"
_BARE_VERB = (
    r"(?:send|e-?mail|message|dm|reply|respond|notify|tweet|post|publish|"
    r"force[- ]push|push|deploy|release|ship|merge|submit|pay|transfer|invoice|"
    r"delete)"
)
_NEGATABLE_PHRASE = (
    r"(?:cut (?:(?:a|the) )?release|tag (?:(?:a|the) )?release|"
    r"ship (?:the )?release|deploy to production|go live|"
    r"post to (?:x|twitter|linkedin|substack|bluesky)|"
    r"publish (?:the )?(?:post|page|article|site|website)|"
    r"open (?:(?:a|the) )?(?:pr|pull request)|"
    r"merge (?:(?:the|a) )?pr|merge (?:the )?pull request|submit (?:(?:a|the) )?pr|"
    r"raise (?:(?:a|the) )?pull request|push to (?:main|origin|remote)|"
    r"force[- ]push)"
)
_PROHIBITION = re.compile(
    rf"(?:(?:please|also|you) )*"
    rf"(?:{_LIST_NEGATOR} {_BARE_VERB}(?: ?, ?{_BARE_VERB})*(?: ?, ?| )"
    rf"(?:or|nor) {_BARE_VERB}(?: (?:anything else|anything|it|this|them))?"
    rf"|{_PHRASE_NEGATOR} (?:(?:ever|directly|manually|yourself) )*"
    rf"{_NEGATABLE_PHRASE})"
    rf"[.!]?",
    re.I,
)
_LIST_MARKER = re.compile(r"^(?:[-*•]|\d{1,3}[.)]) +")
_SENTENCE_GAP = re.compile(r"(?<=[.!?]) +")
# Openers of a following sentence that may limit, delay or undo a prohibition.
_QUALIFIER_START = re.compile(
    r"(?:(?:please|also) )*"
    r"(?:until|till|unless|before|after|yet|except|excepting|if|while|whilst|"
    r"when|whenever|once|without|only|but|however|instead|actually|though|"
    r"although|otherwise|pending|provided|providing|assuming|during|barring|"
    r"as (?:long|soon) as|in case|other than|apart from|aside from|save for|"
    r"then|and|or|nor|so|just|kidding|at least|that is|"
    r"not (?:yet|until|before|unless|without|today|now)|"
    r"for (?:now|today|the moment|the time being)|now|today|tonight|tomorrow|"
    r"later|first|wait|hold|do (?:it|so|that|this)|go ahead|anyway|"
    r"it|that|this|these|those|they|exception\w*|exempt\w*|ignore|disregard|"
    r"scratch|strike|cancel)\b",
    re.I,
)
# Words that make a following sentence unsafe wherever they sit in it.
_QUALIFIER_ANYWHERE = re.compile(
    r"\b(?:until|unless|yet|anyway|approv\w*|sign(?:s|ed|ing)?[- ]off)\b", re.I
)
# Words in a heading or list introduction that make the lines under it unsafe.
_UNSAFE_INTRO = re.compile(
    r"\b(?:until|till|unless|before|after|once|yet|except|if|while|whilst|when|"
    r"whenever|without|only|pending|provided|not|no|never|don['’]?t|"
    r"mustn['’]?t|ignore|disregard|skip|remove|delete|drop|longer)\b",
    re.I,
)
# A "." that does not end a sentence, or an ellipsis that leaves one open.
_OPEN_ENDING = re.compile(r"(?:\b(?:e\.g|i\.e|vs|cf|etc|approx|incl)\.|\.\.)$", re.I)


def _ends_sentence(text: str) -> bool:
    return text.endswith((".", "!", "?")) and not _OPEN_ENDING.search(text)


_QUOTE_MARKS = '"`\u201c\u201d\u201e\u00ab\u00bb()[]'
# Markup that can quote or strike out the lines it surrounds.
_WRAPPING_MARKUP = re.compile(
    r"<!--|<(?:blockquote|del|s|strike|pre|code|q)\b|~~", re.I
)


def _inside_quote(before: str) -> bool:
    return (
        before.count('"') % 2 == 1
        or before.count("`") % 2 == 1
        or "\u201e" in before
        or before.count("“") > before.count("”")
        or before.count("«") > before.count("»")
        or before.count("(") > before.count(")")
        or before.count("[") > before.count("]")
    )


def _qualifies(follower: str) -> bool:
    """Whether the sentence after a prohibition may limit, delay or undo it."""
    sentence = _SENTENCE_GAP.split(follower)[0]
    if sentence and not re.search(r"[^\W\d_]", sentence):
        return True  # a bare number or symbol: what follows it cannot be judged
    return bool(
        _QUALIFIER_START.match(re.sub(r"^[\W_]+", "", sentence))
        or _QUALIFIER_ANYWHERE.search(sentence)
    )


@dataclass(frozen=True)
class _Line:
    indent: int
    marker: str
    body: str
    #: A blank line separates this one from the previous non-blank line.
    after_blank: bool


def _split_lines(value: object) -> list[_Line]:
    """Non-blank lines, normalized like ``_normalize_lines``, keeping layout."""
    folded = _FORMAT_CHARS.sub("", unicodedata.normalize("NFKC", str(value)))
    lines: list[_Line] = []
    blank = False
    for raw in folded.splitlines():
        content = _HSPACE.sub(" ", raw).strip()
        if not content:
            blank = True
            continue
        expanded = raw.expandtabs(4)
        found = _LIST_MARKER.match(content)
        marker = found.group(0) if found else ""
        indent = len(expanded) - len(expanded.lstrip())
        lines.append(_Line(indent, marker, content[len(marker) :], blank))
        blank = False
    return lines


def _safe_introduction(body: str) -> bool:
    return not (body.endswith(",") or _UNSAFE_INTRO.search(body))


def _starts_statement(line: _Line, previous: _Line | None, blocked: bool) -> bool:
    """Whether the first sentence of ``line`` stands apart from what is above.

    ``blocked`` says the paragraph introducing this line carries a condition or
    a negation and was not closed as a sentence.
    """
    if previous is None:
        return True
    ended = _ends_sentence(previous.body)
    if not line.marker:
        if ended:
            return True
        # Otherwise only a blank line separates it from a heading; a bare line
        # break is a wrapped sentence.
        return line.after_blank and _safe_introduction(previous.body) and not blocked
    if previous.marker:
        if previous.body.endswith((",", ":")):
            return False
        if not ended and _qualifies(previous.body):
            return False
        if previous.indent < line.indent and not _safe_introduction(previous.body):
            return False
    return not blocked


def _remove_prohibitions(value: object, following: str) -> str:
    """``value`` with each pure prohibition replaced by a bare ".".

    ``following`` is the first line of the next field, so a qualifier placed
    there still holds the statement. Negation never crosses fields: a statement
    must match within one line of one field.
    """
    lines = _split_lines(value)
    if any(_WRAPPING_MARKUP.search(line.marker + line.body) for line in lines):
        return "\n".join(line.marker + line.body for line in lines)
    out: list[str] = []
    quote_marks = ""  # quote characters seen so far, for parity
    previous: _Line | None = None
    # The paragraph that introduces what follows (the last unindented-from-a-list
    # run of non-list lines).
    conditional = False  # it mentions a condition or a negation
    blocked = False  # ... and was left open, so it governs the lines below
    for index, line in enumerate(lines):
        sentences = _SENTENCE_GAP.split(line.body)
        kept = list(sentences)
        for pos, sentence in enumerate(sentences):
            before = quote_marks
            quote_marks += "".join(c for c in sentence if c in _QUOTE_MARKS)
            if not _PROHIBITION.fullmatch(sentence) or _inside_quote(before):
                continue
            if pos:
                if not _ends_sentence(sentences[pos - 1]):
                    continue
            elif not _starts_statement(line, previous, blocked):
                continue
            if pos + 1 < len(sentences):
                follower = sentences[pos + 1]
            elif index + 1 < len(lines):
                below = lines[index + 1]
                if below.indent > line.indent:
                    continue
                if not (
                    sentence.endswith((".", "!")) or below.after_blank or below.marker
                ):
                    continue
                follower = below.body
            else:
                follower = following
            if _qualifies(follower):
                continue
            kept[pos] = "."
        out.append(line.marker + " ".join(kept))
        # An indented line under a list item continues it; it does not become
        # the introduction of the items that follow.
        if not line.marker and not (
            previous and previous.marker and line.indent > previous.indent
        ):
            if (
                previous is None
                or line.after_blank
                or previous.marker
                or _ends_sentence(previous.body)
            ):
                conditional = False
            conditional = conditional or bool(_UNSAFE_INTRO.search(line.body))
            blocked = not _ends_sentence(line.body) and (
                conditional or line.body.endswith(",")
            )
        previous = line
    return "\n".join(out)


def _without_prohibitions(fields: tuple[object, ...]) -> list[str]:
    present = [f for f in fields if f]
    result: list[str] = []
    for index, value in enumerate(present):
        following = next(
            (
                lines[0].body
                for lines in map(_split_lines, present[index + 1 :])
                if lines
            ),
            "",
        )
        result.append(_remove_prohibitions(value, following))
    return result


def _normalize_text(*parts: object) -> str:
    joined = " ".join(str(p) for p in parts if p)
    folded = unicodedata.normalize("NFKC", joined)
    return _WHITESPACE.sub(" ", _FORMAT_CHARS.sub("", folded)).strip()


#: ``action_type | None -> "low" | "high" | "never"`` under the EFFECTIVE policy;
#: ``None`` maps to the policy's ``blast_radius_default``.
Verdict = Callable[["str | None"], str]
_RANK = {"low": 0, "high": 1, "never": 2}


def infer_text_evidence(
    *fields: object, verdict: Verdict | None = None
) -> tuple[str, str] | None:
    """``(action_type, label)`` the task's text names, or ``None``.

    Reads the phrase patterns (``_HIGH_PATTERNS``) and the imperative-verb
    detector (``_outward_verb_hits``) over every field in full. ``label`` is a
    fixed verb name or a sanitized matched phrase, safe to show an operator.
    Both detectors see the fields with pure prohibitions removed.
    With a ``verdict``, the strictest candidate wins and ties keep the earlier
    one; without it the first phrase match (else first verb match) is returned.
    """
    asked = _without_prohibitions(fields)
    text = _normalize_text(*asked)
    if not text:
        return None
    candidates: list[tuple[str, str]] = []
    for pattern, action in _HIGH_PATTERNS:
        m = pattern.search(text)
        if m:
            candidates.append((action, _sanitize_label(m.group(0))))
    candidates.extend(_outward_verb_hits(*asked))
    if not candidates:
        return None
    if verdict is None:
        return candidates[0]
    best = candidates[0]
    for cand in candidates[1:]:
        if _RANK[verdict(cand[0])] > _RANK[verdict(best[0])]:
            best = cand
    return best


def infer_action_type_from_text(
    *fields: object, verdict: Verdict | None = None
) -> str | None:
    """The high-blast type the task's own text names, or ``None``.

    Every text field the caller passes (title, body AND description) is read, in
    full — nothing is truncated and no field is ignored because another is
    present, because a phrase missed in a late or secondary field is a missed
    escalation. With an effective ``verdict``, choose the strictest recognized
    action; ties preserve pattern order. Without it, keep the first match for
    compatibility. Returns ``None`` when nothing matches. Only the established
    action vocabulary is emitted, never an alias or ``operator_only``.
    """
    hit = infer_text_evidence(*fields, verdict=verdict)
    return hit[0] if hit else None


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

    text_fields = (
        snapshot.get("title"),
        snapshot.get("body"),
        snapshot.get("description"),
    )

    declared = normalize_action_type(snapshot.get("action_type"))
    if declared:
        # A declared type the policy calls LOW is a label, not a promise: if the
        # task's own text asks for an outward action, hold it. HIGH, NEVER and
        # anything not comparable pass through exactly as declared.
        if verdict is not None and verdict(declared) == "low":
            hit = infer_text_evidence(*text_fields, verdict=verdict)
            if hit is not None and _RANK[verdict(hit[0])] > _RANK["low"]:
                return ResolvedActionType(
                    hit[0],
                    SOURCE_DECLARED_ESCALATED,
                    f"declared {_sanitize_label(declared) or 'low'}, but the task "
                    f"text names an outward action ({hit[1]})",
                )
        return ResolvedActionType(declared, SOURCE_DECLARED)

    skill_key = skill.lower() if skill else ""
    prior = priors.get(skill_key) if skill_key else None

    if verdict is not None:
        hit = infer_text_evidence(*text_fields, verdict=verdict)
        if hit is not None:
            text_type, label = hit
            current = verdict(prior)
            candidate = verdict(text_type)
            # Strictly stricter under the effective policy, and never from NEVER.
            if current != "never" and _RANK[candidate] > _RANK[current]:
                return ResolvedActionType(
                    text_type,
                    SOURCE_CONTENT,
                    f"the task text names an outward action ({label})",
                )

    if prior is not None:
        return ResolvedActionType(prior, SOURCE_AGENT_PRIOR)
    return ResolvedActionType(None, SOURCE_NONE)

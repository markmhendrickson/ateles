#!/usr/bin/env python3
"""Stage 0 of the rule migration: the machine-generated rule inventory.

`docs/foundation/migration.md` defines "stage 0, inventory" as a real migration
stage, and it has been run twice before -- a record inventory (`status.md`
revision 28), a skill inventory (revision 32). This is the third: every place a
rule is STATED across the system, extracted per rule rather than per file,
clustered by KIND rather than by value, and written to
`docs/foundation/rule_inventory.md`.

Why a script and not a table someone typed
------------------------------------------
The prose version of this inventory (ateles#1114's comment thread) was wrong
twice, both times caught only by re-measuring: Cursor was reported as 5 files
when it holds 31, and "five lenses citing three foundation files" was five
lenses citing five different files. A hand-count cannot be diffed and cannot
detect its own drift. This can: re-run it and `--check` fails if the committed
output no longer matches the measured system.

The four properties the inventory has to have
---------------------------------------------
1. MACHINE-GENERATED. Re-runnable, so drift is detectable rather than asserted.

2. PER-RULE, NOT PER-FILE. `~/.codex/AGENTS.md` is 26KB of many rules; a file
   count says nothing. Statements are extracted individually.

3. FIELD-NAME AGNOSTIC for entities. `standing_rule` text lives in
   `instruction` (30 rows), `content` (13), `rule` (10), `summary` (6), and
   `rule_text` (5); 4 rows carry none and 12 carry more than one. `agent_policy`
   uses a different set again (`description`, `rule`, `body`, `summary`). A
   reader checking one field name drops rows SILENTLY and reports a clean run --
   the undeclared-field failure `CLAUDE.md` already names, on the read side. So
   every known spelling is read, `raw_fragments` included, and rows with no text
   at all are reported rather than skipped.

4. DEDUPLICATED BY KIND, NOT BY VALUE. This is the governing constraint from
   `migration.md`: "standing rules go to `task_policy` by kind, never by value."
   Never-stash stated in five harnesses is ONE rule with five statements, not
   five rules. Clustering is by kind signature, and the cluster reports every
   location plus whether the statements AGREE or DIVERGE.

   A kind is decided by the MERGE TEST: two statements are the same rule only
   if a session cannot satisfy one while violating the other. The first
   revision clustered by TOPIC instead, which merged distinct rules -- the
   harness-questions-tool rule vanished into a status-update cluster and the
   absence was caught by the operator, not by the instrument. So every cluster
   now reports its distinct-statement count and one that is a topical bucket
   is emitted as NEEDS-SPLIT rather than counted as a rule. The test binds in
   both directions: over-splitting inflates the rule count and understates the
   duplication the migration exists to collapse, and is equally wrong.

Public-data posture -- read this before changing the emitter
------------------------------------------------------------
Both repos are PUBLIC and at least five rule entities carry operator specifics
(a live BTC address, a payee first name, a vendor, an instructor, a gym, EUR
amounts). The inventory therefore records a rule's LOCATION and KIND and NEVER
its operator-specific VALUE. A pattern screen cannot recognize arbitrary proper
nouns, client identifiers, daemon names, or path components. The emitter
therefore never copies dynamic rule values or metadata into either public
output: it projects them to a closed vocabulary of store kinds and generic
locations. A PII-shaped literal in a committed inventory is the exact failure
this workstream already had to rewrite git history to undo (ateles#1099).

Reachability is measured, not assumed
-------------------------------------
ateles#1118: `agent_loader.py` filters every `agent_policy` on `agent_sub`,
which is empty in all 25 rows, so every agent loads zero policies. A store can
be fully populated and deliver nothing. Each store therefore carries a
`reachable` verdict distinct from its populated count.

Read-only. Neotoma PROD, never the dev instance. Writes one repo file.

Usage:
    python3 execution/scripts/render_rule_inventory.py            # write
    python3 execution/scripts/render_rule_inventory.py --check    # verify
    python3 execution/scripts/render_rule_inventory.py --json OUT # public dump
    python3 execution/scripts/render_rule_inventory.py --check \
        --private-diagnostics /tmp/rule-inventory-locators.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT = REPO_ROOT / "docs" / "foundation" / "rule_inventory.md"

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)

# Every field name under which a rule's normative text is known to live, across
# both entity types. Property 3: a single-field reader drops rows silently.
TEXT_FIELDS = (
    "instruction",
    "content",
    "rule",
    "summary",
    "rule_text",
    "rule_body",
    "description",
    "body",
    "policy_text",
    "policy",
    "guidance",
    "text",
    "details",
)

# The entity types that hold rules. `task_policy` is the migration's target home
# for operator preferences and is itself a live rule store, so it is inventoried
# rather than treated only as a destination.
RULE_ENTITY_TYPES = ("standing_rule", "agent_policy", "task_policy")

# The canonical measurement runner supplies the additional repository clones
# that belong to the rule estate. Keeping this list in runner configuration
# avoids publishing repository names or local paths while making missing
# configuration a blocking, unread measurement rather than an empty one.
CANONICAL_REPOSITORY_ROOTS_ENV = "RULE_INVENTORY_CANONICAL_REPOSITORY_ROOTS"


# ---------------------------------------------------------------------------
# PII screen
# ---------------------------------------------------------------------------

# Patterns for operator-specific VALUES that must never reach a public file.
# Deliberately over-broad: a false positive costs one withheld statement, a
# false negative costs a history rewrite.
PII_PATTERNS: tuple[tuple[str, str], ...] = (
    ("btc_address", r"\b(?:bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}\b"),
    ("iban", r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    ("eur_amount", r"(?:€\s?\d|(?<![\w.])\d[\d.,]*\s?(?:EUR|euros?)\b)"),
    ("usd_amount", r"\$\s?\d[\d.,]*"),
    ("email", r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    ("phone", r"(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,}\d"),
    ("stx_address", r"\bS[PM][0-9A-Z]{30,}\b"),
)

# Allowed because they name a SYSTEM, not an operator value. An agent's AAuth
# sub and the swarm domain are role identifiers and appear throughout the
# public corpus already.
PII_ALLOW = re.compile(
    r"@ateles-swarm|@anthropic\.com|noreply@|example\.(?:com|org)|"
    r"markmhendrickson\.com|@neotoma|user@host",
    re.I,
)

# A PATTERN screen catches structured values -- an address, an IBAN, an amount.
# It does NOT catch a NAME, and names are what the operator-specific rule
# entities actually carry: a payee's first name, an instructor, a gym, a vendor.
# Verified directly: all five entities named in the brief as carrying PII pass
# the pattern screen cleanly, because their PII is a proper noun. So a second
# screen keys on the DOMAINS where a statement is about the operator's own
# affairs rather than the swarm's conduct. It is deliberately blunt: in these
# domains the statement is withheld and only its kind recorded, which is what
# "record the LOCATION and KIND, never the VALUE" requires.
PII_DOMAIN = re.compile(
    r"\b(?:yoga|therapy|therapist|instructor|gym|trainer|massage|physio|"
    r"payee|landlord|tenant|invoice to|pay(?:ment)?s? to|send (?:btc|money|eur)|"
    r"wallet|wise|iban|bizum|salary|rent|utility bill|supplement|"
    r"doctor|clinic|prescription|diagnos|medication|blood|lab result)\b",
    re.I,
)


def screen_for_pii(text: str) -> tuple[bool, list[str]]:
    """Return (is_clean, reasons). Applied to every string before emission.

    Two screens, because one is not enough. The pattern screen catches
    structured values; the domain screen catches statements about the
    operator's personal affairs, whose PII is typically a proper noun no
    pattern matches. Over-broad on purpose: a false positive costs one withheld
    statement, a false negative costs a git-history rewrite (ateles#1099).
    """
    if not text:
        return True, []
    reasons = []
    for name, pat in PII_PATTERNS:
        for m in re.finditer(pat, text):
            if PII_ALLOW.search(m.group(0)):
                continue
            reasons.append(name)
            break
    if PII_DOMAIN.search(text):
        reasons.append("operator_personal_domain")
    return (not reasons), sorted(set(reasons))


WITHHELD = "operator-specific, value withheld"

# Substituted for the text of an entity whose fields trip the screen. It is not
# empty, because the inventory still has to record that a rule is STATED here --
# the location and the fact of it are the point; only the value is withheld.
# Such a statement lands in no kind cluster (it matches no signature), so it is
# counted and located, and never classified on evidence nobody can see.
WITHHELD_MARKER = WITHHELD


# Volatile identifiers a quoted statement may carry. They are state claims, and
# `conformance.md#phases-and-implementation-state` keeps those out of this
# directory: a commit hash or an issue number belongs to `status.md`, not to a
# design document. They are also no part of a rule's KIND -- which is what this
# inventory clusters on -- so redacting them loses nothing it measures and
# keeps the quotation from smuggling a state claim into the corpus.
VOLATILE = (
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<sha>"),
    (re.compile(r"(?<![\w/])#\d{2,6}\b"), "<issue>"),
)


def safe_statement(text: str, limit: int = 120) -> str:
    """One line describing a rule, screened. Never emits an operator value."""
    clean, _ = screen_for_pii(text)
    if not clean:
        return WITHHELD
    flat = " ".join(text.split())
    # Redact volatile identifiers BEFORE stripping markdown: the `#` strip would
    # otherwise take the sigil off an issue number and leave a bare integer the
    # redaction can no longer recognise.
    for rx, repl in VOLATILE:
        flat = rx.sub(repl, flat)
    flat = re.sub(r"[`*_#\[\]]", "", flat)
    if len(flat) > limit:
        flat = flat[: limit - 1].rsplit(" ", 1)[0] + "…"
    return flat or "(no text)"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class Statement:
    """One place a rule is stated. The unit of measurement."""

    store: str
    location: str  # file path or entity id
    locator: str  # line number or field name
    text: str
    kind: str = ""  # assigned by classify()
    last_modified: str = ""

    @property
    def safe_text(self) -> str:
        # The public inventory measures kind and location, not value. Pattern
        # screens cannot recognize arbitrary proper nouns, so even text that
        # passes screen_for_pii() must not be emitted.
        return WITHHELD


@dataclass
class Store:
    """One rule store, with its reachability verdict."""

    name: str
    location: str
    populated: int = 0
    statements: int = 0
    last_modified: str = ""
    reachable: str = "unknown"
    reach_note: str = ""
    note: str = ""
    read_ok: bool = True
    read_error: str = ""
    # Safe aggregate measurements used by generated narrative. They remain
    # numbers rather than prose so the emitter cannot accidentally publish a
    # path, name, or other identifier embedded in an internal note.
    distinct_versions: int | None = None
    rule_bearing_files: int | None = None


# Thresholds for the NEEDS-SPLIT probe.
#
# At 0.85/8 the probe fires on every cluster the operator's own reading found
# over-merged. It is deliberately tuned to over-report rather than under-
# report: a false flag costs one human read of a cluster, a missed one is the
# failure this probe exists to prevent, and it is invisible.
#
# What the probe measures is statement VARIETY, which is a proxy for rule
# identity and not the thing itself. Two consequences, both observed in the
# corpus rather than supposed: a cluster can be flagged because it genuinely
# holds several rules (the questions-tool case), or because a correctly-merged
# rule has collected statements that merely mention it (the never-stash case,
# which catches a hook's own test fixture). The probe cannot tell these apart
# -- it reports the cluster, a human reads it, and the remedy is a split in
# the first case and a narrower signature in the second.
#
# MIN_STATEMENTS_TO_JUDGE exists because the ratio is meaningless in the small
# tail: two statements worded differently sit at 1.0, which is ordinary.
SPLIT_RATIO = 0.85
MIN_STATEMENTS_TO_JUDGE = 8


@dataclass
class Cluster:
    """One RULE. Possibly stated in many places, possibly divergently."""

    kind: str
    label: str
    statements: list[Statement] = field(default_factory=list)

    @property
    def rule_id(self) -> str:
        h = hashlib.sha256(self.kind.encode()).hexdigest()[:6]
        return f"R-{h}"

    @property
    def stores(self) -> list[str]:
        return sorted({s.store for s in self.statements})

    @property
    def distinct_openings(self) -> int:
        """How many DIFFERENT statements this cluster holds.

        The over-merge probe. A rule restated across stores repeats itself --
        never-stash is stated in five harnesses in close to the same words --
        so a genuine cluster has far fewer distinct openings than statements.
        A cluster whose distinct count approaches its statement count is
        holding statements that merely share vocabulary: a topical bucket.

        Six words, because that is enough to separate "never invent quotes"
        from "never invent commitments" while still collapsing the same rule
        quoted with different leading whitespace or list markers.
        """
        return len({" ".join(s.text.lower().split()[:6]) for s in self.statements})

    @property
    def needs_split(self) -> bool:
        """True when the cluster is a topical bucket rather than a rule.

        Emitted as NEEDS-SPLIT rather than as a rule, so over-merge is visible
        in the OUTPUT. The first revision of this inventory merged the
        harness-questions-tool rule into a status-update cluster, and the
        absence was caught by the operator noticing a rule he knew existed was
        missing -- not by the instrument. An inventory whose only over-merge
        detector is a reader's memory is not measuring its own clustering.

        The threshold is a FLAG, not a verdict, and the document says so. Two
        checks, because the ratio alone misreads both tails: a 2-statement
        cluster is at ratio 1.0 whenever the two are worded differently, which
        is ordinary, and a large cluster can be a genuine bucket at 0.85. So a
        cluster must be both large enough to judge and near-unique to trip it.
        """
        n = len(self.statements)
        return n >= MIN_STATEMENTS_TO_JUDGE and (
            self.distinct_openings / n >= SPLIT_RATIO
        )

    @property
    def shapes(self) -> set[str]:
        """The normative shapes present, ignoring statements that mark none.

        An "unmarked" statement mentions the rule without saying how strongly it
        binds -- narration, a cross-reference, a heading. It is not evidence of
        disagreement, so it does not make a cluster diverge. Counting it as a
        shape flagged 28 of 36 clusters, which is a property of the test rather
        than of the corpus.
        """
        return {_normative_shape(s.text) for s in self.statements} - {"unmarked"}

    @property
    def diverges(self) -> bool:
        """Property 4: same rule, stated with different binding force.

        The highest-value output. Divergence is judged on whether statements
        agree about how strongly the rule BINDS, not on wording -- two
        statements of never-stash phrased differently agree; one saying "never"
        and one saying "prefer" do not. This is the ateles#1115 shape: two live
        `agent_policy` rows, same safety rule, one `recommended` and one
        `mandatory`.

        A prohibition and a bare mandatory are NOT a divergence: "never force
        push" and "always use --ff-only" bind equally. The disagreement that
        matters is binding versus advisory.
        """
        if len(self.statements) < 2:
            return False
        sh = self.shapes
        binding = {"prohibitive/binding", "mandatory"} & sh
        advisory = {"advisory"} & sh
        return bool(binding and advisory)


def _normative_shape(text: str) -> str:
    """Reduce a statement to how strongly it binds."""
    low = text.lower()
    if re.search(
        r"\bnever\b|\bmust not\b|\bdo not\b|\bdon't\b|\bforbidden\b|"
        r"\bhard-block|\brefus|\bmandatory\b",
        low,
    ):
        return "prohibitive/binding"
    if re.search(r"\balways\b|\bmust\b|\brequired\b|\bshall\b", low):
        return "mandatory"
    if re.search(
        r"\bprefer\b|\bshould\b|\brecommend|\bdefault to\b|"
        r"\bwhere possible\b|\badvisory\b|\btry to\b",
        low,
    ):
        return "advisory"
    return "unmarked"


# ---------------------------------------------------------------------------
# Rule KINDS -- the deduplication axis
# ---------------------------------------------------------------------------
#
# THE MERGE TEST
# --------------
# Two statements are the SAME RULE only if **a session cannot satisfy one while
# violating the other**. Topical similarity is not sufficient and never has
# been. If a session can obey one statement and break the other in the same
# turn, they are two rules however alike they read.
#
# `CLAUDE.md` already carries the governing principle, in the rule-parity
# checker's own terms: "Near-identical leads are reported but never collapsed
# -- `Dispatch, don't work inline` and `Dispatch, don't drift inline` are two
# rules." The merge test is that principle made mechanical for this inventory.
#
# Worked: "give status updates unprompted" and "pose every open decision
# through the harness questions tool" are both about surfacing things to the
# operator. A session that ends its turn with a prose decision list has
# satisfied the first and violated the second. Two rules. The first revision of
# this table merged them, and the absence was noticed by the operator rather
# than by the instrument -- which is why the NEEDS-SPLIT probe below exists.
#
# The converse failure is equally wrong. A rule genuinely restated across seven
# stores is ONE rule with seven statements, not seven rules: never-stash is
# stated in five harnesses in five wordings and a session cannot obey any one
# of them while breaking another. Splitting that would inflate the rule count
# and understate the duplication the migration exists to collapse. The test is
# satisfy/violate, applied honestly in BOTH directions.
#
# A kind is what the rule is ABOUT, not what it says. This is the "by kind,
# never by value" constraint made mechanical: the signature decides the
# cluster, and the cluster carries every location plus the agree/diverge
# verdict.
#
# ORDER IS SIGNIFICANT. `classify()` assigns a statement to EVERY kind it
# matches, so a broad signature sitting beside a narrow one silently absorbs
# the narrow rule's statements into both. Where two kinds overlap by
# vocabulary, the narrow one carries a negative lookahead or a distinguishing
# token rather than relying on position -- position alone is not a mechanism.
#
# Deliberately conservative. A statement matching no signature lands in the
# unclassified pile rather than being forced into a neighbour -- an over-eager
# merge hides a divergence, which is the one output this inventory exists to
# produce.

KIND_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    # (kind key, human label, regex over lowercased text)
    # The prohibition and the recovery procedure are two rules. A session that
    # never stashes cannot violate the recovery rule, and a session recovering
    # another agent's stash has already had the prohibition broken for it. The
    # recovery signature is listed FIRST and the prohibition excludes it, so
    # `apply <sha>, never pop` does not also count as a statement of never-stash.
    (
        "git_stash_recovery",
        "Recover a stash by apply-with-SHA, never pop a shared stack",
        r"stash (?:apply|list|push)|never.{0,10}\bpop\b|apply by sha|"
        r"stash recovery|drop that entry",
    ),
    # The prohibition excludes statements that are really about the recovery
    # PROCEDURE, and the exclusion is applied to the whole statement -- a
    # lookahead at the match position is not enough, since a statement reading
    # "`git stash push -u -m` — never bare `git stash`" carries the recovery
    # verb before the prohibition token.
    #
    # It keys on the recovery COMMANDS, never on the bare word "pop". The
    # canonical statement of this rule is "NEVER `git stash` in any form — the
    # stash stack is shared across worktrees and other sessions pop it", so a
    # bare-`pop` exclusion drops the rule's own primary statement in two
    # harnesses and keeps only its restatements. A rule whose signature
    # excludes its own canonical wording is worse than an over-merge: the
    # over-merge is at least visible in a count.
    (
        "git_never_stash",
        "Never use git stash; WIP-commit instead",
        r"^(?!.*(?:stash (?:apply|list|push)|\bpop\b (?:that|the) (?:entry|stash)|"
        r"apply by sha|never.{0,5}\bpop\b))"
        r".*(?:git stash|never stash|\bstash\b.{0,40}(?:forbidden|never|guard))",
    ),
    # SPLIT from one "worktree" kind that matched the bare word and so swept in
    # every rule that merely mentions a worktree. Two rules, plus the recovery
    # rule the never-stash kind was also absorbing. A session can give each
    # agent its own worktree (satisfying the first) and still commit into a
    # sibling repo's shared clone (violating the second): they have separate
    # enforcement -- `one worktree, one agent` is prose, the sibling-repo guard
    # is a PreToolUse hook.
    (
        "worktree_one_per_agent",
        "One worktree, one agent; never point two at the same tree",
        r"one worktree.{0,15}one agent|one agent.{0,20}one worktree|"
        r"two agents.{0,30}same worktree|its own worktree",
    ),
    (
        "shared_clone_no_mutation",
        "Never mutate a sibling repo's shared main clone; add a worktree first",
        r"shared (?:main )?clone|sibling.?repo|sibling_repo_worktree_guard|"
        r"git worktree add|main clone",
    ),
    (
        "neotoma_prod_only",
        "Always use the Neotoma prod instance, never dev",
        r"neotoma prod|prod.{0,20}never.{0,15}dev|mcpsrv_neotoma.{0,30}always",
    ),
    (
        "gws_over_gmail_mcp",
        "Use the gws CLI for Google Workspace, not the MCP",
        r"\bgws\b.{0,60}(cli|gmail|mcp)|gmail mcp",
    ),
    (
        "gmail_send_gate",
        "Gmail sends and draft-updates need per-message approval",
        r"drafts update|draft.{0,15}can send|messages send|send gate|never send.{0,25}(mail|email)",
    ),
    (
        "public_repo_pii",
        "Both repos are public; scrub PII before committing",
        r"public repo|both repos are public|scrub.{0,20}(pii|client|username)|"
        r"strip pii|no operator data|pii-free",
    ),
    (
        "verify_write_landed",
        "Read a write back; a success code is not a landed write",
        r"read it back|write that reports success|verify.{0,25}(write|landed)|"
        r"not treat a 2xx|success.{0,15}is not",
    ),
    # SPLIT. "Verify the claim against the system of record" and "validate the
    # instrument before believing what it returned" are separately stated in
    # both `CLAUDE.md` and `neotoma/AGENTS.md`, and are separately violable: a
    # session that queries the live system of record and believes a false zero
    # has satisfied the first and violated the second. That is the shape of the
    # three independent false zeros `CLAUDE.md` records on one day.
    (
        "verify_before_asserting",
        "Verify against the live system of record before asserting",
        r"verify before assert|check the live system|"
        r"verified only when you just checked|before asserting",
    ),
    (
        "validate_the_instrument",
        "Validate the instrument before believing a measurement; a surprising zero is the tool",
        r"validate the instrument|surprising (?:number|zero)|"
        r"claim about (?:your tooling|the query)|false zero|"
        r"prove the instrument",
    ),
    (
        "fail_closed",
        "Absent or malformed safety values take the restrictive branch",
        r"fail clos|fail-clos|restrictive branch|fail open|fail-open",
    ),
    # SPLIT because `CLAUDE.md` names these as two rules by its own parity
    # rule: "`Dispatch, don't work inline` and `Dispatch, don't drift inline`
    # are two rules." The first is about where work is FILED at the moment it
    # is recommended; the second is about a session that files correctly and
    # then does the work anyway, one small step at a time. A session violates
    # the second precisely by satisfying the first and then not stopping.
    (
        "dispatch_not_inline",
        "Dispatch work to the owning agent; file it as you recommend it",
        r"dispatch, don't work|dispatch.{0,25}not inline|inline execution|"
        r"owning agent|orchestrat.{0,20}not.{0,15}workhorse|delegate",
    ),
    (
        "dispatch_no_drift",
        "Do not drift into an agent's work one step at a time",
        r"dispatch, don't drift|drift inline|"
        r"one small step at a time|agent's whole job itself",
    ),
    (
        "durable_work_not_task_chip",
        "Durable work goes to a dispatched agent, never a harness task chip",
        r"task chip|spawn_task|never.{0,20}chip|chip is not an entity|"
        r"unclaimable and invisible",
    ),
    (
        "no_merge_over_objection",
        "Do not merge while a live blocking review stands",
        r"blocking review|do not merge|merge.{0,20}gated|required approval",
    ),
    (
        "no_verify_bypass",
        "Never bypass the pre-commit hook with --no-verify",
        r"--no-verify|no-verify|skip_tests",
    ),
    # SPLIT from one "irreversible actions need approval" kind. Three rules
    # that pull against each other and so must be counted separately -- the
    # whole point of the consent boundary is WHERE it falls, and a single
    # cluster made the boundary invisible. A session can correctly proceed
    # without asking on a reversible action (satisfying the second) while
    # sending mail without approval (violating the first); the third bounds
    # which actions never pass to an agent at all.
    (
        "consent_gate_external",
        "Irreversible or outward-facing actions need per-action operator approval",
        r"consent gate|operator approval|irreversible|outward-facing|"
        r"never.{0,20}without.{0,20}approval|confirm with",
    ),
    (
        "operator_only_actions",
        "Some actions stay the operator's absolutely; hand them back with the command",
        r"operator[- ]only|stay(?:s)? mark's|remain the operator's|"
        r"credential rotation|hand back.{0,25}operator|exact command",
    ),
    (
        "blast_radius_classification",
        "Classify an action's blast radius before acting on it",
        r"blast[- ]radius|low_blast|high_blast|"
        r"(?:low|high)-risk operations|autonomy calibration",
    ),
    (
        "secrets_never_hardcoded",
        "Never hardcode secrets or credentials",
        r"hardcode.{0,20}(secret|credential|token|iban)|never commit.{0,20}secret|"
        r"secrets? (management|from env)",
    ),
    (
        "config_from_entity",
        "Operator-specific config comes from entities, not code",
        r"config-source|hardcoded config|operator-specific config|"
        r"context entit|from env|portable|fork test",
    ),
    # SPLIT from one "durable memory in Neotoma" kind. Storing an artifact in
    # Neotoma, storing it PROACTIVELY rather than when asked, and storing the
    # full body rather than a path or a summary are three rules a session can
    # satisfy and violate independently: a session that stores a `task` with a
    # file path in it has stored to Neotoma and still lost the content.
    (
        "store_in_neotoma",
        "Durable memory belongs in Neotoma, not harness files",
        r"neotoma first|durable memory|store.{0,25}neotoma|"
        r"memory file|not.{0,15}markdown file",
    ),
    (
        "store_proactively",
        "Store artifacts as the work happens, not at session end",
        r"proactive(?:ly)? stor|store.{0,20}proactiv|"
        r"do not wait until end of session|same turn as the work|"
        r"store artifacts as",
    ),
    (
        "store_body_not_pointer",
        "Store the full body, not a path or a summary standing in for it",
        r"a path field is not storage|full markdown in|"
        r"body.{0,25}full (?:prose )?narrative|populate the.{0,15}body|"
        r"summary entity is not a source",
    ),
    (
        "persist_every_turn",
        "Persist every conversation turn to Neotoma",
        r"turn-by-turn|every turn|conversation_message|per-turn",
    ),
    (
        "plan_merge_before_correct",
        "Re-read and merge a plan field before correcting it",
        r"re-read.{0,25}merge|correct.{0,20}replaces|merge.{0,20}before writing|"
        r"stale in-memory",
    ),
    (
        "no_done_without_artifact",
        "Never mark work done citing an unverifiable artifact",
        r"never mark.{0,25}done|unverifiable completion|verify the artifact exists",
    ),
    (
        "agent_prompts_public",
        "Agent prompts are public and carry no operator data",
        r"prompts are (always )?public|pii-free|prompt.{0,30}no operator|"
        r"public.{0,20}prompt",
    ),
    (
        "renamed_agent_no_refs",
        "A renamed agent leaves no stale reference",
        r"renamed agent|retired name|stale reference|rename.{0,25}same change",
    ),
    (
        "daemon_checkout_fresh",
        "Daemons run dedicated checkouts that must be fresh",
        r"rc-src|deployment checkout|checkout drift|ff-only|daemon.{0,25}checkout",
    ),
    (
        "restart_daemons",
        "Restart affected daemons after a merge, then verify",
        r"restart.{0,20}daemon|launchctl|redeploy",
    ),
    (
        "test_must_fail_red",
        "A test that cannot fail on its subject is decoration",
        r"cannot fail|goes red|revert the fix|ratifies the bug|decoration",
    ),
    (
        "reuse_prior_art",
        "Extend the mechanism that exists; do not build a parallel one",
        r"prior art|already exists|parallel (mechanism|one)|reuse the existing",
    ),
    (
        "summarize_operator_input",
        "Echo the operator's input, cleaned up, each reply",
        r"summarize what the operator|transcrib|cleaned up|relay.{0,20}speech",
    ),
    # SPLIT from a single "status updates and open decisions" kind. Three
    # rules, not one: a turn can carry a status update and no decision list, a
    # decision list posed as prose rather than through the questions tool, or a
    # decision list that names a carried decision without restating it. Each is
    # separately violable, and the questions-tool rule -- a live standing_rule
    # entity -- was invisible while the three shared a bucket.
    (
        "status_update_unprompted",
        "Give status updates unprompted, per workstream",
        r"status update|give status|updates? unprompted|"
        r"what moved.{0,30}what is blocked",
    ),
    (
        "decisions_end_every_turn",
        "End every turn with the decisions that need the operator",
        r"end every turn|decisions that need|carry every open decision|"
        r"re-?raise.{0,25}by name|repeat(?:ing)? (?:them|pending) (?:each|every) turn",
    ),
    (
        "decisions_via_questions_tool",
        "Pose open decisions through the harness questions tool, not inline prose",
        r"askuserquestion|questions tool|harness question|"
        r"pose.{0,30}decision.{0,30}(tool|call)|labeled options",
    ),
    (
        "proceed_with_recommendation",
        "Act on your recommendation; ask only at a real fork",
        r"proceed with your recommendation|don't ask|auto-proceed|"
        r"genuine fork|take it and report",
    ),
    (
        "pr_body_from_file",
        "Pass PR and comment bodies by file, never inline",
        r"body-file|--body-file|body from a file",
    ),
    (
        "verify_gh_identity",
        "Verify the GitHub identity before any write",
        r"gh api user|verify.{0,20}(gh|github).{0,20}(account|identity)|"
        r"unset GH_TOKEN",
    ),
    (
        "recurring_never_done",
        "Recurring obligations roll their date; never complete",
        r"never.{0,20}mark.{0,25}complet|roll.{0,20}due_date|recurring obligation",
    ),
    # SPLIT from one "never invent facts, quotes, emotion, or reactions" kind.
    # The memory corpus states these separately and they are separately
    # violable: a draft can be scrupulous about quotes and still assert what
    # the operator felt; a report can invent no emotion and still fabricate a
    # finding. Each has its own correction history. The single kind held 45
    # statements at 44 distinct openings -- a topical bucket, not a rule.
    (
        "no_invented_facts",
        "Never invent facts about the operator's life, tools, or past",
        r"never invent or assume facts|no invented facts|invent.{0,30}\bfacts\b|"
        r"never invent.{0,25}(amounts?|fields?|credential|hostname|next step)",
    ),
    (
        "no_invented_quotes",
        "Never invent a quote; every quote traces to its source",
        r"never invent quotes?|no invented quotes?|verify every quote|"
        r"invent.{0,20}quotes?|verbatim quote.{0,40}never",
    ),
    (
        "no_fabricated_operator_state",
        "Never assert what the operator feels, thinks, or said without evidence",
        r"fabricate operator emotion|operator (?:emotion|internal state)|"
        r"never assert what the operator|unverified.{0,20}speech|"
        r"fabricated.{0,20}(emotion|admission)|never said he",
    ),
    (
        "no_invented_findings",
        "Never fabricate a finding or a conclusion to appear useful",
        r"no invented findings|fabricate a finding|invent.{0,20}findings?|"
        r"speculation is labeled|confident fabrication",
    ),
    (
        "no_invented_praise",
        "Never invent praise or a judgement of someone else's work",
        r"invented praise|unverifiable superlative|claimed judgments|"
        r"praise of their work|masterclass in",
    ),
    (
        "no_predicted_third_party_reaction",
        "Never predict or assert a third party's reaction",
        r"third.?part(?:y|ies)'? reaction|predict a third|"
        r"never predict.{0,25}reaction|put words in a participant",
    ),
    (
        "pii_minimization",
        "Minimize personal data at capture; purpose-bind it",
        r"rgpd|gdpr|minimi[sz]e at capture|legitimate interest|art\. ?9|"
        r"personal data",
    ),
    (
        "no_untested_remediation",
        "Never assert a remediation you have not tested",
        r"untested remediation|never assert.{0,25}not tested|reproduce before",
    ),
    ("squash_merge", "Merge by squash", r"squash"),
    (
        "conventional_commits",
        "Commit and PR titles follow the live title convention",
        r"conventional commit|commit message format|pr title",
    ),
    (
        "test_colocation",
        "Tests follow this repo's naming and placement convention",
        r"test_\*\.py|\.test\.ts|test file.{0,25}(naming|placement|colocat)",
    ),
)

_COMPILED_KINDS = [
    (k, lbl, re.compile(rx, re.I | re.S)) for k, lbl, rx in KIND_SIGNATURES
]

KIND_LABELS = {k: lbl for k, lbl, _ in KIND_SIGNATURES}


def classify(text: str) -> list[str]:
    """Return every kind a statement matches. A statement may state two rules."""
    hits = [k for k, _lbl, rx in _COMPILED_KINDS if rx.search(text)]
    return hits


# ---------------------------------------------------------------------------
# Target home, per the conformance authority table
# ---------------------------------------------------------------------------
#
# `docs/foundation/conformance.md`, "Direction of truth per class of record":
#   Agent behavioural rule      -> agent_policy entities
#   Operator preferences        -> task_policy entities
#   Session standing instruction-> CLAUDE.md
#   Design invariants           -> docs/foundation/, PR-reviewed
#
# A kind whose home cannot be derived from the table is reported UNCLASSIFIED
# rather than guessed -- the authority table is the authority, and inventing a
# home here would be exactly the kind of quiet re-decision the migration exists
# to prevent.

TARGET_HOME: dict[str, str] = {
    "git_never_stash": "agent_policy",
    "neotoma_prod_only": "agent_policy",
    "gws_over_gmail_mcp": "agent_policy",
    "gmail_send_gate": "agent_policy",
    "public_repo_pii": "agent_policy",
    "verify_write_landed": "agent_policy",
    "verify_before_asserting": "agent_policy",
    "fail_closed": "docs/foundation/",
    "dispatch_not_inline": "agent_policy",
    "no_merge_over_objection": "docs/foundation/",
    "no_verify_bypass": "agent_policy",
    "consent_gate_external": "docs/foundation/",
    "secrets_never_hardcoded": "agent_policy",
    "config_from_entity": "agent_policy",
    "store_in_neotoma": "agent_policy",
    "persist_every_turn": "agent_policy",
    "plan_merge_before_correct": "agent_policy",
    "no_done_without_artifact": "agent_policy",
    "agent_prompts_public": "agent_policy",
    "renamed_agent_no_refs": "agent_policy",
    "daemon_checkout_fresh": "agent_policy",
    "restart_daemons": "CLAUDE.md",
    "test_must_fail_red": "agent_policy",
    "reuse_prior_art": "agent_policy",
    "summarize_operator_input": "task_policy",
    "status_update_unprompted": "task_policy",
    "proceed_with_recommendation": "task_policy",
    "pr_body_from_file": "agent_policy",
    "verify_gh_identity": "agent_policy",
    "recurring_never_done": "task_policy",
    "pii_minimization": "docs/foundation/",
    "no_untested_remediation": "agent_policy",
    "squash_merge": "agent_policy",
    "conventional_commits": "agent_policy",
    "test_colocation": "agent_policy",
    "git_stash_recovery": "agent_policy",
    "worktree_one_per_agent": "agent_policy",
    "shared_clone_no_mutation": "agent_policy",
    "decisions_end_every_turn": "task_policy",
    "decisions_via_questions_tool": "task_policy",
    "no_invented_facts": "task_policy",
    "no_invented_quotes": "task_policy",
    "no_fabricated_operator_state": "task_policy",
    "no_invented_findings": "task_policy",
    "no_invented_praise": "task_policy",
    "no_predicted_third_party_reaction": "task_policy",
    "store_proactively": "agent_policy",
    "store_body_not_pointer": "agent_policy",
    "operator_only_actions": "docs/foundation/",
    "blast_radius_classification": "docs/foundation/",
    "dispatch_no_drift": "agent_policy",
    "durable_work_not_task_chip": "agent_policy",
    "validate_the_instrument": "agent_policy",
}


# ---------------------------------------------------------------------------
# Operator-ruled candidates mined from transcripts, NOT measured inventory
# ---------------------------------------------------------------------------
#
# This section records the operator's disposition of the transcript-derived
# candidates. The rest of the file is measured: re-run it and the numbers move
# with the system. These do not enter the counts until the accepted rule lands
# in its authoritative home. Generating the ruling from a corpus scan would
# dress a judgement as a measurement, so the dispositions stay explicit.
#
# Source: genuine operator messages in ~/.claude/projects/*/*.jsonl over the
# 21 days to 2026-09-19 -- 3,068 transcripts, 10,171 user-role messages, of
# which 4,806 survive filtering for tool_result payloads, <system-reminder>,
# <command-*> blocks, task notifications and agent dispatch prompts, and 338
# are correction-shaped. Candidates were then checked against every store
# before being proposed: the eight file stores and the three entity types.
#
# PII: the transcripts carry the operator's personal life in quantity. Each
# rule below is stated generically and no incident's specifics are reproduced.
# Candidates that could not be generalized without naming a person, vendor,
# client, health fact or amount were DROPPED rather than sanitized -- one
# genuine rule about how personal-fitness records are structured is omitted
# entirely for this reason, and the omission is recorded here rather than
# hidden.


@dataclass(frozen=True)
class RuleRuling:
    rule: str
    status: str
    home: str
    note: str


PROPOSAL_RULINGS = {
    "P1": RuleRuling(
        "A retraction posted as a COMMENT does not clear an APPROVED review; "
        "the approval stands until it is formally dismissed",
        "ACCEPTED",
        "`docs/foundation/github.md`",
        "Code-host review semantics belong in the code-host mapping.",
    ),
    "P2": RuleRuling(
        "A prose-matching guard fires on text that names its own rule, so a "
        "document describing a rule trips the gate that enforces it",
        "ACCEPTED",
        "`agent_policy`",
        "A generic rule for authors and reviewers of guards.",
    ),
    "P3": RuleRuling(
        "Durable work goes to a dispatched agent, never a harness task chip — "
        "a chip is not an entity, so it is unclaimable and invisible to the swarm",
        "DUPLICATE",
        "`agent_policy` (`R-ae9bca`)",
        "Already captured by the dispatch rule; create no second rule.",
    ),
    "P4": RuleRuling(
        "Monitoring does not end at merge: carry a change through release and "
        "deployment until it is confirmed live on every instance that needs it",
        "ACCEPTED",
        "`agent_policy`",
        "PR shepherding behaviour; workflow declarations still own their step lists.",
    ),
    "P5": RuleRuling(
        "Request operator review only when technical gates are clear and "
        "operator approval is the sole remaining gate",
        "ACCEPTED",
        "`agent_policy`",
        "Narrowed by the operator; an earlier request would misstate readiness.",
    ),
    "P6": RuleRuling(
        "Stage a reply at the END of its thread, having first checked the "
        "external system for the thread's latest message",
        "ACCEPTED",
        "`docs/foundation/gmail.md`",
        "This is the mail adapter's per-thread operation, not a general preference.",
    ),
    "P7": RuleRuling(
        "On resuming an interrupted watcher, import everything that arrived "
        "during the gap — not only what arrives afterward",
        "ACCEPTED",
        "`docs/foundation/adapters.md`",
        "A watcher resumption invariant shared across import adapters.",
    ),
    "P8": RuleRuling(
        "Check durable storage for an already-imported source before importing it again",
        "ACCEPTED",
        "`docs/foundation/adapters.md`",
        "A source-dedup invariant shared across import adapters.",
    ),
    "P9": RuleRuling(
        "Produce an internal recap for the operator covering the work done, "
        "distinct from any outward-facing recap",
        "ACCEPTED",
        "`task_policy`",
        "The recap presentation is an operator preference, not public prompt text.",
    ),
    "P10": RuleRuling(
        "Avoid a named stylistic tell in generated prose because it reads as machine-written",
        "QUARANTINED",
        "none",
        "No rule is created until the exact stylistic tell and scope are supplied.",
    ),
}

GENERALIZATION_RULINGS = {
    "G1": RuleRuling(
        "Never interpolate untrusted or code-bearing text into a shell command; "
        "write it to a file and pass the path",
        "ACCEPTED",
        "`agent_policy`",
        "Keep the concrete `--body-file` rule beside the general shell-injection rule.",
    ),
    "G2": RuleRuling(
        "Any deployment step is unverified until read back from the thing that now runs",
        "DUPLICATE",
        "`agent_policy` (`R-680852`)",
        "Fold into the existing read-back rule; preserve the concrete daemon sequence.",
    ),
    "G3": RuleRuling(
        "Any outward, irreversible action needs per-action approval, and approval "
        "never carries forward",
        "DUPLICATE",
        "foundation consent rule (`R-fba8d`)",
        "Already captured; preserve the concrete Gmail gate and its tests.",
    ),
}


def render_rulings_section() -> str:
    """Render transcript-derived candidates and their operator dispositions.

    This is an appendix to measured inventory, not a second rule store. An
    accepted row enters the measured counts only after the authoritative home
    receives it through that home's own governance path.
    """
    lines = [
        "## Operator rulings (2026-09-21) on transcript-derived candidates",
        "",
        "**Nothing in this section is counted anywhere above.** The inventory measures "
        "rules in their authoritative homes. Recording a ruling here does not make an "
        "accepted rule binding; it records the disposition and destination so the "
        "governed migration can put it in the one home its audience reads. Duplicate "
        "rows create no new rule, and quarantined rows have no destination.",
        "",
        "**Method and its limits.** 3,068 transcripts from the 21 days to 2026-09-19; "
        "10,171 user-role messages, 4,806 after filtering tool results, system "
        "reminders, command blocks, task notifications and agent dispatch prompts; "
        "338 correction-shaped. Recall is not claimed.",
        "",
        "**PII.** Every candidate is generic. Candidates that could not be generalized "
        "without personal data were dropped rather than sanitized.",
        "",
        "### A. Candidate rules and dispositions",
        "",
        "| # | Rule as ruled | Disposition | Authoritative home | Reason |",
        "|---|---|---|---|---|",
    ]
    for key, ruling in PROPOSAL_RULINGS.items():
        lines.append(
            f"| {key} | {ruling.rule} | **{ruling.status}** | "
            f"{ruling.home} | {ruling.note} |"
        )
    lines.extend(
        [
            "",
            "### B. Generalization dispositions",
            "",
            "| # | General rule | Disposition | Authoritative home | Reason |",
            "|---|---|---|---|---|",
        ]
    )
    for key, ruling in GENERALIZATION_RULINGS.items():
        lines.append(
            f"| {key} | {ruling.rule} | **{ruling.status}** | "
            f"{ruling.home} | {ruling.note} |"
        )
    lines.extend(
        [
            "",
            "### C. What this section does not claim",
            "",
            "- **Not measured inventory.** Accepted candidates enter the counts only "
            "after their authoritative home contains them.",
            "- **Not enforcement.** Each destination's existing gate, review and "
            "read-back obligations still apply.",
            "- **Not exhaustive.** The transcript filter misses corrections without "
            "imperative markers; recall is unknown.",
        ]
    )
    return "\n".join(lines)


def strip_volatile_measurement_date(text: str) -> str:
    """Remove only the run date before comparing generated inventory output."""
    return re.sub(
        r"\*\*Measured:\*\* \d{4}-\d{2}-\d{2}",
        "**Measured:**",
        text,
    )


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _portable(path: str) -> str:
    """Strip this checkout's location from an emitted path.

    A worktree name is a session artifact and a weak identifier; committing one
    makes the inventory unreproducible from another checkout and leaks the
    session's own naming. The repo root is stripped BEFORE `~`, because the
    worktree lives under the home directory and the other order leaves the
    worktree name in place.
    """
    p = str(path)
    root = str(REPO_ROOT)
    if p.startswith(root + "/"):
        return p[len(root) + 1 :]
    p = p.replace(str(Path.home()), "~")
    # Claude encodes the absolute checkout path into the project directory
    # name. Redact that segment too: replacing only the home prefix leaves the
    # operator's username and checkout name in a public generated file.
    return re.sub(
        r"^~/\.claude/projects/[^/]+/",
        "~/.claude/projects/<project>/",
        p,
    )


# Public rendering is a projection onto this closed vocabulary. It is not a
# denylist of identifiers seen on one machine: an unknown store, path, label,
# locator, date, or reachability value is withheld by default. This is the
# enforcement boundary that a proper-noun screen cannot provide.
PUBLIC_STORE_NAMES = frozenset(
    {
        "ateles/CLAUDE.md",
        "neotoma/AGENTS.md",
        "ateles/CLAUDE.md checkout copies",
        "neotoma/AGENTS.md checkout copies",
        "Claude Code user rules",
        "Claude Code project memory",
        "Codex",
        "Cursor",
        "OpenClaw",
        "Skills (ateles repo)",
        "Skills (user root)",
        "foundation reference repo",
        "Canonical repository instruction roots",
        "Claude Code hooks (ateles)",
        *(f"{entity_type} entities" for entity_type in RULE_ENTITY_TYPES),
    }
)

PUBLIC_STORE_LOCATIONS = {
    "ateles/CLAUDE.md": "CLAUDE.md",
    "neotoma/AGENTS.md": "~/repos/neotoma/AGENTS.md",
    "ateles/CLAUDE.md checkout copies": "~/repos/<checkout>",
    "neotoma/AGENTS.md checkout copies": "~/repos/<checkout>",
    "Claude Code user rules": "~/.claude/CLAUDE.md",
    "Claude Code project memory": "~/.claude/projects/<project>/memory",
    "Codex": "~/.codex/AGENTS.md",
    "Cursor": "~/.cursor/rules",
    "OpenClaw": "~/.openclaw/agents/<agent>",
    "Skills (ateles repo)": ".claude/skills/<skill>",
    "Skills (user root)": "~/.claude/skills/<skill>",
    "foundation reference repo": "~/repos/<reference>",
    "Canonical repository instruction roots": "~/repos/<canonical-roots>",
    "Claude Code hooks (ateles)": ".claude/hooks",
    **{f"{entity_type} entities": "<entity>" for entity_type in RULE_ENTITY_TYPES},
}

PUBLIC_STATEMENT_LOCATIONS = {
    "ateles/CLAUDE.md": "CLAUDE.md",
    "neotoma/AGENTS.md": "~/repos/neotoma/AGENTS.md",
    "ateles/CLAUDE.md checkout copies": "~/repos/<checkout>/CLAUDE.md",
    "neotoma/AGENTS.md checkout copies": "~/repos/<checkout>/AGENTS.md",
    "Claude Code user rules": "~/.claude/CLAUDE.md",
    "Claude Code project memory": "~/.claude/projects/<project>/memory/<file>",
    "Codex": "~/.codex/AGENTS.md",
    "Cursor": "~/.cursor/rules/<file>",
    "OpenClaw": "~/.openclaw/agents/<agent>/<instruction-file>",
    "Skills (ateles repo)": ".claude/skills/<skill>/SKILL.md",
    "Skills (user root)": "~/.claude/skills/<skill>/SKILL.md",
    "foundation reference repo": "~/repos/<reference>/<file>",
    "Canonical repository instruction roots": (
        "~/repos/<canonical-root>/<instruction-file>"
    ),
    "Claude Code hooks (ateles)": ".claude/hooks/<hook>.py",
    **{f"{entity_type} entities": "<entity>" for entity_type in RULE_ENTITY_TYPES},
}

PUBLIC_REACHABILITY = frozenset(
    {
        "yes",
        "no",
        "unknown",
        "n/a",
        "divergent",
        "stale",
        "per-project",
        "cited, unread",
        "sidecar only",
        "on retrieval",
    }
)

# Full equality is meaningful only on the canonical measurement host, where
# every store kind this stage-0 inventory covers is available. GitHub-hosted
# runners do not have the user-level harness stores; a merge gate running there
# must say so and fail, not compare a partial measurement to the committed file.
REQUIRED_MEASUREMENT_STORES = PUBLIC_STORE_NAMES


def public_store_name(name: str) -> str:
    """Return a public store-kind label, failing closed for unknown input."""
    if name in PUBLIC_STORE_NAMES:
        return name
    # A repository owner is runtime/operator metadata. The useful information
    # is that this is the reference repository, not whose namespace it uses.
    if name.endswith("/foundation repo"):
        return "foundation reference repo"
    return "private rule store"


def public_store_location(store_name: str, _raw_location: str) -> str:
    """Return a generic store location derived only from its public kind."""
    public_name = public_store_name(store_name)
    return PUBLIC_STORE_LOCATIONS.get(public_name, "<private-location>")


def public_statement_location(store_name: str, _raw_location: str) -> str:
    """Return a generic statement location derived only from store kind."""
    public_name = public_store_name(store_name)
    return PUBLIC_STATEMENT_LOCATIONS.get(public_name, "<private-location>")


def public_locator(locator: str) -> str:
    """Keep only line numbers and the fixed entity field vocabulary."""
    if re.fullmatch(r"L\d+", locator) or locator == "docstring":
        return locator
    if locator in TEXT_FIELDS:
        return locator
    if (
        locator.startswith("raw_fragments.")
        and locator.removeprefix("raw_fragments.") in TEXT_FIELDS
    ):
        return locator
    return "<field>"


def public_last_modified(value: str) -> str:
    """Only an ISO calendar date is useful public inventory metadata."""
    return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or "") else "—"


def public_reachability(value: str) -> str:
    """Project reachability onto the generator-owned public vocabulary."""
    return value if value in PUBLIC_REACHABILITY else "unknown"


def public_kind(kind: str) -> str:
    """Kinds are safe only when declared by this generator."""
    return kind if kind in KIND_LABELS else "unclassified"


def public_cluster_label(kind: str, _raw_label: str) -> str:
    """Use the generator-owned label, never a caller-provided label."""
    return KIND_LABELS.get(kind, "unclassified rule kind")


def public_cluster_stores(cluster: Cluster) -> list[str]:
    """Return de-duplicated public store-kind labels for a cluster."""
    return sorted(
        {public_store_name(statement.store) for statement in cluster.statements}
    )


def public_rule_id(cluster: Cluster) -> str:
    """Expose a stable identifier only for a declared rule kind."""
    return cluster.rule_id if cluster.kind in KIND_LABELS else "R-unclassified"


def public_payload(
    clusters: list[Cluster],
    stores: list[Store],
    unclassified: list[Statement],
    statements: list[Statement],
) -> dict:
    """Return the same fail-closed public projection used by Markdown."""
    return {
        "clusters": [
            {
                "id": public_rule_id(cluster),
                "kind": public_kind(cluster.kind),
                "label": public_cluster_label(cluster.kind, cluster.label),
                "diverges": cluster.diverges,
                "distinct_openings": cluster.distinct_openings,
                "needs_split": cluster.needs_split,
                "target_home": TARGET_HOME.get(cluster.kind),
                "statements": [
                    {
                        "store": public_store_name(statement.store),
                        "location": public_statement_location(
                            statement.store, statement.location
                        ),
                        "locator": public_locator(statement.locator),
                        "safe_text": statement.safe_text,
                    }
                    for statement in cluster.statements
                ],
            }
            for cluster in clusters
        ],
        "stores": [
            {
                "name": public_store_name(store.name),
                "location": public_store_location(store.name, store.location),
                "populated": store.populated if store.read_ok else None,
                "statements": store.statements if store.read_ok else None,
                "last_modified": (
                    public_last_modified(store.last_modified) if store.read_ok else None
                ),
                "reachable": (
                    public_reachability(store.reachable) if store.read_ok else "unread"
                ),
                "read_ok": store.read_ok,
            }
            for store in stores
        ],
        "unclassified_count": len(unclassified),
        "totals": {"rules": len(clusters), "statements": len(statements)},
    }


def private_diagnostics_payload(clusters: list[Cluster]) -> dict:
    """Return private source locators without copying any statement value.

    The public artifact deliberately collapses file names and entity ids. This
    payload is the non-committable bridge for a human resolving NEEDS-SPLIT or
    divergence: it says which private source to open, but the source itself is
    still the only place the rule value can be read.
    """
    return {
        "warning": (
            "PRIVATE LOCATORS: contains local paths and entity ids; do not commit. "
            "Statement values are intentionally absent."
        ),
        "clusters": [
            {
                "id": public_rule_id(cluster),
                "kind": public_kind(cluster.kind),
                "needs_split": cluster.needs_split,
                "diverges": cluster.diverges,
                "sources": [
                    {
                        "store": statement.store,
                        "location": statement.location,
                        "locator": statement.locator,
                    }
                    for statement in cluster.statements
                ],
            }
            for cluster in clusters
            if cluster.needs_split or cluster.diverges
        ],
    }


def write_private_diagnostics(path: str | Path, clusters: list[Cluster]) -> None:
    """Write mode-0600 private locators, refusing every in-repository path."""
    target = Path(path).expanduser().resolve()
    if target == REPO_ROOT or REPO_ROOT in target.parents:
        raise ValueError(
            "private diagnostics path must be outside the repository; "
            "use a temporary or other non-versioned directory"
        )
    payload = json.dumps(private_diagnostics_payload(clusters), indent=2) + "\n"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(payload)
    finally:
        if fd >= 0:
            os.close(fd)


def measurement_readiness(stores: list[Store]) -> tuple[list[str], list[str]]:
    """Return safe public store-kind names missing or unread for full equality."""
    present = {public_store_name(store.name) for store in stores}
    missing = sorted(REQUIRED_MEASUREMENT_STORES - present)
    unread = sorted(
        {public_store_name(store.name) for store in stores if not store.read_ok}
    )
    return missing, unread


def measurement_totals(clusters: list[Cluster]) -> tuple[int, int]:
    """Return the rule and clustered-statement totals used to reject silence."""
    return len(clusters), sum(len(cluster.statements) for cluster in clusters)


def _mtime(p: Path) -> str:
    try:
        return date.fromtimestamp(p.stat().st_mtime).isoformat()
    except OSError:
        return ""


def _git_last_commit(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%cs"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def neotoma_query(entity_type: str, limit: int = 200) -> dict:
    """POST /entities/query -- the canonical list route.

    /retrieve_entities is the MCP TOOL name, not a REST path, and 404s on the
    hosted instance (`check_neotoma_rest_paths.py` exists for this mistake).
    """
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        raise RuntimeError("NEOTOMA_BEARER_TOKEN unset")
    req = urllib.request.Request(
        f"{NEOTOMA_BASE_URL}/entities/query",
        data=json.dumps(
            {
                "entity_type": entity_type,
                "limit": limit,
                "include_snapshots": True,
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            # Cloudflare 1010-blocks urllib's default UA on the hosted instance.
            "User-Agent": "ateles-rule-inventory/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def read_entities(
    cache: Path | None = None,
    repository_input_root: Path = REPO_ROOT,
) -> tuple[list[Statement], list[Store]]:
    """Read the three rule entity types, field-name agnostically."""
    statements: list[Statement] = []
    stores: list[Store] = []

    for etype in RULE_ENTITY_TYPES:
        store = Store(name=f"{etype} entities", location="Neotoma PROD")
        payload = None
        cache_file = cache / f"{etype}.json" if cache else None

        if cache_file and cache_file.exists():
            payload = json.loads(cache_file.read_text())
        else:
            try:
                payload = neotoma_query(etype)
                if cache_file:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps(payload))
            except Exception as exc:  # noqa: BLE001
                store.read_ok = False
                store.read_error = f"{type(exc).__name__}: {exc}"
                stores.append(store)
                continue

        ents = payload.get("entities", [])
        store.populated = len(ents)
        notext = 0
        latest = ""

        for e in ents:
            eid = e.get("entity_id", "?")
            outer = e.get("snapshot") or {}
            snap = outer.get("snapshot", outer) if isinstance(outer, dict) else {}
            # The undeclared-field trap: /store accepts undeclared fields and
            # routes them to raw_fragments, where rule text also lands.
            frags = e.get("raw_fragments") or {}

            # PII is screened PER ENTITY, not per field. An entity whose rule is
            # about the operator's own affairs carries the payee's name in some
            # fields and not others -- screening each field independently
            # emitted the clean ones and leaked the subject. If ANY field of an
            # entity trips the screen, EVERY field of it is withheld, including
            # its `canonical_name`, which is never emitted in any case because
            # it carries payee names directly.
            allfields = [
                v
                for v in list(snap.values()) + list(frags.values())
                if isinstance(v, str)
            ]
            allfields.append(str(e.get("canonical_name", "")))
            entity_clean, _ = screen_for_pii(" \n".join(allfields))

            seen = False
            for fname in TEXT_FIELDS:
                for src, srclabel in ((snap, fname), (frags, f"raw_fragments.{fname}")):
                    val = src.get(fname) if isinstance(src, dict) else None
                    if isinstance(val, str) and val.strip():
                        statements.append(
                            Statement(
                                store=store.name,
                                location=eid,
                                locator=srclabel,
                                text=val if entity_clean else WITHHELD_MARKER,
                                last_modified=(e.get("last_observation_at") or "")[:10],
                            )
                        )
                        seen = True
            if not seen:
                notext += 1
            obs = (e.get("last_observation_at") or "")[:10]
            latest = max(latest, obs)

        store.statements = sum(1 for s in statements if s.store == store.name)
        store.last_modified = latest
        if notext:
            store.note = (
                f"{notext} row(s) carry no rule text under any known field name"
            )
        stores.append(store)

    # Reachability -- ateles#1118. Populated is not delivered.
    loader = repository_input_root / "lib" / "daemon_runtime" / "agent_loader.py"
    for st in stores:
        if st.name.startswith("agent_policy"):
            filt = False
            if loader.exists():
                filt = 'snap.get("agent_sub")' in loader.read_text()
            st.reachable = "no" if filt else "unknown"
            st.reach_note = (
                "agent_loader filters on agent_sub, empty in every row "
                "(ateles#1118) — every agent loads zero policies"
                if filt
                else "loader filter not found; re-verify"
            )
        elif st.name.startswith("standing_rule"):
            st.reachable = "sidecar only"
            st.reach_note = (
                "delivered to serverInfo._neotoma.standing_rules, a field "
                "agents do not read (ateles#1114)"
            )
        elif st.name.startswith("task_policy"):
            st.reachable = "on retrieval"
            st.reach_note = "read only when a skill or session retrieves it explicitly"

    return statements, stores


# --- markdown / prose stores -------------------------------------------------

BULLET_RULE = re.compile(r"^\s*[-*]\s+\*\*(?P<lead>[^*]+)\*\*\s*(?P<rest>.*)$")

# A rule statement is NORMATIVE and DIRECTED: it tells a reader to do or not do
# something. Deliberately narrower than "contains the word must" -- an early
# revision matched any line carrying an imperative and returned 26,516
# statements, a 736x duplication factor that was an artifact of the instrument
# and not a fact about the system. Validate the instrument before believing the
# measurement: a surprising number is usually the tool.
IMPERATIVE = re.compile(
    r"(?:^|[\s(—,;:])(?:"
    r"never\s+\w|always\s+\w|must\s+(?:not\s+)?\w|do\s+not\s+\w|don't\s+\w|"
    r"avoid\s+\w|refuse\s+\w|require[sd]?\s+\w|forbidden|prohibited|"
    r"prefer\s+\w|should\s+(?:not\s+)?\w|only\s+ever\s|use\s+\w+\s+(?:not|rather)|"
    r"NEVER|ALWAYS|MUST"
    r")",
    re.I,
)

# Lines that carry an imperative but are not a rule being STATED here:
# narration about rules, citations, code, and changelog prose.
NOT_A_RULE = re.compile(
    r"^\s*(?:\||>|```|#{1,6}\s|\d+\.\s*$|<!--)"  # tables, quotes, code
    r"|^\s*(?:import|from|def |class |return |assert |if |for |print\()"
    r"|(?:https?://|\.py:\d|\.md:\d)"  # links and citations
    r"|^\s*[-*]\s*\[[ x]\]"  # checklists
    r"|\b(?:was|were|had|used to|previously|motivated by|"
    r"this happened|for example|e\.g\.|i\.e\.)\b",
    re.I,
)


def extract_md_rules(
    path: Path, store_name: str, max_chars: int = 600
) -> list[Statement]:
    """Per-rule extraction from a markdown instruction file.

    Property 2. Two shapes are recognised: a bolded-lead bullet (the shape
    `verify_claude_md_merge.py` already keys on, so this inventory and the
    parity checker agree on what a rule is), and any other line stating a
    normative directive. A file is many rules, and a file count says nothing.
    """
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    mtime = _mtime(path)
    out: list[Statement] = []
    in_fence = False
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = BULLET_RULE.match(line)
        if m:
            text = f"{m.group('lead')} {m.group('rest')}"[:max_chars]
            out.append(
                Statement(store_name, str(path), f"L{i}", text, last_modified=mtime)
            )
            continue
        stripped = line.strip()
        if len(stripped) < 30 or len(stripped) > 700:
            continue
        if NOT_A_RULE.search(stripped):
            continue
        if IMPERATIVE.search(stripped):
            out.append(
                Statement(
                    store_name,
                    str(path),
                    f"L{i}",
                    stripped[:max_chars],
                    last_modified=mtime,
                )
            )
    return out


def read_canonical_repository_instruction_roots(
    configured: str | None = None,
) -> tuple[list[Statement], Store]:
    """Measure runner-configured canonical repository instruction roots.

    The configured values are private runner state. Public outputs receive one
    aggregate store kind and generic locations only. A missing, malformed,
    absent, symlinked, or unreadable root makes the whole aggregate unread:
    partial success would silently drop a required repo and turn a lower bound
    into a completeness claim.
    """
    store = Store(
        name="Canonical repository instruction roots",
        location="",
        reachable="per-project",
        reach_note="each repository's root instruction file binds its own sessions",
    )
    raw = (
        os.environ.get(CANONICAL_REPOSITORY_ROOTS_ENV, "")
        if configured is None
        else configured
    )
    root_values = [item.strip() for item in raw.split(os.pathsep) if item.strip()]
    if not root_values:
        store.read_ok = False
        store.read_error = f"{CANONICAL_REPOSITORY_ROOTS_ENV} is required"
        return [], store

    roots: list[Path] = []
    seen: set[str] = set()
    for value in root_values:
        root = Path(value).expanduser()
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)

    instruction_files: list[Path] = []
    seen_instruction_files: set[str] = set()
    try:
        for root in roots:
            if not root.is_absolute():
                raise ValueError(f"canonical repository root is not absolute: {root}")
            if root.is_symlink() or not root.is_dir():
                raise FileNotFoundError(
                    f"canonical repository root is unavailable: {root}"
                )
            git_dir = root / ".git"
            if git_dir.is_symlink() or not git_dir.is_dir():
                raise ValueError(
                    f"canonical repository root is not a primary clone: {root}"
                )
            git_env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            verified = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    "--path-format=absolute",
                    "--show-toplevel",
                    "--absolute-git-dir",
                    "--git-common-dir",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                env=git_env,
            )
            resolved_root = root.resolve(strict=True)
            resolved_git_dir = git_dir.resolve(strict=True)
            identity = [line.strip() for line in verified.stdout.splitlines()]
            if (
                verified.returncode != 0
                or len(identity) != 3
                or Path(identity[0]).resolve(strict=True) != resolved_root
                or Path(identity[1]).resolve(strict=True) != resolved_git_dir
                or Path(identity[2]).resolve(strict=True) != resolved_git_dir
            ):
                raise ValueError(
                    f"canonical repository root is not a primary clone: {root}"
                )
            for filename in ("CLAUDE.md", "AGENTS.md", ".cursorrules"):
                candidate = root / filename
                if candidate.exists() or candidate.is_symlink():
                    resolved = candidate.resolve(strict=True)
                    if not resolved.is_relative_to(root.resolve()):
                        raise ValueError(
                            "repository instruction symlink leaves its canonical root: "
                            f"{candidate}"
                        )
                    if not resolved.is_file():
                        raise ValueError(
                            f"repository instruction path is not a file: {candidate}"
                        )
                    resolved_key = str(resolved)
                    if resolved_key in seen_instruction_files:
                        continue
                    # Prove readability before accepting any partial aggregate.
                    resolved.read_bytes()
                    seen_instruction_files.add(resolved_key)
                    instruction_files.append(resolved)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        store.read_ok = False
        store.read_error = f"{type(exc).__name__}: {exc}"
        return [], store

    store.location = os.pathsep.join(str(root) for root in roots)
    store.populated = len(roots)
    store.note = (
        f"{len(instruction_files)} root instruction file(s) across "
        f"{len(roots)} configured canonical repository root(s)"
    )
    store.last_modified = max((_mtime(path) for path in instruction_files), default="")
    statements: list[Statement] = []
    for path in instruction_files:
        statements.extend(extract_md_rules(path, store.name))
    store.statements = len(statements)
    return statements, store


def read_file_stores(
    home: Path,
    repository_input_root: Path = REPO_ROOT,
) -> tuple[list[Statement], list[Store]]:
    statements: list[Statement] = []
    stores: list[Store] = []

    def add_store(name, loc, files, note="", reachable="yes", reach_note=""):
        st = Store(
            name=name,
            location=loc,
            populated=len(files),
            note=note,
            reachable=reachable,
            reach_note=reach_note,
        )
        lm = ""
        for f in files:
            lm = max(lm, _mtime(f))
        st.last_modified = lm
        stores.append(st)
        return st

    # -- repo instruction files ------------------------------------------
    ateles_md = repository_input_root / "CLAUDE.md"
    if ateles_md.exists():
        st = add_store(
            "ateles/CLAUDE.md",
            str(ateles_md),
            [ateles_md],
            reach_note="re-injected from disk at every compaction",
        )
        s = extract_md_rules(ateles_md, st.name)
        statements += s
        st.statements = len(s)

    neo_md = home / "repos" / "neotoma" / "AGENTS.md"
    if neo_md.exists():
        st = add_store(
            "neotoma/AGENTS.md", str(neo_md), [neo_md], note="sibling repo, read-only"
        )
        s = extract_md_rules(neo_md, st.name)
        statements += s
        st.statements = len(s)

    # -- other canonical repository instruction roots --------------------
    # The operator's bounded cross-repo sweep is configured on the canonical
    # measurement runner. It intentionally excludes worktrees: those are
    # version-drift copies measured below, not independent rule authorship.
    repository_statements, repository_store = (
        read_canonical_repository_instruction_roots()
    )
    statements += repository_statements
    stores.append(repository_store)

    # -- the copy problem: one instruction file, many checkouts ----------
    # A rule that lives in only one checkout does not bind
    # (docs/foundation/principles.md#1). Measured rather than assumed.
    for label, fname in (
        ("ateles/CLAUDE.md", "CLAUDE.md"),
        ("neotoma/AGENTS.md", "AGENTS.md"),
    ):
        copies, digests, newest = [], set(), ""
        for p in (home / "repos").glob(f"*/{fname}"):
            copies.append(p)
            try:
                digests.add(hashlib.md5(p.read_bytes()).hexdigest())
            except OSError:
                pass
            newest = max(newest, _mtime(p))
        for dep in (home / "ateles-rc-src" / fname, home / "neotoma-rc-src" / fname):
            if dep.exists():
                copies.append(dep)
                digests.add(hashlib.md5(dep.read_bytes()).hexdigest())
        if copies:
            st = add_store(
                f"{label} checkout copies",
                str(home / "repos"),
                copies,
                note=(
                    f"{len(copies)} copies on disk in {len(digests)} distinct "
                    f"versions — each checkout binds its own"
                ),
                reachable="divergent",
                reach_note=(
                    "a session or daemon reads the copy in ITS checkout, "
                    "not origin/main"
                ),
            )
            st.distinct_versions = len(digests)
            st.statements = 0  # counted once, at the canonical file

    # -- user-level Claude Code -------------------------------------------
    user_md = home / ".claude" / "CLAUDE.md"
    if user_md.exists():
        st = add_store("Claude Code user rules", str(user_md), [user_md])
        s = extract_md_rules(user_md, st.name)
        statements += s
        st.statements = len(s)

    # -- project memory ----------------------------------------------------
    mem = sorted((home / ".claude" / "projects").glob("*/memory/*.md"))
    if mem:
        dirs = {p.parent for p in mem}
        st = add_store(
            "Claude Code project memory",
            str(home / ".claude/projects"),
            mem,
            note=f"{len(mem)} files across {len(dirs)} project dirs",
            reachable="per-project",
            reach_note="MEMORY.md index loads; linked files load on demand",
        )
        s: list[Statement] = []
        for f in mem:
            s += extract_md_rules(f, st.name)
        statements += s
        st.statements = len(s)

    # -- Codex --------------------------------------------------------------
    codex = home / ".codex" / "AGENTS.md"
    if codex.exists():
        st = add_store(
            "Codex", str(codex), [codex], note="largest single rule file on the machine"
        )
        s = extract_md_rules(codex, st.name)
        statements += s
        st.statements = len(s)

    # -- Cursor -------------------------------------------------------------
    cdir = home / ".cursor" / "rules"
    if cdir.is_dir():
        allf = sorted(p for p in cdir.iterdir() if p.is_file() or p.is_symlink())
        live = [p for p in allf if ".backup." not in p.name]
        backups = [p for p in allf if ".backup." in p.name]
        symlinks = [p for p in allf if p.is_symlink()]
        st = add_store(
            "Cursor",
            str(cdir),
            allf,
            note=(
                f"{len(allf)} entries: {len(live)} live, {len(backups)} dated "
                f".backup. copies, {len(symlinks)} symlinks into the neotoma repo"
            ),
            reachable="stale",
        )
        st.reach_note = "a preference maintained by copy-on-edit, backups 29 deep"
        s = []
        for p in live:
            if p.is_symlink():
                continue  # the target belongs to the sibling repo, counted there
            s += extract_md_rules(p, st.name)
        statements += s
        st.statements = len(s)

    # -- OpenClaw -----------------------------------------------------------
    # Prior inventories recorded this store as "1". That 1 is a DIRECTORY
    # (`agents/main/`), and beneath it is session state plus a vendored Codex
    # home carrying that harness's own shipped skills. No operator-authored
    # rule file exists here. Counted honestly: an instruction file at the agent
    # root is a rule store; a vendored dependency tree is not, and sweeping it
    # returned 21,411 statements from files the operator never wrote.
    oc = home / ".openclaw" / "agents"
    ocf: list[Path] = []
    if oc.is_dir():
        for agent_dir in sorted(p for p in oc.iterdir() if p.is_dir()):
            for cand in (
                "AGENTS.md",
                "CLAUDE.md",
                "SOUL.md",
                "instructions.md",
                "agent/AGENTS.md",
                "agent/instructions.md",
            ):
                p = agent_dir / cand
                if p.is_file():
                    ocf.append(p)
    vendored = sum(1 for _ in oc.rglob("codex-home/**/SKILL.md")) if oc.is_dir() else 0
    st = add_store(
        "OpenClaw",
        str(oc),
        ocf,
        note=(
            f"no operator-authored rule file; the agent root holds session "
            f"state and a vendored Codex home ({vendored} shipped SKILL.md "
            f"files that are a dependency, not operator rules). Earlier "
            f"inventories counted the directory itself as 1 rule"
        ),
        reachable="n/a",
    )
    s = []
    for p in ocf:
        s += extract_md_rules(p, st.name)
    statements += s
    st.statements = len(s)

    # -- skills, three roots -------------------------------------------------
    for label, root in (
        ("Skills (ateles repo)", repository_input_root / ".claude" / "skills"),
        ("Skills (user root)", home / ".claude" / "skills"),
    ):
        files = sorted(root.glob("*/SKILL.md")) if root.is_dir() else []
        if not files:
            continue
        withrules = [
            f
            for f in files
            if re.search(
                r"\bNEVER\b|\bALWAYS\b|\bMUST\b|\bdo not\b",
                f.read_text(errors="replace"),
            )
        ]
        st = add_store(
            label,
            str(root),
            files,
            note=f"{len(withrules)} of {len(files)} contain rule language",
        )
        st.rule_bearing_files = len(withrules)
        s = []
        for f in withrules:
            s += extract_md_rules(f, st.name)
        statements += s
        st.statements = len(s)

    # -- the foundation repo -------------------------------------------------
    fr = home / "repos" / "foundation"
    if fr.is_dir():
        files = sorted(
            p
            for p in fr.rglob("*.md")
            if ".git" not in p.parts and "tmp" not in p.parts
        )
        st = add_store(
            "foundation reference repo",
            str(fr),
            files,
            reachable="cited, unread",
            reach_note=(
                "five lens skills cite five different files as "
                "canonical; no evidence any lens loads one at "
                "runtime"
            ),
        )
        st.last_modified = _git_last_commit(fr) or st.last_modified
        s = []
        for f in files:
            s += extract_md_rules(f, st.name)
        statements += s
        st.statements = len(s)

    # -- hooks: rules stated as code ----------------------------------------
    hooks = sorted((repository_input_root / ".claude" / "hooks").glob("*.py"))
    if hooks:
        st = add_store(
            "Claude Code hooks (ateles)",
            str(repository_input_root / ".claude/hooks"),
            hooks,
            note="rules stated as enforcement code, not prose",
            reachable="yes",
            reach_note="binds only where settings.json wires it",
        )
        s = []
        for f in hooks:
            doc = f.read_text(errors="replace")[:4000]
            for line in doc.splitlines():
                t = line.strip().lstrip("#").strip()
                if (
                    len(t) > 30
                    and IMPERATIVE.search(t)
                    and not t.startswith(("import", "from"))
                ):
                    s.append(
                        Statement(
                            st.name,
                            str(f),
                            "docstring",
                            t[:600],
                            last_modified=_mtime(f),
                        )
                    )
        statements += s
        st.statements = len(s)

    return statements, stores


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def build_clusters(
    statements: list[Statement],
) -> tuple[list[Cluster], list[Statement]]:
    """Group statements by KIND. Returns (clusters, unclassified)."""
    buckets: dict[str, Cluster] = {}
    unclassified: list[Statement] = []
    for s in statements:
        kinds = classify(s.text)
        if not kinds:
            unclassified.append(s)
            continue
        for k in kinds:
            c = buckets.setdefault(k, Cluster(kind=k, label=KIND_LABELS[k]))
            st = Statement(s.store, s.location, s.locator, s.text, k, s.last_modified)
            c.statements.append(st)
    clusters = sorted(buckets.values(), key=lambda c: (-len(c.statements), c.kind))
    return clusters, unclassified


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


def _canonical_generated_text(text: str) -> str:
    """Return generated Markdown with exactly one terminal newline."""
    return text.rstrip("\n") + "\n"


def render(
    clusters: list[Cluster],
    stores: list[Store],
    unclassified: list[Statement],
    statements: list[Statement],
) -> str:
    total_scanned = len(statements)
    withheld_n = total_scanned
    clustered = sum(len(c.statements) for c in clusters)
    total_rules = len(clusters)
    dup = (clustered / total_rules) if total_rules else 0
    diverging = [c for c in clusters if c.diverges]
    needs_split = [c for c in clusters if c.needs_split]
    stores_by_name = {store.name: store for store in stores}
    today = date.today().isoformat()

    L: list[str] = []
    A = L.append

    A("<!-- GENERATED by execution/scripts/render_rule_inventory.py — do not edit. -->")
    A(
        "<!-- Source: the rule stores themselves, measured. Neotoma PROD "
        "read-only, plus the harness and repository files the store table "
        "names. -->"
    )
    A("")
    A("# The rule inventory: every place a rule is stated")
    A("")
    A(
        f"**Kind:** foundation companion; generated, never authored. "
        f"**Generated by:** `execution/scripts/render_rule_inventory.py`, held "
        f"equal to the measured system by `--check`. **Measured:** {today}."
    )
    A("")
    A(
        "Not keyed, not in the kernel, and never inlined into a review prompt: "
        "this document states no rule about how the swarm works. It reports where "
        "the rules that do are written down."
    )
    A("")
    A(
        "Stage 0 of the rule migration, in the sense `migration.md` already gives "
        "the word: the inventory a migration starts from. It is generated rather "
        "than authored because the prose version this replaces was wrong twice, "
        "both times caught only by re-measuring — a harness-file count changed on "
        "rerun, and lens-to-source citations had been miscounted. A hand-count "
        "cannot be diffed and cannot detect its own drift."
    )
    A("")
    A(
        "**This file records a rule's LOCATION and KIND, never its VALUE.** "
        "Both repos are public, so every statement value is replaced with "
        f"*{WITHHELD}*. Store names, paths, locators, dates, labels, and "
        "reachability are projected onto a generator-owned public vocabulary; "
        "unknown metadata fails closed to a generic label. This structural "
        "projection is the gate because a pattern screen cannot recognize every "
        "proper noun or private identifier."
    )
    A("")
    A(
        "**Public empty state and private recovery path.** The public statement "
        "bodies are deliberately absent, and public locations show "
        "store-kind granularity rather than a private filename or entity id. To "
        "resolve a NEEDS-SPLIT or divergence candidate, generate a mode-0600 "
        "private locator map outside the repository, then open the named source "
        "directly: `python3 execution/scripts/render_rule_inventory.py "
        "--check --private-diagnostics /tmp/rule-inventory-locators.json`. The "
        "command refuses a target inside the repository and the diagnostic still "
        "contains no statement values."
    )
    A("")
    A(
        "**It is perishable.** Re-run it; never edit it to keep up. A figure here "
        "without an instrument is a defect in the generator."
    )
    A("")

    A("## The headline: duplication factor")
    A("")
    A("| Measure | Value |")
    A("|---|---|")
    A(f"| Distinct rules (clusters) | **{total_rules}** |")
    A(f"| …of those, still flagged NEEDS-SPLIT | **{len(needs_split)}** |")
    A(f"| Statements of those rules, across all stores | **{clustered}** |")
    A(f"| **Duplication factor** | **{dup:.1f}×** |")
    A(f"| Clusters whose statements DIVERGE on binding force | **{len(diverging)}** |")
    A(f"| Normative statements scanned in total | {total_scanned} |")
    A(f"| …of those, matching no known rule kind | {len(unclassified)} |")
    A(f"| Statement values withheld from public output | {withheld_n} |")
    A(f"| Stores inventoried | {len(stores)} |")
    A("")
    A(
        "The duplication factor is the point. `migration.md` governs the target "
        "shape — *standing rules go to `task_policy` by kind, never by value* — so "
        "one rule stated in fourteen places collapses to ONE entity with fourteen "
        "locations, not fourteen entities. The factor is how much collapsing "
        "there is to do; the divergence count is how much of it needs a ruling "
        "rather than a merge."
    )
    A("")
    A(
        f"The factor is computed over the {clustered} statements that match a "
        f"known rule kind, not over all {total_scanned} scanned. The remainder "
        "are procedure, context, or rules whose kind has no signature yet — "
        "counting them would inflate the figure with statements the migration "
        "has nothing to collapse."
    )
    A("")

    A(
        "### The earlier 13.9× was an upper bound on duplication, and a lower "
        "bound on the rule count"
    )
    A("")
    A(
        "The first revision of this inventory reported **36 rules at 13.9×**. "
        "That figure was wrong in a specific and correctable direction, and it "
        "is restated here rather than quietly replaced."
    )
    A("")
    A(
        "Its clustering merged by TOPIC. Statements that shared vocabulary "
        "landed together whether or not they stated the same rule, so some of "
        "the 13.9× was not duplication at all — it was distinct rules stacked in "
        "one bucket. Duplication was therefore **over**-stated and the rule "
        "count **under**-stated: 13.9× is an upper bound on the first and 36 a "
        "lower bound on the second. Neither is a measurement of what it named."
    )
    A("")
    A(
        "How it was caught matters more than the number. The operator noticed a "
        "rule he knew existed — *pose open decisions through the harness "
        "questions tool* — was absent from the 36. It had not been missed by the "
        "extractor: a `standing_rule` entity and eight further statements were "
        "all present in the document, absorbed into a cluster labelled *give "
        "status updates and open decisions unprompted*. Those are two rules. One "
        "says SURFACE a decision, the other says HOW; a turn that ends with a "
        "prose decision list satisfies the first and violates the second. The "
        "instrument could not see this, because nothing in it measured its own "
        "clustering. The `Distinct` column and the NEEDS-SPLIT verdict exist so "
        "the next over-merge is visible in the output rather than waiting on a "
        "reader's memory of a rule that should be there."
    )
    A("")
    A(
        f"This revision applies the merge test — two statements are the same "
        f"rule only if a session cannot satisfy one while violating the other — "
        f"and reports **{total_rules} rules at {dup:.1f}×**, with "
        f"**{len(needs_split)}** clusters still flagged as buckets. The new "
        "figure is not proposed as final either: a NEEDS-SPLIT count above zero "
        "is the document saying so about itself."
    )
    A("")

    A("## The stores")
    A("")
    A(
        "`populated` is what the store holds; `reachable` is whether it gets to an "
        "agent. They are different questions, and ateles#1118 is why the column "
        "exists: `agent_policy` is fully populated and delivers nothing, because "
        "`agent_loader.py` filters on `agent_sub`, which is empty in every row."
    )
    A("")
    unread_stores = [s for s in stores if not s.read_ok]
    if unread_stores:
        A(
            f"> **{len(unread_stores)} store(s) could not be read on this run** and "
            "are listed below as UNREAD. An unread store is NOT an empty one: its "
            "rules are missing from every count on this page, and the counts are "
            "therefore lower bounds. Re-run where the reader has credentials."
        )
        A("")
    A("| Store | Location | Populated | Statements | Last modified | Reachable |")
    A("|---|---|---|---|---|---|")
    for st in sorted(stores, key=lambda s: -s.statements):
        name = public_store_name(st.name)
        loc = public_store_location(st.name, st.location)
        if not st.read_ok:
            A(f"| {name} | `{loc}` | — | — | — | **UNREAD** |")
            continue
        A(
            f"| {name} | `{loc}` | {st.populated} | {st.statements} | "
            f"{public_last_modified(st.last_modified)} | "
            f"{public_reachability(st.reachable)} |"
        )
    A("")

    A("## NEEDS-SPLIT: clusters that are still topical buckets")
    A("")
    A(
        "**The merge test.** Two statements are the same rule only if *a session "
        "cannot satisfy one while violating the other*. Topical similarity is "
        "not sufficient. `CLAUDE.md` states the governing principle for its own "
        "rule-parity checker — *near-identical leads are reported but never "
        "collapsed; `Dispatch, don't work inline` and `Dispatch, don't drift "
        "inline` are two rules* — and this is that principle applied to the "
        "inventory's clustering."
    )
    A("")
    if needs_split:
        A(
            f"**{len(needs_split)} clusters below do not pass it yet.** They are "
            "listed as buckets rather than counted as clean rules. Each holds "
            "statements whose openings are nearly all distinct, which means the "
            "cluster is grouping by shared vocabulary rather than by rule "
            "identity — the same defect that hid the questions-tool rule."
        )
        A("")
        A(
            "**A flag is not a verdict, and it does not say which defect it "
            "found.** The probe reads the first six words of each statement, so "
            "a high ratio means only that the cluster's statements are mostly "
            "unlike each other. Read directly, the flagged clusters turn out to "
            "carry two different defects, and the remedy differs:"
        )
        A("")
        A(
            "- **Genuine over-merge** — the cluster holds distinct rules that "
            "share vocabulary. This is what hid the questions-tool rule, and the "
            "remedy is a split."
        )
        A(
            "- **Extraction noise** — the cluster holds a correctly-merged rule "
            "plus statements that merely MENTION it. The never-stash cluster is "
            "the worked case: of its statements, the prohibition itself is "
            "stated in several harnesses in close to the same words and is "
            "correctly ONE rule, but the cluster also catches a hook's own test "
            "fixture and a rule about task chips whose example happens to be a "
            "stash. The remedy there is a narrower signature, not a split."
        )
        A("")
        A(
            "Both need a human read of their private sources against the merge "
            "test, exactly as the divergence list does. The public artifact does "
            "not contain those statement bodies; use the `--private-diagnostics` "
            "locator map described above, then read the named source. What the "
            "probe is for is that neither defect is now discoverable only by a "
            "reader noticing an absence."
        )
        A("")
        A("| Rule | Statements | Distinct | Ratio | Stores |")
        A("|---|---|---|---|---|")
        for c in sorted(needs_split, key=lambda c: -len(c.statements)):
            n = len(c.statements)
            A(
                f"| `{public_rule_id(c)}` "
                f"{public_cluster_label(c.kind, c.label)} | "
                f"{n} | {c.distinct_openings} | "
                f"{c.distinct_openings / n:.2f} | "
                f"{len(public_cluster_stores(c))} |"
            )
        A("")
    else:
        A(
            "No cluster trips the probe on this run. That is not proof the "
            "clustering is correct — the probe measures statement variety, not "
            "rule identity — but no cluster is currently a bucket by this test."
        )
        A("")

    if diverging:
        A("## Divergence: the same rule, stated differently")
        A("")
        A(
            "The highest-value output. Each row is one rule whose statements do "
            "not agree on how strongly it binds. A consumer's behaviour then "
            "depends on which copy it happens to read, which is the failure "
            "ateles#1115 found in `agent_policy` (two live rows, same safety rule, "
            "one `recommended` and one `mandatory`) and ateles#1121 found between "
            "a foundation file and the lens that cites it. **A divergence needs a "
            "ruling, not a merge** — the migration cannot pick a side on its own."
        )
        A("")
        A(
            "**A flagged divergence is a candidate, not a verdict.** The test "
            "reads prose, so it cannot tell a rule being STATED from a rule being "
            "DESCRIBED: a sentence explaining that a hook is deliberately "
            "fail-open reads as an advisory statement of the fail-closed rule. "
            "Spot-checked on two clusters at generation time — the consent-gate "
            "row is genuine (`CLAUDE.md` says proceed without asking; an "
            "`agent_policy` row says approval is mandatory), the fail-closed row "
            "is an artifact of exactly that confusion. Each row below needs a "
            "human read of its private sources before it is ruled on; use the "
            "`--private-diagnostics` locator map rather than looking for bodies in "
            f"this public file. The value of the list is that it is "
            f"{len(diverging)} rows rather than {clustered}."
        )
        A("")
        A("| Rule | Statements | Shapes present | Stores |")
        A("|---|---|---|---|")
        for c in diverging:
            # `c.shapes` excludes "unmarked" -- a statement that mentions the
            # rule without saying how strongly it binds is not evidence of
            # disagreement, and listing it here would suggest it was.
            A(
                f"| `{public_rule_id(c)}` "
                f"{public_cluster_label(c.kind, c.label)} | "
                f"{len(c.statements)} | "
                f"{', '.join(sorted(c.shapes))} | "
                f"{', '.join(public_cluster_stores(c))} |"
            )
        A("")

    A("## The clusters")
    A("")
    A(
        "One row per rule; every location it is stated. `agree` means every "
        "statement binds the same way — it does not mean the wording matches, and "
        "it is not a claim that the statements are interchangeable."
    )
    A("")
    A(
        "`Distinct` is how many different statements the cluster holds, keyed on "
        "each statement's first six words. A rule restated across stores repeats "
        "itself, so a genuine cluster has far fewer distinct statements than "
        "statements. A cluster whose distinct count approaches its statement "
        f"count is carrying statements that merely share vocabulary, and is "
        f"emitted as **NEEDS-SPLIT** rather than as a rule (at or above "
        f"{SPLIT_RATIO:g} with at least {MIN_STATEMENTS_TO_JUDGE} statements)."
    )
    A("")
    A("| id | Rule | Statements | Distinct | Stores | Agree? | Target home |")
    A("|---|---|---|---|---|---|---|")
    for c in clusters:
        home = TARGET_HOME.get(c.kind, "**UNCLASSIFIED**")
        verdict = (
            "**NEEDS-SPLIT**"
            if c.needs_split
            else ("DIVERGE" if c.diverges else "agree")
        )
        A(
            f"| `{public_rule_id(c)}` | "
            f"{public_cluster_label(c.kind, c.label)} | "
            f"{len(c.statements)} | {c.distinct_openings} | "
            f"{len(public_cluster_stores(c))} | {verdict} | {home} |"
        )
    A("")

    A("### Public store-kind locations for each rule")
    A("")
    A(
        "Statement values are deliberately absent. Locations below are public "
        "store-kind shapes, not navigable private paths; generate the private "
        "locator map described above when a source-level read is required."
    )
    A("")
    for c in clusters:
        A(f"#### `{public_rule_id(c)}` — {public_cluster_label(c.kind, c.label)}")
        A("")
        A(
            f"Target home: **{TARGET_HOME.get(c.kind, 'UNCLASSIFIED')}** · "
            f"{len(c.statements)} statements, {c.distinct_openings} distinct · "
            f"{'**NEEDS-SPLIT**' if c.needs_split else ('**DIVERGE**' if c.diverges else 'agree')}"
        )
        A("")
        A("| Store | Public location shape | At |")
        A("|---|---|---|")
        for s in sorted(c.statements, key=lambda x: (x.store, x.location)):
            A(
                f"| {public_store_name(s.store)} | "
                f"`{public_statement_location(s.store, s.location)}` | "
                f"{public_locator(s.locator)} |"
            )
        A("")

    unmapped = [c for c in clusters if c.kind not in TARGET_HOME]
    A("## What this inventory could not classify")
    A("")
    if unmapped:
        A(
            "Rule kinds with no derivable home in the authority table "
            "(`conformance.md`, *Direction of truth per class of record*). Listed "
            "rather than guessed: inventing a home is the quiet re-decision the "
            "migration exists to prevent."
        )
        A("")
        for c in unmapped:
            A(f"- `{public_rule_id(c)}` — {public_cluster_label(c.kind, c.label)}")
    else:
        A("Every rule kind maps to a home in the authority table.")
    A("")
    A(
        f"{len(unclassified)} statements match no known rule kind. They are "
        "**not** classified into a neighbouring cluster: an over-eager merge would "
        "hide a divergence, which is the one output this inventory exists to "
        "produce. They are procedure, context, or rules whose kind has no "
        "signature yet — adding a signature to `KIND_SIGNATURES` is how the "
        "coverage grows."
    )
    A("")

    A("## Prior art: where this disagrees with the hand-count it replaces")
    A("")
    A(
        "Sources: the prose inventory on ateles#1114, and the findings on "
        "ateles#1115, #1118 and #1121 that this document's reachability and "
        "divergence columns exist to generalize."
    )
    A("")
    A(
        "That prose inventory is the input to this one, not a thing it discards: "
        "each of its findings is a claim with a location attached, and several "
        "were found by accident rather than by systematic search. The claims were "
        "re-measured and the locations kept. Where the measurement disagrees, "
        "both figures are given — the disagreements are themselves the argument "
        "for generating the inventory rather than typing it."
    )
    A("")
    openclaw_store = stores_by_name.get("OpenClaw")
    cursor_store = stores_by_name.get("Cursor")
    project_memory_store = stores_by_name.get("Claude Code project memory")
    repo_skills_store = stores_by_name.get("Skills (ateles repo)")
    openclaw_rules = (
        str(openclaw_store.statements) if openclaw_store else "not present on this run"
    )
    cursor_files = (
        str(cursor_store.populated) if cursor_store else "not present on this run"
    )
    project_memory_files = (
        str(project_memory_store.populated)
        if project_memory_store
        else "not present on this run"
    )
    if repo_skills_store and repo_skills_store.rule_bearing_files is not None:
        repo_skill_measurement = (
            f"{repo_skills_store.rule_bearing_files} of {repo_skills_store.populated}"
        )
    else:
        repo_skill_measurement = "not present on this run"
    A("| Claim on the prose inventory | Measured here | Reading |")
    A("|---|---|---|")
    A(
        f"| OpenClaw holds 1 rule | **{openclaw_rules}** | The `1` is a "
        "directory, not a file. "
        "Beneath it: session state and a vendored Codex home whose shipped skills "
        "are a dependency, not operator rules. Sweeping it yields 21,411 "
        "statements from files the operator never wrote. |"
    )
    A(
        f"| Cursor holds 31 files (already corrected once from 5) | "
        f"**{cursor_files} files** | The current total is the measured store row "
        "above; live/backup/symlink composition remains internal diagnostic "
        "metadata rather than public path detail. |"
    )
    A(
        f"| Project memory: 329 files across 15 dirs | "
        f"**{project_memory_files} files** | The current file total is derived "
        "from the measured store; private project-directory names are not "
        "emitted. |"
    )
    A(
        f"| 88 of 97 ateles skills contain rule language | "
        f"**{repo_skill_measurement}** | "
        "Different instrument: the earlier count matched `do not` case-"
        "insensitively across the whole file. |"
    )
    A(
        "| Five lenses cite five different foundation files, one consumer each | "
        "**confirmed as the store's reachability verdict** | Not re-derived; "
        "cited. See the foundation-repo reconciliation (2026-09-19). |"
    )
    A(
        f"| Six rule stores | **{len(stores)} inventoried** | The store list was "
        "a floor. The "
        "additions: `task_policy` entities (a live store, not only a target), "
        "hooks (rules stated as code), skills split by root, and — the largest — "
        "the per-checkout copies below. |"
    )
    A("")
    A("### The store nobody had counted: one instruction file, many checkouts")
    A("")
    ateles_copies = stores_by_name.get("ateles/CLAUDE.md checkout copies")
    neotoma_copies = stores_by_name.get("neotoma/AGENTS.md checkout copies")
    if ateles_copies and neotoma_copies:
        ateles_versions = ateles_copies.distinct_versions or 0
        neotoma_versions = neotoma_copies.distinct_versions or 0
        copy_measurement = (
            f"**{ateles_copies.populated} copies of `ateles/CLAUDE.md` in "
            f"{ateles_versions} distinct versions**, and "
            f"**{neotoma_copies.populated} copies of `neotoma/AGENTS.md` in "
            f"{neotoma_versions} distinct versions**."
        )
    else:
        missing_copy_stores = [
            name
            for name, store in (
                ("ateles instruction copies", ateles_copies),
                ("neotoma instruction copies", neotoma_copies),
            )
            if store is None
        ]
        copy_measurement = (
            "copy measurement unavailable on this run: "
            + ", ".join(missing_copy_stores)
            + "."
        )
    A(
        "`CLAUDE.md` is re-injected from disk at every compaction, which is what "
        "makes it the home for standing instructions. The disk it is read from is "
        "the one in the session's own checkout. Measured on this machine: "
        f"{copy_measurement}"
    )
    A("")
    A(
        "So a rule's reach is not whether it is in `CLAUDE.md` but which copy of "
        "`CLAUDE.md` the reader opened, and the deployment checkouts the daemons "
        "run from (`~/ateles-rc-src`, `~/neotoma-rc-src`) are two more copies "
        "again. This is `docs/foundation/principles.md#1` — a rule that lives in "
        "only one checkout does not bind — measured rather than asserted, and it "
        "is the concrete mechanism behind ateles#973, where a session ran for "
        "hours from a worktree whose `CLAUDE.md` lacked the never-stash rule and "
        "both compaction hooks."
    )
    A("")

    A(render_rulings_section())

    A("## Method, so a re-run means something")
    A("")
    A(
        "- **Entities** are read field-name agnostically. `standing_rule` text "
        "lives under five different field names and 4 rows carry none; "
        "`agent_policy` uses a different set again. `raw_fragments` is read too, "
        "because `/store` accepts undeclared fields and routes rule text there. A "
        "reader checking one field name drops rows and reports a clean run."
    )
    A(
        "- **Files** are read per rule, not per file. A bolded-lead bullet is one "
        "rule (the shape `verify_claude_md_merge.py` keys on, so the inventory and "
        "the parity checker agree on what a rule is); so is any other line "
        "carrying an imperative."
    )
    A(
        "- **Clusters** are by kind, never by value, per `migration.md`, and "
        "the merge test decides a kind: two statements are the same rule only "
        "if a session cannot satisfy one while violating the other. Topical "
        "similarity is not sufficient, and the test is applied in both "
        "directions — a rule restated across seven stores is still ONE rule, so "
        "over-splitting is as wrong as over-merging."
    )
    A(
        "- **Over-merge is measured, not assumed.** Every cluster reports its "
        "distinct-statement count, and one whose distinct count approaches its "
        "statement count is emitted as NEEDS-SPLIT rather than as a rule."
    )
    A(
        "- **Divergence** is judged on whether statements bind the same way, "
        "not on wording."
    )
    A(
        "- **Target homes** come from the authority table in `conformance.md` and "
        "from nowhere else."
    )
    A("- Read-only against Neotoma **prod**. Nothing is written to the record.")
    return _canonical_generated_text("\n".join(L))


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify the committed inventory matches the system",
    )
    ap.add_argument(
        "--json",
        metavar="PATH",
        help="also dump the public-safe extraction metadata",
    )
    ap.add_argument(
        "--private-diagnostics",
        metavar="PATH",
        help=(
            "write private source locators (never values) outside the repository "
            "for NEEDS-SPLIT/divergence diagnosis"
        ),
    )
    ap.add_argument(
        "--require-complete-measurement",
        action="store_true",
        help=(
            "fail before comparison when any canonical store kind is missing or "
            "unread; required by the merge-gating workflow"
        ),
    )
    ap.add_argument(
        "--cache", metavar="DIR", help="cache entity reads here (re-run offline)"
    )
    ap.add_argument(
        "--repository-input-root",
        metavar="DIR",
        help=(
            "read repository-authored rule inputs from this data-only tree; "
            "the executing renderer remains this trusted script"
        ),
    )
    ap.add_argument(
        "--expected-output",
        metavar="PATH",
        help=(
            "compare against this candidate rule_inventory.md; requires "
            "--repository-input-root and --check"
        ),
    )
    args = ap.parse_args()

    home = Path.home()
    cache = Path(args.cache) if args.cache else None

    repository_input_root = REPO_ROOT
    expected_output = OUTPUT
    if args.repository_input_root:
        repository_input_root = Path(args.repository_input_root)
        if (
            not repository_input_root.is_absolute()
            or repository_input_root.is_symlink()
            or not repository_input_root.is_dir()
        ):
            print(
                "rule inventory equality unavailable: repository input data "
                "root is invalid",
                file=sys.stderr,
            )
            return 3
        repository_input_root = repository_input_root.resolve(strict=True)
    if args.expected_output:
        if not args.repository_input_root or not args.check:
            print(
                "rule inventory equality unavailable: candidate output "
                "requires data-root check mode",
                file=sys.stderr,
            )
            return 3
        candidate_output = Path(args.expected_output)
        try:
            expected_output = candidate_output.resolve(strict=True)
        except OSError:
            print(
                "rule inventory equality unavailable: candidate output is missing",
                file=sys.stderr,
            )
            return 3
        if (
            candidate_output.is_symlink()
            or not expected_output.is_file()
            or not expected_output.is_relative_to(repository_input_root)
        ):
            print(
                "rule inventory equality unavailable: candidate output is "
                "outside the declared input data",
                file=sys.stderr,
            )
            return 3

    ent_stmts, ent_stores = read_entities(cache, repository_input_root)
    file_stmts, file_stores = read_file_stores(home, repository_input_root)

    statements = ent_stmts + file_stmts
    stores = ent_stores + file_stores
    clusters, unclassified = build_clusters(statements)

    if args.require_complete_measurement:
        missing, unread = measurement_readiness(stores)
        if missing or unread:
            details = []
            if missing:
                details.append("missing store kinds: " + ", ".join(missing))
            if unread:
                details.append("unread store kinds: " + ", ".join(unread))
            canonical_store = "Canonical repository instruction roots"
            if canonical_store in missing or canonical_store in unread:
                details.append(
                    "configure "
                    + CANONICAL_REPOSITORY_ROOTS_ENV
                    + " on the canonical measurement runner"
                )
            print(
                "rule inventory equality unavailable: full measurement is "
                "incomplete; " + "; ".join(details),
                file=sys.stderr,
            )
            return 3
        rule_count, statement_count = measurement_totals(clusters)
        if rule_count < 1 or statement_count < 1:
            print(
                "rule inventory equality unavailable: full measurement is "
                "incomplete; instrument returned zero rules or statements",
                file=sys.stderr,
            )
            return 3

    out = render(clusters, stores, unclassified, statements)

    # Defense in depth for structured values. Proper nouns are kept out by the
    # closed public projection above; this catches a future literal address,
    # amount, email, or similar value introduced in static emitter prose.
    clean, reasons = screen_for_pii(out)
    if not clean:
        print(
            "PUBLIC OUTPUT SCREEN FAILED: " + ", ".join(reasons),
            file=sys.stderr,
        )
        return 2

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                public_payload(clusters, stores, unclassified, statements),
                indent=2,
            )
        )

    if args.private_diagnostics:
        try:
            write_private_diagnostics(args.private_diagnostics, clusters)
        except (OSError, ValueError) as exc:
            print(f"PRIVATE DIAGNOSTIC WRITE FAILED: {exc}", file=sys.stderr)
            return 2
        print(
            "wrote private locator diagnostics outside the repository "
            f"(statement values absent): {Path(args.private_diagnostics).expanduser()}",
            file=sys.stderr,
        )

    if args.check:
        if not expected_output.exists():
            print("candidate rule inventory is absent", file=sys.stderr)
            return 1
        cur = expected_output.read_text()
        # The measurement date changes every run and is not corpus drift.
        if strip_volatile_measurement_date(cur) != strip_volatile_measurement_date(out):
            print(
                "rule inventory is stale — re-run "
                "execution/scripts/render_rule_inventory.py",
                file=sys.stderr,
            )
            return 1
        print("rule inventory matches the measured system")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(out)
    unread = [s.name for s in stores if not s.read_ok]
    clustered = sum(len(c.statements) for c in clusters)
    print(
        f"wrote {OUTPUT.relative_to(REPO_ROOT)}: "
        f"{len(clusters)} rules, {clustered} statements of them "
        f"({len(statements)} scanned), "
        f"{clustered / max(len(clusters), 1):.1f}x duplication, "
        f"{sum(1 for c in clusters if c.diverges)} diverging, "
        f"{sum(1 for c in clusters if c.needs_split)} NEEDS-SPLIT"
    )
    if unread:
        print(f"UNREAD stores: {', '.join(unread)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

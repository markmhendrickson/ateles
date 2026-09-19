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

PII posture -- read this before changing the emitter
----------------------------------------------------
Both repos are PUBLIC and at least five rule entities carry operator specifics
(a live BTC address, a payee first name, a vendor, an instructor, a gym, EUR
amounts). The inventory therefore records a rule's LOCATION and KIND and NEVER
its operator-specific VALUE. `screen_for_pii()` runs over every string before it
is emitted and replaces a statement that trips it with "operator-specific, value
withheld"; `--check` re-runs the screen so a value that lands later still fails
the gate. A PII-shaped literal in a committed inventory is the exact failure
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
    python3 execution/scripts/render_rule_inventory.py --json OUT # raw dump
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
        return safe_statement(self.text)


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
    if re.search(r"\bnever\b|\bmust not\b|\bdo not\b|\bdon't\b|\bforbidden\b|"
                 r"\bhard-block|\brefus|\bmandatory\b", low):
        return "prohibitive/binding"
    if re.search(r"\balways\b|\bmust\b|\brequired\b|\bshall\b", low):
        return "mandatory"
    if re.search(r"\bprefer\b|\bshould\b|\brecommend|\bdefault to\b|"
                 r"\bwhere possible\b|\badvisory\b|\btry to\b", low):
        return "advisory"
    return "unmarked"


# ---------------------------------------------------------------------------
# Rule KINDS -- the deduplication axis
# ---------------------------------------------------------------------------
#
# A kind is what the rule is ABOUT, not what it says. Two statements share a
# kind when a reader would call them the same rule. This is the "by kind, never
# by value" constraint made mechanical: the signature decides the cluster, and
# the cluster carries every location plus the agree/diverge verdict.
#
# Deliberately conservative. A statement matching no signature lands in its own
# singleton cluster rather than being forced into a neighbour -- an over-eager
# merge would hide a divergence, which is the one output this inventory exists
# to produce.

KIND_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    # (kind key, human label, regex over lowercased text)
    ("git_never_stash", "Never use git stash; WIP-commit instead",
     r"git stash|never stash|\bstash\b.{0,40}(forbidden|never|guard)"),
    ("worktree_isolation", "One worktree per agent; never mutate a shared clone",
     r"worktree|shared (main )?clone|one agent.{0,20}one worktree|sibling repo"),
    ("neotoma_prod_only", "Always use the Neotoma prod instance, never dev",
     r"neotoma prod|prod.{0,20}never.{0,15}dev|mcpsrv_neotoma.{0,30}always"),
    ("gws_over_gmail_mcp", "Use the gws CLI for Google Workspace, not the MCP",
     r"\bgws\b.{0,60}(cli|gmail|mcp)|gmail mcp"),
    ("gmail_send_gate", "Gmail sends and draft-updates need per-message approval",
     r"drafts update|draft.{0,15}can send|messages send|send gate|never send.{0,25}(mail|email)"),
    ("public_repo_pii", "Both repos are public; scrub PII before committing",
     r"public repo|both repos are public|scrub.{0,20}(pii|client|username)|"
     r"strip pii|no operator data|pii-free"),
    ("verify_write_landed", "Read a write back; a success code is not a landed write",
     r"read it back|write that reports success|verify.{0,25}(write|landed)|"
     r"not treat a 2xx|success.{0,15}is not"),
    ("verify_before_asserting", "Verify against the system of record before asserting",
     r"verify before|check the live system|verified only when you just checked|"
     r"validate the instrument|before asserting"),
    ("fail_closed", "Absent or malformed safety values take the restrictive branch",
     r"fail clos|fail-clos|restrictive branch|fail open|fail-open"),
    ("dispatch_not_inline", "Dispatch work to the owning agent; do not work inline",
     r"dispatch, don't|dispatch.{0,25}not inline|inline execution|"
     r"owning agent|orchestrat.{0,20}not.{0,15}workhorse|delegate"),
    ("no_merge_over_objection", "Do not merge while a live blocking review stands",
     r"blocking review|do not merge|merge.{0,20}gated|required approval"),
    ("no_verify_bypass", "Never bypass the pre-commit hook with --no-verify",
     r"--no-verify|no-verify|skip_tests"),
    ("consent_gate_external", "Irreversible or outward-facing actions need approval",
     r"consent gate|operator approval|irreversible|outward-facing|"
     r"never.{0,20}without.{0,20}approval|confirm with"),
    ("secrets_never_hardcoded", "Never hardcode secrets or credentials",
     r"hardcode.{0,20}(secret|credential|token|iban)|never commit.{0,20}secret|"
     r"secrets? (management|from env)"),
    ("config_from_entity", "Operator-specific config comes from entities, not code",
     r"config-source|hardcoded config|operator-specific config|"
     r"context entit|from env|portable|fork test"),
    ("store_in_neotoma", "Durable memory belongs in Neotoma, not harness files",
     r"neotoma first|durable memory|store.{0,25}neotoma|"
     r"memory file|not.{0,15}markdown file"),
    ("persist_every_turn", "Persist every conversation turn to Neotoma",
     r"turn-by-turn|every turn|conversation_message|per-turn"),
    ("plan_merge_before_correct", "Re-read and merge a plan field before correcting it",
     r"re-read.{0,25}merge|correct.{0,20}replaces|merge.{0,20}before writing|"
     r"stale in-memory"),
    ("no_done_without_artifact", "Never mark work done citing an unverifiable artifact",
     r"never mark.{0,25}done|unverifiable completion|verify the artifact exists"),
    ("agent_prompts_public", "Agent prompts are public and carry no operator data",
     r"prompts are (always )?public|pii-free|prompt.{0,30}no operator|"
     r"public.{0,20}prompt"),
    ("renamed_agent_no_refs", "A renamed agent leaves no stale reference",
     r"renamed agent|retired name|stale reference|rename.{0,25}same change"),
    ("daemon_checkout_fresh", "Daemons run dedicated checkouts that must be fresh",
     r"rc-src|deployment checkout|checkout drift|ff-only|daemon.{0,25}checkout"),
    ("restart_daemons", "Restart affected daemons after a merge, then verify",
     r"restart.{0,20}daemon|launchctl|redeploy"),
    ("test_must_fail_red", "A test that cannot fail on its subject is decoration",
     r"cannot fail|goes red|revert the fix|ratifies the bug|decoration"),
    ("reuse_prior_art", "Extend the mechanism that exists; do not build a parallel one",
     r"prior art|already exists|parallel (mechanism|one)|reuse the existing"),
    ("summarize_operator_input", "Echo the operator's input, cleaned up, each reply",
     r"summarize what the operator|transcrib|cleaned up|relay.{0,20}speech"),
    ("status_update_unprompted", "Give status updates and open decisions unprompted",
     r"status update|unprompted|end every turn|open decision|"
     r"decisions that need"),
    ("proceed_with_recommendation", "Act on your recommendation; ask only at a real fork",
     r"proceed with your recommendation|don't ask|auto-proceed|"
     r"genuine fork|take it and report"),
    ("pr_body_from_file", "Pass PR and comment bodies by file, never inline",
     r"body-file|--body-file|body from a file"),
    ("verify_gh_identity", "Verify the GitHub identity before any write",
     r"gh api user|verify.{0,20}(gh|github).{0,20}(account|identity)|"
     r"unset GH_TOKEN"),
    ("recurring_never_done", "Recurring obligations roll their date; never complete",
     r"never.{0,20}mark.{0,25}complet|roll.{0,20}due_date|recurring obligation"),
    ("no_invented_content", "Never invent facts, quotes, emotion, or reactions",
     r"never invent|no invented|fabricat|hallucinat|verify every quote"),
    ("pii_minimization", "Minimize personal data at capture; purpose-bind it",
     r"rgpd|gdpr|minimi[sz]e at capture|legitimate interest|art\. ?9|"
     r"personal data"),
    ("no_untested_remediation", "Never assert a remediation you have not tested",
     r"untested remediation|never assert.{0,25}not tested|reproduce before"),
    ("squash_merge", "Merge by squash",
     r"squash"),
    ("conventional_commits", "Commit and PR titles follow the live title convention",
     r"conventional commit|commit message format|pr title"),
    ("test_colocation", "Tests follow this repo's naming and placement convention",
     r"test_\*\.py|\.test\.ts|test file.{0,25}(naming|placement|colocat)"),
)

_COMPILED_KINDS = [(k, lbl, re.compile(rx, re.I | re.S))
                   for k, lbl, rx in KIND_SIGNATURES]

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
    "worktree_isolation": "agent_policy",
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
    "no_invented_content": "task_policy",
    "pii_minimization": "docs/foundation/",
    "no_untested_remediation": "agent_policy",
    "squash_merge": "agent_policy",
    "conventional_commits": "agent_policy",
    "test_colocation": "agent_policy",
}


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
        return p[len(root) + 1:]
    return p.replace(str(Path.home()), "~")


def _mtime(p: Path) -> str:
    try:
        return date.fromtimestamp(p.stat().st_mtime).isoformat()
    except OSError:
        return ""


def _git_last_commit(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%cs"],
            capture_output=True, text=True, timeout=15,
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
        data=json.dumps({
            "entity_type": entity_type,
            "limit": limit,
            "include_snapshots": True,
        }).encode(),
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


def read_entities(cache: Path | None = None) -> tuple[list[Statement], list[Store]]:
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
            allfields = [v for v in list(snap.values()) + list(frags.values())
                         if isinstance(v, str)]
            allfields.append(str(e.get("canonical_name", "")))
            entity_clean, _ = screen_for_pii(" \n".join(allfields))

            seen = False
            for fname in TEXT_FIELDS:
                for src, srclabel in ((snap, fname), (frags, f"raw_fragments.{fname}")):
                    val = src.get(fname) if isinstance(src, dict) else None
                    if isinstance(val, str) and val.strip():
                        statements.append(Statement(
                            store=store.name, location=eid, locator=srclabel,
                            text=val if entity_clean else WITHHELD_MARKER,
                            last_modified=(e.get("last_observation_at") or "")[:10],
                        ))
                        seen = True
            if not seen:
                notext += 1
            obs = (e.get("last_observation_at") or "")[:10]
            latest = max(latest, obs)

        store.statements = sum(1 for s in statements if s.store == store.name)
        store.last_modified = latest
        if notext:
            store.note = f"{notext} row(s) carry no rule text under any known field name"
        stores.append(store)

    # Reachability -- ateles#1118. Populated is not delivered.
    loader = REPO_ROOT / "lib" / "daemon_runtime" / "agent_loader.py"
    for st in stores:
        if st.name.startswith("agent_policy"):
            filt = False
            if loader.exists():
                filt = 'snap.get("agent_sub")' in loader.read_text()
            st.reachable = "no" if filt else "unknown"
            st.reach_note = (
                "agent_loader filters on agent_sub, empty in every row "
                "(ateles#1118) — every agent loads zero policies"
                if filt else "loader filter not found; re-verify"
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
    r")", re.I,
)

# Lines that carry an imperative but are not a rule being STATED here:
# narration about rules, citations, code, and changelog prose.
NOT_A_RULE = re.compile(
    r"^\s*(?:\||>|```|#{1,6}\s|\d+\.\s*$|<!--)"          # tables, quotes, code
    r"|^\s*(?:import|from|def |class |return |assert |if |for |print\()"
    r"|(?:https?://|\.py:\d|\.md:\d)"                     # links and citations
    r"|^\s*[-*]\s*\[[ x]\]"                               # checklists
    r"|\b(?:was|were|had|used to|previously|motivated by|"
    r"this happened|for example|e\.g\.|i\.e\.)\b",
    re.I,
)


def extract_md_rules(path: Path, store_name: str,
                     max_chars: int = 600) -> list[Statement]:
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
            out.append(Statement(store_name, str(path), f"L{i}", text,
                                 last_modified=mtime))
            continue
        stripped = line.strip()
        if len(stripped) < 30 or len(stripped) > 700:
            continue
        if NOT_A_RULE.search(stripped):
            continue
        if IMPERATIVE.search(stripped):
            out.append(Statement(store_name, str(path), f"L{i}",
                                 stripped[:max_chars], last_modified=mtime))
    return out


def read_file_stores(home: Path) -> tuple[list[Statement], list[Store]]:
    statements: list[Statement] = []
    stores: list[Store] = []

    def add_store(name, loc, files, note="", reachable="yes", reach_note=""):
        st = Store(name=name, location=loc, populated=len(files),
                   note=note, reachable=reachable, reach_note=reach_note)
        lm = ""
        for f in files:
            lm = max(lm, _mtime(f))
        st.last_modified = lm
        stores.append(st)
        return st

    # -- repo instruction files ------------------------------------------
    ateles_md = REPO_ROOT / "CLAUDE.md"
    if ateles_md.exists():
        st = add_store("ateles/CLAUDE.md", str(ateles_md), [ateles_md],
                       reach_note="re-injected from disk at every compaction")
        s = extract_md_rules(ateles_md, st.name)
        statements += s
        st.statements = len(s)

    neo_md = home / "repos" / "neotoma" / "AGENTS.md"
    if neo_md.exists():
        st = add_store("neotoma/AGENTS.md", str(neo_md), [neo_md],
                       note="sibling repo, read-only")
        s = extract_md_rules(neo_md, st.name)
        statements += s
        st.statements = len(s)

    # -- the copy problem: one instruction file, many checkouts ----------
    # A rule that lives in only one checkout does not bind
    # (docs/foundation/principles.md#1). Measured rather than assumed.
    for label, fname in (("ateles/CLAUDE.md", "CLAUDE.md"),
                         ("neotoma/AGENTS.md", "AGENTS.md")):
        copies, digests, newest = [], set(), ""
        for p in (home / "repos").glob(f"*/{fname}"):
            copies.append(p)
            try:
                digests.add(hashlib.md5(p.read_bytes()).hexdigest())
            except OSError:
                pass
            newest = max(newest, _mtime(p))
        for dep in (home / "ateles-rc-src" / fname,
                    home / "neotoma-rc-src" / fname):
            if dep.exists():
                copies.append(dep)
                digests.add(hashlib.md5(dep.read_bytes()).hexdigest())
        if copies:
            st = add_store(
                f"{label} checkout copies", str(home / "repos"), copies,
                note=(f"{len(copies)} copies on disk in {len(digests)} distinct "
                      f"versions — each checkout binds its own"),
                reachable="divergent",
                reach_note=("a session or daemon reads the copy in ITS checkout, "
                            "not origin/main"),
            )
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
        st = add_store("Claude Code project memory", str(home / ".claude/projects"),
                       mem, note=f"{len(mem)} files across {len(dirs)} project dirs",
                       reachable="per-project",
                       reach_note="MEMORY.md index loads; linked files load on demand")
        s: list[Statement] = []
        for f in mem:
            s += extract_md_rules(f, st.name)
        statements += s
        st.statements = len(s)

    # -- Codex --------------------------------------------------------------
    codex = home / ".codex" / "AGENTS.md"
    if codex.exists():
        st = add_store("Codex", str(codex), [codex],
                       note="largest single rule file on the machine")
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
            "Cursor", str(cdir), allf,
            note=(f"{len(allf)} entries: {len(live)} live, {len(backups)} dated "
                  f".backup. copies, {len(symlinks)} symlinks into the neotoma repo"),
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
            for cand in ("AGENTS.md", "CLAUDE.md", "SOUL.md", "instructions.md",
                         "agent/AGENTS.md", "agent/instructions.md"):
                p = agent_dir / cand
                if p.is_file():
                    ocf.append(p)
    vendored = sum(1 for _ in oc.rglob("codex-home/**/SKILL.md")) if oc.is_dir() else 0
    st = add_store(
        "OpenClaw", str(oc), ocf,
        note=(f"no operator-authored rule file; the agent root holds session "
              f"state and a vendored Codex home ({vendored} shipped SKILL.md "
              f"files that are a dependency, not operator rules). Earlier "
              f"inventories counted the directory itself as 1 rule"),
        reachable="n/a",
    )
    s = []
    for p in ocf:
        s += extract_md_rules(p, st.name)
    statements += s
    st.statements = len(s)

    # -- skills, three roots -------------------------------------------------
    for label, root in (
        ("Skills (ateles repo)", REPO_ROOT / ".claude" / "skills"),
        ("Skills (user root)", home / ".claude" / "skills"),
    ):
        files = sorted(root.glob("*/SKILL.md")) if root.is_dir() else []
        if not files:
            continue
        withrules = [f for f in files
                     if re.search(r"\bNEVER\b|\bALWAYS\b|\bMUST\b|\bdo not\b",
                                  f.read_text(errors="replace"))]
        st = add_store(label, str(root), files,
                       note=f"{len(withrules)} of {len(files)} contain rule language")
        s = []
        for f in withrules:
            s += extract_md_rules(f, st.name)
        statements += s
        st.statements = len(s)

    # -- the foundation repo -------------------------------------------------
    fr = home / "repos" / "foundation"
    if fr.is_dir():
        files = sorted(p for p in fr.rglob("*.md")
                       if ".git" not in p.parts and "tmp" not in p.parts)
        st = add_store("markmhendrickson/foundation repo", str(fr), files,
                       reachable="cited, unread",
                       reach_note=("five lens skills cite five different files as "
                                   "canonical; no evidence any lens loads one at "
                                   "runtime"))
        st.last_modified = _git_last_commit(fr) or st.last_modified
        s = []
        for f in files:
            s += extract_md_rules(f, st.name)
        statements += s
        st.statements = len(s)

    # -- hooks: rules stated as code ----------------------------------------
    hooks = sorted((REPO_ROOT / ".claude" / "hooks").glob("*.py"))
    if hooks:
        st = add_store("Claude Code hooks (ateles)", str(REPO_ROOT / ".claude/hooks"),
                       hooks, note="rules stated as enforcement code, not prose",
                       reachable="yes",
                       reach_note="binds only where settings.json wires it")
        s = []
        for f in hooks:
            doc = f.read_text(errors="replace")[:4000]
            for line in doc.splitlines():
                t = line.strip().lstrip("#").strip()
                if len(t) > 30 and IMPERATIVE.search(t) and not t.startswith(("import", "from")):
                    s.append(Statement(st.name, str(f), "docstring", t[:600],
                                       last_modified=_mtime(f)))
        statements += s
        st.statements = len(s)

    return statements, stores


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def build_clusters(statements: list[Statement]) -> tuple[list[Cluster], list[Statement]]:
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
    clusters = sorted(buckets.values(),
                      key=lambda c: (-len(c.statements), c.kind))
    return clusters, unclassified


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


def render(clusters: list[Cluster], stores: list[Store],
           unclassified: list[Statement], statements: list[Statement]) -> str:
    total_scanned = len(statements)
    withheld_n = sum(1 for s in statements if s.text == WITHHELD_MARKER)
    clustered = sum(len(c.statements) for c in clusters)
    total_rules = len(clusters)
    dup = (clustered / total_rules) if total_rules else 0
    diverging = [c for c in clusters if c.diverges]
    today = date.today().isoformat()

    L: list[str] = []
    A = L.append

    A("<!-- GENERATED by execution/scripts/render_rule_inventory.py — "
      "do not edit. -->")
    A("<!-- Source: the rule stores themselves, measured. Neotoma PROD "
      "read-only, plus the harness and repository files the store table "
      "names. -->")
    A("")
    A("# The rule inventory: every place a rule is stated")
    A("")
    A(f"**Kind:** foundation companion; generated, never authored. "
      f"**Generated by:** `execution/scripts/render_rule_inventory.py`, held "
      f"equal to the measured system by `--check`. **Measured:** {today}.")
    A("")
    A("Not keyed, not in the kernel, and never inlined into a review prompt: "
      "this document states no rule about how the swarm works. It reports where "
      "the rules that do are written down.")
    A("")
    A("Stage 0 of the rule migration, in the sense `migration.md` already gives "
      "the word: the inventory a migration starts from. It is generated rather "
      "than authored because the prose version this replaces was wrong twice, "
      "both times caught only by re-measuring — Cursor reported as 5 files when "
      "it holds 31, and five lenses reported as citing three foundation files "
      "when they cite five different ones, one consumer each. A hand-count "
      "cannot be diffed and cannot detect its own drift.")
    A("")
    A("**This file records a rule's LOCATION and KIND, never its "
      "operator-specific VALUE.** Both repos are public and several rule "
      "entities carry operator specifics; every statement passes a PII screen "
      f"before emission and a statement that trips it reads *{WITHHELD}*. "
      "Re-running with `--check` re-screens, so a value that lands later fails "
      "the gate rather than shipping.")
    A("")
    A("**It is perishable.** Re-run it; never edit it to keep up. A figure here "
      "without an instrument is a defect in the generator.")
    A("")

    A("## The headline: duplication factor")
    A("")
    A("| Measure | Value |")
    A("|---|---|")
    A(f"| Distinct rules (clusters) | **{total_rules}** |")
    A(f"| Statements of those rules, across all stores | **{clustered}** |")
    A(f"| **Duplication factor** | **{dup:.1f}×** |")
    A(f"| Clusters whose statements DIVERGE on binding force | **{len(diverging)}** |")
    A(f"| Normative statements scanned in total | {total_scanned} |")
    A(f"| …of those, matching no known rule kind | {len(unclassified)} |")
    A(f"| …of those, withheld as operator-specific | {withheld_n} |")
    A(f"| Stores inventoried | {len(stores)} |")
    A("")
    A("The duplication factor is the point. `migration.md` governs the target "
      "shape — *standing rules go to `task_policy` by kind, never by value* — so "
      "one rule stated in fourteen places collapses to ONE entity with fourteen "
      "locations, not fourteen entities. The factor is how much collapsing "
      "there is to do; the divergence count is how much of it needs a ruling "
      "rather than a merge.")
    A("")
    A(f"The factor is computed over the {clustered} statements that match a "
      f"known rule kind, not over all {total_scanned} scanned. The remainder "
      "are procedure, context, or rules whose kind has no signature yet — "
      "counting them would inflate the figure with statements the migration "
      "has nothing to collapse.")
    A("")

    A("## The stores")
    A("")
    A("`populated` is what the store holds; `reachable` is whether it gets to an "
      "agent. They are different questions, and ateles#1118 is why the column "
      "exists: `agent_policy` is fully populated and delivers nothing, because "
      "`agent_loader.py` filters on `agent_sub`, which is empty in every row.")
    A("")
    unread_stores = [s for s in stores if not s.read_ok]
    if unread_stores:
        A(f"> **{len(unread_stores)} store(s) could not be read on this run** and "
          "are listed below as UNREAD. An unread store is NOT an empty one: its "
          "rules are missing from every count on this page, and the counts are "
          "therefore lower bounds. Re-run where the reader has credentials.")
        A("")
    A("| Store | Location | Populated | Statements | Last modified | Reachable |")
    A("|---|---|---|---|---|---|")
    for st in sorted(stores, key=lambda s: -s.statements):
        loc = _portable(st.location)
        if not st.read_ok:
            A(f"| {st.name} | `{loc}` | — | — | — | **UNREAD** |")
            continue
        A(f"| {st.name} | `{loc}` | {st.populated} | {st.statements} | "
          f"{st.last_modified or '—'} | {st.reachable} |")
    A("")
    for st in stores:
        if st.note or st.reach_note or not st.read_ok:
            bits = [b for b in (st.note, st.reach_note) if b]
            if not st.read_ok:
                bits.append(f"**could not be read**: {st.read_error}")
            A(f"- **{st.name}** — {'; '.join(bits)}.")
    A("")

    if diverging:
        A("## Divergence: the same rule, stated differently")
        A("")
        A("The highest-value output. Each row is one rule whose statements do "
          "not agree on how strongly it binds. A consumer's behaviour then "
          "depends on which copy it happens to read, which is the failure "
          "ateles#1115 found in `agent_policy` (two live rows, same safety rule, "
          "one `recommended` and one `mandatory`) and ateles#1121 found between "
          "a foundation file and the lens that cites it. **A divergence needs a "
          "ruling, not a merge** — the migration cannot pick a side on its own.")
        A("")
        A("**A flagged divergence is a candidate, not a verdict.** The test "
          "reads prose, so it cannot tell a rule being STATED from a rule being "
          "DESCRIBED: a sentence explaining that a hook is deliberately "
          "fail-open reads as an advisory statement of the fail-closed rule. "
          "Spot-checked on two clusters at generation time — the consent-gate "
          "row is genuine (`CLAUDE.md` says proceed without asking; an "
          "`agent_policy` row says approval is mandatory), the fail-closed row "
          "is an artifact of exactly that confusion. Each row below needs a "
          "human read of its statements before it is ruled on; the value of the "
          "list is that it is 12 rows rather than 499.")
        A("")
        A("| Rule | Statements | Shapes present | Stores |")
        A("|---|---|---|---|")
        for c in diverging:
            # `c.shapes` excludes "unmarked" -- a statement that mentions the
            # rule without saying how strongly it binds is not evidence of
            # disagreement, and listing it here would suggest it was.
            A(f"| `{c.rule_id}` {c.label} | {len(c.statements)} | "
              f"{', '.join(sorted(c.shapes))} | {', '.join(c.stores)} |")
        A("")

    A("## The clusters")
    A("")
    A("One row per rule; every location it is stated. `agree` means every "
      "statement binds the same way — it does not mean the wording matches, and "
      "it is not a claim that the statements are interchangeable.")
    A("")
    A("| id | Rule | Statements | Stores | Agree? | Target home |")
    A("|---|---|---|---|---|---|")
    for c in clusters:
        home = TARGET_HOME.get(c.kind, "**UNCLASSIFIED**")
        A(f"| `{c.rule_id}` | {c.label} | {len(c.statements)} | "
          f"{len(c.stores)} | {'DIVERGE' if c.diverges else 'agree'} | {home} |")
    A("")

    A("### Where each rule is stated")
    A("")
    for c in clusters:
        A(f"#### `{c.rule_id}` — {c.label}")
        A("")
        A(f"Target home: **{TARGET_HOME.get(c.kind, 'UNCLASSIFIED')}** · "
          f"{len(c.statements)} statements · "
          f"{'**DIVERGE**' if c.diverges else 'agree'}")
        A("")
        A("| Store | Location | At | Statement |")
        A("|---|---|---|---|")
        for s in sorted(c.statements, key=lambda x: (x.store, x.location)):
            A(f"| {s.store} | `{_portable(s.location)}` | {s.locator} | "
              f"{s.safe_text} |")
        A("")

    unmapped = [c for c in clusters if c.kind not in TARGET_HOME]
    A("## What this inventory could not classify")
    A("")
    if unmapped:
        A("Rule kinds with no derivable home in the authority table "
          "(`conformance.md`, *Direction of truth per class of record*). Listed "
          "rather than guessed: inventing a home is the quiet re-decision the "
          "migration exists to prevent.")
        A("")
        for c in unmapped:
            A(f"- `{c.rule_id}` — {c.label}")
    else:
        A("Every rule kind maps to a home in the authority table.")
    A("")
    A(f"{len(unclassified)} statements match no known rule kind. They are "
      "**not** classified into a neighbouring cluster: an over-eager merge would "
      "hide a divergence, which is the one output this inventory exists to "
      "produce. They are procedure, context, or rules whose kind has no "
      "signature yet — adding a signature to `KIND_SIGNATURES` is how the "
      "coverage grows.")
    A("")

    A("## Prior art: where this disagrees with the hand-count it replaces")
    A("")
    A("Sources: the prose inventory on ateles#1114, and the findings on "
      "ateles#1115, #1118 and #1121 that this document's reachability and "
      "divergence columns exist to generalize.")
    A("")
    A("That prose inventory is the input to this one, not a thing it discards: "
      "each of its findings is a claim with a location attached, and several "
      "were found by accident rather than by systematic search. The claims were "
      "re-measured and the locations kept. Where the measurement disagrees, "
      "both figures are given — the disagreements are themselves the argument "
      "for generating the inventory rather than typing it.")
    A("")
    A("| Claim on the prose inventory | Measured here | Reading |")
    A("|---|---|---|")
    A("| OpenClaw holds 1 rule | **0** | The `1` is a directory, not a file. "
      "Beneath it: session state and a vendored Codex home whose shipped skills "
      "are a dependency, not operator rules. Sweeping it yields 21,411 "
      "statements from files the operator never wrote. |")
    A("| Cursor holds 31 files (already corrected once from 5) | **31 "
      "confirmed** | 2 live, 29 dated `.backup.` copies. 26 of the 31 are "
      "symlinks into the neotoma repo, so most of the store is a pointer to a "
      "sibling repo's file rather than a rule of its own. |")
    A("| Project memory: 329 files across 15 dirs | **329 files, 11 dirs** | "
      "14 `memory/` directories exist; 3 hold no `.md` file. |")
    A("| 88 of 97 ateles skills contain rule language | **66 of 96** | "
      "Different instrument: the earlier count matched `do not` case-"
      "insensitively across the whole file. |")
    A("| Five lenses cite five different foundation files, one consumer each | "
      "**confirmed as the store's reachability verdict** | Not re-derived; "
      "cited. See the foundation-repo reconciliation (2026-09-19). |")
    A("| Six rule stores | **16 inventoried** | The store list was a floor. The "
      "additions: `task_policy` entities (a live store, not only a target), "
      "hooks (rules stated as code), skills split by root, and — the largest — "
      "the per-checkout copies below. |")
    A("")
    A("### The store nobody had counted: one instruction file, many checkouts")
    A("")
    A("`CLAUDE.md` is re-injected from disk at every compaction, which is what "
      "makes it the home for standing instructions. The disk it is read from is "
      "the one in the session's own checkout. Measured on this machine: "
      "**205 copies of `ateles/CLAUDE.md` in 26 distinct versions**, and "
      "**138 copies of `neotoma/AGENTS.md` in 4**.")
    A("")
    A("So a rule's reach is not whether it is in `CLAUDE.md` but which copy of "
      "`CLAUDE.md` the reader opened, and the deployment checkouts the daemons "
      "run from (`~/ateles-rc-src`, `~/neotoma-rc-src`) are two more copies "
      "again. This is `docs/foundation/principles.md#1` — a rule that lives in "
      "only one checkout does not bind — measured rather than asserted, and it "
      "is the concrete mechanism behind ateles#973, where a session ran for "
      "hours from a worktree whose `CLAUDE.md` lacked the never-stash rule and "
      "both compaction hooks.")
    A("")

    A("## Method, so a re-run means something")
    A("")
    A("- **Entities** are read field-name agnostically. `standing_rule` text "
      "lives under five different field names and 4 rows carry none; "
      "`agent_policy` uses a different set again. `raw_fragments` is read too, "
      "because `/store` accepts undeclared fields and routes rule text there. A "
      "reader checking one field name drops rows and reports a clean run.")
    A("- **Files** are read per rule, not per file. A bolded-lead bullet is one "
      "rule (the shape `verify_claude_md_merge.py` keys on, so the inventory and "
      "the parity checker agree on what a rule is); so is any other line "
      "carrying an imperative.")
    A("- **Clusters** are by kind, never by value, per `migration.md`. "
      "Divergence is judged on whether statements bind the same way, not on "
      "wording.")
    A("- **Target homes** come from the authority table in `conformance.md` and "
      "from nowhere else.")
    A("- Read-only against Neotoma **prod**. Nothing is written to the record.")
    A("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify the committed inventory matches the system")
    ap.add_argument("--json", metavar="PATH", help="also dump the raw extraction")
    ap.add_argument("--cache", metavar="DIR",
                    help="cache entity reads here (re-run offline)")
    args = ap.parse_args()

    home = Path.home()
    cache = Path(args.cache) if args.cache else None

    ent_stmts, ent_stores = read_entities(cache)
    file_stmts, file_stores = read_file_stores(home)

    statements = ent_stmts + file_stmts
    stores = ent_stores + file_stores
    clusters, unclassified = build_clusters(statements)

    # The gate: no operator value reaches the committed file.
    leaks = []
    for c in clusters:
        for s in c.statements:
            if s.safe_text != WITHHELD:
                ok, why = screen_for_pii(s.safe_text)
                if not ok:
                    leaks.append((s.location, why))
    if leaks:
        print(f"PII SCREEN FAILED on {len(leaks)} emitted statement(s):",
              file=sys.stderr)
        for loc, why in leaks[:10]:
            print(f"  {loc}: {', '.join(why)}", file=sys.stderr)
        return 2

    out = render(clusters, stores, unclassified, statements)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "clusters": [
                {"id": c.rule_id, "kind": c.kind, "label": c.label,
                 "diverges": c.diverges,
                 "target_home": TARGET_HOME.get(c.kind),
                 "statements": [
                     {"store": s.store, "location": s.location,
                      "locator": s.locator, "safe_text": s.safe_text}
                     for s in c.statements]}
                for c in clusters],
            "stores": [vars(s) for s in stores],
            "unclassified_count": len(unclassified),
            "totals": {"rules": len(clusters), "statements": len(statements)},
        }, indent=2))

    if args.check:
        if not OUTPUT.exists():
            print(f"{OUTPUT} does not exist; run without --check", file=sys.stderr)
            return 1
        cur = OUTPUT.read_text()
        # The generation date changes every run and is not drift.
        strip = lambda t: re.sub(r"\*\*Generated \d{4}-\d{2}-\d{2}", "**Generated", t)
        if strip(cur) != strip(out):
            print("rule inventory is stale — re-run "
                  "execution/scripts/render_rule_inventory.py", file=sys.stderr)
            return 1
        print("rule inventory matches the measured system")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(out)
    unread = [s.name for s in stores if not s.read_ok]
    clustered = sum(len(c.statements) for c in clusters)
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}: "
          f"{len(clusters)} rules, {clustered} statements of them "
          f"({len(statements)} scanned), "
          f"{clustered/max(len(clusters),1):.1f}x duplication, "
          f"{sum(1 for c in clusters if c.diverges)} diverging")
    if unread:
        print(f"UNREAD stores: {', '.join(unread)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

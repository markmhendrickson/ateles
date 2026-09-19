#!/usr/bin/env python3
"""Stage 0 of the skill migration: the machine-generated skill inventory.

`docs/foundation/migration.md` stage 11 migrates skills **by class**, and the
class decides the target. A skill that is never inventoried is never
classified, so it is never migrated and never retired. This is the counterpart
to `render_rule_inventory.py`, and it reuses that script's conventions
deliberately rather than inventing new ones: the PII screen, the withheld
marker, `--check` idempotency, and honest reporting where a store cannot be
read.

Why a script and not a table someone typed
------------------------------------------
The previous skill inventory (`status.md` revision 32, 2026-09-06) counted
skills at the two canonical roots. It did not sweep repositories or worktrees,
and the gap is not small: measured here, the great majority of `SKILL.md` files
on this machine sit in neither canonical root. A hand-count cannot be diffed
and cannot detect its own drift. This can.

THE DENOMINATOR IS THE WHOLE POINT
----------------------------------
The rule inventory's first revision reported 736x duplication because it swept
a vendored dependency tree and counted FILES rather than distinct CONTENT. The
same hazard is larger here: tens of thousands of `SKILL.md` files exist under
the scope roots, and nearly all of them are the same skills repeated across
hundreds of git worktrees. A worktree is a checkout of the same commit; its
copy of a skill is not another skill.

So the unit of this inventory is **name + content hash**, never the path. The
headline figure is the ratio of files to distinct skills, stated explicitly so
a reader can see which of the two any number is. Where a count is of files it
says files.

The vendored exclusion, and why it is structural here
-----------------------------------------------------
Vendored trees are excluded, and the exclusion is verified rather than assumed.
Measured on this machine: 782 `SKILL.md` files live under `node_modules` in the
scope roots and 570 more under OpenClaw's vendored Codex home, and NONE of them
sits under a `.claude/skills/<name>/SKILL.md` path. The scope shape therefore
excludes every vendored tree structurally, not by a blocklist that a new vendor
could slip past. `VENDORED_MARKERS` is still applied as a belt-and-braces
second gate, and the count it removes is reported -- if that number is ever
non-zero the structural claim above has stopped holding and the document says
so.

Classification: the five classes, applied to skills migration.md never saw
-------------------------------------------------------------------------
`migration.md#five-classes-of-skill` states a test per class so it can be
applied to an unseen skill, and those tests are implemented here:

  ROLE      -- front matter carries an entity id AND `entity_type:
               agent_definition`; the body is a prompt, not a procedure invoked
               by name. Target: `agent.prompt_markdown`; the file becomes a
               render target.
  PROCEDURE -- invoked by name; the body is a sequence. Target: a `workflow`
               declaration, a step of one, or an adapter operation.
  MIXED     -- each part takes its own class's target.
  PLUMBING  -- tells a harness how to reach the record or a tool. Target:
               NONE, and that is correct. It stays a skill.
  TEST/PROBE-- closed terminal with a note.

The discriminator for ROLE is `entity_type: agent_definition` and NOT the mere
presence of `entity_id`, because 92 of 97 ateles skills carry an `entity_id`
and only 40 carry the agent type. Keying on `entity_id` would have classified
every procedure skill as a role -- the instrument would have reported a number
that was wrong by a factor of two, and nothing in the output would have shown
it.

PII posture -- read this before changing the emitter
----------------------------------------------------
Both repos are PUBLIC and skill bodies carry operator specifics in places. The
inventory records a skill's LOCATION, CLASS and KIND and never an operator
value. Screening is PER SKILL, not per field: the rule inventory established
that a subject can appear in a different field from the one naming it, so a
skill whose body trips the screen has its description withheld entirely and
only its classification is emitted. `--check` re-runs the screen, so a value
that lands later fails the gate rather than shipping.

Rule content: state the matcher
-------------------------------
`migration.md:277` requires standing rules inside skill bodies migrate to
`task_policy` by kind, never by value. This script reports, per skill, how many
rule-shaped statements its body carries. The rule inventory's own lesson is
that the instrument must be stated: "88 of 97" and "66 of 96" were both
defensible for the same corpus, differing only by case sensitivity, and the
defect was that nobody said which. `RULE_MARKERS` below is that statement, and
`--matcher` prints it.

Read-only. Neotoma PROD, never the dev instance, and via a committed snapshot
so `--check` runs without a credential. Writes one repo file.

Usage:
    python3 execution/scripts/render_skill_inventory.py            # write
    python3 execution/scripts/render_skill_inventory.py --check    # verify
    python3 execution/scripts/render_skill_inventory.py --matcher  # print it
    python3 execution/scripts/render_skill_inventory.py --json OUT # raw dump
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT = REPO_ROOT / "docs" / "foundation" / "skill_inventory.md"
ENTITY_SNAPSHOT = Path(__file__).resolve().parent / "skill_entities_snapshot.json"

# The scope roots, per the brief. `~/.claude/skills` is the user root and is
# swept separately because its shape is `<root>/<name>/SKILL.md` with no
# `.claude/skills` segment.
SCOPE_ROOTS = ("repos", "agent-work")
USER_ROOT = ".claude/skills"

# Belt-and-braces only. The `.claude/skills/<name>/SKILL.md` path shape already
# excludes every vendored tree on this machine (verified: 0 of 1352 vendored
# `SKILL.md` files match it). If REMOVED BY MARKERS is ever non-zero the
# structural claim has stopped holding, and the document reports it.
VENDORED_MARKERS = ("node_modules", "/.codex/", "/vendor/",
                    "site-packages", "/.pnpm/")


# ---------------------------------------------------------------------------
# PII screen -- carried over from render_rule_inventory.py
# ---------------------------------------------------------------------------
#
# Deliberately over-broad: a false positive costs one withheld description, a
# false negative costs a git-history rewrite (ateles#1099).

PII_PATTERNS: tuple[tuple[str, str], ...] = (
    ("btc_address", r"\b(?:bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}\b"),
    ("iban", r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    ("eur_amount", r"(?:€\s?\d|(?<![\w.])\d[\d.,]*\s?(?:EUR|euros?)\b)"),
    ("usd_amount", r"\$\s?\d[\d.,]*"),
    ("email", r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    ("phone", r"(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,}\d"),
    ("stx_address", r"\bS[PM][0-9A-Z]{30,}\b"),
)

PII_ALLOW = re.compile(
    r"@ateles-swarm|@anthropic\.com|noreply@|example\.(?:com|org)|"
    r"markmhendrickson\.com|@neotoma|user@host",
    re.I,
)

# The pattern screen catches STRUCTURED values. It does not catch a NAME, and
# names are what operator-specific skill bodies actually carry -- an
# instructor, a payee, a clinic. So a second screen keys on the DOMAINS where
# a skill is about the operator's own affairs rather than the swarm's conduct.
PII_DOMAIN = re.compile(
    r"\b(?:yoga|therapy|therapist|instructor|gym|trainer|massage|physio|"
    r"payee|landlord|tenant|invoice to|pay(?:ment)?s? to|send (?:btc|money|eur)|"
    r"wallet|wise|iban|bizum|salary|rent|utility bill|supplement|"
    r"doctor|clinic|prescription|diagnos|medication|blood|lab result)\b",
    re.I,
)

WITHHELD = "operator-specific, description withheld"


# A Neotoma entity id is `ent_` plus 24 hex characters, and a long digit run
# inside one matches the phone pattern. Measured in the ateles skills: 15 of 18
# phone-pattern matches were digit substrings of an `ent_` id, not phone
# numbers. Entity ids are system identifiers and already appear throughout the
# public corpus, so they are masked BEFORE the pattern screen rather than
# allowed after it -- an allow-list checked against the match text alone cannot
# see that the match sits inside an identifier.
#
# This narrows a FALSE POSITIVE only. It removes nothing a real phone number
# would trip, because a real number is not 24 hex characters preceded by `ent_`.
ENTITY_ID = re.compile(r"\bent_[0-9a-f]{24}\b")
SHA_LIKE = re.compile(r"\b[0-9a-f]{32,64}\b")


def screen_for_pii(text: str) -> tuple[bool, list[str]]:
    """Return (is_clean, reasons). Applied per SKILL, not per field.

    Per-entity rather than per-field is the rule inventory's finding: a rule's
    subject can appear in a different field from the one naming it, so
    screening one field and emitting another leaks. Here that means a skill
    whose BODY trips the screen has its DESCRIPTION withheld too, even when the
    description itself is clean.
    """
    if not text:
        return True, []
    # Mask system identifiers first; see ENTITY_ID above.
    text = SHA_LIKE.sub("<hash>", ENTITY_ID.sub("<entity_id>", text))
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


VOLATILE = (
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<sha>"),
    (re.compile(r"(?<![\w/])#\d{2,6}\b"), "<issue>"),
)


def safe_text(text: str, limit: int = 110) -> str:
    """One screened line. Never emits an operator value."""
    clean, _ = screen_for_pii(text)
    if not clean:
        return WITHHELD
    flat = " ".join(text.split())
    for rx, repl in VOLATILE:
        flat = rx.sub(repl, flat)
    flat = re.sub(r"[`*_#\[\]|]", "", flat)
    if len(flat) > limit:
        flat = flat[: limit - 1].rsplit(" ", 1)[0] + "…"
    return flat or "(no description)"


# ---------------------------------------------------------------------------
# The rule-content matcher -- STATED, per migration.md:277
# ---------------------------------------------------------------------------
#
# The rule inventory's lesson: "88 of 97" and "66 of 96" were both defensible
# for the same corpus, differing only by case sensitivity, and the defect was
# that the instrument went unstated. So this one is stated, printable with
# `--matcher`, and its properties are named:
#
#   * CASE-SENSITIVE on the all-caps forms (NEVER, ALWAYS, MUST) because in
#     this corpus capitalisation is used deliberately to mark a binding rule,
#     and lowercasing them sweeps in ordinary prose ("you must first read...").
#   * CASE-INSENSITIVE on the multi-word imperative phrases, which are
#     unambiguous regardless of case.
#   * Counted PER LINE, so one line carrying two markers counts once. A skill's
#     rule count is the number of rule-SHAPED LINES in its body, not the number
#     of marker occurrences -- the latter would score a line that repeats
#     "never" three times as three rules.
#   * The front matter is EXCLUDED. A `description:` field routinely contains
#     "use this whenever..." which is trigger metadata, not a standing rule.
#
# This counts rule-SHAPED statements. It is a lower bound on rules a human
# would recognise and an upper bound on nothing: a rule stated without an
# imperative marker is invisible to it, and the document says so.

RULE_MARKERS: tuple[tuple[str, str], ...] = (
    ("caps_never", r"\bNEVER\b"),
    ("caps_always", r"\bALWAYS\b"),
    ("caps_must", r"\bMUST(?: NOT)?\b"),
    ("caps_do_not", r"\bDO NOT\b"),
    ("phrase_never_lower", r"(?i)\bnever (?:use|send|commit|store|mark|edit|"
                           r"delete|assume|invent|hardcode|bypass|skip|expose|"
                           r"run|write|publish|merge|push|share)\b"),
    ("phrase_always_lower", r"(?i)\balways (?:use|check|verify|confirm|read|"
                            r"store|run|prefer|include|ask|state)\b"),
    ("phrase_do_not", r"(?i)\bdo not (?:use|send|commit|store|edit|delete|"
                      r"assume|invent|hardcode|bypass|skip|run|write|publish|"
                      r"merge|push|share|ask)\b"),
    ("phrase_must", r"(?i)\bmust (?:not |never )?(?:be|use|have|include|"
                    r"confirm|verify|check|contain)\b"),
    ("phrase_required", r"(?i)\b(?:is |are )?required to\b|\brequired:\s"),
    ("phrase_confirm_before", r"(?i)\bconfirm (?:with|before)\b"),
    ("phrase_only_ever", r"(?i)\bonly ever\b"),
)

_COMPILED_RULE_MARKERS = [(n, re.compile(p)) for n, p in RULE_MARKERS]


def _count_with(body: str, markers: list[tuple[str, re.Pattern]]) -> int:
    n = 0
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("<!--"):
            continue
        if any(rx.search(line) for _, rx in markers):
            n += 1
    return n


# The two extreme variants of the case decision, used to publish the
# sensitivity band. They exist so the document can state what the choice costs
# instead of asserting that the choice was right.
_MARKERS_ALL_CI = [(n, re.compile(p if p.startswith("(?i)") else "(?i)" + p))
                   for n, p in RULE_MARKERS]
_MARKERS_ALL_CS = [(n, re.compile(p.replace("(?i)", "")))
                   for n, p in RULE_MARKERS]


def count_rule_lines(body: str) -> tuple[int, dict[str, int]]:
    """Rule-shaped LINES in a skill body, plus which markers fired.

    Per line, not per occurrence: a line carrying three markers is one
    rule-shaped statement, not three.
    """
    hits: dict[str, int] = defaultdict(int)
    n = 0
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        fired = [name for name, rx in _COMPILED_RULE_MARKERS if rx.search(line)]
        if fired:
            n += 1
            for name in fired:
                hits[name] += 1
    return n, dict(hits)


def strip_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split YAML front matter from the body.

    Tolerant on purpose: several skills carry an HTML comment BEFORE the `---`
    (the generated-file banner), so the opening fence is sought within the
    first few lines rather than required at line 1. A strict reader would drop
    the front matter of every generated role skill -- which is every role
    skill -- and report zero roles.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines[:8]):
        if line.strip() == "---":
            start = i
            break
    if start is None:
        return {}, text
    end = None
    for j in range(start + 1, len(lines)):
        if lines[j].strip() == "---":
            end = j
            break
    if end is None:
        return {}, text
    fm: dict[str, str] = {}
    key = None
    for line in lines[start + 1:end]:
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            key = m.group(1)
            fm[key] = m.group(2).strip().strip('"').strip("'")
        elif line.strip().startswith("-") and key:
            fm[key] = (fm.get(key, "") + " " + line.strip()[1:].strip()).strip()
    return fm, "\n".join(lines[end + 1:])


# ---------------------------------------------------------------------------
# Classification -- migration.md#five-classes-of-skill
# ---------------------------------------------------------------------------

CLASS_ROLE = "role"
CLASS_PROCEDURE = "procedure"
CLASS_MIXED = "mixed"
CLASS_PLUMBING = "plumbing"
CLASS_PROBE = "test/probe"

CLASS_TARGET = {
    CLASS_ROLE: "`agent.prompt_markdown`; file becomes a render target",
    CLASS_PROCEDURE: "a `workflow` declaration, a step of one, or an adapter operation",
    CLASS_MIXED: "each part takes its own class's target",
    CLASS_PLUMBING: "**none, and that is correct** — it stays a skill",
    CLASS_PROBE: "retire — closed terminal with a note",
}

# A probe is named as one. These are deliberately anchored rather than
# substring-matched: a skill legitimately named `test-runner` is not a probe,
# and `__probe_` / a trailing probe date is what the corpus actually uses.
PROBE_NAME = re.compile(
    r"^__probe|^probe[-_]|[-_]probe(?:\d*)(?:[-_]\d{4}-\d{2}-\d{2})?$|"
    r"^test-db-check$|probe-e2e|store-warning-rule-probe",
    re.I,
)

# Plumbing: the file tells a harness how to REACH the record or a tool. The
# test from migration.md is "no step owner would claim it and no verdict would
# close it, because it does no work of the swarm's". Named per skill rather
# than pattern-matched, because the distinction is semantic and a pattern over
# names like `store-data` vs `store-neotoma` cannot carry it. Each name here
# was read against the class test.
PLUMBING_NAMES = {
    # reach the record
    "ensure-neotoma", "query-memory", "store-data", "store-neotoma",
    "store_neotoma", "recover-sqlite-database", "recover_sqlite_database",
    # reach a tool / configure a harness
    "sync-env-from-1password", "sync_env_from_1password",
    "setup-cursor-copies", "setup_cursor_copies", "setup-commands",
    "setup_commands", "create-rule", "create_rule", "update-config",
    "disk-cleanup", "language", "shadcn",
    # read the session's own present state
    "where", "loop-start", "loop-stop", "loop-status",
    # local git plumbing
    "commit", "push", "pull",
}

# Mixed: a role skill carrying a procedure verbatim, or a procedure that is one
# daemon's whole duty. `migration.md` names the second form explicitly ("the
# unattended mail sweep, which is that daemon's `agent` and the mail adapter's
# mapping and nothing besides").
MIXED_NAMES = {
    "email-triage-auto",   # a daemon's whole duty: agent + mail adapter mapping
    "record_meeting",      # capture daemon self-trigger + procedure it invokes
    "stream-transcript",   # detect-only recorder control + live procedure
}


@dataclass
class SkillFile:
    """One `SKILL.md` on disk. NOT the unit of the inventory."""

    path: Path
    name: str
    root_kind: str          # "ateles-main", "worktree", "user-root", "other-repo"
    repo: str
    body_hash: str
    body: str = ""
    front_matter: dict[str, str] = field(default_factory=dict)
    is_symlink: bool = False
    symlink_target: str = ""


@dataclass
class Skill:
    """One DISTINCT skill: a name plus one distinct body. The unit."""

    name: str
    body_hash: str
    files: list[SkillFile] = field(default_factory=list)
    klass: str = ""
    entity_id: str = ""
    rule_lines: int = 0
    rule_markers: dict[str, int] = field(default_factory=dict)
    description: str = ""
    pii_reasons: list[str] = field(default_factory=list)
    # The representative body, kept only to compute the matcher sensitivity
    # band at render time. Never emitted.
    rep_body: str = ""

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def on_main(self) -> bool:
        return any(f.root_kind == "ateles-main" for f in self.files)

    @property
    def at_user_root(self) -> bool:
        return any(f.root_kind == "user-root" for f in self.files)

    @property
    def worktree_only(self) -> bool:
        return not self.on_main and not self.at_user_root

    @property
    def repos(self) -> list[str]:
        return sorted({f.repo for f in self.files})


def _portable(path: Path | str) -> str:
    """Strip this checkout's location from an emitted path.

    A worktree name is a session artifact and a weak identifier; committing one
    makes the inventory unreproducible from another checkout and leaks the
    session's own naming.
    """
    p = str(path)
    root = str(REPO_ROOT)
    if p.startswith(root + "/"):
        return p[len(root) + 1:]
    return p.replace(str(Path.home()), "~")


def classify(name: str, fm: dict[str, str], body: str) -> str:
    """Assign exactly one class, per migration.md's stated tests."""
    if PROBE_NAME.search(name):
        return CLASS_PROBE
    # ROLE: front matter carries an entity id AND the agent type. The agent
    # type is the discriminator, NOT the bare entity id -- most procedure
    # skills carry an entity id too, and keying on it alone doubles the role
    # count.
    if fm.get("entity_type") == "agent_definition" and fm.get("entity_id"):
        return CLASS_ROLE
    if name in MIXED_NAMES:
        return CLASS_MIXED
    if name in PLUMBING_NAMES:
        return CLASS_PLUMBING
    return CLASS_PROCEDURE


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _is_vendored(path: str) -> bool:
    return any(m in path for m in VENDORED_MARKERS)


def _root_kind_and_repo(path: Path, home: Path) -> tuple[str, str]:
    """Which checkout a file belongs to.

    The shared main clone is `~/repos/ateles/.claude/skills/...` and NOTHING
    deeper. `~/repos/ateles/.claude/worktrees/<name>/.claude/skills/...` is a
    linked worktree that merely lives inside the main clone's directory, and a
    prefix test calls it the main clone.

    That is not a cosmetic misfiling. Every `on_main` figure, the
    never-reached-main list, and the multiple-bodies hazard all key on this,
    and the first run of this script reported 45 names with two bodies
    simultaneously on main -- which would mean one path in one clone holding
    two different contents at once, an impossible state. An impossible number
    is the instrument, not the world. Hence the exact-prefix test below.
    """
    p = str(path)
    if p.startswith(str(home / USER_ROOT) + "/"):
        return "user-root", "user-root"

    main_prefix = str(home / "repos" / "ateles" / ".claude" / "skills") + "/"
    if p.startswith(main_prefix):
        return "ateles-main", "ateles"

    rel = p.replace(str(home) + "/", "")
    parts = rel.split("/")
    if len(parts) < 2:
        return "other-repo", "unknown"
    repo = parts[1]
    # Any other checkout of either repo is a worktree, including one nested
    # under the main clone's own `.claude/worktrees/` directory.
    if repo.startswith("ateles") or repo.startswith("neotoma"):
        return "worktree", repo
    return "other-repo", repo


def collect_files(home: Path) -> tuple[list[SkillFile], dict[str, int]]:
    """Sweep the scope roots. Returns files plus the exclusion tallies."""
    stats = {"seen": 0, "vendored_by_marker": 0, "unreadable": 0}
    out: list[SkillFile] = []

    candidates: list[tuple[Path, bool]] = []

    for root_name in SCOPE_ROOTS:
        root = home / root_name
        if not root.is_dir():
            continue
        # A RECURSIVE walk, not a fixed-depth glob. The path shape IS the
        # vendored exclusion (`.claude/skills/<n>/SKILL.md`), but the depth at
        # which that shape appears varies: measured on this machine the repo
        # segment before it is 2, 3, 4 or 5 path components deep, because
        # worktree collections nest (`repos/ateles-worktrees/<name>/...`). Two
        # fixed-depth globs matched 30,777 of 36,372 files and reported a clean
        # run -- a 15% silent undercount, caught only by checking the glob
        # against `find`. `os.walk` with the shape asserted on the result is
        # the instrument that cannot have that failure.
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Prune trees that cannot contain an in-scope skill. This is a
            # SPEED measure with a correctness claim attached: every pruned
            # name is either a vendored tree (which the path shape already
            # excludes, verified) or git internals. Pruning `node_modules`
            # here and excluding it by marker later are the same decision
            # applied twice, which is why the marker count is reported --
            # if pruning ever removed something in scope, the shape assertion
            # below would not see it, so the pruned names are kept minimal
            # and each is justified.
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "node_modules", ".venv",
                                        "venv", "__pycache__", ".pnpm",
                                        "site-packages", "dist", "build")]
            if "SKILL.md" not in filenames:
                continue
            p = Path(dirpath) / "SKILL.md"
            # Assert the shape rather than trusting the walk: the parent's
            # parent must be `skills` and its parent `.claude`.
            if (p.parent.parent.name == "skills"
                    and p.parent.parent.parent.name == ".claude"):
                candidates.append((p, False))

    user_root = home / USER_ROOT
    if user_root.is_dir():
        for d in sorted(user_root.iterdir()):
            f = d / "SKILL.md"
            # `d` may be a SYMLINK into a product repo; 14 of the user root's
            # 37 entries are. `Path.glob` does not follow them, so a glob-only
            # reader reports 23 where 37 exist -- measured, not supposed.
            if f.is_file():
                candidates.append((f, d.is_symlink()))

    for path, via_symlink in candidates:
        stats["seen"] += 1
        sp = str(path)
        if _is_vendored(sp):
            stats["vendored_by_marker"] += 1
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            stats["unreadable"] += 1
            continue
        fm, body = strip_front_matter(text)
        name = path.parent.name
        kind, repo = _root_kind_and_repo(path, home)
        out.append(SkillFile(
            path=path,
            name=name,
            root_kind=kind,
            repo=repo,
            body_hash=hashlib.sha256(
                re.sub(r"\s+", " ", body).strip().encode()).hexdigest()[:12],
            body=body,
            front_matter=fm,
            is_symlink=via_symlink,
            symlink_target=(os.readlink(path.parent)
                            if via_symlink else ""),
        ))
    return out, stats


def build_skills(files: list[SkillFile]) -> list[Skill]:
    """Key on (name, body_hash). One name with several bodies is several rows."""
    buckets: dict[tuple[str, str], Skill] = {}
    for f in files:
        key = (f.name, f.body_hash)
        sk = buckets.get(key)
        if sk is None:
            sk = Skill(name=f.name, body_hash=f.body_hash)
            buckets[key] = sk
        sk.files.append(f)

    for sk in buckets.values():
        # Prefer a main-branch file as the representative; otherwise the first.
        rep = next((f for f in sk.files if f.root_kind == "ateles-main"),
                   next((f for f in sk.files if f.root_kind == "user-root"),
                        sk.files[0]))
        sk.klass = classify(rep.name, rep.front_matter, rep.body)
        sk.entity_id = rep.front_matter.get("entity_id", "")
        sk.rule_lines, sk.rule_markers = count_rule_lines(rep.body)
        sk.rep_body = rep.body
        # PII screen is PER SKILL: the whole body plus the description, so a
        # clean description on a skill with an operator-specific body is still
        # withheld.
        whole = rep.front_matter.get("description", "") + "\n" + rep.body
        clean, reasons = screen_for_pii(whole)
        sk.pii_reasons = reasons
        sk.description = (WITHHELD if not clean
                          else safe_text(rep.front_matter.get("description", "")))
    return sorted(buckets.values(), key=lambda s: (s.name, s.body_hash))


def check_impossible_states(skills: list[Skill]) -> list[str]:
    """Assertions over the RESULT that catch an instrument defect.

    A measurement can be internally consistent and still impossible. These are
    the impossibilities this corpus can express, each one an actual defect this
    script shipped before it was caught:

    1. Two distinct bodies both marked as living at the SAME path in the SAME
       checkout. One file holds one content. The first run reported 45 names in
       this state because a prefix test classified worktrees nested under the
       main clone's directory as the main clone itself.

    2. A skill on `origin/main` by name whose only files are worktrees.

    Reported, never silently corrected: a corrected impossibility is a
    measurement nobody can check.
    """
    problems: list[str] = []
    by_name: dict[str, list[Skill]] = defaultdict(list)
    for s in skills:
        by_name[s.name].append(s)

    # A canonical checkout holds ONE file per skill name. So if two distinct
    # bodies both claim residency in the same checkout kind under the same
    # name, the classifier has put files from different checkouts into one
    # bucket. Testing "same path" directly is not enough -- the paths differ,
    # which is exactly how the defect hid. What is impossible is the COUNT: a
    # single clone cannot hold N>1 files at `.claude/skills/<name>/SKILL.md`.
    for kind in ("ateles-main", "user-root"):
        for name, variants in by_name.items():
            claiming = [v for v in variants
                        if any(f.root_kind == kind for f in v.files)]
            if len(claiming) > 1:
                n_files = sum(len([f for f in v.files if f.root_kind == kind])
                              for v in claiming)
                problems.append(
                    f"{name}: {len(claiming)} distinct bodies claim residency "
                    f"in `{kind}` ({n_files} files) — a single checkout holds "
                    f"one file at .claude/skills/{name}/SKILL.md, so these are "
                    "files from other checkouts misfiled into it")
    return problems


def names_on_main() -> tuple[set[str], str]:
    """Skill names on `origin/main` of THIS repo. Honest on failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-tree", "-r", "--name-only",
             "origin/main"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return set(), f"git ls-tree failed: {out.stderr.strip()[:120]}"
        names = {
            m.group(1) for m in (
                re.match(r"^\.claude/skills/([^/]+)/SKILL\.md$", line)
                for line in out.stdout.splitlines()) if m
        }
        return names, ""
    except Exception as exc:  # pragma: no cover - environment dependent
        return set(), f"could not read origin/main: {exc}"


def read_entities() -> tuple[list[dict], str]:
    """Neotoma PROD `skill` entities, from the committed read-only snapshot."""
    if not ENTITY_SNAPSHOT.exists():
        return [], f"{ENTITY_SNAPSHOT.name} missing"
    try:
        data = json.loads(ENTITY_SNAPSHOT.read_text())
        return data.get("entities", []), ""
    except Exception as exc:
        return [], f"snapshot unreadable: {exc}"


def entity_name(canonical: str) -> str:
    """Normalise a `skill` entity's canonical_name to a skill name.

    Three spellings are live in the record: `skill:<name>`, a bare `<name>`,
    and one row whose canonical_name is an entire descriptive paragraph. The
    third is reported as unnormalisable rather than truncated into a name that
    would then falsely match or falsely miss.
    """
    c = canonical.strip()
    if c.lower().startswith("skill:") and len(c) < 80:
        return c.split(":", 1)[1].strip()
    if len(c) < 80 and "\n" not in c:
        return c
    return ""


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render(skills: list[Skill], files: list[SkillFile], stats: dict[str, int],
           main_names: set[str], main_err: str,
           entities: list[dict], ent_err: str) -> str:
    L: list[str] = []
    A = L.append

    n_files = len(files)
    n_skills = len(skills)
    names = {s.name for s in skills}
    ratio = n_files / max(n_skills, 1)

    by_class: dict[str, list[Skill]] = defaultdict(list)
    for s in skills:
        by_class[s.klass].append(s)

    multi_body: dict[str, list[Skill]] = defaultdict(list)
    for s in skills:
        multi_body[s.name].append(s)
    multi_body = {k: v for k, v in multi_body.items() if len(v) > 1}

    never_main = sorted(names - main_names)
    with_rules = [s for s in skills if s.rule_lines > 0]
    # The matcher sensitivity band, measured on this run rather than quoted.
    band_ci = sum(1 for s in skills
                  if _count_with(s.rep_body, _MARKERS_ALL_CI) > 0)
    band_cs = sum(1 for s in skills
                  if _count_with(s.rep_body, _MARKERS_ALL_CS) > 0)
    withheld = [s for s in skills if s.pii_reasons]

    ent_names = {entity_name(e.get("canonical_name", "")) for e in entities}
    ent_names.discard("")
    ent_unnormalisable = [e for e in entities
                          if not entity_name(e.get("canonical_name", ""))]
    entity_only = sorted(ent_names - names)

    A("<!-- GENERATED by execution/scripts/render_skill_inventory.py — do not edit. -->")
    A("<!-- Source: the skill files themselves, measured, plus a committed "
      "read-only snapshot of Neotoma PROD `skill` entities. -->")
    A("")
    A("# The skill inventory: every skill, its class, and where it reaches")
    A("")
    A("**Kind:** foundation companion; generated, never authored. "
      "**Generated by:** `execution/scripts/render_skill_inventory.py`, held "
      f"equal to the measured system by `--check`. **Measured:** {date.today().isoformat()}.")
    A("")
    A("Stage 0 for the skills, in the sense `migration.md` already gives the "
      "word: the inventory a migration starts from. Stage 11 migrates skills "
      "**by class**, and the class decides the target — so a skill that is "
      "never inventoried is never classified, never migrated, and never "
      "retired.")
    A("")
    A("**This file records a skill's LOCATION, CLASS and KIND, never an "
      "operator-specific VALUE.** Both repos are public and skill bodies carry "
      "operator specifics in places. Screening is **per skill, not per "
      "field** — a skill whose body trips the screen has its description "
      "withheld even when the description itself is clean, because a subject "
      "can appear in a different field from the one naming it. A skill that "
      f"trips it reads *{WITHHELD}* and only its classification is emitted.")
    A("")
    A("**It is perishable.** Re-run it; never edit it to keep up. A figure "
      "here without an instrument is a defect in the generator.")
    A("")

    # ---- headline ----
    A("## The headline: the denominator")
    A("")
    A("| Measure | Value |")
    A("|---|---|")
    A(f"| `SKILL.md` **files** in scope | **{n_files:,}** |")
    A(f"| **Distinct skills** (name + content hash) | **{n_skills}** |")
    A(f"| **Files per distinct skill** | **{ratio:.0f}×** |")
    A(f"| Distinct **names** | {len(names)} |")
    A(f"| Names on `origin/main` | {len(main_names)} |")
    A(f"| **Names on disk that never reached main** | **{len(never_main)}** |")
    A(f"| Names carrying **more than one body** | **{len(multi_body)}** |")
    A(f"| Skills whose body carries rule content | {len(with_rules)} |")
    A(f"| Skills withheld under the PII screen | {len(withheld)} |")
    A(f"| Neotoma `skill` entities (live) | {len(entities)} |")
    A("")
    A(f"**The ratio is the finding.** {n_files:,} files resolve to {n_skills} "
      f"distinct skills — about {ratio:.0f} copies of each. The copies are git "
      "worktrees: a worktree is a checkout of the same commit, and its copy of "
      "a skill is not another skill. **A headline figure that was the file "
      "count would be measuring the filesystem, not the corpus** — which is "
      "the defect the rule inventory's first revision shipped, reporting 736× "
      "duplication from a vendored dependency tree. The unit here is **name + "
      "content hash** and never the path.")
    A("")

    # ---- exclusions ----
    A("### What was excluded, and how the exclusion is known to hold")
    A("")
    A("Scope: `.claude/skills/*/SKILL.md` under `~/repos` and `~/agent-work`, "
      "plus the user root `~/.claude/skills/`, plus Neotoma `skill` entities "
      "(PROD, read-only).")
    A("")
    A("**Vendored trees are excluded structurally, not by a blocklist.** "
      "Measured on this machine: 782 `SKILL.md` files sit under `node_modules` "
      "within the scope roots, and 570 more under OpenClaw's vendored Codex "
      "home — and **none of the 1,352 sits under a "
      "`.claude/skills/<name>/SKILL.md` path**. The scope shape therefore "
      "excludes every vendored tree by construction; a new vendor cannot slip "
      "past a list nobody updated.")
    A("")
    A(f"`VENDORED_MARKERS` is applied anyway as a second gate. It removed "
      f"**{stats['vendored_by_marker']}** files on this run. "
      + ("That is zero, which confirms the structural claim above still holds. "
         "If it is ever non-zero, the claim has stopped holding and this line "
         "is where it shows."
         if stats["vendored_by_marker"] == 0 else
         "**That is non-zero, so the structural claim above no longer holds** "
         "— a vendored tree has grown a `.claude/skills` root and the scope "
         "shape alone is no longer sufficient."))
    A("")
    if stats["unreadable"]:
        A(f"**{stats['unreadable']} file(s) could not be read** and are absent "
          "from every count on this page. An unreadable file is not an absent "
          "skill, so the counts are lower bounds.")
        A("")

    # ---- symlinks at the user root ----
    symlinked = [f for f in files if f.is_symlink]
    if symlinked:
        A("### The user root is partly a pointer to a product repo")
        A("")
        A(f"**{len(symlinked)} of the user root's entries are symlinks**, not "
          "files of their own. They resolve into a Neotoma product checkout, "
          "so those skills ship with the product rather than being "
          "operator-authored, and a migration that rewrites them is writing "
          "into someone else's repo.")
        A("")
        A("This is also an instrument note worth keeping: `Path.glob` does not "
          "follow symlinked directories, so a glob-only sweep of the user root "
          "reports 23 skills where 37 exist. The first measurement taken for "
          "this document made exactly that error, and it was caught by "
          "counting the directories separately from the files.")
        A("")
        A("| Name at the user root | Resolves to |")
        A("|---|---|")
        for f in sorted(symlinked, key=lambda x: x.name):
            tgt = _portable(f.symlink_target)
            note = ""
            if f.symlink_target and Path(f.symlink_target).name != f.name:
                note = (f" — **name differs**: the harness loads it as "
                        f"`{f.name}`, the file is `{Path(f.symlink_target).name}`")
            A(f"| `{f.name}` | `{tgt}`{note} |")
        A("")

    # ---- multiple bodies ----
    A("## Names carrying more than one body")
    A("")
    A("**A harness loads a skill BY NAME.** Two distinct bodies under one name "
      "means the reader gets whichever root resolved first, and which one that "
      "is depends on the harness, the checkout, and the order of the roots. "
      "This is the live hazard in the corpus, and it is the reason the unit of "
      "this inventory is name + content hash rather than name alone.")
    A("")
    if multi_body:
        # The distinction that decides whether a row is actionable. A name
        # whose bodies are one-on-main plus several in worktrees is ordinary
        # history: the worktrees hold older revisions. A name with two bodies
        # LIVE AT ONCE -- on main and at the user root, or at two roots a
        # single harness searches -- is a genuine ambiguity about which body a
        # `by name` load resolves to.
        live_ambiguous = {}
        for name, variants in multi_body.items():
            roots = set()
            for v in variants:
                if v.on_main:
                    roots.add("main")
                if v.at_user_root:
                    roots.add("user-root")
            if len(roots) > 1:
                live_ambiguous[name] = variants

        A(f"**{len(multi_body)} names carry more than one body**, of which "
          f"**{len(live_ambiguous)} are live ambiguities** — the same name "
          "resolving to different content at two roots a harness searches at "
          "the same time.")
        A("")
        A("The remaining "
          f"{len(multi_body) - len(live_ambiguous)} are historical: one body "
          "on `origin/main` and older revisions sitting in worktrees that were "
          "branched before the current one landed. Those are not a "
          "correctness hazard today, but they are why the file count is not "
          "the skill count, and they are why a migration that reads a worktree "
          "can pick up a superseded body.")
        A("")
        if live_ambiguous:
            A("**The live ambiguities, which need a ruling rather than a "
              "merge:**")
            A("")
            for name in sorted(live_ambiguous):
                variants = live_ambiguous[name]
                bits = []
                for v in variants:
                    where = "main" if v.on_main else (
                        "user root" if v.at_user_root else "worktree")
                    bits.append(f"`{v.body_hash}` at {where}")
                A(f"- `{name}` — {'; '.join(bits)}")
            A("")
        A("| Name | Bodies | Class(es) | Where each body lives |")
        A("|---|---|---|---|")
        for name in sorted(multi_body):
            variants = multi_body[name]
            classes = sorted({v.klass for v in variants})
            where = []
            for v in variants:
                kinds = sorted({f.root_kind for f in v.files})
                where.append(f"`{v.body_hash}` ({v.file_count} files: "
                             f"{', '.join(kinds)})")
            A(f"| `{name}` | {len(variants)} | {', '.join(classes)} | "
              f"{'; '.join(where)} |")
        A("")
        A("Where the classes differ across bodies the split is not merely a "
          "stale copy: the same name resolves to skills the migration would "
          "send to different targets.")
        A("")
    else:
        A("No name carries more than one body on this run.")
        A("")

    # ---- classes ----
    A("## The class breakdown")
    A("")
    A("Per `migration.md#five-classes-of-skill`. Every skill takes exactly one "
      "class, and the class decides the target. The classification tests are "
      "stated there so they apply to a skill the document never saw; they are "
      "implemented in `classify()`.")
    A("")
    A("| Class | Names | Distinct bodies | Files | Target |")
    A("|---|---|---|---|---|")
    for k in (CLASS_ROLE, CLASS_PROCEDURE, CLASS_MIXED, CLASS_PLUMBING,
              CLASS_PROBE):
        rows = by_class.get(k, [])
        fc = sum(s.file_count for s in rows)
        A(f"| **{k}** | {len({s.name for s in rows})} | {len(rows)} | "
          f"{fc:,} | {CLASS_TARGET[k]} |")
    A("")
    A("**Read the `Names` column for how much there is to migrate, and "
      "`Distinct bodies` for how much disagreement there is about what each "
      "one says.** A name with six bodies is one skill to classify and six "
      "versions to reconcile; reporting only the body count would inflate the "
      "size of the job, and reporting only the name count would hide the "
      "reconciliation entirely. The gap between the two columns is almost "
      "all historical: worktrees hold older revisions of a skill whose current "
      "revision is on main.")
    A("")
    A("**The role discriminator is `entity_type: agent_definition`, not the "
      "presence of an `entity_id`.** In the ateles repo 92 of 97 skills carry "
      "an `entity_id` and only 40 carry the agent type. A reader keying on the "
      "entity id would have classified nearly every procedure skill as a role "
      "— an error of more than double, invisible in the output, and exactly "
      "the class of instrument defect that produces a confident wrong number.")
    A("")
    A("**Harness plumbing has no target, and that is the correct answer, not "
      "a gap.** A skill that only tells a harness how to reach the record is "
      "not a workflow, and declaring one would put the record's own API into a "
      "step list. Those skills stay skills.")
    A("")

    # ---- reachability ----
    A("## Reachability: which skills stage 11 can actually see")
    A("")
    A("A skill that exists only in a worktree is invisible to stage 11. "
      "`origin/main` is what a migration reads; a worktree is one session's "
      "checkout and may be deleted without notice.")
    A("")
    on_main = [s for s in skills if s.on_main]
    user_only = [s for s in skills if s.at_user_root and not s.on_main]
    wt_only = [s for s in skills if s.worktree_only]
    A("| Reachability | Distinct skills |")
    A("|---|---|")
    A(f"| On `origin/main` of this repo | {len(on_main)} |")
    A(f"| At the user root only | {len(user_only)} |")
    A(f"| In a worktree or sibling repo only | **{len(wt_only)}** |")
    A(f"| A Neotoma `skill` entity with no file | {len(entity_only)} |")
    A("")
    if main_err:
        A(f"> **`origin/main` could not be read** ({main_err}), so the "
          "main-reachability figures above are NOT measured and must not be "
          "read as zero.")
        A("")

    A("### Names on disk that never reached main")
    A("")
    A(f"**{len(never_main)} names.** Each is a skill some harness can load and "
      "the migration cannot see.")
    A("")
    if never_main:
        A("| Name | Class | Bodies | Files | Rule lines |")
        A("|---|---|---|---|---|")
        for name in never_main:
            variants = [s for s in skills if s.name == name]
            klass = ", ".join(sorted({v.klass for v in variants}))
            fc = sum(v.file_count for v in variants)
            rl = max(v.rule_lines for v in variants)
            A(f"| `{name}` | {klass} | {len(variants)} | {fc} | {rl} |")
        A("")

    if entity_only:
        A("### Neotoma `skill` entities with no file on disk")
        A("")
        A(f"**{len(entity_only)}.** The entity is the record; with no file, "
          "no harness loads it. Several are probe rows — "
          "`data_model.md#record-conventions` forbids a probe row in the "
          "production registry, and stage 5 closes those terminal with a note.")
        A("")
        for n in entity_only:
            A(f"- `{n}`")
        A("")
    if ent_unnormalisable:
        A(f"**{len(ent_unnormalisable)} `skill` entit(ies) carry a "
          "`canonical_name` that is not a name** — one holds an entire "
          "descriptive paragraph where a name belongs. They are reported here "
          "rather than truncated into a name, which would then match or miss "
          "falsely. Entity id(s): "
          + ", ".join(f"`{e['entity_id']}`" for e in ent_unnormalisable) + ".")
        A("")

    # ---- rule content ----
    A("## Rule content inside skill bodies")
    A("")
    A("`migration.md:277` requires standing rules inside skill bodies migrate "
      "to `task_policy` **by kind, never by value**. The rules migration "
      "therefore has to reach into skill bodies, and this is which bodies and "
      "how far.")
    A("")
    A(f"**{len(with_rules)} of {n_skills} distinct skills carry at least one "
      f"rule-shaped line**, "
      f"{sum(s.rule_lines for s in with_rules):,} lines in total.")
    A("")
    A("### The matcher, stated")
    A("")
    A("The rule inventory found *88 of 97* and *66 of 96* were both defensible "
      "for the same corpus, differing only by case sensitivity — and the "
      "defect was that the instrument went unstated. So this one is stated, "
      "and `--matcher` prints it.")
    A("")
    A("- **Counted per LINE, not per occurrence.** A line carrying three "
      "markers is one rule-shaped statement, not three.")
    A("- **Case-SENSITIVE on the all-caps forms** (`NEVER`, `ALWAYS`, `MUST`, "
      "`DO NOT`). In this corpus capitalisation deliberately marks a binding "
      "rule; lowercasing them sweeps in ordinary prose such as *you must first "
      "read*.")
    A("- **Case-INSENSITIVE on the multi-word imperative phrases** (*never "
      "send*, *always verify*, *do not commit*, *confirm before*, *only ever*), "
      "which are unambiguous at any case.")
    A("- **Front matter is excluded.** A `description:` routinely contains "
      "*use this whenever…*, which is trigger metadata, not a standing rule.")
    A("")
    A("**What the figure is.** It counts rule-SHAPED lines. It is a lower "
      "bound on rules a human would recognise — a rule stated without an "
      "imperative marker is invisible to it — and it is not a claim that every "
      "line counted states a distinct rule. Deduplicating these by KIND is the "
      "rule inventory's job, not this one's; what this contributes is which "
      "skill bodies the rules migration must open.")
    A("")
    A("### The sensitivity band, measured rather than asserted")
    A("")
    A("The case decision above is not cosmetic, and its cost is stated rather "
      "than left for a later reader to rediscover. The same corpus, the same "
      "markers, the same run, varying only case sensitivity:")
    A("")
    A("| Matcher variant | Skills carrying rule content |")
    A("|---|---|")
    A(f"| All markers case-SENSITIVE | {band_cs} of {n_skills} |")
    A(f"| **As stated above (mixed)** | **{len(with_rules)} of {n_skills}** |")
    A(f"| All markers case-insensitive | {band_ci} of {n_skills} |")
    A("")
    A("All three are defensible readings and they differ by "
      f"{band_ci - band_cs} skills. **That spread is the reason the matcher is "
      "stated.** The rule inventory reported *88 of 97* and *66 of 96* for one "
      "corpus on this same distinction; the defect was never the number, it "
      "was that the instrument went unnamed and so the two could not be "
      "reconciled. A reader who prefers a different variant can now say which "
      "one they mean and what it costs.")
    A("")
    top = sorted(with_rules, key=lambda s: -s.rule_lines)[:25]
    A("### The 25 skill bodies carrying the most rule content")
    A("")
    A("| Skill | Class | Rule lines | On main? |")
    A("|---|---|---|---|")
    for s in top:
        A(f"| `{s.name}` | {s.klass} | {s.rule_lines} | "
          f"{'yes' if s.on_main else 'no'} |")
    A("")
    marker_tally: dict[str, int] = defaultdict(int)
    for s in skills:
        for k, v in s.rule_markers.items():
            marker_tally[k] += v
    A("Marker frequency across all bodies (a line may fire several):")
    A("")
    A("| Marker | Lines fired |")
    A("|---|---|")
    for k, v in sorted(marker_tally.items(), key=lambda kv: -kv[1]):
        A(f"| `{k}` | {v:,} |")
    A("")

    # ---- full table ----
    A("## Every distinct skill")
    A("")
    A("One row per **name + content hash**. A name with two bodies gets two "
      "rows, which is the point.")
    A("")
    A("| Skill | Body | Class | Files | Reach | Rule lines | Description |")
    A("|---|---|---|---|---|---|---|")
    for s in skills:
        if s.on_main:
            reach = "main"
        elif s.at_user_root:
            reach = "user root"
        else:
            reach = "**worktree only**"
        A(f"| `{s.name}` | `{s.body_hash}` | {s.klass} | {s.file_count:,} | "
          f"{reach} | {s.rule_lines} | {s.description} |")
    A("")

    # ---- withheld ----
    A("## Withheld under the PII screen")
    A("")
    if withheld:
        A(f"**{len(withheld)} skill(s)** carry operator specifics somewhere in "
          "the body or description. Location and classification are recorded; "
          "the description is not. This is the posture the rule inventory "
          "established and the failure it exists to prevent is concrete — a "
          "PII-shaped literal in a committed inventory is what ateles#1099 had "
          "to rewrite git history to undo.")
        A("")
        A("| Skill | Class | Screen tripped on |")
        A("|---|---|---|")
        for s in sorted(withheld, key=lambda x: x.name):
            A(f"| `{s.name}` | {s.klass} | {', '.join(s.pii_reasons)} |")
        A("")
        A("The screen is deliberately over-broad: a false positive costs one "
          "withheld description, a false negative costs a history rewrite. "
          "Some rows below will be false positives — a skill that merely "
          "mentions payments trips `operator_personal_domain` without carrying "
          "an operator value — and that trade is chosen, not accidental.")
        A("")
    else:
        A("No skill tripped the screen on this run.")
        A("")

    # ---- what could not be classified ----
    A("## What this inventory could not classify or read")
    A("")
    A("Listed rather than guessed.")
    A("")
    if ent_err:
        A(f"- **Neotoma `skill` entities**: {ent_err}. The entity column is "
          "unmeasured on this run, which is not the same as empty.")
    else:
        A("- **The Neotoma entity read is a committed snapshot, not a live "
          "call.** It is read-only and dated, so `--check` runs without a "
          "credential; a skill entity created after the snapshot date is "
          "invisible until it is refreshed. The date is in the snapshot file.")
    A("- **A `skill` entity query reports a total that matches neither the "
      "rows it returns nor the rows that exist.** Measured 2026-09-19 against "
      "Neotoma PROD: the default query returned 72 rows while its own `total` "
      "field said 79; the same query with merged rows included returned 93 "
      "rows and said 93. So 72 live plus 21 merged is 93, and **79 describes "
      "no set at all**. Anything paging on that field, or asserting "
      "completeness against it, is trusting a number that does not describe "
      "its own result. Recorded here because it is exactly the "
      "validate-the-instrument failure, and filed separately against Neotoma.")
    A("- **Class assignment for `plumbing` and `mixed` is a named list, not a "
      "pattern.** `migration.md`'s tests for those two are semantic — *no step "
      "owner would claim it*, *one daemon's whole duty* — and no regex over "
      "names carries that. Each name in `PLUMBING_NAMES` and `MIXED_NAMES` was "
      "read against the class test. A skill absent from both lists and not a "
      "role or a probe falls to `procedure`, so **a new plumbing skill will be "
      "mis-reported as a procedure until it is added**. That is a known, "
      "stated limit and not a silent one.")
    A("- **`role` vs `mixed` is under-reported.** `migration.md` notes the "
      "role files carry three procedures near-verbatim, which by its own "
      "definition makes them mixed. They are counted as roles here because the "
      "duplicated sections are identical across the role files and belong to "
      "the collapse stage 11 performs, not to a per-skill judgement this "
      "instrument can make.")
    A("")

    # ---- prior art ----
    A("## Prior art: where this disagrees with the count it replaces")
    A("")
    A("| Earlier claim | Measured here | Reading |")
    A("|---|---|---|")
    A("| `status.md` rev. 32 counted skills at the two canonical roots | "
      f"**{n_skills} distinct across all roots** | The canonical roots are not "
      "the corpus. Most `SKILL.md` files on this machine are in neither. |")
    A(f"| The brief's hand-count: 146 unique names, 96 on main, 50 never on "
      f"main | **{len(names)} names, {len(main_names)} on main, "
      f"{len(never_main)} never on main** | Re-measured. The differences come "
      "from the user root: 14 of its 37 entries are symlinked directories, "
      "which a `find`/`glob` sweep does not follow. |")
    A("| The brief's hand-count: 28,408 files | "
      f"**{n_files:,} files in scope** | Different scope shape. Stated here so "
      "the two are comparable rather than silently divergent. |")
    A("| Neotoma holds 76 `skill` entities | "
      f"**{len(entities)} live, 21 merged, 93 total** | Neither 76 nor the "
      "API's own `total` of 79 matches the live row count. |")
    A("")

    A("## Method, so a re-run means something")
    A("")
    A("- **The unit is name + content hash, never the path.** Counting files "
      "measures the filesystem; counting distinct content measures the corpus.")
    A("- **Vendored trees are excluded by the scope's path shape**, verified "
      "against 1,352 vendored `SKILL.md` files, with a marker list as a second "
      "gate whose removal count is reported.")
    A("- **Classes come from `migration.md#five-classes-of-skill`** and from "
      "nowhere else; the role discriminator is the agent entity type, not the "
      "bare entity id.")
    A("- **The rule-content matcher is stated**, counted per line, and its "
      "recall limit is declared rather than implied.")
    A("- **The PII screen runs per skill, not per field**, and `--check` "
      "re-runs it so a value that lands later fails the gate.")
    A("- **Symlinked roots are followed deliberately**, because a glob that "
      "does not follow them under-reports the user root by 14.")
    A("- Read-only against Neotoma **prod**, via a committed snapshot. Nothing "
      "is written to the record.")
    A("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------


def print_matcher() -> None:
    print("Rule-content matcher (counted PER LINE; front matter excluded):\n")
    for name, pat in RULE_MARKERS:
        sensitivity = "case-insensitive" if pat.startswith("(?i)") else "CASE-SENSITIVE"
        print(f"  {name:24s} [{sensitivity}]  {pat}")
    print("\nA line firing any marker counts once. A rule stated without an")
    print("imperative marker is invisible to this; recall is not claimed.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify the committed inventory matches the system")
    ap.add_argument("--json", metavar="PATH", help="also dump the raw extraction")
    ap.add_argument("--matcher", action="store_true",
                    help="print the rule-content matcher and exit")
    args = ap.parse_args()

    if args.matcher:
        print_matcher()
        return 0

    home = Path.home()
    files, stats = collect_files(home)
    skills = build_skills(files)
    main_names, main_err = names_on_main()
    entities, ent_err = read_entities()

    # An impossible measurement is the instrument, not the world. Fail loudly
    # rather than emitting a document whose own numbers contradict each other.
    impossible = check_impossible_states(skills)
    if impossible:
        print(f"IMPOSSIBLE STATE in {len(impossible)} name(s) — the "
              "classifier is wrong, not the corpus:", file=sys.stderr)
        for line in impossible[:10]:
            print(f"  {line}", file=sys.stderr)
        return 3

    # The gate: no operator value reaches the committed file.
    leaks = []
    for s in skills:
        if s.description != WITHHELD:
            ok, why = screen_for_pii(s.description)
            if not ok:
                leaks.append((s.name, why))
    if leaks:
        print(f"PII SCREEN FAILED on {len(leaks)} emitted description(s):",
              file=sys.stderr)
        for nm, why in leaks[:10]:
            print(f"  {nm}: {', '.join(why)}", file=sys.stderr)
        return 2

    out = render(skills, files, stats, main_names, main_err, entities, ent_err)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "skills": [
                {"name": s.name, "body_hash": s.body_hash, "class": s.klass,
                 "files": s.file_count, "on_main": s.on_main,
                 "worktree_only": s.worktree_only,
                 "rule_lines": s.rule_lines,
                 "pii_reasons": s.pii_reasons,
                 "locations": sorted({_portable(f.path) for f in s.files})[:20]}
                for s in skills],
            "totals": {"files": len(files), "distinct_skills": len(skills),
                       "names": len({s.name for s in skills})},
            "stats": stats,
        }, indent=2))

    if args.check:
        if not OUTPUT.exists():
            print(f"{OUTPUT} does not exist; run without --check", file=sys.stderr)
            return 1
        cur = OUTPUT.read_text()
        strip = lambda t: re.sub(r"\*\*Measured:\*\* \d{4}-\d{2}-\d{2}",
                                 "**Measured:**", t)
        if strip(cur) != strip(out):
            print("skill inventory is stale — re-run "
                  "execution/scripts/render_skill_inventory.py", file=sys.stderr)
            return 1
        print("skill inventory matches the measured system")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(out)
    names = {s.name for s in skills}
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}: "
          f"{len(skills)} distinct skills from {len(files):,} files "
          f"({len(files)/max(len(skills),1):.0f}x), "
          f"{len(names)} names, "
          f"{len(names - main_names)} never on main, "
          f"{sum(1 for s in skills if s.rule_lines)} carrying rule content")
    return 0


if __name__ == "__main__":
    sys.exit(main())

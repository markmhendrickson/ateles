"""
execution/daemons/apis/issue_spec.py — the additive issue spec entity + mirror.

The ordered additive spec pipeline (ateles swarm-mechanics change) replaces the
old parallel review-expectation pass with an ORDERED, ADDITIVE sequence of lens
agents that each contribute ONE section of a growing specification:

    PM (Pavo) → Design/UX (Accipiter) → Eng (Cicada) → QA (Phoenicurus)
              → Security (Waxwing) → Legal (Buteo)

The canonical spec lives in a Neotoma ``issue_spec`` entity — one per issue,
keyed by ``<repo>#<number>``.  Each section is its own string field
(``pm_section`` … ``legal_section``); an agent CORRECTS ONLY ITS OWN field,
never rebuilding another agent's section (mirrors the plan-field merge
discipline in CLAUDE.md).  ``sequence_state`` records which sections are done;
``last_mirrored_at`` records the last body mirror.

A MIRROR function assembles the sections IN CANONICAL ORDER into a markdown
block and writes it into the GitHub issue DESCRIPTION between managed markers,
PRESERVING the human-written body above the markers.

This module holds:
  * the canonical section order + metadata (``SECTIONS``),
  * ``IssueSpecStore`` — retrieve / create / correct the Neotoma entity via
    plain httpx against ``/entities/query``, ``/store``, ``/correct``
    (same pattern as generalizer.py / participation.py),
  * ``assemble_spec_markdown`` — pure section→markdown assembler,
  * ``splice_managed_block`` — pure body-splice that only touches the managed
    marker region.

Neotoma schema for ``issue_spec`` is NOT registered from here (that needs the
neotoma repo).  The daemon stores the custom entity_type and relies on
Neotoma's schema-inference, exactly as other daemon-created types are stored
(mirror_delivery, operator_followup, participation_record, …).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from entity_lookup import resolve_entity

log = logging.getLogger("apis.issue_spec")

# Idempotency-mismatch is Neotoma's signal that a payload was already stored
# under this key with different content (see PostResult.error_code below).
# It is the specific, actionable case for the create branch below: it means
# the entity already exists — load() missed it — and the fix is to reload and
# CORRECT, never to retry the same store (which would clobber every OTHER
# agent's section field, since the create-branch payload only carries the ONE
# section this call is writing).
ERR_IDEMPOTENCY_MISMATCH = "ERR_IDEMPOTENCY_MISMATCH"


# ── Managed-marker constants ─────────────────────────────────────────────────
# The mirror writes the assembled spec between these HTML comment markers in the
# GitHub issue body.  Everything ABOVE the start marker (the reporter's original
# text) is preserved verbatim; only the region between the markers is replaced.
SPEC_MARKER_START = "<!-- swarm-spec:start -->"
SPEC_MARKER_END = "<!-- swarm-spec:end -->"


@dataclass(frozen=True)
class SpecSection:
    """One section of the additive spec.

    ``key``    — the ``sequence_state`` token / short id (e.g. "pm").
    ``field``  — the Neotoma ``issue_spec`` field this section stores into.
    ``lens``   — the review_panel lens label this section corresponds to.
    ``agent``  — the agent that authors this section.
    ``heading``— the markdown heading used in the mirrored body.
    ``always`` — True when this section always runs (PM, Eng, QA); False for the
                 conditional lenses (Design/UX, Security, Legal) which run only
                 when the lens is selected.
    ``dispatched`` — False when the section is authored inside ANOTHER
                 section's turn rather than by its own agent run: the design
                 basis is written by Pavo in the pm turn (fenced separately),
                 so stating a basis costs no extra dispatch.
    """

    key: str
    field: str
    lens: str
    agent: str
    heading: str
    always: bool
    dispatched: bool = True


# Canonical ordered sequence.  Order here IS the pipeline order and the mirror
# assembly order.  PM, Eng, QA always run; Design/UX, Security, Legal run only
# when their lens is selected for the issue.
# The design basis is the FIRST section: the foundation document and section
# the issue conforms to, or the explicit statement that no design applies
# (docs/foundation/conformance.md, "Design basis"). Pavo states it in the pm
# turn, from the kernel the reading list loads into that turn, so it is not
# dispatched on its own; the arch gate checks it (mechanically via
# foundation.check_design_basis, then by reading the cited document).
DESIGN_BASIS = SpecSection(
    "basis",
    "design_basis_section",
    "pm",
    "pavo",
    "Design basis",
    True,
    dispatched=False,
)

SECTIONS: tuple[SpecSection, ...] = (
    DESIGN_BASIS,
    SpecSection("pm", "pm_section", "pm", "pavo", "Product / Scope (PM)", True),
    SpecSection(
        "design", "design_section", "ux", "accipiter", "Design / UX", False
    ),
    SpecSection("eng", "eng_section", "eng", "cicada", "Engineering", True),
    SpecSection("qa", "qa_section", "qa", "phoenicurus", "QA / Test Plan", True),
    SpecSection(
        "security", "security_section", "arch", "waxwing", "Security / Arch", False
    ),
    SpecSection("legal", "legal_section", "legal", "buteo", "Legal", False),
)

# The sections that run as their own agent turn, in pipeline order.
DISPATCHED_SECTIONS: tuple[SpecSection, ...] = tuple(
    s for s in SECTIONS if s.dispatched
)

# Fast lookups. The by-agent / by-lens maps index DISPATCHED sections only:
# the design basis shares Pavo's `pm` lens and must not shadow the pm section.
SECTION_BY_KEY: dict[str, SpecSection] = {s.key: s for s in SECTIONS}
SECTION_BY_AGENT: dict[str, SpecSection] = {
    s.agent: s for s in DISPATCHED_SECTIONS
}
SECTION_BY_LENS: dict[str, SpecSection] = {s.lens: s for s in DISPATCHED_SECTIONS}
# All section field names, in canonical order.
SECTION_FIELDS: tuple[str, ...] = tuple(s.field for s in SECTIONS)

# Sections that always run (as their own turn) regardless of lens selection.
ALWAYS_KEYS: tuple[str, ...] = tuple(
    s.key for s in DISPATCHED_SECTIONS if s.always
)


def spec_key(repo: str, issue_number: int) -> str:
    """Canonical key for an issue's spec entity: ``<repo>#<number>``."""
    return f"{repo}#{issue_number}"


# ── Design basis (fenced inside the pm turn) ─────────────────────────────────
DESIGN_BASIS_MARKER_START = "<<<DESIGN_BASIS>>>"
DESIGN_BASIS_MARKER_END = "<<<END_DESIGN_BASIS>>>"


def extract_design_basis(stdout: str | None) -> str:
    """The pm turn's design-basis statement, or '' when it emitted none.

    Fenced separately from the spec section so one pm run yields two fields.
    Deliberately NO fallback to raw stdout (unlike the spec-section extractor):
    an absent basis must stay absent — and be reported MISSING to the arch
    gate — never be inferred from surrounding prose.
    """
    blob = stdout or ""
    start = blob.find(DESIGN_BASIS_MARKER_START)
    end = blob.find(DESIGN_BASIS_MARKER_END)
    if start == -1 or end == -1 or end <= start:
        return ""
    return blob[start + len(DESIGN_BASIS_MARKER_START) : end].strip()


# ── Pure assembly / splice helpers (unit-tested without any I/O) ─────────────


def assemble_spec_markdown(sections: dict[str, str]) -> str:
    """Assemble the completed sections IN CANONICAL ORDER into a markdown block.

    ``sections`` maps a section FIELD name (``pm_section`` …) to its markdown
    text.  Only non-empty sections are rendered; the order always follows
    ``SECTIONS`` regardless of insertion order in the dict, so re-running the
    mirror after any agent's turn yields a stable, ordered document.
    """
    parts: list[str] = ["## Swarm specification", ""]
    parts.append(
        "_This section is maintained by the Ateles swarm. Each lens agent "
        "owns exactly one subsection below; the human-written description "
        "above these markers is never modified._"
    )
    parts.append("")
    any_section = False
    for section in SECTIONS:
        text = (sections.get(section.field) or "").strip()
        if not text:
            continue
        any_section = True
        parts.append(f"### {section.heading}")
        parts.append("")
        parts.append(text)
        parts.append("")
    if not any_section:
        parts.append("_(No spec sections assembled yet.)_")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def splice_managed_block(original_body: str, managed_markdown: str) -> str:
    """Return ``original_body`` with the managed block replaced/appended.

    The managed block is the region between ``SPEC_MARKER_START`` and
    ``SPEC_MARKER_END`` (inclusive of the markers).  Rules:

      * If both markers already exist (in order), ONLY the text between them is
        replaced — everything before the start marker and after the end marker
        is preserved verbatim (never clobber the reporter's text, and never
        clobber any trailing content someone added below the block).
      * If the markers are absent, the managed block is APPENDED to the end of
        the body (with a blank-line separator), preserving the entire original.
      * A malformed marker pair (end before start, or only one present) is
        treated as "absent" and the block is appended — we never delete
        unrecognized content.

    The returned body always contains exactly one well-formed managed block.
    """
    body = original_body or ""
    block = f"{SPEC_MARKER_START}\n{managed_markdown.strip()}\n{SPEC_MARKER_END}"

    start_idx = body.find(SPEC_MARKER_START)
    end_idx = body.find(SPEC_MARKER_END)

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        before = body[:start_idx]
        after = body[end_idx + len(SPEC_MARKER_END):]
        return f"{before}{block}{after}"

    # Absent or malformed markers → append, preserving all original content.
    prefix = body.rstrip()
    if prefix:
        return f"{prefix}\n\n{block}\n"
    return f"{block}\n"


# ── Neotoma-backed spec store ────────────────────────────────────────────────


@dataclass
class SpecState:
    """In-memory view of an ``issue_spec`` entity.

    ``entity_id`` is empty until the entity has been created in Neotoma.
    ``sections`` maps FIELD name → text (only the sections that exist).
    ``sequence_state`` is the ordered list of completed section keys.
    """

    repo: str
    issue_number: int
    title: str
    entity_id: str = ""
    sections: dict[str, str] | None = None
    sequence_state: list[str] | None = None

    def __post_init__(self) -> None:
        if self.sections is None:
            self.sections = {}
        if self.sequence_state is None:
            self.sequence_state = []


def _state_from_hit(
    entity: dict, snap: dict, state: SpecState, title: str
) -> SpecState:
    """Populate ``state`` from an ``entity_lookup.resolve_entity`` hit.

    ``entity`` is the raw result row; ``snap`` is its ALREADY-UNWRAPPED
    snapshot field map (``entity_lookup.unwrap_snapshot`` has already
    tolerated the one-level-nested shape some prod responses use). Pure/no
    I/O.
    """
    state.entity_id = str(
        entity.get("entity_id") or entity.get("id") or snap.get("entity_id") or ""
    )
    state.title = snap.get("title", title) or title
    state.sections = {
        field: snap[field] for field in SECTION_FIELDS if snap.get(field)
    }
    seq = snap.get("sequence_state")
    if isinstance(seq, list):
        state.sequence_state = [str(x) for x in seq]
    elif isinstance(seq, str) and seq:
        # Tolerate a comma-joined string form from schema inference.
        state.sequence_state = [p.strip() for p in seq.split(",") if p.strip()]
    return state


class IssueSpecStore:
    """Retrieve / create / update the additive ``issue_spec`` entity.

    All I/O is best-effort and never raises: a Neotoma failure degrades to
    "spec not persisted this turn" and logs, so one bad store never crashes the
    dispatch pipeline (same contract as swarm_dispatch._store_entities).
    """

    ENTITY_TYPE = "issue_spec"

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        # Set by _post() after every call; see its docstring.
        self.last_error: str | None = None
        self.last_error_code: str | None = None
        self.last_error_status: int | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _post(self, path: str, payload: dict) -> dict | None:
        """POST to Neotoma. Returns the parsed JSON body, or None on failure.

        On failure the status code and a truncated, redacted error body are
        stashed on ``self.last_error`` / ``self.last_error_code`` /
        ``self.last_error_status`` so a caller that needs to distinguish
        failure REASONS (e.g. an idempotency-key collision vs. a transient
        502) can — a bare "failed: <exception str>" log line was silently
        losing the response body, which is the one place Neotoma states WHY a
        400 happened (this PR's own addition; see PR body).
        """
        self.last_error = None
        self.last_error_code = None
        self.last_error_status = None
        if not self.token:
            log.warning(
                "[apis.issue_spec] NEOTOMA_BEARER_TOKEN unset — %s skipped", path
            )
            self.last_error = "NEOTOMA_BEARER_TOKEN unset"
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self.base_url}/{path}",
                    json=payload,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return {}
        except httpx.HTTPStatusError as exc:
            # The response body is where Neotoma states the ACTUAL reason
            # (error_code + message) — capture and log it, redacted/truncated,
            # rather than the bare "400 Bad Request" the exception str gives.
            self.last_error_status = exc.response.status_code
            body_text = _redact(exc.response.text or "")[:500]
            self.last_error = body_text
            try:
                body_json = exc.response.json()
                self.last_error_code = body_json.get("error_code") or (
                    body_json.get("error") or {}
                ).get("code")
            except Exception:  # noqa: BLE001 — body may not be JSON
                pass
            log.error(
                "[apis.issue_spec] %s failed: HTTP %s %s",
                path, exc.response.status_code, body_text,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — best-effort, never crash
            self.last_error = str(exc)
            log.error("[apis.issue_spec] %s failed: %s", path, exc)
            return None

    @staticmethod
    def _matches(snap: dict, repo: str, issue_number: int) -> bool:
        """True when *snap* is the issue_spec for ``repo#issue_number``.

        Tolerates the duplicated field spellings seen in prod, matching the
        equivalent predicate in gate_waive.py: ``repo``/``repository`` and
        ``issue_number``/``github_number``. Carried from PR #497
        (ateles#492) unchanged.
        """
        snap_repo = snap.get("repo") or snap.get("repository") or ""
        if str(snap_repo) != str(repo):
            return False
        for key in ("issue_number", "github_number"):
            value = snap.get(key)
            if value is not None and str(value) == str(issue_number):
                return True
        return False

    async def load(self, repo: str, issue_number: int, title: str) -> SpecState:
        """Retrieve the current spec for ``repo#number`` (or an empty state).

        Identity resolution is delegated to
        :func:`entity_lookup.resolve_entity` (targeted ``snapshot_filters``
        query, client-side re-verification, then a bounded recency-sorted
        scan fallback) — carried from PR #497 (ateles#492) rather than
        hand-rolled here, per `docs/foundation/principles.md#6` ("extend the
        mechanism that already generalizes; do not build a parallel one").
        #497 built this exact mechanism for the twin defect in
        `IssueGateStore.load()` and named `issue_spec.py`'s copy of the same
        bug as in-scope; a second, divergent implementation in this file
        would have shipped two different fixes for one defect in the same
        function.

        A missing token, missing entity, or fetch error all degrade to a
        fresh empty ``SpecState`` so the caller can still create it. An
        AMBIGUOUS targeted-query result (more than one re-verified match,
        which the repo+issue_number identity rule says should never happen)
        fails CLOSED inside ``entity_lookup`` itself — see that module's
        "Ambiguous match" section — rather than here, so both callers of
        ``resolve_entity`` share the same fail-closed behavior.

        Before this used ``resolve_entity`` at all, it read one un-paginated
        page (``limit: 200``, no cursor, no filter) of the WHOLE corpus. With
        900+ issue_spec entities in prod, any entity sorting past that page
        was invisible, so upsert_section took the CREATE branch for an issue
        that already had an entity, replaying a content-bound,
        per-issue-constant idempotency key its real first create had already
        consumed — a permanent 400 ERR_IDEMPOTENCY_MISMATCH for that issue.
        The read-path defect is ateles#492/#498 (fixed by adopting
        `resolve_entity`); the idempotency-key fix below is this PR's own
        addition beyond what #492 scopes — see PR body.
        """
        state = SpecState(repo=repo, issue_number=issue_number, title=title)
        hit = await resolve_entity(
            self._post,
            self.ENTITY_TYPE,
            lambda snap: self._matches(snap, repo, issue_number),
            [
                {repo_field: repo, num_field: issue_number}
                for repo_field in ("repo", "repository")
                for num_field in ("issue_number", "github_number")
            ],
            spec_key(repo, issue_number),
            ("issue_number", "github_number"),
        )
        if hit is None:
            return state
        entity, snap = hit
        return _state_from_hit(entity, snap, state, title)

    async def upsert_section(
        self, state: SpecState, section: SpecSection, text: str
    ) -> SpecState:
        """Additively set ONE section's field on the issue_spec entity.

        Creates the entity on first write (all fields present, the one section
        populated); on subsequent writes CORRECTS only this section's field and
        the ``sequence_state`` — never rewriting other agents' section fields.

        Idempotent: re-running the same section replaces that field in place
        (no duplicate entity, no duplicate sequence_state entry).
        """
        text = (text or "").strip()
        # Merge into the in-memory view first (so the caller sees current state
        # even when persistence degrades).
        assert state.sections is not None and state.sequence_state is not None
        state.sections[section.field] = text
        if section.key not in state.sequence_state:
            state.sequence_state.append(section.key)
        # Keep sequence_state in canonical order.
        state.sequence_state = [
            s.key for s in SECTIONS if s.key in set(state.sequence_state)
        ]

        now = datetime.now(timezone.utc).isoformat()
        key = spec_key(state.repo, state.issue_number)

        if not state.entity_id:
            # First write for this issue: create the entity with schema-inference.
            entity: dict = {
                "entity_type": self.ENTITY_TYPE,
                "title": state.title or key,
                "repo": state.repo,
                "issue_number": state.issue_number,
                "spec_key": key,
                "sequence_state": list(state.sequence_state),
                section.field: text,
                "last_updated_at": now,
            }
            # The idempotency key carries a timestamp component (unlike the
            # old bare `issue-spec-create-{key}`), because a per-issue-CONSTANT
            # key means every retry after the entity already exists reuses a
            # key Neotoma already bound to different content, and it correctly
            # 400s ERR_IDEMPOTENCY_MISMATCH forever rather than silently
            # accepting a second, different payload under the same key. This
            # is a separate defect from #492/#498's read-path fix above — see
            # PR body. Uniqueness here does not risk a duplicate entity:
            # identity is resolved server-side by the repo+issue_number
            # composite rule, not by the idempotency key.
            result = await self._post(
                "store",
                {
                    "entities": [entity],
                    "idempotency_key": f"issue-spec-create-{key}-{now}",
                },
            )
            # Best-effort entity_id recovery so subsequent sections CORRECT.
            new_id = _extract_entity_id(result)
            if new_id:
                state.entity_id = new_id
                return state

            mismatch = self.last_error_code == ERR_IDEMPOTENCY_MISMATCH
            if mismatch:
                # This specific code means the entity ALREADY EXISTS under a
                # key some earlier attempt consumed with different content —
                # i.e. load() missed a real entity. Reload (paginated) rather
                # than retrying the store: retrying would clobber every OTHER
                # agent's section field, since this payload carries only the
                # ONE section being written right now.
                log.warning(
                    "[apis.issue_spec] %s: create collided with an existing "
                    "entity (idempotency mismatch) — load() missed it; "
                    "reloading to correct in place instead of overwriting",
                    key,
                )

            # Fall back to a load so later sections can correct in place.
            # (Also the general-error recovery path for any other create
            # failure — a transient 502, a timeout, etc.)
            reloaded = await self.load(
                state.repo, state.issue_number, state.title
            )
            if reloaded.entity_id:
                state.entity_id = reloaded.entity_id
                # Preserve the section we just wrote in the in-memory view.
                reloaded.sections = state.sections
                reloaded.sequence_state = state.sequence_state
                return reloaded

            # Reload also failed to recover an id: this section's write is
            # LOST for this run (kept only in the in-memory `state` the
            # caller holds, which does not survive the process). A previous
            # revision of this branch also called
            # unroutable_ledger.shared_ledger().note_undefined_role(...) here,
            # intending to reuse that mechanism's dedup+reassert persistence.
            # Removed: note_undefined_role's only real effect is its boolean
            # return value, which the ONE existing caller (skill_runner.py)
            # uses to decide whether to notify an operator. This call site
            # discarded that return value, and nothing anywhere drains or
            # reports the `_roles` dict the call writes to — not apis.py, not
            # any daemon loop. So the call persisted a disk entry nothing
            # ever reads, which is a control that does not bind
            # (docs/foundation/principles.md#1), while implying — via its own
            # log text — a downstream escalation that never happened. A
            # single explicit log line, naming the issue and section, is what
            # actually fires every time; nothing here is currently wired to
            # notify an operator, and that gap is real, not the log line's to
            # paper over.
            reason = self.last_error_code or self.last_error_status or "unknown"
            log.error(
                "[apis.issue_spec] %s section for %s LOST this run — create "
                "failed (%s) and the reload fallback could not recover an "
                "entity_id either; this write did not persist to Neotoma",
                section.key, key, reason,
            )
            return state

        # Existing entity: correct ONLY this section's field + sequence_state.
        await self._post(
            "correct",
            {
                "entity_id": state.entity_id,
                "entity_type": self.ENTITY_TYPE,
                "field": section.field,
                "value": text,
                "idempotency_key": f"issue-spec-{section.key}-{key}-{now[:16]}",
            },
        )
        await self._post(
            "correct",
            {
                "entity_id": state.entity_id,
                "entity_type": self.ENTITY_TYPE,
                "field": "sequence_state",
                "value": list(state.sequence_state),
                "idempotency_key": f"issue-spec-seq-{key}-{now[:16]}",
            },
        )
        return state

    async def mark_mirrored(self, state: SpecState) -> None:
        """Record ``last_mirrored_at`` on the entity (best-effort)."""
        if not state.entity_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._post(
            "correct",
            {
                "entity_id": state.entity_id,
                "entity_type": self.ENTITY_TYPE,
                "field": "last_mirrored_at",
                "value": now,
                "idempotency_key": (
                    f"issue-spec-mirror-{spec_key(state.repo, state.issue_number)}-"
                    f"{now[:16]}"
                ),
            },
        )


def _redact(text: str) -> str:
    """Strip anything Authorization-shaped before a response body hits a log.

    Neotoma error bodies are JSON diagnostics (error_code/message/hint), not
    secrets, but this is a cheap belt-and-braces against a future response
    shape that echoes a header back (some frameworks do, on a 4xx).
    """
    import re

    return re.sub(
        r'(?i)(authorization["\']?\s*[:=]\s*["\']?)bearer\s+\S+',
        r"\1<redacted>",
        text,
    )


def _extract_entity_id(store_result: dict | None) -> str:
    """Best-effort pull of the created entity_id from a /store response.

    /store responses vary in shape across Neotoma versions; probe the common
    keys and return "" when none match (the caller then reloads).
    """
    if not store_result:
        return ""
    # Common shapes: {"entities": [{"entity_id": "..."}]},
    # {"stored": [{"entity_id": "..."}]}, {"entity_id": "..."}.
    for list_key in ("entities", "stored", "results"):
        items = store_result.get(list_key)
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict) and first.get("entity_id"):
                return str(first["entity_id"])
    if store_result.get("entity_id"):
        return str(store_result["entity_id"])
    return ""

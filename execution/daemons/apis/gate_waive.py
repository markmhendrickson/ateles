"""
execution/daemons/apis/gate_waive.py — dispatcher-side gate waiving (ateles#285).

WHY THIS MODULE EXISTS
----------------------
``/confirm-gates-clear`` used to be executed by *prompting Lanius* to correct
the issue entity's ``gate_status``.  That failed three times on ateles#241:

  * 2026-07-23 — PARTIAL apply: only ``arch`` was waived, ``ux`` stayed pending
    (worse than nothing, because the issue then *looks* cleared).
  * 2026-07-27T17:08 — ``rc=1``, at least logged.
  * 2026-07-27T17:37 — SILENT: agent spawned, produced nothing.  No entity
    write, no ``dispatch ok``, no error, no GitHub comment.  The entity's
    ``last_observation_at`` stayed four days stale.

A gate waive is a DETERMINISTIC STATE TRANSITION — ``gate_status.<gate>`` →
``"waived"`` for every unsigned pre-impl gate, plus one ``owner_history``
append.  There is no judgement in it.  Handing a mechanical mutation to an LLM
turn is precisely why it can silently no-op or half-apply.  So this module
performs the write DISPATCHER-SIDE, then RE-READS the entity and verifies the
transition actually landed.

Shape notes learned from the live prod entity (``ent_4c1f77bc5fc86a2bad2025d6``):

  * ``gate_status`` round-trips as a **JSON-encoded string**, not a dict, when
    Neotoma's schema inference types the field as a string.  Both forms must be
    parsed on read, and the write preserves whichever form was stored.
  * The issue entity carries ``repo`` AND ``repository`` (same value) and
    ``issue_number`` (int) alongside ``github_number`` (string).  Matching must
    tolerate all of them.
  * The prod REST surface exposes the read as POST ``/entities/query``, NOT
    ``/retrieve_entities`` (which 404s) — same gotcha documented in
    ``issue_spec.py``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from lib.issue_number import issue_matches, number_filter_candidates

log = logging.getLogger("apis.gate_waive")


# Gate states that count as ALREADY CLEARED — never re-waive these.
CLEARED_GATE_STATES: frozenset[str] = frozenset(
    # `not_applicable` appears in live gate_status values alongside
    # `not_required` — both mean "this gate will never be signed because it does
    # not apply here". Omitting it made a legitimately-cleared gate read as
    # uncleared (ateles#460).
    {"signed_off", "waived", "not_required", "not_applicable", "skipped"}
)

# The value a waived gate is set to.
WAIVED = "waived"


# ── Pure helpers (no I/O — unit tested directly) ─────────────────────────────


def parse_gate_status(raw: object) -> dict[str, str]:
    """Normalize a stored ``gate_status`` value into ``{gate: state}``.

    Tolerates the three shapes seen in prod:
      * a real dict (``{"pm": "signed_off", ...}``),
      * a JSON-encoded string (schema inference typed the field as a string),
      * anything else / missing → ``{}``.
    """
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except (ValueError, TypeError):
            log.warning("[apis.gate_waive] gate_status is not valid JSON: %r", text[:120])
            return {}
        if isinstance(decoded, dict):
            return {str(k): str(v) for k, v in decoded.items()}
        return {}
    return {}


def parse_owner_history(raw: object) -> list[dict]:
    """Normalize a stored ``owner_history`` into a list of dicts."""
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(decoded, list):
            return [entry for entry in decoded if isinstance(entry, dict)]
    return []


def gates_needing_waive(
    gate_status: dict[str, str], pre_impl_gates: tuple[str, ...]
) -> list[str]:
    """Return the pre-impl gates that are NOT already cleared, in gate order.

    This is the ALL-GATES SWEEP: every gate in *pre_impl_gates* is considered,
    including ones absent from the stored ``gate_status`` (an absent gate is
    unsigned, not cleared).  The 2026-07-23 regression waived only ``arch``
    while ``ux`` stayed ``pending``; that cannot happen here because the sweep
    is a total function over *pre_impl_gates*, not an agent's iteration.
    """
    out: list[str] = []
    for gate in pre_impl_gates:
        state = (gate_status.get(gate) or "").strip().lower()
        if state in CLEARED_GATE_STATES:
            continue
        out.append(gate)
    return out


def apply_waives(
    gate_status: dict[str, str], gates: list[str]
) -> dict[str, str]:
    """Return a NEW gate_status with each gate in *gates* set to ``waived``.

    Merge semantics: every key already present is preserved untouched.  This
    mirrors the plan-field merge discipline — never rebuild a map from a stale
    partial copy.
    """
    merged = dict(gate_status)
    for gate in gates:
        merged[gate] = WAIVED
    return merged


def waive_history_entries(gates: list[str], timestamp: str) -> list[dict]:
    """Build the ``owner_history`` append entries for a waive sweep."""
    return [
        {
            "gate": gate,
            "action": WAIVED,
            "actor": "operator",
            "reason": "operator /confirm-gates-clear override",
            "timestamp": timestamp,
        }
        for gate in gates
    ]


def verify_waived(
    gate_status: dict[str, str], gates: list[str]
) -> list[str]:
    """Return the subset of *gates* that are STILL not cleared.

    An empty list means the transition landed.  A non-empty list is a
    verification FAILURE and must be reported loudly, never swallowed.
    """
    return [
        gate
        for gate in gates
        if (gate_status.get(gate) or "").strip().lower() not in CLEARED_GATE_STATES
    ]


def format_waive_comment(
    marker: str,
    header: str,
    waived: list[str],
    already_clear: list[str],
    failed: list[str],
    entity_found: bool = True,
) -> str:
    """Render the operator-visible GitHub comment for a waive attempt.

    ALWAYS produces a body — success, no-op, and failure all get a comment.
    Today's failure produced zero GitHub-visible output, which is a core part
    of the bug (#285 point 3).

    The body deliberately contains NO command token (no literal
    ``/confirm-gates-clear``) so the dispatcher's own comment can never
    re-trigger the command detector (the neotoma#1686 self-trigger defence
    already applied to the swarm-run confirmation).
    """
    lines = [marker, header, ""]

    if not entity_found:
        lines.append(
            "⚠️ **Gate waive could not be applied** — no Neotoma issue "
            "entity was found for this issue, so there is no `gate_status` to "
            "waive. The gate pipeline will keep blocking until the issue is "
            "triaged (Lanius new-issue protocol) and gates are initialized."
        )
        return "\n".join(lines)

    if failed:
        lines.append(
            "❌ **Gate waive FAILED verification** — after writing, the "
            "issue entity still reports these gates as unsigned: "
            + ", ".join(f"`{g}`" for g in failed)
            + "."
        )
        if waived:
            lines.append("")
            lines.append(
                "Gates that did land: "
                + ", ".join(f"`{g}`" for g in waived)
                + "."
            )
        lines.append("")
        lines.append(
            "The waive was applied dispatcher-side and then re-read to verify. "
            "A failure here means the Neotoma write did not persist — the "
            "review pipeline will KEEP BLOCKING. Do not assume this issue is "
            "unblocked."
        )
        return "\n".join(lines)

    if waived:
        lines.append(
            "✅ **Gates waived by operator override:** "
            + ", ".join(f"`{g}`" for g in waived)
            + "."
        )
        lines.append("")
        lines.append(
            "Verified by re-reading the issue entity after the write. The review "
            "pipeline will now proceed."
        )
        if already_clear:
            lines.append("")
            lines.append(
                "Already cleared beforehand (left untouched): "
                + ", ".join(f"`{g}`" for g in already_clear)
                + "."
            )
        return "\n".join(lines)

    lines.append(
        "ℹ️ **No gates needed waiving** — all pre-impl gates were "
        "already signed off or waived: "
        + (", ".join(f"`{g}`" for g in already_clear) or "(none configured)")
        + ". The pipeline is already clear."
    )
    return "\n".join(lines)


# ── Neotoma-backed issue-entity store ────────────────────────────────────────


@dataclass
class IssueGateState:
    """In-memory view of an ``issue`` entity's gate fields."""

    repo: str
    issue_number: int
    entity_id: str = ""
    gate_status: dict[str, str] = field(default_factory=dict)
    owner_history: list[dict] = field(default_factory=list)
    current_owner: str = ""
    # True when the stored gate_status was a JSON string (not a dict), so the
    # write-back preserves the stored representation.
    gate_status_was_string: bool = False

    @property
    def found(self) -> bool:
        return bool(self.entity_id)

    @property
    def triaged(self) -> bool:
        """True when the entity exists AND triage has initialised its gates.

        ``found`` answers "is there an object to write to"; ``triaged`` answers
        "has the gate pipeline actually claimed this issue". They diverge for a
        whole population of issues: entities created through ``/store`` (CLI,
        MCP, sync) exist immediately, but triage fires only on a GitHub
        ``issue.opened`` webhook, so their ``gate_status`` is never written.
        Such an issue looks healthy to every ``found`` check while being
        invisible to the gates — no owner, no pending gate, nothing to advance.

        Callers recovering gate state must branch on this, not on ``found``.
        """
        return self.found and bool(self.gate_status)


@dataclass
class WaiveOutcome:
    """Result of a dispatcher-side waive sweep."""

    entity_found: bool = False
    targeted: list[str] = field(default_factory=list)
    already_clear: list[str] = field(default_factory=list)
    waived: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    verified: bool = False

    @property
    def ok(self) -> bool:
        """True when nothing needs reporting as a failure.

        A no-op sweep on a found entity (everything already clear) is ``ok``.
        A missing entity or any unverified gate is NOT.
        """
        return self.entity_found and not self.failed


class IssueGateStore:
    """Read / correct the ``gate_status`` + ``owner_history`` of an issue entity.

    All I/O is best-effort in the sense that it never raises into the dispatch
    pipeline — but unlike the old prompt-driven path, every failure is recorded
    in the returned :class:`WaiveOutcome` so the caller can report it loudly.
    Silence is the bug; degradation must be visible.
    """

    ENTITY_TYPE = "issue"
    # Upper bound on the legacy fallback scan (200/page). Generous versus the
    # current corpus (~4.1k) while still bounding a gate check.
    _MAX_SCAN_PAGES = 40

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _post(self, path: str, payload: dict) -> dict | None:
        if not self.token:
            log.warning(
                "[apis.gate_waive] NEOTOMA_BEARER_TOKEN unset — %s skipped", path
            )
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
        except Exception as exc:  # noqa: BLE001 — never crash the pipeline
            log.error("[apis.gate_waive] %s failed: %s", path, exc)
            return None

    @staticmethod
    def _matches(snap: dict, repo: str, issue_number: int) -> bool:
        """True when *snap* is the issue entity for ``repo#issue_number``.

        Delegates to ``issue_number.issue_matches`` so this daemon, the MCP
        server, and every other reader share ONE definition of how the number
        is spelled. Previously each site kept its own tuple of field names and
        they drifted: this one knew three spellings, others knew two, and the
        fourth (``github_issue_number``, 82 rows in prod) was known to none.

        ateles#390: ``number`` was missing from this list, and it is the field
        the triage path actually writes. Measured against prod on 2026-09-02,
        ``ent_e882a86eb583b828ac00f98b`` (ateles#390's own entity) stores
        ``number: 390`` with no ``issue_number`` and no ``github_number``, so
        both the server-side filter and this client-side matcher missed it and
        the store reported "no Neotoma issue entity" for an entity that plainly
        exists — the exact error in the issue's symptom log.
        """
        return issue_matches(snap, repo, issue_number)

    async def load(self, repo: str, issue_number: int) -> IssueGateState:
        """Retrieve the gate state for ``repo#issue_number``.

        NOTE: prod exposes the read as POST ``/entities/query``, NOT
        ``/retrieve_entities`` (which 404s) — see issue_spec.py for the same
        gotcha. A missing token / entity / error degrades to an EMPTY state
        with ``found == False``, which the caller reports rather than ignores.

        The lookup is filtered SERVER-side on the composite identity
        (``github_number`` + ``repo``). It previously fetched an unpaginated
        first page — ``limit: 500``, no cursor — and scanned it client-side.
        With 4,144 issue entities in the corpus that reached roughly 12% of
        them: any issue outside that arbitrary window read as "no entity", and
        because every caller fails CLOSED, the result was a silent block
        attributed to a missing entity rather than to a truncated read.
        Filtering server-side returns exactly the one row and removes the
        window entirely.

        ateles#390: the filter keyed on ``github_number`` ALONE, but issue
        entities are written with ``number``. Measured against prod on
        2026-09-02, a ``github_number`` filter for ateles#390 returned 0 rows
        while the same query on ``number`` returned exactly
        ``ent_e882a86eb583b828ac00f98b``. Every gate lookup therefore fell
        through to the bounded scan and — because ``_matches`` did not know
        ``number`` either — reported "no Neotoma issue entity".

        ateles#741: the filter list is now generated by
        ``issue_number.number_filter_candidates``, which spans all FOUR number
        spellings and BOTH repo spellings. Two gaps remained after #390's fix:
        ``github_issue_number`` (82 rows in prod) was tried by nothing, and the
        repo half of every filter hardcoded ``repo``, so a row carrying only
        ``repository`` fell through to the paged scan on every single lookup.
        """
        state = IssueGateState(repo=repo, issue_number=issue_number)
        entities: list[dict] = []
        for snapshot_filters in number_filter_candidates(issue_number, repo):
            data = await self._post(
                "entities/query",
                {
                    "entity_type": self.ENTITY_TYPE,
                    "limit": 10,
                    "include_snapshots": True,
                    "snapshot_filters": snapshot_filters,
                },
            )
            if data is None:
                return state
            entities = data.get("entities", [])
            if entities:
                break
        if not entities:
            # Fall back to an unfiltered scan for entities whose snapshot does
            # not carry the composite fields (e.g. legacy rows keyed by
            # local_issue_id or title). Bounded and paged, so a miss here means
            # the entity genuinely is not there.
            entities = await self._scan_for_issue(repo, issue_number)
        for entity in entities:
            snap = entity.get("snapshot") or {}
            # Some responses nest the field map one level deeper.
            inner = snap.get("snapshot")
            if isinstance(inner, dict):
                snap = inner
            if not self._matches(snap, repo, issue_number):
                continue
            state.entity_id = str(
                entity.get("entity_id") or entity.get("id") or snap.get("entity_id") or ""
            )
            raw_gates = snap.get("gate_status")
            state.gate_status_was_string = isinstance(raw_gates, str)
            state.gate_status = parse_gate_status(raw_gates)
            state.owner_history = parse_owner_history(snap.get("owner_history"))
            state.current_owner = str(snap.get("current_owner") or "")
            break
        return state

    async def _scan_for_issue(self, repo: str, issue_number: int) -> list[dict]:
        """Paged fallback scan for entities lacking composite snapshot fields.

        Uses cursor paging rather than a single truncated page, so "not found"
        means absent rather than beyond an arbitrary window. Bounded by
        ``_MAX_SCAN_PAGES`` so a pathological corpus cannot hang a gate check.
        """
        cursor = ""
        for _ in range(self._MAX_SCAN_PAGES):
            payload: dict = {
                "entity_type": self.ENTITY_TYPE,
                "limit": 200,
                "include_snapshots": True,
            }
            if cursor:
                payload["cursor"] = cursor
            data = await self._post("entities/query", payload)
            if not data:
                return []
            page = data.get("entities", [])
            for entity in page:
                snap = entity.get("snapshot") or {}
                inner = snap.get("snapshot")
                if isinstance(inner, dict):
                    snap = inner
                if self._matches(snap, repo, issue_number):
                    return [entity]
            cursor = data.get("next_cursor") or ""
            if not cursor or not page:
                return []
        log.warning(
            "[gate_waive] %s#%s: scan hit the %d-page bound without a match",
            repo,
            issue_number,
            self._MAX_SCAN_PAGES,
        )
        return []

    def _encode_gate_status(
        self, state: IssueGateState, gate_status: dict[str, str]
    ) -> object:
        """Encode gate_status back in the representation it was stored as."""
        if state.gate_status_was_string:
            return json.dumps(gate_status)
        return gate_status

    async def waive(
        self,
        repo: str,
        issue_number: int,
        pre_impl_gates: tuple[str, ...],
    ) -> WaiveOutcome:
        """Waive every unsigned pre-impl gate, then VERIFY by re-reading.

        This is the whole fix for #285: a deterministic dispatcher-side write
        followed by a read-back assertion.  The outcome always describes what
        happened, so the caller can post an operator-visible comment on success
        AND on failure.
        """
        outcome = WaiveOutcome()
        state = await self.load(repo, issue_number)
        if not state.found:
            log.error(
                "[apis.gate_waive] no Neotoma issue entity for %s#%s — "
                "gate waive cannot be applied",
                repo,
                issue_number,
            )
            return outcome

        outcome.entity_found = True
        targeted = gates_needing_waive(state.gate_status, pre_impl_gates)
        outcome.targeted = list(targeted)
        outcome.already_clear = [g for g in pre_impl_gates if g not in targeted]

        if not targeted:
            # Nothing to do — but this is still a REPORTED outcome, not silence.
            outcome.verified = True
            log.info(
                "[apis.gate_waive] %s#%s: all pre-impl gates already clear (%s)",
                repo,
                issue_number,
                ", ".join(outcome.already_clear) or "none",
            )
            return outcome

        now = datetime.now(timezone.utc).isoformat()
        merged_gates = apply_waives(state.gate_status, targeted)
        merged_history = list(state.owner_history) + waive_history_entries(
            targeted, now
        )
        key = f"{repo}#{issue_number}"

        await self._post(
            "correct",
            {
                "entity_id": state.entity_id,
                "entity_type": self.ENTITY_TYPE,
                "field": "gate_status",
                "value": self._encode_gate_status(state, merged_gates),
                "idempotency_key": f"gate-waive-status-{key}-{now[:16]}",
            },
        )
        await self._post(
            "correct",
            {
                "entity_id": state.entity_id,
                "entity_type": self.ENTITY_TYPE,
                "field": "owner_history",
                "value": merged_history,
                "idempotency_key": f"gate-waive-history-{key}-{now[:16]}",
            },
        )

        # VERIFY: re-read and assert the transition landed. Never trust the
        # write — the whole class of bug in #285 / #263 is "the dispatcher
        # assumed the mutation happened".
        reread = await self.load(repo, issue_number)
        if not reread.found:
            outcome.failed = list(targeted)
            log.error(
                "[apis.gate_waive] %s#%s: verification re-read found no entity "
                "— waive UNVERIFIED for %s",
                repo,
                issue_number,
                ", ".join(targeted),
            )
            return outcome

        still_open = verify_waived(reread.gate_status, targeted)
        outcome.failed = still_open
        outcome.waived = [g for g in targeted if g not in still_open]
        outcome.verified = not still_open

        if still_open:
            log.error(
                "[apis.gate_waive] %s#%s: gate waive VERIFICATION FAILED — "
                "still unsigned after write: %s (landed: %s)",
                repo,
                issue_number,
                ", ".join(still_open),
                ", ".join(outcome.waived) or "none",
            )
        else:
            log.info(
                "[apis.gate_waive] %s#%s: waived and verified %s",
                repo,
                issue_number,
                ", ".join(outcome.waived),
            )
        return outcome

    async def waive_many(
        self,
        repo: str,
        issue_numbers: list[int],
        pre_impl_gates: tuple[str, ...],
    ) -> "AggregateWaiveOutcome":
        """Waive *pre_impl_gates* on every issue in *issue_numbers* (ateles#390).

        Pure fan-out over :meth:`waive`, which already write-verifies each
        entity — so this adds no new mutation shape and inherits that method's
        idempotency (``gates_needing_waive`` targets only unsigned gates, so a
        replay on an already-waived issue is a verified no-op, not a re-write).

        Every number is resolved against the SINGLE *repo* argument.  Parent
        links are extracted as bare ``#N`` from the PR body and a bare number is
        only meaningful within its own repo; Neotoma issue entities are keyed by
        ``(repo, issue_number)``, never by number alone.  A cross-repo parent is
        filtered out at resolution time, before it reaches here.

        One failing parent does NOT short-circuit the loop: a partially
        resolvable set still lands what it can, and the aggregate reports both
        halves so the operator sees which issue is still blocking.  Never raises
        — the caller owns reporting.
        """
        aggregate = AggregateWaiveOutcome()
        for number in issue_numbers:
            try:
                outcome = await self.waive(repo, number, pre_impl_gates)
            except Exception as exc:  # noqa: BLE001 — never crash the pipeline
                log.error(
                    "[apis.gate_waive] waive raised on %s#%s: %s — recording "
                    "as a failure and continuing with the remaining parents",
                    repo,
                    number,
                    exc,
                )
                outcome = WaiveOutcome(
                    entity_found=True,
                    targeted=list(pre_impl_gates),
                    failed=list(pre_impl_gates),
                )
            aggregate.per_issue.append((number, outcome))
        return aggregate


# ── Multi-parent waive (ateles#390) ──────────────────────────────────────────
#
# ``/confirm-gates-clear`` posted on a PR used to resolve its target as
# ``_parent_issue_number(body) or trigger.number``.  The ``or`` fallback is the
# bug: a PR carries no ``gate_status`` (gates live on the ISSUE entity), so when
# the PR body has no parent link the waive was applied to the PR number and
# could only ever miss.  Worse, once ateles#416 added entity backfill, that miss
# stopped being a loud "no entity" error and started BACKFILLING an issue entity
# for the PR number, then waiving gates on that phantom — a command reporting
# success while the real parent's gates stayed pending.
#
# Two corrections, both here:
#   * resolution never falls back to the PR number (swarm_dispatch side), and
#     an unresolvable parent is its own loud failure mode (`unresolved`);
#   * a PR may close MORE THAN ONE issue, so the waive fans out over every
#     parent instead of only the first match.


@dataclass
class AggregateWaiveOutcome:
    """Result of waiving across every resolved parent issue of a trigger.

    ``per_issue`` preserves resolution order so the operator comment can name
    which issue landed and which did not.  ``unresolved`` is the distinct
    failure the old code could not express: the command was issued on a PR whose
    body names no parent issue at all, so there is nothing to waive — which is
    NOT the same as "the parent's Neotoma entity is missing" and must not be
    reported with that message.
    """

    per_issue: list[tuple[int, WaiveOutcome]] = field(default_factory=list)
    unresolved: bool = False

    @property
    def waived_issues(self) -> list[int]:
        return [n for n, o in self.per_issue if o.waived]

    @property
    def failed_issues(self) -> list[int]:
        return [n for n, o in self.per_issue if not o.ok]

    @property
    def issue_numbers(self) -> list[int]:
        return [n for n, _ in self.per_issue]

    @property
    def ok(self) -> bool:
        """True only when a target was resolved AND every target cleared.

        An empty ``per_issue`` is never ``ok``: "waived nothing, successfully"
        is exactly the false-success the whole module exists to prevent.
        """
        return (
            not self.unresolved
            and bool(self.per_issue)
            and all(o.ok for _, o in self.per_issue)
        )


def format_waive_comment_multi(
    marker: str,
    header: str,
    aggregate: AggregateWaiveOutcome,
) -> str:
    """Render the operator-visible comment for a possibly-multi-parent waive.

    Delegates per-parent rendering to :func:`format_waive_comment` so the
    no-command-token self-trigger defence (neotoma#1686) is inherited rather
    than re-implemented, and prefixes each block with the issue number it
    describes — a bare pass/fail naming no issue is not actionable when a PR
    has two parents and only one of them landed.
    """
    if aggregate.unresolved:
        return "\n".join(
            [
                marker,
                header,
                "",
                "⚠️ **Gate waive could not be applied** — this pull request's "
                "body names no parent issue, so there is no issue entity whose "
                "gates could be waived. Gate state lives on the ISSUE, never on "
                "the PR.",
                "",
                "Link the PR to its parent by adding a line such as "
                "`Closes #123` (or `Part of #123` for a partial "
                "implementation) to the PR description, then re-issue the "
                "command. Nothing was written.",
            ]
        )

    if not aggregate.per_issue:
        # Defensive: a resolved-but-empty target list is a programming error,
        # not an operator error. Report it as a failure, never as success.
        return "\n".join(
            [
                marker,
                header,
                "",
                "❌ **Gate waive resolved no target issue** — nothing was "
                "written. This is a dispatcher bug; the pipeline stays blocked.",
            ]
        )

    if len(aggregate.per_issue) == 1:
        number, outcome = aggregate.per_issue[0]
        body = format_waive_comment(
            marker=marker,
            header=header,
            waived=outcome.waived,
            already_clear=outcome.already_clear,
            failed=outcome.failed,
            entity_found=outcome.entity_found,
        )
        # Name the issue the waive actually targeted. The operator issues the
        # command on the PR; without this line they cannot tell which entity
        # was written, which is how a waive against the wrong number went
        # unnoticed for a month (ateles#390).
        lines = body.split("\n")
        lines.insert(2, f"\nTarget issue: **#{number}**")
        return "\n".join(lines)

    lines = [
        marker,
        header,
        "",
        f"This pull request has **{len(aggregate.per_issue)} parent issues**. "
        "Gate waive was applied to each independently:",
    ]
    for number, outcome in aggregate.per_issue:
        lines.append("")
        lines.append(f"### #{number}")
        block = format_waive_comment(
            marker="",
            header="",
            waived=outcome.waived,
            already_clear=outcome.already_clear,
            failed=outcome.failed,
            entity_found=outcome.entity_found,
        )
        # Drop the empty marker/header lines the single-issue formatter emits.
        lines.extend(
            line for line in block.split("\n")[3:] if line or lines[-1]
        )
    return "\n".join(lines)

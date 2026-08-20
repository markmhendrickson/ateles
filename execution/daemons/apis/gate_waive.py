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

log = logging.getLogger("apis.gate_waive")


# Gate states that count as ALREADY CLEARED — never re-waive these.
CLEARED_GATE_STATES: frozenset[str] = frozenset(
    {"signed_off", "waived", "not_required", "skipped"}
)

# The value a waived gate is set to.
WAIVED = "waived"


# ── Pure helpers (no I/O — unit tested directly) ─────────────────────────────


def parse_snapshot_list_field(raw: object) -> list[dict]:
    """Normalize a stored LIST-valued snapshot field into a list of dicts.

    The list-shaped sibling of ``parse_gate_status``. Neotoma's
    ``/entities/query`` returns list-valued snapshot fields as JSON STRINGS
    (schema inference types them as strings), so a reader that assumes a parsed
    list iterates characters and raises ``AttributeError`` on the first
    ``.get()``. That shipped in ateles#442's drift check and crashed on every
    daemon startup for hours; fail-open swallowed it, so the check reported
    nothing — indistinguishable from reporting clean (#450).

    Tolerates the shapes seen in prod:
      * a real list of dicts,
      * a JSON-encoded string,
      * anything else / missing / malformed → ``[]``.

    Non-dict entries are dropped rather than raising: a partially-malformed
    field should degrade to the rows that parse, not lose all of them.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


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

        Tolerates the duplicated fields seen in prod: ``repo``/``repository``
        and ``issue_number``/``github_number``.
        """
        snap_repo = snap.get("repo") or snap.get("repository") or ""
        if str(snap_repo) != str(repo):
            return False
        for key in ("issue_number", "github_number"):
            value = snap.get(key)
            if value is not None and str(value) == str(issue_number):
                return True
        return False

    async def load(self, repo: str, issue_number: int) -> IssueGateState:
        """Retrieve the gate state for ``repo#issue_number``.

        NOTE: prod exposes the read as POST ``/entities/query``, NOT
        ``/retrieve_entities`` (which 404s) — see issue_spec.py for the same
        gotcha. A missing token / entity / error degrades to an EMPTY state
        with ``found == False``, which the caller reports rather than ignores.
        """
        state = IssueGateState(repo=repo, issue_number=issue_number)
        data = await self._post(
            "entities/query",
            {
                "entity_type": self.ENTITY_TYPE,
                "limit": 500,
                "include_snapshots": True,
            },
        )
        if not data:
            return state
        for entity in data.get("entities", []):
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

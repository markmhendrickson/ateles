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

try:  # package import (normal daemon runtime) with script-import fallback
    from lib.daemon_runtime import neotoma_signed as _ns
except ImportError:  # pragma: no cover
    import neotoma_signed as _ns  # type: ignore

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

    This function alone cannot distinguish "genuinely absent" from
    "malformed/unreadable" — both degrade to ``{}`` here, which is correct
    for `waive()` (an absent gate is simply unsigned and IS a legitimate
    waive target) but WRONG for `sign_off`, which must refuse to write
    through an unreadable map rather than silently treating it as empty
    (Falco's CONFIRMED BLOCKING finding, ateles#795 / PR #1181; see
    `gate_status_is_unreadable` below, which callers that need the
    distinction call ALONGSIDE this one rather than this function changing
    shape for one caller and not the other).
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


def gate_status_is_unreadable(raw: object) -> bool:
    """True when *raw* is PRESENT but could not be parsed as a gate map.

    Distinct from "absent" (``raw`` is ``None`` or ``""``, or already the
    empty dict ``{}``) — an absent gate_status is a real, legitimate state
    (a freshly triaged issue that has not been extended with gates yet) that
    `parse_gate_status` correctly folds to ``{}``. This function names the
    OTHER case that folds to the exact same ``{}``: invalid JSON, a JSON
    array/scalar instead of an object, or any other type `parse_gate_status`
    cannot interpret. `sign_off` must treat unreadable as UNKNOWN and refuse
    (principles §§5 and 7 — unknown stays distinct from a conclusion, and the
    restrictive branch is the default for the field that carries the safety
    meaning), never reconstruct a fresh map from the empty parse and write
    through it, which would silently discard whatever sibling gate state the
    unreadable value actually held.
    """
    if raw is None:
        return False
    if isinstance(raw, dict):
        return False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return False
        try:
            decoded = json.loads(text)
        except (ValueError, TypeError):
            return True
        return not isinstance(decoded, dict)
    # Any other type (list, int, bool, ...) was PRESENT but is not a map.
    return True


def parse_owner_history(raw: object) -> list[dict]:
    """Normalize a stored ``owner_history`` into a list of dicts.

    See `parse_gate_status`'s docstring: this folds "absent" and "unreadable"
    to the same ``[]`` for the same reason, and `owner_history_is_unreadable`
    below is the companion that tells the two apart for a caller (`sign_off`)
    that must refuse on the latter.
    """
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


def owner_history_is_unreadable(raw: object) -> bool:
    """True when *raw* is PRESENT but could not be parsed as a history list.

    Companion to `gate_status_is_unreadable` — see its docstring for why the
    absent/unreadable distinction matters and why it is named here rather
    than changing what `parse_owner_history` returns.
    """
    if raw is None:
        return False
    if isinstance(raw, list):
        return False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return False
        try:
            decoded = json.loads(text)
        except (ValueError, TypeError):
            return True
        return not isinstance(decoded, list)
    return True


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


# ── Lens-signed gate sign-off (ateles#795 amended ADR) ───────────────────────
#
# The dispatcher (Apis) records a gate-owning lens's verdict, but the WRITE
# must carry the LENS's own AAuth identity, never the daemon's shared bearer
# token — otherwise the write is attributed to Apis and the gate record lies
# about who reviewed. `waive()` above is the operator-override sibling of
# this: same target field, same re-read-and-verify shape, different actor
# (the operator, via the daemon bearer, is legitimate there — an operator
# override IS the daemon's own act) and a different value (`waived` vs
# `signed_off`).
#
# Fail-closed error CLASSES, surfaced the way `review_failure_class` in
# swarm_dispatch.py surfaces panel failures — a short, stable token, never a
# stack trace, a token, a key path, or a raw response body (Buteo's review,
# ateles#795).
SIGN_OFF_NO_SIGNING_KEY = "sign_off: no AAuth key for lens"
SIGN_OFF_SIGNING_FAILED = "sign_off: signed write failed"
SIGN_OFF_ENTITY_NOT_FOUND = "sign_off: no issue entity"
# Named for what the check actually is (no head_sha was supplied at all), not
# for a comparison this method cannot make. Renamed from `SIGN_OFF_HEAD_MISMATCH`
# (Loxia review nit + Falco security review, PR #1181): `gate_status` carries no
# per-gate head pin in prod, so there is nothing to "mismatch" against — the
# only check this method makes is that *a* head_sha was named at all. The old
# name read, at a skim, as "the head changed since review", which is not what
# happened and is not what this error means.
SIGN_OFF_NO_HEAD = "sign_off: no head_sha supplied"
SIGN_OFF_VERIFY_FAILED = "sign_off: write did not read back"
SIGN_OFF_GATE_NOT_PENDING = "sign_off: gate not pending for this lens"
# Falco's CONFIRMED BLOCKING finding, ateles#795 / PR #1181: the stored
# gate_status or owner_history was PRESENT but unparseable (invalid JSON, or
# decoded to something other than the expected map/list shape) — distinct
# from a legitimately absent field, which `parse_gate_status`/
# `parse_owner_history` already fold to {}/[] and `sign_off` is happy to
# treat as pending. An unreadable value is UNKNOWN, not empty: writing a
# freshly reconstructed map through it would silently discard whatever
# sibling gate state it actually held.
SIGN_OFF_UNREADABLE_STATE = "sign_off: gate_status or owner_history unreadable"

# Gate states some OTHER authority already set, which `sign_off` must treat as
# a true no-op (no write attempted at all) rather than something to (re-)sign:
# an operator `waive`, a triage-time `not_required`/`not_applicable`, or a
# `skipped` gate are all terminal states this lens's verdict does not own.
# Deliberately narrower than `CLEARED_GATE_STATES` (which also includes
# `signed_off`): `signed_off` IS this method's own state, and a lens signing
# off on an already-`signed_off` gate must still perform the signed write and
# read-back (see the "always re-sign" note in `sign_off`'s docstring) so the
# latest observation on record is always attributable to the reviewing lens,
# not to whichever session cleared it first.
_SIGN_OFF_OTHER_AUTHORITY_STATES: frozenset[str] = frozenset(
    {"waived", "not_required", "not_applicable", "skipped"}
)

# Fields actually declared on the production `issue` entity schema (confirmed
# by Waxwing's `describe_entity_type` read on ateles#795, and matching
# `parse_gate_status`/`parse_owner_history` above, which already round-trip
# both). `gate_writeback_outcome` is NOT on this list — see the module-level
# note in `sign_off()` below. Neotoma silently drops undeclared fields on
# write rather than erroring, so this module writes ONLY what is on this
# list, never a field it would like to exist.
#
# `current_owner` (PR #1181, Cicada's provider-table round): `IssueGateState`
# already READS this field from the entity snapshot (`_load` below,
# `state.current_owner = str(snap.get("current_owner") or "")`), which is how
# `workflow_owner_drift` and the prompts' own handoff logic observe it today.
# A snapshot read returns declared schema fields, not `raw_fragments` — the
# same signal `_SIGN_OFF_DECLARED_FIELDS`'s existing two entries rest on — so
# it is included here on that basis. This was NOT independently re-confirmed
# against prod via a fresh `describe_entity_type`/snapshot call in this PR (no
# Neotoma credential was available in the sandbox this round ran in); the
# `sign_off_advances_owner` write path below is exercised only against the
# same mocked-store shape every other `sign_off` test uses, so a stale
# assumption here would surface as `SIGN_OFF_VERIFY_FAILED` on the very next
# live sign-off, exactly as it would for any other undeclared field, not as a
# schema corruption.
_SIGN_OFF_DECLARED_FIELDS = frozenset({"gate_status", "owner_history", "current_owner"})


@dataclass
class SignOffOutcome:
    """Result of one lens's dispatcher-mediated, lens-signed gate sign-off."""

    ok: bool = False
    gate: str = ""
    lens_agent: str = ""
    lens_sub: str = ""
    error: str = ""  # one of the SIGN_OFF_* class constants above, or ""
    verified: bool = False


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
    # True when the RAW stored value for gate_status / owner_history was
    # PRESENT but could not be parsed (invalid JSON, wrong type) — distinct
    # from a legitimately absent field, which parses to {} / [] cleanly.
    # `sign_off` refuses to write when either is True (Falco's CONFIRMED
    # BLOCKING finding, ateles#795 / PR #1181): a caller must never rebuild a
    # fresh map from an empty parse of an unreadable value and write through
    # it, which would silently discard whatever sibling gate state that
    # value actually held.
    gate_status_unreadable: bool = False
    owner_history_unreadable: bool = False
    # Per-field REDUCER provenance ({field_name: observation_id}), read
    # straight off the snapshot the same way `agent_loader.py`'s
    # `_parse`/`lib.daemon_runtime.gating.read_authenticated_checkpoint_*`
    # already do. This is what lets `sign_off`'s read-back name the EXACT
    # observation `gate_status` came from, rather than trusting the mutable
    # snapshot value alone (Falco's CONFIRMED BLOCKING attribution finding).
    field_provenance: dict[str, str] = field(default_factory=dict)

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

    async def _observations(self, entity_id: str, *, limit: int = 100) -> list[dict]:
        """Read-only fetch of an entity's observations, newest first.

        Mirrors `lib.daemon_runtime.gating._fetch_entity_observations` — the
        SAME read-only endpoint (`GET /entities/{id}/observations`) that
        module already uses to resolve per-field provenance and attribution
        for checkpoint approvals (`read_authenticated_checkpoint_resolution`).
        Reused here rather than re-implemented (principles §§6 and 9) because
        `sign_off`'s attribution read-back (Falco's CONFIRMED BLOCKING
        finding, ateles#795 / PR #1181) needs exactly the same shape: an
        observation's own `provenance.agent_sub`, not the mutable entity
        snapshot the `correct` reducer produces. `gate_waive.py` cannot
        import that private helper directly (it takes no base_url/token —
        it reads its own module-level `NEOTOMA_BASE_URL`/
        `NEOTOMA_BEARER_TOKEN` globals, which would silently point this
        lens-signed store at the DAEMON's own config instead of the base_url
        this store was constructed with), so the same GET is issued here
        under this store's own `base_url`/`token`.

        Best-effort: any transport/parse failure returns ``[]``, which the
        caller treats as UNKNOWN (unattributed), never as "no observations
        exist" being evidence of anything — the caller decides what an empty
        result means, this method only reports what it could read.
        """
        if not self.token:
            log.warning(
                "[apis.gate_waive] NEOTOMA_BEARER_TOKEN unset — observations "
                "read skipped for %s",
                entity_id,
            )
            return []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{self.base_url}/entities/{entity_id}/observations",
                    headers=self._headers(),
                    params={"limit": limit},
                )
                resp.raise_for_status()
                data = resp.json() if resp.content else {}
                observations = data.get("observations") if isinstance(data, dict) else None
                return observations if isinstance(observations, list) else []
        except Exception as exc:  # noqa: BLE001 — never crash the pipeline
            log.warning(
                "[apis.gate_waive] could not fetch observations for %s: %s",
                entity_id,
                exc,
            )
            return []

    @staticmethod
    def _matches(snap: dict, repo: str, issue_number: int) -> bool:
        """True when *snap* is the issue entity for ``repo#issue_number``.

        Tolerates the duplicated fields seen in prod: ``repo``/``repository``
        and ``issue_number``/``github_number``/``number``.

        ateles#390: ``number`` was missing from this list, and it is the field
        the triage path actually writes. Measured against prod on 2026-09-02,
        ``ent_e882a86eb583b828ac00f98b`` (ateles#390's own entity) stores
        ``number: 390`` with no ``issue_number`` and no ``github_number``, so
        both the server-side filter and this client-side matcher missed it and
        the store reported "no Neotoma issue entity" for an entity that plainly
        exists — the exact error in the issue's symptom log.
        """
        snap_repo = snap.get("repo") or snap.get("repository") or ""
        if str(snap_repo) != str(repo):
            return False
        for key in ("issue_number", "github_number", "number"):
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
        ``number`` either — reported "no Neotoma issue entity". Both number
        fields are now tried, so the fast path works for either shape.
        """
        state = IssueGateState(repo=repo, issue_number=issue_number)
        entities: list[dict] = []
        for number_field in ("number", "github_number", "issue_number"):
            data = await self._post(
                "entities/query",
                {
                    "entity_type": self.ENTITY_TYPE,
                    "limit": 10,
                    "include_snapshots": True,
                    "snapshot_filters": {
                        number_field: {"op": "eq", "value": issue_number},
                        "repo": {"op": "eq", "value": repo},
                    },
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
            raw_history = snap.get("owner_history")
            state.gate_status_was_string = isinstance(raw_gates, str)
            state.gate_status = parse_gate_status(raw_gates)
            state.owner_history = parse_owner_history(raw_history)
            state.gate_status_unreadable = gate_status_is_unreadable(raw_gates)
            state.owner_history_unreadable = owner_history_is_unreadable(raw_history)
            state.current_owner = str(snap.get("current_owner") or "")
            raw_provenance = snap.get("provenance")
            if isinstance(raw_provenance, dict):
                state.field_provenance = {
                    str(k): str(v) for k, v in raw_provenance.items() if v
                }
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

    async def sign_off(
        self,
        repo: str,
        issue_number: int,
        gate: str,
        lens_agent: str,
        head_sha: str,
        next_owner: str = "",
    ) -> SignOffOutcome:
        """Record ``gate_status.<gate>`` = ``"signed_off"``, SIGNED as *lens_agent*.

        ``next_owner`` (PR #1181, provider-table round): when supplied, ALSO
        advances ``current_owner`` to this value in the SAME signed write —
        moving the phase-handoff mutation the lens's own prompt used to make
        via an in-session `correct()` (see docs/agents/pavo.md's "Gate
        handoff" recipe, still generated that way — tracked as a post-deploy
        follow-up, not edited by this PR) onto the dispatcher's signed path,
        same as `gate_status` itself. This method does NOT decide who the
        next owner is: `swarm_dispatch` has no gate-to-next-owner resolver
        today (that routing — including the label fast-paths and the
        interface-surface override — lives only in the per-lens prompts), and
        inventing one here would be a second, parallel version of that same
        routing table. Left "" (the default), no `current_owner` write is
        attempted and behaviour is identical to before this parameter existed.

        ateles#795 amended ADR: the write is the SYSTEM OF RECORD, and it must
        be attributed to the reviewing lens, not to Apis. This method never
        falls back to the daemon bearer on any failure — that would silently
        reproduce the exact bug this exists to fix (constraint 1). Every
        failure returns ``SignOffOutcome(ok=False, error=<class>)`` instead;
        callers surface the class the way `review_failure_class` does for
        panel failures, and never treat a non-2xx or a raised exception as a
        signal to retry with the bearer.

        ALWAYS RE-SIGNS an already-``signed_off`` gate (Falco's security review,
        PR #1181): a caller-observed ``signed_off`` snapshot proves nothing
        about WHO signed it — the pre-amendment bug was exactly a shared-bearer
        write landing unattributed, and a naive "already cleared -> no-op"
        short-circuit would let that same unattributed write pass through as a
        verified lens sign-off merely because the value happened to already
        match. So the write and its read-back below are NOT skipped for
        ``signed_off`` (only for a genuinely different authority's terminal
        state — see ``_SIGN_OFF_OTHER_AUTHORITY_STATES``): the write is
        idempotent (same value, freshly re-signed idempotency key), so
        performing it again costs nothing and its read-back is always the
        LENS's own latest observation, never a stale or differently-attributed
        one this call merely trusted.

        Mirrors `waive()`'s safety shape, narrowed to ONE gate and ONE lens:
          1. Re-read current state (never trust a caller-supplied snapshot —
             `waive()`'s own docstring names this as the #241 failure mode).
          2. Refuse only if the gate is a DIFFERENT authority's terminal state
             (`waived`/`not_required`/`not_applicable`/`skipped`) or an
             unrecognized non-pending value — never for `pending` or an
             already-`signed_off` gate, both of which this lens may write (the
             latter is a re-sign, per the note above). Mirrors
             `IssueGateStore._matches`'s (repo, issue) exactness, narrowed
             further to (repo, issue, gate).
          3. Require *head_sha* (the exact commit the lens's clean verdict was
             FOR — `swarm_dispatch` already resolves this as `review_head`
             before the panel runs). `gate_status` carries no per-gate head
             pin in prod to compare against, so the freshness guarantee is
             step 1's fresh re-read itself: current state is loaded INSIDE
             this call, never trusted from a caller-held snapshot, which is
             the same "exact durable state" discipline `waive()`/`_matches`
             apply to (repo, issue) — extended here so a head is always named
             even though there is nowhere yet to store it for comparison.
          4. Sign the write with *lens_agent*'s OWN key and an EXPLICIT
             subject `<lens_agent>@ateles-swarm` — never the ambient
             `NEOTOMA_AAUTH_SUB` this process (Apis) may itself carry
             (constraint 2; see `neotoma_signed.agent_identity`'s docstring).
          5. Write ONLY declared schema fields (`gate_status`, `owner_history`,
             and `current_owner` when *next_owner* is supplied — constraint 4).
             No `gate_writeback_outcome` or other field is attempted; see the
             module-level note above.
          6. READ BACK and assert the field holds exactly what was written
             (constraint 3c) — a 2xx from `signed_request` is not evidence a
             write landed on this codebase's own documented history of
             undeclared-field drops and fire-and-forget writes.

        Raises nothing: every failure path returns a populated
        ``SignOffOutcome`` so the caller can decide (retry, escalate, leave
        pending) rather than crash the dispatch loop.
        """
        lens_sub = f"{lens_agent}@ateles-swarm"
        outcome = SignOffOutcome(gate=gate, lens_agent=lens_agent, lens_sub=lens_sub)

        # Constraint 1 — fail closed BEFORE any write attempt if this lens has
        # no signing key. Checking here, not inside the try/except below,
        # keeps "no key" and "signing raised" as distinct, legible classes.
        if _ns.agent_identity(lens_agent, sub=lens_sub) is None:
            outcome.error = SIGN_OFF_NO_SIGNING_KEY
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s "
                "— refusing to fall back to the daemon bearer",
                repo,
                issue_number,
                gate,
                lens_agent,
                SIGN_OFF_NO_SIGNING_KEY,
            )
            return outcome

        state = await self.load(repo, issue_number)
        if not state.found:
            outcome.error = SIGN_OFF_ENTITY_NOT_FOUND
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s",
                repo,
                issue_number,
                gate,
                lens_agent,
                SIGN_OFF_ENTITY_NOT_FOUND,
            )
            return outcome

        if state.gate_status_unreadable or state.owner_history_unreadable:
            # Falco's CONFIRMED BLOCKING finding, ateles#795 / PR #1181: a
            # PRESENT-but-unparseable gate_status/owner_history is UNKNOWN,
            # not empty. `parse_gate_status`/`parse_owner_history` already
            # folded it to {}/[] above (the shape every other branch below
            # reads), so — unlike a genuinely absent field — writing through
            # that empty reconstruction here would silently discard whatever
            # sibling gate state the unreadable value actually held. Refuse
            # before any write is attempted, exactly like the no-signing-key
            # precondition above.
            outcome.error = SIGN_OFF_UNREADABLE_STATE
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s "
                "(gate_status_unreadable=%s, owner_history_unreadable=%s)",
                repo,
                issue_number,
                gate,
                lens_agent,
                SIGN_OFF_UNREADABLE_STATE,
                state.gate_status_unreadable,
                state.owner_history_unreadable,
            )
            return outcome

        current = (state.gate_status.get(gate) or "").strip().lower()
        if current in _SIGN_OFF_OTHER_AUTHORITY_STATES:
            # A gate some OTHER authority already cleared (operator `waive`,
            # `not_required`/`not_applicable` at triage, `skipped`) is a true
            # no-op: this lens's sign_off must never overwrite a different
            # actor's terminal state, and there is nothing to (re-)sign here —
            # skip both the write and the read-back below.
            outcome.ok = True
            outcome.verified = True
            log.info(
                "[apis.gate_waive] sign_off %s#%s gate=%s: already %r "
                "(other authority) — no-op, no write attempted",
                repo,
                issue_number,
                gate,
                current,
            )
            return outcome
        if current and current != "pending" and current != "signed_off":
            # Some other non-cleared, non-pending value (unexpected shape) —
            # do not overwrite a state this method does not understand.
            outcome.error = SIGN_OFF_GATE_NOT_PENDING
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s: current state %r "
                "is neither pending nor cleared — refusing to write",
                repo,
                issue_number,
                gate,
                current,
            )
            return outcome

        if not head_sha:
            # Required, not optional: the caller must name the exact commit
            # its clean verdict was FOR (swarm_dispatch already resolves this
            # as `review_head` before the panel runs). `gate_status` itself
            # carries no per-gate head pin in prod (see this module's shape
            # notes) — there is no stored value to compare against — so the
            # freshness guarantee this method gives is the re-read immediately
            # above (current state is loaded fresh, inside this call, not
            # passed in stale by the caller), matching `waive()`'s own
            # "re-read before write" shape for the (repo, issue, gate) triple.
            # An empty head_sha means the caller skipped resolving one, which
            # is itself a bug in the call site, not a state to write through.
            outcome.error = SIGN_OFF_NO_HEAD
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s: no head_sha supplied "
                "— refusing to certify a verdict against an unnamed commit",
                repo,
                issue_number,
                gate,
            )
            return outcome

        now = datetime.now(timezone.utc).isoformat()
        merged_gates = dict(state.gate_status)
        merged_gates[gate] = "signed_off"
        merged_history = list(state.owner_history) + [
            {
                "gate": gate,
                "action": "signed_off",
                "actor": lens_agent,
                "reason": f"lens-signed sign-off via AAuth sub {lens_sub}",
                "timestamp": now,
            }
        ]
        key = f"{repo}#{issue_number}"

        fields_to_write: list[tuple[str, object]] = [
            ("gate_status", self._encode_gate_status(state, merged_gates)),
            ("owner_history", merged_history),
        ]
        next_owner = next_owner.strip()
        if next_owner:
            fields_to_write.append(("current_owner", next_owner))

        for field_name, value in fields_to_write:
            # Constraint 4 — never write a field this module has not confirmed
            # is declared on the production schema.
            assert field_name in _SIGN_OFF_DECLARED_FIELDS, (
                f"sign_off attempted to write undeclared field {field_name!r}"
            )
            try:
                status, _resp = await _ns.signed_request(
                    "POST",
                    f"{self.base_url}/correct",
                    {
                        "entity_id": state.entity_id,
                        "entity_type": self.ENTITY_TYPE,
                        "field": field_name,
                        "value": value,
                        "idempotency_key": (
                            f"gate-signoff-{gate}-{key}-{field_name}-{now[:16]}"
                        ),
                    },
                    agent_name=lens_agent,
                    sub=lens_sub,
                )
            except Exception as exc:  # noqa: BLE001 — classify, never crash
                # Error CLASS only in the log line — never the exception's
                # full text, which can carry a signed-request body, an auth
                # header, or a filesystem path (Buteo's review, ateles#795).
                outcome.error = SIGN_OFF_SIGNING_FAILED
                log.error(
                    "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s field=%s: "
                    "%s (%s)",
                    repo,
                    issue_number,
                    gate,
                    lens_agent,
                    field_name,
                    SIGN_OFF_SIGNING_FAILED,
                    type(exc).__name__,
                )
                return outcome
            if status < 200 or status >= 300:
                # Falco's security review, PR #1181 (NON-BLOCKING, defense in
                # depth): `signed_request` returns its HTTP status rather than
                # raising on a non-2xx, so a caller that ignores it relies
                # entirely on the read-back below to catch a rejected write.
                # The read-back IS fail-closed on its own (constraint 3c), but
                # treating a non-2xx as an explicit failure CLASS here — rather
                # than silently falling through to a read-back that happens to
                # also fail — makes the failure attributable to the write
                # itself, not to an unrelated-looking verify-failed reread.
                outcome.error = SIGN_OFF_SIGNING_FAILED
                log.error(
                    "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s field=%s: "
                    "%s (HTTP %s)",
                    repo,
                    issue_number,
                    gate,
                    lens_agent,
                    field_name,
                    SIGN_OFF_SIGNING_FAILED,
                    status,
                )
                return outcome

        # Constraint 3c / CLAUDE.md "read it back" — a successful signed POST
        # is not evidence the write landed.
        reread = await self.load(repo, issue_number)
        if not reread.found or (reread.gate_status.get(gate) or "").strip().lower() != "signed_off":
            outcome.error = SIGN_OFF_VERIFY_FAILED
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s "
                "(read-back state=%r)",
                repo,
                issue_number,
                gate,
                lens_agent,
                SIGN_OFF_VERIFY_FAILED,
                reread.gate_status.get(gate) if reread.found else "(entity missing)",
            )
            return outcome

        # Same read-it-back discipline for `current_owner` when a handoff was
        # requested — a landed `gate_status` write is not evidence the SAME
        # POST's `current_owner` field also survived (this codebase's own
        # documented history of undeclared/dropped-field writes is exactly why
        # each field gets its own read-back rather than one covering both).
        if next_owner and reread.current_owner.strip() != next_owner:
            outcome.error = SIGN_OFF_VERIFY_FAILED
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s "
                "(current_owner read-back=%r, wanted=%r)",
                repo,
                issue_number,
                gate,
                lens_agent,
                SIGN_OFF_VERIFY_FAILED,
                reread.current_owner,
                next_owner,
            )
            return outcome

        # Falco's CONFIRMED BLOCKING attribution finding, ateles#795 / PR
        # #1181: the two checks above prove the VALUE landed, not WHO wrote
        # it — an already-`signed_off` gate is, by this method's own design,
        # re-signed rather than skipped (see the docstring's "ALWAYS
        # RE-SIGNS" note) precisely because a value match proves nothing
        # about attribution. So the terminal proof here is the immutable
        # observation `gate_status`'s reducer provenance now names: it must
        # exist, must be NEWER than this call's own pre-write read (`now`,
        # captured before any write was attempted), and must carry THIS
        # lens's own `agent_sub` (`lens_sub`) — never the daemon's. Mirrors
        # `lib.daemon_runtime.gating.read_authenticated_checkpoint_resolution`,
        # the existing pattern for exactly this shape of proof (principles
        # §§6 and 9): resolve the observation id from per-field provenance,
        # fetch it, and read `provenance.agent_sub` off THAT observation —
        # never off the mutable snapshot. Missing, unreadable, stale, or
        # mismatched attribution means `verified=False` (fail closed, per
        # principles §§5 and 7), even though `outcome.ok` is already True
        # for the value having landed.
        observation_id = reread.field_provenance.get("gate_status", "")
        attributed = False
        if observation_id:
            observations = await self._observations(reread.entity_id)
            observation = next(
                (o for o in observations if isinstance(o, dict) and o.get("id") == observation_id),
                None,
            )
            if isinstance(observation, dict):
                obs_provenance = observation.get("provenance")
                observed_at = str(
                    observation.get("created_at")
                    or observation.get("observed_at")
                    or observation.get("timestamp")
                    or ""
                )
                fresh = bool(observed_at) and observed_at >= now
                if (
                    isinstance(obs_provenance, dict)
                    and str(obs_provenance.get("agent_sub") or "").strip() == lens_sub
                    and fresh
                ):
                    attributed = True
                else:
                    log.error(
                        "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: "
                        "observation %s attribution did not verify "
                        "(agent_sub=%r, observed_at=%r, fresh=%s)",
                        repo,
                        issue_number,
                        gate,
                        lens_agent,
                        observation_id,
                        obs_provenance.get("agent_sub") if isinstance(obs_provenance, dict) else None,
                        observed_at,
                        fresh,
                    )
        else:
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: no "
                "field-provenance entry for gate_status on read-back — "
                "cannot attribute the write to %s",
                repo,
                issue_number,
                gate,
                lens_agent,
                lens_sub,
            )

        if not attributed:
            # Fail closed (principles §§5, 7): the VALUE landed, but this
            # call cannot prove IT was the one that put it there, so it must
            # not report success. `ok=False` routes this through the same
            # `failed_sign_offs` escalation path as any other sign_off
            # failure — never a silent partial success.
            outcome.ok = False
            outcome.verified = False
            outcome.error = SIGN_OFF_VERIFY_FAILED
            return outcome

        outcome.ok = True
        outcome.verified = True
        log.info(
            "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s (sub=%s): "
            "verified signed_off%s",
            repo,
            issue_number,
            gate,
            lens_agent,
            lens_sub,
            f", current_owner->{next_owner}" if next_owner else "",
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

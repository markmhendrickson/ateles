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

import asyncio
import inspect
import json
import logging
import os
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:  # package import (normal daemon runtime) with script-import fallback
    from lib.daemon_runtime import neotoma_signed as _ns
    from lib.daemon_runtime.aauth_httpsig import jwk_thumbprint, public_part_of
    # The checkpoint verifier's own trusted-tier set, reused rather than
    # re-typed so the two attribution checks cannot drift apart.
    from lib.daemon_runtime.gating import _TRUSTED_AAUTH_TIERS
except ImportError:  # pragma: no cover
    import neotoma_signed as _ns  # type: ignore
    from aauth_httpsig import jwk_thumbprint, public_part_of  # type: ignore
    from gating import _TRUSTED_AAUTH_TIERS  # type: ignore

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


def uncleared_gates(
    gate_status: dict[str, str],
    gates: tuple[str, ...] | list[str],
    declared_gates: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """THE gate-clearance predicate. Every caller in the swarm derives from it.

    Returns the subset of *gates*, in order, that are NOT cleared — where
    "cleared" means the stored state is in `CLEARED_GATE_STATES`, or the gate
    is one this issue's workflow does not declare at all.

    WHY THIS FUNCTION EXISTS (ateles#1213)
    --------------------------------------
    Two predicates over one vocabulary drifted. `gates_needing_waive` here read
    an absent gate as ``""``; `swarm_dispatch._gates_green` read the same
    absence as ``"pending"``. Both then refused to clear it — so an absent gate
    blocked build handoff FOREVER, because nothing can sign a gate that does
    not exist. 18 distinct ateles issues logged "not handing off to build" on
    2026-09-23 for exactly this reason.

    The lazy repair — treat every absence as cleared — converts a stuck
    pipeline into an UNGUARDED one: a gate that genuinely applies and is merely
    unwritten would then wave build through unreviewed. So absence is resolved
    against the issue's OWN workflow, which is the authority on which gates
    apply:

      * *declared_gates* is None  → the workflow could not be read. Absence is
        UNKNOWN, and unknown takes the restrictive branch: the gate BLOCKS.
        (`docs/foundation/principles.md#5`.)
      * gate not in *declared_gates* → this workflow never runs that gate. It
        can never be signed, so holding it pending is not caution, it is a
        deadlock. It CLEARS.
      * gate in *declared_gates*, key absent → the gate applies and nobody has
        written it yet. It BLOCKS, exactly as before.

    The live evidence for the third rule being distinct from the second:
    ateles#1209 (`workflow_type: bug`) binds
    `workflow_definition_id: ent_1b6d0acbdc436d3f0dad5a0d`, the
    ``ateles|bug`` workflow, whose declared gates are exactly
    ``pm, impl, pr_review, qa, release`` — key-for-key what its `gate_status`
    holds. The missing `ux`/`arch` keys are that workflow's own declaration,
    not an omission.

    Note the ABSENT value is compared as ``""``, never ``"pending"``. Inventing
    a blocking sentinel for a key that is not there is what made the two
    predicates disagree in the first place: it erases the difference between
    "written as pending" and "never written", which is the exact distinction
    this function is here to preserve.
    """
    out: list[str] = []
    for gate in gates:
        raw = gate_status.get(gate)
        if raw is None and declared_gates is not None and gate not in declared_gates:
            # The workflow does not run this gate. Nothing can ever sign it.
            continue
        if (raw or "").strip().lower() in CLEARED_GATE_STATES:
            continue
        out.append(gate)
    return out


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
    # Delegates to `uncleared_gates` with NO declared-gate set: a waive sweep
    # is deliberately total over *pre_impl_gates*, so an absent gate is a
    # legitimate waive target rather than something to skip. Sharing the
    # predicate is what stops the two from drifting again (ateles#1213).
    return uncleared_gates(gate_status, pre_impl_gates, declared_gates=None)


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
# The value landed, but the observation behind it does not carry this lens's
# subject, this lens's key thumbprint, and a trusted attribution tier, or it
# predates this call. Split out of `SIGN_OFF_VERIFY_FAILED` so the operator is
# told to look at the signer and the observation's age, not at the schema.
SIGN_OFF_ATTRIBUTION_FAILED = "sign_off: write not attributed to the lens"
# A sign-off failed AND the gate still reads `signed_off` afterwards: either the
# compensating restore could not be confirmed, or the gate already read
# `signed_off` before this call and this call could not re-sign it. Its own
# class because it is the one failure where the record reads CLEARED without a
# verified sign-off behind it, which must never be reported as "still pending".
SIGN_OFF_CLEARED_UNVERIFIED = "sign_off: gate may read cleared without a verified sign-off"
# Not a failure: another authority (an operator waive, triage's
# `not_required`) already cleared the gate, so no lens write was made. Kept
# distinct from a verified sign-off so nothing credits the lens with it.
SIGN_OFF_OTHER_AUTHORITY = "sign_off: gate already cleared by another authority"

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
# The wire name of the safety field, for callers and tests that must name it
# without spelling the literal (the foundation vocabulary retires the term;
# this module is where the live Neotoma field is still read and written).
GATE_STATUS_FIELD = "gate_status"


@dataclass
class SignOffOutcome:
    """Result of one lens's dispatcher-mediated, lens-signed gate sign-off."""

    ok: bool = False
    gate: str = ""
    lens_agent: str = ""
    lens_sub: str = ""
    error: str = ""  # one of the SIGN_OFF_* class constants above, or ""
    verified: bool = False
    # The gate's value as this call last READ it from the record — after a
    # failure, the value re-read once the failure was settled (and any
    # compensating restore attempted). Never an assumed value: the failure
    # surface prints it as the `observed` field.
    observed_state: str = ""


def _lens_key_thumbprint(identity: dict) -> str | None:
    """RFC 7638 thumbprint of the lens's own AAuth key, or None if unreadable.

    The attribution read-back compares an observation's `agent_thumbprint`
    against this, as `gating.read_authenticated_checkpoint_resolution` does
    for a checkpoint approver: a subject name alone is self-asserted, the key
    thumbprint is not. Computed from the SAME key file `signed_request` signs
    with (`identity["key"]`), via `aauth_httpsig.jwk_thumbprint`.
    """
    try:
        jwk = json.loads(Path(identity["key"]).read_text())
        return jwk_thumbprint(public_part_of(jwk)) or None
    except Exception:  # noqa: BLE001 — unreadable key means no provable attribution
        return None


def _parse_utc(value: object) -> datetime | None:
    """*value* as an aware UTC datetime, or None when it is not a timestamp.

    Accepts the ISO-8601 shapes seen on this path: a `Z` suffix or an explicit
    offset, with or without fractional seconds. A naive timestamp is read as
    UTC (Neotoma stamps are UTC). Comparing the raw STRINGS was wrong: a
    `...:05Z` stamp sorts after `...:05.500000+00:00` although it is half a
    second earlier (both security runs at e874537f).
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text[-1] in "Zz":
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _observation_time(observation: dict) -> datetime | None:
    return _parse_utc(
        observation.get("created_at")
        or observation.get("observed_at")
        or observation.get("timestamp")
    )


# ── Pre-deploy sign-offs (grandfathering by SERVER time) ─────────────────────
# Before lens-signed sign-offs deployed, every gate clearance was an unsigned
# write, so the re-proof below would hold every existing `signed_off` gate as
# pending. The operator names the deploy instant here; a `signed_off` value
# whose setting write Neotoma INGESTED before it counts as a legacy sign-off.
# Unset or unparseable means no grandfathering at all: every unsigned value is
# unproven (fail closed).
GATE_SIGNING_CUTOFF_ENV = "APIS_GATE_SIGNING_CUTOFF"


def gate_signing_cutoff() -> datetime | None:
    """The configured cutoff as an aware UTC datetime, or None.

    Read from `APIS_GATE_SIGNING_CUTOFF` at call time. It must be ISO 8601
    WITH an explicit offset or `Z` (`2026-09-24T09:00:00Z`): a naive time
    could be read in the wrong zone, which would move the cutoff by hours, so
    it is treated as unparseable. None — no grandfathering — when unset,
    empty, or unparseable.
    """
    raw = os.environ.get(GATE_SIGNING_CUTOFF_ENV, "").strip()
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw[-1] in "Zz" else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        log.error(
            "[apis.gate_waive] %s=%r is not an ISO 8601 timestamp with an "
            "offset — no pre-deploy sign-off is grandfathered",
            GATE_SIGNING_CUTOFF_ENV,
            raw[:64],
        )
        return None
    return parsed.astimezone(timezone.utc)


def _server_ingested_at(observation: object) -> datetime | None:
    """When Neotoma ingested *observation*: its server-assigned `created_at`.

    Never `observed_at`, which is event time a writer or sync peer can carry
    in, and never a fallback to it: an observation without a parseable
    `created_at` has no server time. Confirmed against neotoma
    `src/shared/action_schemas.ts` (`at_ingested`: "`created_at` (row-insertion
    time)", distinct from `observed_at`, which backfilled or late-arriving
    observations carry in the past), `src/services/observation_storage.ts`
    (`created_at: new Date().toISOString()` set on insert, `observed_at` taken
    from the caller's params), and the live `GET /entities/{id}/observations`
    response, which carries `created_at` on every observation.
    """
    if not isinstance(observation, dict):
        return None
    return _parse_utc(observation.get("created_at"))


def _set_before_signing_cutoff(observation: object, cutoff: datetime | None) -> bool:
    """True when *observation* was ingested strictly before *cutoff*."""
    if cutoff is None:
        return False
    ingested = _server_ingested_at(observation)
    return ingested is not None and ingested < cutoff


def _observation_is_attributed(
    observation: object,
    *,
    lens_sub: str,
    lens_thumbprint: str,
    not_before: str | datetime | None,
) -> bool:
    """True only when *observation* was written by this lens's own key.

    The same three provenance conditions
    `gating.read_authenticated_checkpoint_resolution` requires of a checkpoint
    approver — subject, key thumbprint, trusted attribution tier — plus, when
    *not_before* is given, freshness (the observation is not older than this
    call's pre-write read), compared as parsed datetimes. An observation
    whose timestamp does not parse is not attributed. The tenant check that
    function also makes has no counterpart here: this store holds no expected
    tenant id to compare against.
    """
    if not isinstance(observation, dict):
        return False
    provenance = observation.get("provenance")
    if not isinstance(provenance, dict):
        return False
    observed_at = _observation_time(observation)
    if observed_at is None:
        return False
    if not_before is not None:
        floor = _parse_utc(not_before)
        if floor is None or observed_at < floor:
            return False
    return (
        str(provenance.get("agent_sub") or "").strip() == lens_sub
        and str(provenance.get("agent_thumbprint") or "").strip() == lens_thumbprint
        and provenance.get("attribution_tier") in _TRUSTED_AAUTH_TIERS
    )


def _gate_status_history(
    observations: list[dict], head_id: str
) -> list[tuple[dict[str, str], dict]] | None:
    """`gate_status` observations from *head_id* back, newest first, or None.

    Each item is (parsed gate map, observation). *head_id* is the observation
    the entity's field provenance names for `gate_status` (the value the
    snapshot shows); older ones follow in time order. None — fail closed —
    when *head_id* is empty or not among *observations*, or when any
    `gate_status` observation's timestamp does not parse (the order would be
    a guess).
    """
    if not head_id:
        return None
    carrying = [
        o
        for o in observations
        if isinstance(o, dict)
        and isinstance(o.get("fields"), dict)
        and GATE_STATUS_FIELD in o["fields"]
    ]
    stamped = [(_observation_time(o), o) for o in carrying]
    if any(when is None for when, _ in stamped):
        return None
    # Stable: observations with equal timestamps keep the endpoint's
    # newest-first order.
    stamped.sort(key=lambda pair: pair[0], reverse=True)
    ordered = [o for _, o in stamped]
    start = next((i for i, o in enumerate(ordered) if o.get("id") == head_id), None)
    if start is None:
        return None
    return [
        (parse_gate_status(o["fields"][GATE_STATUS_FIELD]), o) for o in ordered[start:]
    ]


# ── Per-issue write serialisation (second security run at e874537f, N1) ──────
# `gate_status` is ONE field holding every gate's state, and Neotoma's
# `correct` replaces a field's whole value: it has no field-level merge and no
# conditional (compare-and-set) write. So two sign-offs on the same issue that
# overlap each read the map, change one key, and write the whole map back, and
# the later write can bring back a gate the earlier one had just rolled back.
# The dispatcher is one process, so an in-process lock per (Neotoma instance,
# repo, issue) serialises every `sign_off` (including its compensating restore)
# and every `waive`. It does not cover writers outside this process (an agent's
# own MCP session); the tool deny on every seated reviewer is what keeps those
# off `correct`.
_ISSUE_WRITE_LOCKS: weakref.WeakValueDictionary[tuple, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _issue_write_lock(base_url: str, repo: str, issue_number: int) -> asyncio.Lock:
    """The lock serialising gate writes to one issue entity in this process.

    Keyed on the running event loop as well, so a lock is never shared across
    loops (each test's `asyncio.run` gets its own). Held weakly: a lock no
    coroutine holds or waits on is dropped.
    """
    key = (id(asyncio.get_running_loop()), base_url, str(repo), str(issue_number))
    lock = _ISSUE_WRITE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ISSUE_WRITE_LOCKS[key] = lock
    return lock


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
    # True when the READ itself failed or was inconclusive (a transport error,
    # a non-2xx, no token, or the bounded fallback scan running out of pages),
    # as opposed to a read that succeeded and found no entity. Both leave
    # `found` False; only this one means "unknown" (second security run at
    # bf97b1a4: a failed read reported "not found", so the signed_off re-proof
    # returned nothing and pre-panel seating failed open). Same shape as the
    # two `*_unreadable` flags above: `found` keeps its meaning and callers
    # that must tell the cases apart read this flag alongside it.
    read_failed: bool = False
    # True when the read returned a well-formed page whose row for this issue
    # carried no snapshot object (`{}`, a null snapshot, a string or a list),
    # so the gate state is unknown. Set together with `read_failed`; `sign_off`
    # reports it as unreadable state rather than a missing entity
    # (independent security run at b76b1376 on PR #1181, NON-BLOCKING).
    row_unreadable: bool = False
    # The issue's OWN workflow binding, read straight off the snapshot. This
    # is the authority on WHICH gates apply to this issue, and so on whether an
    # absent gate key means "does not apply" (clears) or "applies, unwritten"
    # (blocks) — see `uncleared_gates` (ateles#1213). Empty when the snapshot
    # carries neither, which callers must treat as UNKNOWN (absence blocks),
    # never as "no gates apply".
    workflow_type: str = ""
    workflow_definition_id: str = ""
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


def _entity_page(data: object) -> list[dict] | None:
    """The entity rows of one ``/entities/query`` response, or None when the
    response is not a well-formed entity page.

    Well formed means a JSON object with an ``entities`` list of objects, no
    ``error`` key, and a ``next_cursor`` that is absent, null or a string.
    ``{"entities": []}`` is the one definite not-found. Anything else a 2xx
    can carry (an empty body or a 204, which `_post` returns as ``{}``; ``{}``
    itself; ``{"error": ...}``; ``{"entities": null}``; a top-level list) is
    a failed read, never "no such issue": reading it as absent let the
    pre-panel re-proof seat nobody (independent security run at 8f51ffc2 on
    PR #1181, NON-BLOCKING). A failed transport read (`_post` -> None) is
    None here too.
    """
    if not isinstance(data, dict) or "error" in data:
        return None
    entities = data.get("entities")
    if not isinstance(entities, list) or not all(isinstance(e, dict) for e in entities):
        return None
    cursor = data.get("next_cursor")
    if cursor is not None and not isinstance(cursor, str):
        return None
    return entities


def _row_snapshot(entity: dict) -> dict | None:
    """One row's field map, or None when the row carries no snapshot object.

    Some responses nest the field map one level deeper
    (``snapshot.snapshot``); that inner map is used when it is an object.
    """
    snap = entity.get("snapshot")
    if not isinstance(snap, dict):
        return None
    inner = snap.get("snapshot")
    return inner if isinstance(inner, dict) else snap


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
        # The server said a row matched the filter and none did (independent
        # security run at b76b1376 on PR #1181, NON-BLOCKING).
        filtered_unmatched = False
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
            page = _entity_page(data)
            if page is None:
                state.read_failed = True
                return state
            if any(_row_snapshot(entity) is None for entity in page):
                # A row the filter returned for this issue with no snapshot
                # object is this issue's state, unreadable — never "absent".
                state.read_failed = True
                state.row_unreadable = True
                return state
            entities = [
                entity
                for entity in page
                if self._matches(_row_snapshot(entity) or {}, repo, issue_number)
            ]
            if page:
                filtered_unmatched = not entities
                break
        if not entities:
            # Fall back to an unfiltered scan for entities whose snapshot does
            # not carry the composite fields (e.g. legacy rows keyed by
            # local_issue_id or title), and for a filtered page with no
            # matching row. Bounded and paged, so a miss here means the entity
            # genuinely is not there; a failed or exhausted scan returns None,
            # which is a failed read, not an absent entity. A miss after the
            # filter returned only non-matching rows is a failed read too: the
            # server and the scan disagree about this issue.
            scanned = await self._scan_for_issue(repo, issue_number)
            if scanned is None or (not scanned and filtered_unmatched):
                state.read_failed = True
                return state
            entities = scanned
        for entity in entities:
            snap = _row_snapshot(entity)
            if snap is None or not self._matches(snap, repo, issue_number):
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
            state.workflow_type = str(snap.get("workflow_type") or "")
            state.workflow_definition_id = str(
                snap.get("workflow_definition_id") or ""
            )
            # The reducer's per-field provenance rides on the query row's
            # ENVELOPE (`entity["provenance"]`), beside `snapshot`, not inside
            # it: read live from prod `/entities/query` on 2026-09-23 for
            # ateles#795's issue entity, `snapshot.provenance` was absent and
            # `entity.provenance.gate_status` named the head observation. The
            # snapshot-level read is kept as a fallback for any response that
            # nests it there. Reading only the snapshot left
            # `field_provenance` empty in prod, so every attribution read-back
            # and re-proof failed closed on a missing observation id.
            raw_provenance = entity.get("provenance")
            if not isinstance(raw_provenance, dict):
                raw_provenance = snap.get("provenance")
            if isinstance(raw_provenance, dict):
                state.field_provenance = {
                    str(k): str(v) for k, v in raw_provenance.items() if v
                }
            break
        return state

    async def _scan_for_issue(self, repo: str, issue_number: int) -> list[dict] | None:
        """Paged fallback scan for entities lacking composite snapshot fields.

        Uses cursor paging rather than a single truncated page, so "not found"
        means absent rather than beyond an arbitrary window. Bounded by
        ``_MAX_SCAN_PAGES`` so a pathological corpus cannot hang a gate check.

        Returns ``[]`` only when the scan completed without a match, and None
        when it could not complete: a failed page read, or the page bound hit
        before the corpus ended. Neither of those shows the entity is absent.
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
            page = _entity_page(data)
            if page is None:
                return None
            for entity in page:
                raw = entity.get("snapshot")
                if raw is None:
                    continue  # no snapshot to identify this row by
                snap = _row_snapshot(entity)
                if snap is None:
                    # A snapshot that is present but not an object could be
                    # this issue's, so the scan cannot say it is absent.
                    return None
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
        return None

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
        AND on failure. Serialised with `sign_off` per issue
        (`_issue_write_lock`): both write the whole `gate_status` map.
        """
        async with _issue_write_lock(self.base_url, repo, issue_number):
            return await self._waive_locked(repo, issue_number, pre_impl_gates)

    async def _waive_locked(
        self,
        repo: str,
        issue_number: int,
        pre_impl_gates: tuple[str, ...],
    ) -> WaiveOutcome:
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

        WRITE ORDER AND COMPENSATION (Falco's CONFIRMED BLOCKING partial-write
        finding, PR #1181, first raised at 9c5d199): the safety field is
        written LAST. `owner_history` (and `current_owner`, read back before
        going further) land first; `gate_status` is written only once they
        have. If anything fails at or after the `gate_status` write — the
        write itself (which may have landed before the transport failed), its
        read-back, or the attribution proof — a lens-signed compensating
        correction restores the gate's prior value and is read back. The
        outcome then carries the value actually re-read (`observed_state`),
        and when the gate still reads `signed_off` the class is
        `SIGN_OFF_CLEARED_UNVERIFIED`, never the class of the original
        failure. Callers treat every failed outcome as NOT cleared whatever a
        later re-read shows.

        SERIALISED PER ISSUE (second security run at e874537f, N1): the whole
        call, compensating restore included, runs under `_issue_write_lock`,
        so a concurrent sign-off on the same issue cannot load the map before
        this one's restore and write a rolled-back gate back.

        Raises nothing: every failure path returns a populated
        ``SignOffOutcome`` so the caller can decide (retry, escalate, leave
        pending) rather than crash the dispatch loop. (A cancelled task is not
        a failure path: cancellation propagates, and a write already sent may
        have landed. The next run does not trust that value — see
        `unverified_signed_off_gates`.)
        """
        async with _issue_write_lock(self.base_url, repo, issue_number):
            return await self._sign_off_locked(
                repo, issue_number, gate, lens_agent, head_sha, next_owner
            )

    async def _sign_off_locked(
        self,
        repo: str,
        issue_number: int,
        gate: str,
        lens_agent: str,
        head_sha: str,
        next_owner: str,
    ) -> SignOffOutcome:
        lens_sub = f"{lens_agent}@ateles-swarm"
        outcome = SignOffOutcome(gate=gate, lens_agent=lens_agent, lens_sub=lens_sub)

        # Constraint 1 — fail closed BEFORE any write attempt if this lens has
        # no signing key. Checking here, not inside the try/except below,
        # keeps "no key" and "signing raised" as distinct, legible classes.
        # The key's thumbprint is resolved here too: without it the
        # attribution read-back below can never succeed, so a write would only
        # ever be a write this call must then undo.
        identity = _ns.agent_identity(lens_agent, sub=lens_sub)
        lens_thumbprint = _lens_key_thumbprint(identity) if identity is not None else None
        if identity is None or not lens_thumbprint:
            outcome.error = SIGN_OFF_NO_SIGNING_KEY
            outcome.observed_state = "not read (refused before any read or write)"
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s%s "
                "— refusing to fall back to the daemon bearer",
                repo,
                issue_number,
                gate,
                lens_agent,
                SIGN_OFF_NO_SIGNING_KEY,
                "" if identity is None else " (key thumbprint unreadable)",
            )
            return outcome

        state = await self.load(repo, issue_number)
        if state.row_unreadable:
            # The row for this issue carried no snapshot object: its state is
            # unknown, not absent (independent security run at b76b1376 on PR
            # #1181). Nothing is written, as for an unparseable gate_status.
            outcome.error = SIGN_OFF_UNREADABLE_STATE
            outcome.observed_state = "unreadable"
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s "
                "(the issue row carries no snapshot object)",
                repo,
                issue_number,
                gate,
                lens_agent,
                SIGN_OFF_UNREADABLE_STATE,
            )
            return outcome
        if not state.found:
            outcome.error = SIGN_OFF_ENTITY_NOT_FOUND
            outcome.observed_state = "no issue entity"
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
            outcome.observed_state = "unreadable"
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
        prior_value = current or "pending"
        outcome.observed_state = prior_value
        if current in _SIGN_OFF_OTHER_AUTHORITY_STATES:
            # A gate some OTHER authority already cleared (operator `waive`,
            # `not_required`/`not_applicable` at triage, `skipped`) is a true
            # no-op: this lens's sign_off must never overwrite a different
            # actor's terminal state, and there is nothing to (re-)sign here —
            # skip both the write and the read-back below. `verified` stays
            # False and the class says why (Falco's non-blocking
            # misattribution finding, PR #1181): nothing here was signed by
            # this lens, so nothing may report it as a verified lens sign-off.
            outcome.ok = True
            outcome.verified = False
            outcome.error = SIGN_OFF_OTHER_AUTHORITY
            log.info(
                "[apis.gate_waive] sign_off %s#%s gate=%s: already %r "
                "(other authority) — no-op, no write attempted, not a lens "
                "sign-off",
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
        next_owner = next_owner.strip()

        async def _settle(
            error: str, *, gate_written: bool, history_written: bool
        ) -> SignOffOutcome:
            outcome.error = error
            return await self._settle_failed_sign_off(
                outcome,
                repo=repo,
                issue_number=issue_number,
                gate=gate,
                prior_value=prior_value,
                lens_agent=lens_agent,
                lens_sub=lens_sub,
                idempotency_suffix=f"{key}-{now[:16]}",
                gate_written=gate_written,
                history_written=history_written,
            )

        # 1. The non-safety fields first. A failure here leaves `gate_status`
        #    untouched, so the gate cannot read cleared on a sign-off that
        #    never completed.
        history_written = False
        pre_gate_fields: list[tuple[str, object]] = [("owner_history", merged_history)]
        if next_owner:
            pre_gate_fields.append(("current_owner", next_owner))
        for field_name, value in pre_gate_fields:
            failure = await self._sign_off_write(
                state.entity_id,
                field_name,
                value,
                f"gate-signoff-{gate}-{key}-{field_name}-{now[:16]}",
                lens_agent=lens_agent,
                lens_sub=lens_sub,
                context=(repo, issue_number, gate),
            )
            if failure:
                return await _settle(
                    failure, gate_written=False, history_written=history_written
                )
            if field_name == "owner_history":
                history_written = True

        if next_owner:
            # Same read-it-back discipline for `current_owner`, BEFORE the
            # gate is written: a landed handoff POST is not evidence the field
            # survived (this codebase's documented history of dropped-field
            # writes), and the gate must not clear on a handoff that did not.
            handoff = await self.load(repo, issue_number)
            if not handoff.found or handoff.current_owner.strip() != next_owner:
                log.error(
                    "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s "
                    "(current_owner read-back=%r, wanted=%r)",
                    repo,
                    issue_number,
                    gate,
                    lens_agent,
                    SIGN_OFF_VERIFY_FAILED,
                    handoff.current_owner if handoff.found else "(entity missing)",
                    next_owner,
                )
                return await _settle(
                    SIGN_OFF_VERIFY_FAILED,
                    gate_written=False,
                    history_written=history_written,
                )

        # 2. The safety field, last. From here on the gate may read cleared,
        #    so every failure is settled with a compensating restore.
        failure = await self._sign_off_write(
            state.entity_id,
            "gate_status",
            self._encode_gate_status(state, merged_gates),
            f"gate-signoff-{gate}-{key}-gate_status-{now[:16]}",
            lens_agent=lens_agent,
            lens_sub=lens_sub,
            context=(repo, issue_number, gate),
        )
        if failure:
            return await _settle(failure, gate_written=True, history_written=history_written)

        # Constraint 3c / CLAUDE.md "read it back" — a successful signed POST
        # is not evidence the write landed.
        reread = await self.load(repo, issue_number)
        if (
            not reread.found
            or (reread.gate_status.get(gate) or "").strip().lower() != "signed_off"
        ):
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
            return await _settle(
                SIGN_OFF_VERIFY_FAILED, gate_written=True, history_written=history_written
            )

        # Falco's CONFIRMED BLOCKING attribution finding, ateles#795 / PR
        # #1181, tightened per the second security run's non-blocking finding:
        # the checks above prove the VALUE landed, not WHO wrote it. The
        # terminal proof is the immutable observation `gate_status`'s reducer
        # provenance now names. It must exist, be NEWER than this call's own
        # pre-write read (`now`), and carry this lens's subject, this lens's
        # key thumbprint, and a trusted attribution tier — the conditions
        # `lib.daemon_runtime.gating.read_authenticated_checkpoint_resolution`
        # puts on a checkpoint approver (see `_observation_is_attributed`).
        observation_id = reread.field_provenance.get("gate_status", "")
        attributed = False
        if observation_id:
            observations = await self._observations(reread.entity_id)
            observation = next(
                (
                    o
                    for o in observations
                    if isinstance(o, dict) and o.get("id") == observation_id
                ),
                None,
            )
            attributed = _observation_is_attributed(
                observation,
                lens_sub=lens_sub,
                lens_thumbprint=lens_thumbprint,
                not_before=now,
            )
            if not attributed:
                obs_provenance = (
                    observation.get("provenance") if isinstance(observation, dict) else None
                )
                obs_provenance = obs_provenance if isinstance(obs_provenance, dict) else {}
                log.error(
                    "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: "
                    "observation %s attribution did not verify "
                    "(found=%s, agent_sub=%r, thumbprint_match=%s, tier=%r)",
                    repo,
                    issue_number,
                    gate,
                    lens_agent,
                    observation_id,
                    isinstance(observation, dict),
                    obs_provenance.get("agent_sub"),
                    str(obs_provenance.get("agent_thumbprint") or "") == lens_thumbprint,
                    obs_provenance.get("attribution_tier"),
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
            # Fail closed (principles §§5, 7): the VALUE landed, but this call
            # cannot prove IT put it there, so the gate is restored rather
            # than left reading cleared on an unattributed write.
            return await _settle(
                SIGN_OFF_ATTRIBUTION_FAILED,
                gate_written=True,
                history_written=history_written,
            )

        outcome.ok = True
        outcome.verified = True
        outcome.error = ""
        outcome.observed_state = "signed_off"
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

    async def _signed_correct(
        self,
        entity_id: str,
        field_name: str,
        value: object,
        idempotency_key: str,
        *,
        lens_agent: str,
        lens_sub: str,
    ) -> tuple[int, dict]:
        """One lens-signed `correct`, off the event loop.

        `neotoma_signed.signed_request` is SYNCHRONOUS (it runs the node
        signing helper as a subprocess and returns `(status, body)`). It used
        to be `await`ed directly, which raised `TypeError` on the returned
        tuple AFTER the subprocess had already sent the write, so a live
        sign-off landed its field and then reported a signing failure. It now
        runs in a worker thread; an awaitable result (a test double written as
        a coroutine function) is awaited as well.
        """
        # Constraint 4 — never write a field this module has not confirmed
        # is declared on the production schema.
        assert field_name in _SIGN_OFF_DECLARED_FIELDS, (
            f"sign_off attempted to write undeclared field {field_name!r}"
        )
        result = await asyncio.to_thread(
            _ns.signed_request,
            "POST",
            f"{self.base_url}/correct",
            {
                "entity_id": entity_id,
                "entity_type": self.ENTITY_TYPE,
                "field": field_name,
                "value": value,
                "idempotency_key": idempotency_key,
            },
            agent_name=lens_agent,
            sub=lens_sub,
        )
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _sign_off_write(
        self,
        entity_id: str,
        field_name: str,
        value: object,
        idempotency_key: str,
        *,
        lens_agent: str,
        lens_sub: str,
        context: tuple[str, int, str],
    ) -> str:
        """Perform one sign-off write; return "" on 2xx, else a failure class.

        Error CLASS only in the log line — never the exception's full text,
        which can carry a signed-request body, an auth header, or a
        filesystem path (Buteo's review, ateles#795). A non-2xx is an explicit
        failure too (Falco's security review, PR #1181): `signed_request`
        returns its status rather than raising, and a caller that ignored it
        would lean entirely on a read-back that happens to also fail.
        """
        repo, issue_number, gate = context
        try:
            status, _resp = await self._signed_correct(
                entity_id,
                field_name,
                value,
                idempotency_key,
                lens_agent=lens_agent,
                lens_sub=lens_sub,
            )
        except Exception as exc:  # noqa: BLE001 — classify, never crash
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s field=%s: %s (%s)",
                repo,
                issue_number,
                gate,
                lens_agent,
                field_name,
                SIGN_OFF_SIGNING_FAILED,
                type(exc).__name__,
            )
            return SIGN_OFF_SIGNING_FAILED
        if status < 200 or status >= 300:
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s field=%s: %s (HTTP %s)",
                repo,
                issue_number,
                gate,
                lens_agent,
                field_name,
                SIGN_OFF_SIGNING_FAILED,
                status,
            )
            return SIGN_OFF_SIGNING_FAILED
        return ""

    async def _settle_failed_sign_off(
        self,
        outcome: SignOffOutcome,
        *,
        repo: str,
        issue_number: int,
        gate: str,
        prior_value: str,
        lens_agent: str,
        lens_sub: str,
        idempotency_suffix: str,
        gate_written: bool,
        history_written: bool,
    ) -> SignOffOutcome:
        """Make a failed sign-off leave the record as it found it, and say so.

        Re-reads the gate. If it reads `signed_off` and did not before this
        call, this call's own write put it there: a lens-signed compensating
        correction restores *prior_value* and is read back. The outcome's
        `observed_state` is always the value actually re-read last, and when
        that value is still `signed_off` (restore unconfirmed, or the gate
        already read `signed_off` before this call) the class becomes
        `SIGN_OFF_CLEARED_UNVERIFIED`. A re-read that fails is reported as
        what it is, never as `pending`.

        AUDIT (second security run at e874537f): the `signed_off` history entry
        is written BEFORE the gate (the safety field stays last), so whenever
        that entry landed and the sign-off then failed, a `sign_off_failed`
        entry naming the failure class and the gate's re-read value is
        appended after it — on every failure path, not only when a restore
        ran. `owner_history` therefore does not end on a `signed_off` for a
        sign-off that failed. Best-effort: the gate value, not this note, is
        what callers act on.
        """
        outcome.ok = False
        outcome.verified = False
        original_error = outcome.error
        reread = await self.load(repo, issue_number)
        await self._settle_gate(
            outcome,
            reread,
            repo=repo,
            issue_number=issue_number,
            gate=gate,
            prior_value=prior_value,
            lens_agent=lens_agent,
            lens_sub=lens_sub,
            idempotency_suffix=idempotency_suffix,
            gate_written=gate_written,
            original_error=original_error,
        )
        if history_written:
            await self._record_failed_sign_off(
                reread,
                repo=repo,
                issue_number=issue_number,
                gate=gate,
                lens_agent=lens_agent,
                lens_sub=lens_sub,
                error=outcome.error or original_error,
                observed=outcome.observed_state,
                idempotency_suffix=idempotency_suffix,
            )
        return outcome

    async def _settle_gate(
        self,
        outcome: SignOffOutcome,
        reread: IssueGateState,
        *,
        repo: str,
        issue_number: int,
        gate: str,
        prior_value: str,
        lens_agent: str,
        lens_sub: str,
        idempotency_suffix: str,
        gate_written: bool,
        original_error: str,
    ) -> SignOffOutcome:
        if not reread.found or reread.gate_status_unreadable:
            outcome.observed_state = (
                "unreadable" if reread.found else "unknown (re-read found no entity)"
            )
            if gate_written or prior_value == "signed_off":
                outcome.error = SIGN_OFF_CLEARED_UNVERIFIED
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s, and the "
                "settling re-read could not read the gate (%s) — reported as %s",
                repo,
                issue_number,
                gate,
                lens_agent,
                original_error,
                outcome.observed_state,
                outcome.error,
            )
            return outcome

        current = (reread.gate_status.get(gate) or "").strip().lower() or "pending"
        if current == "signed_off" and prior_value != "signed_off":
            restored = dict(reread.gate_status)
            restored[gate] = prior_value
            failure = await self._sign_off_write(
                reread.entity_id,
                "gate_status",
                self._encode_gate_status(reread, restored),
                f"gate-signoff-restore-{gate}-{idempotency_suffix}",
                lens_agent=lens_agent,
                lens_sub=lens_sub,
                context=(repo, issue_number, gate),
            )
            # Re-read whether or not the restore reported success: a restore
            # whose transport failed may still have landed, and one that
            # reported success is not evidence it did.
            after = await self.load(repo, issue_number)
            if not after.found or after.gate_status_unreadable:
                outcome.observed_state = (
                    "unreadable" if after.found else "unknown (re-read found no entity)"
                )
                outcome.error = SIGN_OFF_CLEARED_UNVERIFIED
                log.error(
                    "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s; "
                    "restore %s but its read-back failed — reported as %s",
                    repo,
                    issue_number,
                    gate,
                    lens_agent,
                    original_error,
                    "failed" if failure else "sent",
                    SIGN_OFF_CLEARED_UNVERIFIED,
                )
                return outcome
            current = (after.gate_status.get(gate) or "").strip().lower() or "pending"

        outcome.observed_state = current
        if current == "signed_off":
            outcome.error = SIGN_OFF_CLEARED_UNVERIFIED
        log.error(
            "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: %s — gate re-read "
            "as %r after settling%s",
            repo,
            issue_number,
            gate,
            lens_agent,
            original_error,
            current,
            (
                f"; reported as {SIGN_OFF_CLEARED_UNVERIFIED}"
                if outcome.error != original_error
                else ""
            ),
        )
        return outcome

    async def _record_failed_sign_off(
        self,
        reread: IssueGateState,
        *,
        repo: str,
        issue_number: int,
        gate: str,
        lens_agent: str,
        lens_sub: str,
        error: str,
        observed: str,
        idempotency_suffix: str,
    ) -> None:
        """Append a `sign_off_failed` entry after this call's `signed_off` one.

        Built on the settling re-read's `owner_history` (a fresh read, taken
        under the per-issue lock). Never written through an unreadable stored
        value, and not written when the re-read found no entity: there is then
        no current list to append to, and writing this call's own copy back
        could drop an entry another writer added.
        """
        if not reread.found or reread.owner_history_unreadable:
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: owner_history "
                "not readable on re-read — sign_off_failed note not written",
                repo,
                issue_number,
                gate,
                lens_agent,
            )
            return
        failure = await self._sign_off_write(
            reread.entity_id,
            "owner_history",
            list(reread.owner_history)
            + [
                {
                    "gate": gate,
                    "action": "sign_off_failed",
                    "actor": lens_agent,
                    "reason": error,
                    "gate_after": observed,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ],
            f"gate-signoff-failed-history-{gate}-{idempotency_suffix}",
            lens_agent=lens_agent,
            lens_sub=lens_sub,
            context=(repo, issue_number, gate),
        )
        if failure:
            log.error(
                "[apis.gate_waive] sign_off %s#%s gate=%s lens=%s: sign_off_failed "
                "note did not land (%s) — owner_history still ends on this "
                "call's signed_off entry",
                repo,
                issue_number,
                gate,
                lens_agent,
                failure,
            )

    async def unverified_signed_off_gates(
        self, state: IssueGateState, owners: Mapping[str, str]
    ) -> set[str]:
        """Gates in *owners* that read `signed_off` without a provable sign-off.

        A later run must not trust a `signed_off` it did not itself verify
        (second security run at e874537f, N2, and the cancellation case): a
        sign-off whose settle could not run or could not restore — a Neotoma
        outage after the gate write landed, or a task cancelled while the
        worker thread's write was already on the wire — leaves the gate
        reading `signed_off`, and nothing else remembers that it failed.

        So the value is re-proven from provenance. *owners* maps each gate to
        the lens that owns it (`pm` -> `pavo`, ...). A `signed_off` gate is
        VERIFIED only when an observation in its current `signed_off` run —
        from the observation the reducer's provenance names for
        `gate_status`, walking back through older `gate_status` observations
        while the gate still reads `signed_off` — was written by that owner's
        own key: the owner's subject, the RFC 7638 thumbprint of the owner's
        key file, and a trusted attribution tier (`_observation_is_attributed`).
        The walk is needed because every sign-off rewrites the whole map: the
        newest observation for a later gate also carries the earlier gates'
        `signed_off`, written by a different lens.

        LEGACY sign-offs: a `signed_off` gate with no owner-signed observation
        is still VERIFIED when the observation that set it (the oldest one in
        its current `signed_off` run) was ingested by Neotoma before
        `APIS_GATE_SIGNING_CUTOFF`, judged by the server-assigned `created_at`,
        never the writer-supplied `observed_at` (`_server_ingested_at`). With
        the cutoff unset or unparseable, nothing is grandfathered. Only the
        literal `signed_off` is re-proven here; the other cleared values
        (`waived`, `not_required`, …) are still trusted as written (tracked
        separately, ateles#1200 / #1203).

        Fails closed: an unreadable record, a missing provenance entry, an
        observations read that returns nothing, an observation whose
        timestamp does not parse, or an owner key this process cannot read
        leaves every `signed_off` gate it affects UNVERIFIED. Callers treat an
        unverified gate as pending, so its owner is seated again and a clean
        verdict re-signs it: a false negative costs one review round.
        """
        signed = {
            gate
            for gate in owners
            if (state.gate_status.get(gate) or "").strip().lower() == "signed_off"
        }
        if not signed:
            return set()
        if not state.found or state.gate_status_unreadable:
            return signed
        head_id = state.field_provenance.get(GATE_STATUS_FIELD, "")
        observations = await self._observations(state.entity_id) if head_id else []
        history = _gate_status_history(observations, head_id)
        if history is None:
            log.warning(
                "[apis.gate_waive] %s#%s: gate_status provenance could not be "
                "read back (observation %r) — treating %s as unverified",
                state.repo,
                state.issue_number,
                head_id or "(none)",
                ", ".join(sorted(signed)),
            )
            return signed
        unverified: set[str] = set()
        legacy: set[str] = set()
        cutoff = gate_signing_cutoff()
        for gate in sorted(signed):
            owner = owners[gate]
            owner_sub = f"{owner}@ateles-swarm"
            identity = _ns.agent_identity(owner, sub=owner_sub)
            thumbprint = _lens_key_thumbprint(identity) if identity is not None else None
            verified = False
            # The oldest observation of the current `signed_off` run seen so
            # far: the one that set the value, as far as this history reaches.
            setter: dict | None = None
            for gates_map, observation in history:
                if (gates_map.get(gate) or "").strip().lower() != "signed_off":
                    break
                setter = observation
                if thumbprint and _observation_is_attributed(
                    observation,
                    lens_sub=owner_sub,
                    lens_thumbprint=thumbprint,
                    not_before=None,
                ):
                    verified = True
                    break
            if not verified and _set_before_signing_cutoff(setter, cutoff):
                verified = True
                legacy.add(gate)
            if not verified:
                unverified.add(gate)
        if legacy:
            log.info(
                "[apis.gate_waive] %s#%s: %s read signed_off from a write "
                "Neotoma ingested before %s=%s — accepted as a legacy sign-off",
                state.repo,
                state.issue_number,
                ", ".join(sorted(legacy)),
                GATE_SIGNING_CUTOFF_ENV,
                cutoff.isoformat() if cutoff else "",
            )
        if unverified:
            log.warning(
                "[apis.gate_waive] %s#%s: %s read signed_off without an "
                "observation signed by the owning lens — treated as pending",
                state.repo,
                state.issue_number,
                ", ".join(sorted(unverified)),
            )
        return unverified

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

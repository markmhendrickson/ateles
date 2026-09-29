"""Media-generation capability client: ``generate(slot, prompt, ...)``.

Trust boundary
--------------
This module runs HOST-SIDE ONLY: in a process that is allowed to hold
generation credentials (the operator's shell, a host script, or a daemon
parent). It must never run inside a dispatched agent child. That child's
environment is scrubbed by ``skill_runner._subscription_only_env``, which
strips generation credential names and sets ``ATELES_DISPATCHED_AGENT=1``; this
client refuses to run when that marker is present. The credential itself lives
in a mode-0600 file that only this client parses (``credentials.py``); it is
never placed in any child environment, ``vendor_binding`` field,
``generation_record``, ``GenerationResult``, exception, or log.

Single public surface: this module (``lib.capabilities.generation``). There is
no CLI, MCP, or HTTP entry point in this change.

Call flow
---------
1. resolve the ``vendor_binding`` for the exact slot (else ``BINDING_MISSING``);
2. read the monthly cap from the binding (else ``CAP_UNSET``/``CAP_UNREADABLE``);
3. estimate the call cost and authorize it against the slot AND cap-group
   spend under the ledger lock (else ``CAP_EXHAUSTED``);
4. resolve the credential in this process (else ``CREDENTIAL_UNRESOLVED``);
5. mint ``generation_id``, THEN call the vendor;
6. on a vendor failure only, try the binding's fallback once; cap, credential
   and binding refusals never fall back to a different paid vendor;
7. finalize the ledger row, store the artifact, then store a private
   ``generation_record``.

Spend is recorded BEFORE the vendor is asked: a ``pending`` row at the
estimate is written under the ledger lock first, and every state except
``voided`` counts toward the cap. The fallback vendor is tried only when the
primary DEFINITIVELY did not bill (a 4xx before any job existed, or a
connection that never reached the vendor). If the vendor may have billed (a
Veo job that started and then timed out, a download that failed, an empty
answer), the estimate stays held, the fallback is NOT tried, and the caller is
not told to retry.

No vendor request is made on any refusal in steps 1 to 4. Two refusal codes
mean money MAY have moved and must not be retried blindly: ``SPENT_UNRECORDED``
and ``ARTIFACT_UNSAVED`` (see ``errors.POST_SPEND_CODES``), plus a
``VENDOR_ERROR``/``EMPTY_RESULT`` that carries ``retryable=False``.

Examples (illustrative; each needs a host with bindings, caps and credentials)
-----------------------------------------------------------------------------
One call per slot::

    from lib.capabilities import generate

    generate("vector_mark_generation", "A geometric monogram mark, two colours")
    generate("image_generation", "Concept board, warm daylight", tier="draft")
    generate("video_generation", "Slow dolly across a desk", duration_seconds=8,
             resolution="1080p")

Catching a refusal and branching on its code::

    from lib.capabilities import GenerationRefused, generate

    try:
        result = generate("image_generation", prompt)
    except GenerationRefused as refused:
        if refused.code in ("CAP_EXHAUSTED", "CAP_UNSET"):
            print(refused.code, refused.spent_usd, refused.cap_usd,
                  refused.remaining_usd)
            print(refused.hint)  # what to do next, e.g. ask the operator
        else:
            raise

Success carries what the caller needs to decide about another round:
``generation_id``, ``artifact_ref``, ``vendor``, ``model_tier`` (the ones that
ACTUALLY ran, including a fallback), ``cost_usd``, ``remaining_cap_usd``, and
``slot``.
"""

from __future__ import annotations

import hashlib
import math
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from lib.credential_scrub import AGENT_CHILD_MARKER_ENV

from . import neotoma_http, records
from .credentials import Secret, resolve_credential
from .errors import (
    ARTIFACT_UNSAVED,
    BINDING_MISSING,
    CAP_UNREADABLE,
    CREDENTIAL_UNRESOLVED,
    EMPTY_RESULT,
    FALLBACK_EXHAUSTED,
    SPENT_UNRECORDED,
    VENDOR_ERROR,
    CapabilityError,
    EmptyArtifact,
    GenerationRefused,
    RasterizerUnavailable,
    VendorFailure,
)
from .rasterize import Renderer, looks_like_svg, rasterize_svg
from .spend import (
    STATE_ACCEPTED_UNFINISHED,
    STATE_COMPLETED,
    STATE_PENDING,
    STATE_VOIDED,
    LedgerWriteError,
    LockedLedger,
    SpendLedger,
    parse_cap_policy,
    write_all,
    CapPolicy,
)
from .vendor_binding import Fetcher, VendorBinding, default_fetcher, resolve_vendor_binding
from .vendors import (
    CredentialRouteUnavailable,
    PriceUnknown,
    VendorAdapter,
    VendorOutput,
    default_adapters,
)

ARTIFACT_PATH_ENV = "ATELES_GENERATION_ARTIFACT_PATH"
_EXT = {
    "image/svg+xml": "svg",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "video/mp4": "mp4",
}
_TIER_KEYS = {"draft": "draft_model_tier", "standard": "standard_model_tier", "lite": "lite_model_tier"}
_FALLBACK_SUFFIX = "~fallback"


@dataclass(frozen=True)
class GenerationResult:
    generation_id: str
    slot: str
    artifact_ref: str
    vendor: str
    model_tier: str
    cost_usd: float
    remaining_cap_usd: float
    media_type: str
    binding_entity_id: str
    requested_vendor: str
    fallback_used: bool = False
    raster_preview_ref: str | None = None
    record_entity_id: str | None = None
    record_persisted: bool = False
    warnings: tuple[str, ...] = ()


# --- persistence of the generation_record ------------------------------------


class RecordSink(Protocol):
    def store(self, record: dict[str, Any], idempotency_key: str) -> tuple[str | None, str | None]:
        """Return (entity_id, warning). ``entity_id`` is None unless verified."""


JsonRequest = Callable[[str, str, "dict[str, Any] | None"], "dict[str, Any]"]

_SCHEMA_HINT = (
    "If this is the first live call, register the schema first: "
    "python3 execution/scripts/register_generation_record_schema.py --apply"
)


class NeotomaRecordSink:
    """Store a ``generation_record`` with an idempotency key, then read it back.

    Registration of the schema (``register_generation_record_schema.py``) must
    precede the first live store. A 2xx is never taken as proof: the entity is
    fetched and the fields that matter (including ``visibility``) are compared.
    """

    def __init__(self, request: JsonRequest | None = None) -> None:
        self._request = request or neotoma_http.request_json

    def store(self, record: dict[str, Any], idempotency_key: str) -> tuple[str | None, str | None]:
        try:
            stored = self._request(
                "POST",
                "/store",
                {
                    "idempotency_key": idempotency_key,
                    "observation_source": "workflow_state",
                    "entities": [record],
                },
            )
            entity_id = next(
                (
                    row.get("entity_id")
                    for row in stored.get("entities", [])
                    if row.get("entity_type") == records.GENERATION_RECORD_ENTITY_TYPE
                ),
                None,
            )
            if not entity_id:
                return None, f"generation_record store returned no entity id. {_SCHEMA_HINT}"
            readback = self._request("GET", f"/entities/{entity_id}", None)
            snap = readback.get("snapshot") or {}
            if isinstance(snap.get("snapshot"), dict):
                snap = snap["snapshot"]
            for name in (
                "generation_id", "slot", "prompt", "vendor", "model_tier", "artifact_ref",
                "cost_usd", "requested_vendor", "fallback_used",
            ):
                if snap.get(name) != record.get(name):
                    return None, f"generation_record read-back mismatch on {name}. {_SCHEMA_HINT}"
            if snap.get("visibility") != records.PRIVATE:
                return None, "generation_record did not land as private; do not reference it"
            return str(entity_id), None
        except Exception as exc:  # noqa: BLE001 - persistence must not lose the artifact
            detail = type(exc).__name__
            if isinstance(exc, neotoma_http.NeotomaRequestError):
                detail = f"{detail} {exc}: {exc.body[:200]}"
            elif isinstance(exc, neotoma_http.NeotomaConfigError):
                detail = f"{detail}: {exc}"
            return None, f"generation_record was not stored ({detail}). {_SCHEMA_HINT}"


# --- the client ---------------------------------------------------------------


def _default_artifact_root() -> Path:
    override = os.environ.get(ARTIFACT_PATH_ENV)
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "ateles" / "generation_artifacts"


class _NotBilled(CapabilityError):
    """Internal: the vendor definitively did not bill; the row was voided."""

    def __init__(self, failure: VendorFailure, vendor: str) -> None:
        self.failure = failure
        self.vendor = vendor
        super().__init__(str(failure))


class _Held(CapabilityError):
    """Internal: the vendor may have billed; the estimate stays counted."""

    def __init__(self, failure: Exception, vendor: str, estimate: float, policy: CapPolicy) -> None:
        self.failure = failure
        self.vendor = vendor
        self.estimate = estimate
        self.policy = policy
        super().__init__(str(failure))


@dataclass
class _Attempt:
    out: VendorOutput
    policy: CapPolicy
    binding: VendorBinding
    ledger_id: str
    month: str
    estimate: float
    fallback_used: bool = False


@dataclass
class _Finalized:
    attempt: _Attempt
    path: Path
    remaining: float
    created_at: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class CapabilityClient:
    """Wires the pieces together. Every dependency is injectable for tests."""

    fetch: Fetcher | None = None
    ledger: SpendLedger | None = None
    adapters: Mapping[str, VendorAdapter] | None = None
    sink: RecordSink | None = None
    artifact_root: Path | None = None
    process_values: Mapping[str, str] | None = None
    renderer: Renderer | None = None
    new_id: Callable[[], str] = field(default=lambda: f"gen_{uuid.uuid4().hex}")

    def __post_init__(self) -> None:
        self._ledger = self.ledger or SpendLedger()
        self._adapters = dict(self.adapters) if self.adapters is not None else default_adapters()
        self._sink = self.sink or NeotomaRecordSink()
        self._root = Path(self.artifact_root) if self.artifact_root else _default_artifact_root()
        self._fetch = self.fetch or default_fetcher

    # -- helpers --------------------------------------------------------------

    def _env(self) -> Mapping[str, str]:
        return os.environ if self.process_values is None else self.process_values

    def _guard_host_side(self, slot: str) -> None:
        if self._env().get(AGENT_CHILD_MARKER_ENV):
            raise GenerationRefused(
                CREDENTIAL_UNRESOLVED,
                slot,
                "this process is a dispatched agent child; generation runs "
                "host-side only",
                "Ask the operator session or a host-side runner to make the "
                "call. Agents never hold generation credentials.",
            )

    def _adapter_for(self, vendor: str, slot: str) -> VendorAdapter:
        adapter = self._adapters.get(vendor)
        if adapter is None:
            raise GenerationRefused(
                BINDING_MISSING,
                slot,
                f"no adapter is registered for vendor {vendor!r}",
                "Correct the binding's vendor to a supported id: "
                + ", ".join(sorted(self._adapters)) + ".",
            )
        return adapter

    @staticmethod
    def _select_model(binding: VendorBinding, tier: str | None) -> str:
        c = binding.constraints or {}
        if tier in (None, "", "default"):
            model = c.get("model_tier") or binding.tool_namespace
        else:
            key = _TIER_KEYS.get(tier)
            model = c.get(key) if key else None
            if not model:
                raise GenerationRefused(
                    BINDING_MISSING,
                    binding.capability,
                    f"the binding has no {tier!r} tier",
                    "Use tier=None, or one of: "
                    + ", ".join(t for t, k in _TIER_KEYS.items() if c.get(k))
                    + ".",
                )
        if not isinstance(model, str) or not model.strip():
            raise GenerationRefused(
                BINDING_MISSING,
                binding.capability,
                "the binding names no model",
                "Set model_tier in the binding's constraints or tool_namespace.",
            )
        return model.strip()

    def _credential(self, adapter: VendorAdapter, binding: VendorBinding) -> Secret | None:
        if not adapter.credential_names:
            return None
        cfile = None
        if binding.constraints:
            cfile = binding.constraints.get("credential_env_file")
        return resolve_credential(
            binding.capability,
            credential_location=binding.credential_location,
            allowed_names=adapter.credential_names,
            credential_file=cfile if isinstance(cfile, str) else None,
        )

    @staticmethod
    def _row(
        *, ledger_id: str, generation_id: str, slot: str, policy: CapPolicy, vendor: str,
        model: str, cost: float, state: str, created_at: str, prompt_sha: str,
    ) -> dict[str, Any]:
        return {
            "generation_id": ledger_id,
            "parent_generation_id": generation_id,
            "slot": slot,
            "billing_slot": policy.slot,
            "vendor": vendor,
            "model_tier": model,
            "cost_usd": cost,
            "state": state,
            "cap_group": policy.group,
            "created_at": created_at,
            "prompt_sha256": prompt_sha,
        }

    # -- one authorized attempt against one binding ---------------------------

    def _attempt(
        self,
        locked: LockedLedger,
        binding: VendorBinding,
        *,
        requested_slot: str,
        prompt: str,
        tier: str | None,
        opts: Mapping[str, Any],
        generation_id: str,
        ledger_id: str,
        fallback_used: bool,
    ) -> _Attempt:
        """Authorize, write the PENDING row, call the vendor.

        Raises ``GenerationRefused`` before the vendor on any policy failure,
        ``_NotBilled`` when the vendor definitively did not bill (row voided),
        and ``_Held`` when it may have (row stays counted).
        """
        policy = parse_cap_policy(binding)
        adapter = self._adapter_for(binding.vendor, requested_slot)
        model = self._select_model(binding, tier)
        try:
            estimate = adapter.estimate_cost(model, opts)
        except PriceUnknown as exc:
            raise GenerationRefused(
                CAP_UNREADABLE,
                requested_slot,
                f"the call cost cannot be estimated ({exc})",
                "Use a supported model and options, or ask the operator to add "
                "the price. The client refuses to spend on an unpriced call.",
            ) from None
        except CredentialRouteUnavailable as exc:
            raise GenerationRefused(CREDENTIAL_UNRESOLVED, requested_slot, str(exc), exc.hint) from None
        if (
            isinstance(estimate, bool)
            or not isinstance(estimate, (int, float))
            or not math.isfinite(estimate)
            or estimate < 0
        ):
            raise GenerationRefused(
                CAP_UNREADABLE,
                requested_slot,
                "the adapter produced a cost estimate that is not a finite non-negative number",
                "This is an adapter defect; nothing was requested or spent.",
            )
        auth = locked.authorize(policy, float(estimate))  # CAP_EXHAUSTED stops here
        secret = self._credential(adapter, binding)
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
        row_args = dict(
            ledger_id=ledger_id, generation_id=generation_id, slot=requested_slot,
            policy=policy, vendor=binding.vendor, model=model, prompt_sha=prompt_sha,
        )
        # The estimate is on the ledger BEFORE the vendor is asked.
        try:
            locked.record(
                self._row(cost=float(estimate), state=STATE_PENDING,
                          created_at=locked.now_iso(), **row_args),
                policy.slot, auth.month,
            )
        except LedgerWriteError as exc:
            raise GenerationRefused(
                CAP_UNREADABLE, requested_slot,
                f"the pending spend row could not be written ({exc}); no vendor request was made",
                "Operator: repair the ledger location and retry. Nothing was spent.",
            ) from None

        def settle(state: str, cost: float) -> None:
            try:
                locked.record(
                    self._row(cost=cost, state=state, created_at=locked.now_iso(), **row_args),
                    policy.slot, auth.month,
                )
            except LedgerWriteError:
                # The pending row still counts at the estimate: fail closed.
                pass

        try:
            out = adapter.generate(prompt=prompt, model=model, opts=opts, credential=secret)
        except VendorFailure as failure:
            if failure.no_charge:
                settle(STATE_VOIDED, 0.0)
                raise _NotBilled(failure, binding.vendor) from None
            settle(STATE_ACCEPTED_UNFINISHED, float(estimate))
            raise _Held(failure, binding.vendor, float(estimate), policy) from None
        except EmptyArtifact as failure:
            settle(STATE_ACCEPTED_UNFINISHED, float(estimate))
            raise _Held(failure, binding.vendor, float(estimate), policy) from None
        except Exception as failure:  # noqa: BLE001 - unknown is possibly billed
            settle(STATE_ACCEPTED_UNFINISHED, float(estimate))
            raise _Held(failure, binding.vendor, float(estimate), policy) from None
        return _Attempt(out, policy, binding, ledger_id, auth.month, float(estimate), fallback_used)

    # -- public ---------------------------------------------------------------

    def generate(
        self,
        slot: str,
        prompt: str,
        *,
        tier: str | None = None,
        allow_fallback: bool = True,
        **opts: Any,
    ) -> GenerationResult:
        self._guard_host_side(slot if isinstance(slot, str) else "")
        if not isinstance(prompt, str) or not prompt.strip():
            raise GenerationRefused(
                EMPTY_RESULT, slot if isinstance(slot, str) else None,
                "the prompt is empty", "Supply a non-empty prompt; nothing was requested.",
            )
        binding = resolve_vendor_binding(slot, fetch=self._fetch)
        generation_id = self.new_id()  # minted BEFORE any vendor call
        tried: list[str] = [binding.vendor]
        use_fallback = bool(
            allow_fallback and binding.fallback and binding.fallback != binding.vendor
        )

        with self._ledger.locked(slot) as locked:
            try:
                attempt = self._attempt(
                    locked, binding, requested_slot=slot, prompt=prompt, tier=tier,
                    opts=opts, generation_id=generation_id, ledger_id=generation_id,
                    fallback_used=False,
                )
            except _Held as held:
                raise self._held_refusal(locked, held, slot, tried, generation_id) from None
            except _NotBilled as nb:
                attempt = self._fallback(
                    locked, nb, binding, use_fallback, tried, slot=slot, prompt=prompt,
                    opts=opts, generation_id=generation_id,
                )
            finalized = self._finalize(locked, attempt, slot=slot, prompt=prompt, generation_id=generation_id)
        return self._post_process(finalized, requested=binding, slot=slot, prompt=prompt, generation_id=generation_id)

    def _fallback(
        self,
        locked: LockedLedger,
        nb: _NotBilled,
        primary: VendorBinding,
        use_fallback: bool,
        tried: list[str],
        *,
        slot: str,
        prompt: str,
        opts: Mapping[str, Any],
        generation_id: str,
    ) -> _Attempt:
        first = self._vendor_error(nb.vendor, nb.failure, slot, tried)
        if not use_fallback:
            raise first from None
        tried.append(primary.fallback or "")
        try:
            fallback = self._fallback_binding(primary)
            return self._attempt(
                locked, fallback, requested_slot=slot, prompt=prompt, tier=None, opts=opts,
                generation_id=generation_id, ledger_id=generation_id + _FALLBACK_SUFFIX,
                fallback_used=True,
            )
        except _Held as held:
            refusal = self._held_refusal(locked, held, slot, tried, generation_id)
            raise GenerationRefused(
                FALLBACK_EXHAUSTED, slot,
                f"primary {primary.vendor} did not bill, but fallback {primary.fallback} "
                f"may have: {refusal.message}",
                refusal.hint, spent_usd=refusal.spent_usd, cap_usd=refusal.cap_usd,
                remaining_usd=refusal.remaining_usd, vendors_tried=tuple(tried),
                retryable=False, generation_id=generation_id,
            ) from None
        except (GenerationRefused, _NotBilled, LookupError) as second:
            if isinstance(second, GenerationRefused):
                reason = f"{second.code}: {second.message}"
                extra: dict[str, Any] = {
                    "spent_usd": second.spent_usd,
                    "cap_usd": second.cap_usd,
                    "remaining_usd": second.remaining_usd,
                }
            else:
                reason = type(second).__name__
                extra = {}
            raise GenerationRefused(
                FALLBACK_EXHAUSTED, slot,
                f"primary {primary.vendor} failed ({first.code}) and fallback "
                f"{primary.fallback} could not serve the call ({reason})",
                "Vendors tried: " + ", ".join(tried) + ". No third vendor is "
                "attempted. Fix the operator-side cause or retry later.",
                vendors_tried=tuple(tried), **extra,
            ) from None

    def _fallback_binding(self, primary: VendorBinding) -> VendorBinding:
        """The binding that fills the fallback vendor's own default slot."""
        adapter = self._adapters.get(primary.fallback or "")
        if adapter is None:
            raise LookupError(f"no adapter for fallback vendor {primary.fallback!r}")
        fb = resolve_vendor_binding(adapter.default_slot, fetch=self._fetch)
        if fb.vendor != primary.fallback:
            raise LookupError("the fallback slot is bound to a different vendor")
        return fb

    @staticmethod
    def _vendor_error(vendor: str, failure: VendorFailure, slot: str, tried: list[str]) -> GenerationRefused:
        """A definitive no-charge vendor failure (the row was voided)."""
        status = failure.http_status
        klass = f"HTTP {status}" if status is not None else "connection failure"
        retryable = failure.retryable
        advice = (
            "Retryable: the vendor did not bill this call; try again shortly."
            if retryable
            else "Operator-fix: the vendor rejected the request without billing; "
            "retrying unchanged will not help."
        )
        return GenerationRefused(
            VENDOR_ERROR, slot, f"{vendor} rejected the call ({klass}): {failure}",
            advice, vendors_tried=tuple(tried), retryable=retryable,
        )

    def _held_refusal(
        self, locked: LockedLedger, held: _Held, slot: str, tried: list[str], generation_id: str
    ) -> GenerationRefused:
        """The vendor may have billed: the estimate stays counted, no retry, no fallback."""
        empty = isinstance(held.failure, EmptyArtifact)
        try:
            spent, cap, remaining = locked.budget(held.policy)
        except GenerationRefused:
            spent = cap = remaining = None
        return GenerationRefused(
            EMPTY_RESULT if empty else VENDOR_ERROR,
            slot,
            f"{held.vendor} "
            + (
                "answered but produced no usable artifact"
                if empty
                else "failed after accepting the call"
                + (f" (HTTP {held.failure.http_status})" if getattr(held.failure, "http_status", None) else "")
                + f": {held.failure}"
            )
            + f". The vendor may have billed this call, so its estimate of "
            f"${held.estimate:.2f} is held against the cap (generation_id {generation_id}).",
            "Do not retry blindly and no fallback vendor was tried: a retry can bill "
            "again. If you can confirm the vendor did not bill, an operator can void "
            "the ledger row for this generation_id.",
            spent_usd=spent, cap_usd=cap, remaining_usd=remaining,
            vendors_tried=tuple(tried), retryable=False, generation_id=generation_id,
        )

    # -- after a successful vendor answer --------------------------------------

    def _finalize(
        self, locked: LockedLedger, attempt: _Attempt, *, slot: str, prompt: str, generation_id: str
    ) -> _Finalized:
        out, policy, binding = attempt.out, attempt.policy, attempt.binding
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
        created_at = locked.now_iso()

        def row(state: str, cost: float) -> dict[str, Any]:
            return self._row(
                ledger_id=attempt.ledger_id, generation_id=generation_id, slot=slot,
                policy=policy, vendor=out.vendor, model=out.model_tier, cost=cost,
                state=state, created_at=created_at, prompt_sha=prompt_sha,
            )

        unusable = None
        if not out.data:
            unusable = f"{out.vendor} returned an empty artifact"
        elif out.media_type == "image/svg+xml" and not looks_like_svg(out.data):
            unusable = f"{out.vendor} returned a payload that is not an SVG"
        if unusable:
            try:
                locked.record(row(STATE_ACCEPTED_UNFINISHED, attempt.estimate), policy.slot, attempt.month)
            except LedgerWriteError:
                pass  # the pending row already counts at the estimate
            raise self._held_refusal(
                locked,
                _Held(EmptyArtifact(unusable), out.vendor, attempt.estimate, policy),
                slot, [out.vendor], generation_id,
            ) from None

        unrecorded = False
        try:
            locked.record(row(STATE_COMPLETED, out.cost_usd), policy.slot, attempt.month)
        except LedgerWriteError:
            unrecorded = True  # pending row still counts at the estimate
        spent, cap, remaining_now = None, None, None
        try:
            spent, cap, remaining_now = locked.budget(policy)
        except GenerationRefused:
            pass
        ext = _EXT.get(out.media_type, "bin")
        try:
            path = self._write_artifact(slot, generation_id, ext, out.data)
        except OSError as exc:
            raise GenerationRefused(
                ARTIFACT_UNSAVED, slot,
                f"{out.vendor} produced the artifact and its ${out.cost_usd:.2f} is "
                f"{'held at the estimate' if unrecorded else 'recorded'}, but it could "
                f"not be written to disk ({type(exc).__name__}); the bytes are lost "
                f"(generation_id {generation_id})",
                "Do NOT retry: the spend is counted and a retry bills again. Operator: "
                "fix the artifact location (ATELES_GENERATION_ARTIFACT_PATH) before the next call.",
                spent_usd=spent, cap_usd=cap, remaining_usd=remaining_now,
                retryable=False, generation_id=generation_id,
            ) from None
        if unrecorded:
            raise GenerationRefused(
                SPENT_UNRECORDED, slot,
                f"the call was paid (${out.cost_usd:.2f}) and the artifact was saved at "
                f"{path}, but the final ledger row could not be written; the pending row "
                f"at the estimate (${attempt.estimate:.2f}) still counts against the cap "
                f"(generation_id {generation_id})",
                "Do NOT retry: that bills again. Operator: repair the ledger location and "
                f"record generation_id {generation_id} at cost {out.cost_usd} by hand.",
                spent_usd=spent, cap_usd=cap, remaining_usd=remaining_now,
                retryable=False, generation_id=generation_id, artifact_ref=str(path),
            )
        remaining = locked.remaining_after(policy)
        return _Finalized(attempt, path, remaining, created_at, list(out.warnings))

    def _post_process(
        self, fin: _Finalized, *, requested: VendorBinding, slot: str, prompt: str, generation_id: str
    ) -> GenerationResult:
        attempt = fin.attempt
        out, policy, active = attempt.out, attempt.policy, attempt.binding
        warnings = fin.warnings
        preview: str | None = None
        if out.media_type == "image/svg+xml":
            try:
                preview = str(
                    rasterize_svg(
                        out.data, fin.path.with_suffix(".preview.png"), slot=slot,
                        renderer=self.renderer,
                    )
                )
            except RasterizerUnavailable as exc:
                warnings.append(f"raster_preview_unavailable: {exc}")
        elif out.media_type.startswith("image/"):
            preview = str(fin.path)

        record_entity_id, record_warning = self._store_record(
            records.build_generation_record(
                generation_id=generation_id, slot=slot, prompt=prompt,
                vendor=out.vendor, model_tier=out.model_tier, cost_usd=out.cost_usd,
                artifact_ref=str(fin.path), binding_entity_id=active.entity_id,
                created_at=fin.created_at, requested_vendor=requested.vendor,
                fallback_used=attempt.fallback_used, billing_slot=policy.slot,
                cap_group=policy.group or "", remaining_cap_usd=fin.remaining,
            ),
            generation_id,
        )
        if record_warning:
            warnings.append(record_warning)
        return GenerationResult(
            generation_id=generation_id, slot=slot, artifact_ref=str(fin.path),
            vendor=out.vendor, model_tier=out.model_tier, cost_usd=out.cost_usd,
            remaining_cap_usd=fin.remaining, media_type=out.media_type,
            binding_entity_id=active.entity_id, requested_vendor=requested.vendor,
            fallback_used=attempt.fallback_used, raster_preview_ref=preview,
            record_entity_id=record_entity_id, record_persisted=record_entity_id is not None,
            warnings=tuple(warnings),
        )

    def _store_record(self, record: dict[str, Any], generation_id: str) -> tuple[str | None, str | None]:
        try:
            return self._sink.store(record, f"generation-{generation_id}")
        except Exception as exc:  # noqa: BLE001
            return None, f"generation_record was not stored ({type(exc).__name__}). {_SCHEMA_HINT}"

    def _write_artifact(self, slot: str, generation_id: str, ext: str, data: bytes) -> Path:
        directory = self._root / slot
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{generation_id}.{ext}"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return path


_default_client: CapabilityClient | None = None


def generate(
    slot: str,
    prompt: str,
    *,
    tier: str | None = None,
    allow_fallback: bool = True,
    client: CapabilityClient | None = None,
    **opts: Any,
) -> GenerationResult:
    """Generate one asset for ``slot``. See the module docstring."""
    global _default_client
    if client is None:
        if _default_client is None:
            _default_client = CapabilityClient()
        client = _default_client
    return client.generate(slot, prompt, tier=tier, allow_fallback=allow_fallback, **opts)

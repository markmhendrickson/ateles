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
7. store the artifact, append the ledger row (spend is recorded only after the
   artifact is received), then store a private ``generation_record``.

No vendor request is made on any refusal in steps 1 to 4.

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
import json
import os
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from . import records
from .credential_names import AGENT_CHILD_MARKER_ENV
from .credentials import Secret, redact, resolve_credential
from .errors import (
    BINDING_MISSING,
    CAP_UNREADABLE,
    CREDENTIAL_UNRESOLVED,
    EMPTY_RESULT,
    FALLBACK_EXHAUSTED,
    VENDOR_ERROR,
    CapabilityError,
    EmptyArtifact,
    GenerationRefused,
    RasterizerUnavailable,
    VendorFailure,
)
from .rasterize import Renderer, looks_like_svg, rasterize_svg
from .slots import SLOTS
from .spend import CapPolicy, LockedLedger, SpendLedger, parse_cap_policy
from .vendor_binding import (
    Fetcher,
    VendorBinding,
    default_fetcher,
    neotoma_token,
    resolve_vendor_binding,
    _neotoma_base,
    NEOTOMA_USER_AGENT,
)
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


def _default_json_request(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
    token = neotoma_token()
    if not token:
        raise RuntimeError("no Neotoma credential available")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_neotoma_base()}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": NEOTOMA_USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return json.load(resp)


class NeotomaRecordSink:
    """Store a ``generation_record`` with an idempotency key, then read it back.

    Registration of the schema (``register_generation_record_schema.py``) must
    precede the first live store. A 2xx is never taken as proof: the entity is
    fetched and the fields that matter (including ``visibility``) are compared.
    """

    def __init__(self, request: JsonRequest | None = None) -> None:
        self._request = request or _default_json_request

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
                return None, "generation_record store returned no entity id"
            readback = self._request("GET", f"/entities/{entity_id}", None)
            snap = readback.get("snapshot") or {}
            if isinstance(snap.get("snapshot"), dict):
                snap = snap["snapshot"]
            for name in ("generation_id", "slot", "prompt", "vendor", "model_tier", "artifact_ref"):
                if snap.get(name) != record.get(name):
                    return None, f"generation_record read-back mismatch on {name}"
            if snap.get("cost_usd") != record.get("cost_usd"):
                return None, "generation_record read-back mismatch on cost_usd"
            if snap.get("visibility") != records.PRIVATE:
                return None, "generation_record did not land as private; do not reference it"
            return str(entity_id), None
        except Exception as exc:  # noqa: BLE001 - persistence must not lose the artifact
            return None, f"generation_record was not stored ({type(exc).__name__})"


# --- the client ---------------------------------------------------------------


def _default_artifact_root() -> Path:
    override = os.environ.get(ARTIFACT_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "ateles" / "generation_artifacts"


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
            process_values=self.process_values,
        )

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
    ) -> tuple[VendorOutput, CapPolicy, Secret | None]:
        """Authorize, resolve the credential, call the vendor. Raises
        ``GenerationRefused`` before the vendor on any policy failure and
        ``VendorFailure``/``EmptyArtifact`` for an upstream failure."""
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
        locked.authorize(policy, estimate)  # CAP_EXHAUSTED stops here, before any vendor call
        secret = self._credential(adapter, binding)
        # generation_id already exists; it is minted before this point so a
        # retried persist can dedupe on it.
        out = adapter.generate(prompt=prompt, model=model, opts=opts, credential=secret)
        return out, policy, secret

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
        tried: list[str] = []
        use_fallback = bool(
            allow_fallback and binding.fallback and binding.fallback != binding.vendor
        )

        with self._ledger.locked(slot) as locked:
            active = binding
            fallback_used = False
            try:
                tried.append(binding.vendor)
                out, policy, _ = self._attempt(
                    locked, binding, requested_slot=slot, prompt=prompt, tier=tier,
                    opts=opts, generation_id=generation_id,
                )
            except (VendorFailure, EmptyArtifact) as primary_failure:
                out, policy, active, fallback_used = self._after_primary_failure(
                    locked, primary_failure, binding, use_fallback, tried,
                    slot=slot, prompt=prompt, opts=opts, generation_id=generation_id,
                )
            return self._finish(
                locked, out, policy, active, requested=binding, slot=slot,
                prompt=prompt, generation_id=generation_id,
                fallback_used=fallback_used,
            )

    def _fallback_binding(self, primary: VendorBinding) -> VendorBinding:
        """The binding that fills the fallback vendor's own default slot."""
        adapter = self._adapters.get(primary.fallback or "")
        if adapter is None:
            raise LookupError(f"no adapter for fallback vendor {primary.fallback!r}")
        fb = resolve_vendor_binding(adapter.default_slot, fetch=self._fetch)
        if fb.vendor != primary.fallback:
            raise LookupError("the fallback slot is bound to a different vendor")
        return fb

    def _after_primary_failure(
        self,
        locked: LockedLedger,
        failure: Exception,
        primary: VendorBinding,
        use_fallback: bool,
        tried: list[str],
        *,
        slot: str,
        prompt: str,
        opts: Mapping[str, Any],
        generation_id: str,
    ):
        if isinstance(failure, EmptyArtifact):
            first = GenerationRefused(
                EMPTY_RESULT, slot, f"{primary.vendor} answered but produced no artifact",
                "Treat as failed; nothing was recorded as spent. Retry or change the prompt.",
                vendors_tried=tuple(tried), retryable=True,
            )
        else:
            first = self._vendor_error(primary.vendor, failure, slot, tried)
        if not use_fallback:
            raise first from None
        tried.append(primary.fallback or "")
        try:
            fallback = self._fallback_binding(primary)
            out, policy, _ = self._attempt(
                locked, fallback, requested_slot=slot, prompt=prompt, tier=None,
                opts=opts, generation_id=generation_id,
            )
            return out, policy, fallback, True
        except (GenerationRefused, VendorFailure, EmptyArtifact, LookupError) as second:
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

    @staticmethod
    def _vendor_error(vendor: str, failure: Exception, slot: str, tried: list[str]) -> GenerationRefused:
        status = getattr(failure, "http_status", None)
        retryable = bool(getattr(failure, "retryable", True))
        klass = f"HTTP {status}" if status is not None else "transport failure"
        advice = (
            "Retryable: try again shortly."
            if retryable
            else "Operator-fix: the request or account was rejected; retrying "
            "unchanged will not help."
        )
        return GenerationRefused(
            VENDOR_ERROR, slot, f"{vendor} failed after authorization ({klass}): {failure}",
            advice, vendors_tried=tuple(tried), retryable=retryable,
        )

    def _finish(
        self,
        locked: LockedLedger,
        out: VendorOutput,
        policy: CapPolicy,
        active: VendorBinding,
        *,
        requested: VendorBinding,
        slot: str,
        prompt: str,
        generation_id: str,
        fallback_used: bool,
    ) -> GenerationResult:
        if not out.data:
            raise GenerationRefused(
                EMPTY_RESULT, slot, f"{out.vendor} returned an empty artifact",
                "Treat as failed; it was not recorded as spent or stored.",
                vendors_tried=(out.vendor,), retryable=True,
            )
        if out.media_type == "image/svg+xml" and not looks_like_svg(out.data):
            raise GenerationRefused(
                EMPTY_RESULT, slot, f"{out.vendor} returned a payload that is not an SVG",
                "Treat as failed; it was not recorded as spent or stored.",
                vendors_tried=(out.vendor,), retryable=True,
            )
        ext = _EXT.get(out.media_type, "bin")
        path = self._write_artifact(slot, generation_id, ext, out.data)
        warnings = list(out.warnings)
        created_at = locked.now_iso()  # same clock as the ledger month
        row = {
            "generation_id": generation_id,
            "slot": slot,
            "billing_slot": policy.slot,
            "vendor": out.vendor,
            "model_tier": out.model_tier,
            "cost_usd": out.cost_usd,
            "cap_group": policy.group,
            "created_at": created_at,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        }
        try:
            locked.record(row, policy.slot)
        except GenerationRefused as exc:
            raise GenerationRefused(
                CAP_UNREADABLE, slot,
                f"the artifact was saved at {path} but its spend could NOT be "
                "recorded in the ledger",
                "Operator: repair the ledger location and record generation_id "
                f"{generation_id} (cost {out.cost_usd}) by hand. " + exc.hint,
            ) from None
        remaining = locked.remaining_after(policy)

        preview: str | None = None
        if out.media_type == "image/svg+xml":
            try:
                preview_path = rasterize_svg(
                    out.data, path.with_suffix(".preview.png"), slot=slot,
                    renderer=self.renderer,
                )
                preview = str(preview_path)
            except RasterizerUnavailable as exc:
                warnings.append(f"raster_preview_unavailable: {exc}")
        elif out.media_type.startswith("image/"):
            preview = str(path)

        record_entity_id, record_warning = self._store_record(
            records.build_generation_record(
                generation_id=generation_id, slot=slot, prompt=prompt,
                vendor=out.vendor, model_tier=out.model_tier, cost_usd=out.cost_usd,
                artifact_ref=str(path), binding_entity_id=active.entity_id,
                created_at=created_at,
            ),
            generation_id,
        )
        if record_warning:
            warnings.append(record_warning)
        return GenerationResult(
            generation_id=generation_id, slot=slot, artifact_ref=str(path),
            vendor=out.vendor, model_tier=out.model_tier, cost_usd=out.cost_usd,
            remaining_cap_usd=remaining, media_type=out.media_type,
            binding_entity_id=active.entity_id, requested_vendor=requested.vendor,
            fallback_used=fallback_used, raster_preview_ref=preview,
            record_entity_id=record_entity_id, record_persisted=record_entity_id is not None,
            warnings=tuple(warnings),
        )

    def _store_record(self, record: dict[str, Any], generation_id: str) -> tuple[str | None, str | None]:
        try:
            return self._sink.store(record, f"generation-{generation_id}")
        except Exception as exc:  # noqa: BLE001
            return None, f"generation_record was not stored ({type(exc).__name__})"

    def _write_artifact(self, slot: str, generation_id: str, ext: str, data: bytes) -> Path:
        directory = self._root / slot
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{generation_id}.{ext}"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
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

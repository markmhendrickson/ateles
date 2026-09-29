"""Resolve a generation slot to its ``vendor_binding`` entity.

Extends the existing ``vendor_binding`` type (foundation decision 35); there is
no second binding type for generation. The lookup is always
``entity_type = vendor_binding`` + ``capability = <exact slot>`` under the
authenticated operator's own Neotoma scope. No function here accepts a
caller-supplied ``entity_id``, so a caller cannot bypass the capability filter
or read across a user boundary.

Neotoma unreachable, unauthenticated, or returning no match all resolve to
``BINDING_MISSING``: unknown is not a permission to spend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from . import neotoma_http
from .errors import BINDING_MISSING, GenerationRefused
from .slots import SLOTS

BINDING_ENTITY_TYPE = "vendor_binding"
NONE_BY_DESIGN = "none by design"
_PAGE = 100
_MAX_PAGES = 10

# A fetcher takes (entity_type) and returns the list of entity rows as the
# Neotoma /entities/query route returns them. Injected in tests.
Fetcher = Callable[[str], "list[dict[str, Any]]"]


@dataclass(frozen=True)
class VendorBinding:
    entity_id: str
    capability: str
    vendor: str
    tool_namespace: str
    credential_location: str
    fallback: str | None
    constraints_raw: str
    constraints: dict[str, Any] | None  # None => present but unparseable
    visibility: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def constraints_unset(self) -> bool:
        return not (self.constraints_raw or "").strip()


def _snapshot_of(row: dict[str, Any]) -> dict[str, Any]:
    outer = row.get("snapshot") or {}
    inner = outer.get("snapshot", outer) if isinstance(outer, dict) else {}
    return inner if isinstance(inner, dict) else {}


def default_fetcher(entity_type: str) -> "list[dict[str, Any]]":
    """Page through ``POST /entities/query`` for one entity type."""
    rows: list[dict[str, Any]] = []
    for page in range(_MAX_PAGES):
        data = neotoma_http.request_json(
            "POST",
            "/entities/query",
            {
                "entity_type": entity_type,
                "limit": _PAGE,
                "offset": page * _PAGE,
                "include_snapshots": True,
            },
        )
        batch = data.get("entities") or []
        rows.extend(batch)
        if len(batch) < _PAGE:
            break
    return rows


def _parse_constraints(raw: object) -> tuple[str, dict[str, Any] | None]:
    """(raw_text, parsed dict or None when present-but-unparseable)."""
    if raw is None:
        return "", {}
    if isinstance(raw, dict):
        return json.dumps(raw), dict(raw)
    text = str(raw)
    if not text.strip():
        return "", {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return text, None
    if not isinstance(parsed, dict):
        return text, None
    return text, parsed


def _normalize_fallback(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower().startswith(NONE_BY_DESIGN) or text.lower() == "none":
        return None
    return text


def binding_from_row(row: dict[str, Any]) -> VendorBinding:
    snap = _snapshot_of(row)
    raw_text, parsed = _parse_constraints(snap.get("constraints"))
    return VendorBinding(
        entity_id=str(row.get("entity_id") or ""),
        capability=str(snap.get("capability") or ""),
        vendor=str(snap.get("vendor") or "").strip(),
        tool_namespace=str(snap.get("tool_namespace") or "").strip(),
        credential_location=str(snap.get("credential_location") or "").strip(),
        fallback=_normalize_fallback(snap.get("fallback")),
        constraints_raw=raw_text,
        constraints=parsed,
        visibility=str(snap.get("visibility") or ""),
        raw=snap,
    )


def resolve_vendor_binding(slot: str, *, fetch: Fetcher | None = None) -> VendorBinding:
    """The single ``vendor_binding`` for ``slot`` or ``BINDING_MISSING``."""
    if slot not in SLOTS:
        raise GenerationRefused(
            BINDING_MISSING,
            slot if isinstance(slot, str) else None,
            f"{slot!r} is not a generation slot",
            "Use one of the exact slot strings: " + ", ".join(SLOTS) + ". "
            "Aliases are not accepted.",
        )
    fetcher = fetch or default_fetcher
    try:
        rows = fetcher(BINDING_ENTITY_TYPE)
    except neotoma_http.NeotomaConfigError as exc:
        raise GenerationRefused(
            BINDING_MISSING,
            slot,
            f"the binding store cannot be reached from this process: {exc}",
            "This is a client configuration problem, not a missing binding. Run "
            "the client in a process that has NEOTOMA_BASE_URL and "
            "NEOTOMA_BEARER_TOKEN available (see "
            "docs/dev/generation_capability_client.md). Nothing was spent.",
        ) from None
    except neotoma_http.NeotomaRequestError as exc:
        raise GenerationRefused(
            BINDING_MISSING,
            slot,
            f"the binding store rejected the read ({exc})",
            "HTTP 401/403 usually means the bearer token is stale or scoped to "
            "another owner; fix the token, not the binding. Nothing was spent.",
        ) from None
    except Exception as exc:  # noqa: BLE001 - any read failure fails closed
        raise GenerationRefused(
            BINDING_MISSING,
            slot,
            f"vendor_binding could not be read ({type(exc).__name__})",
            "The binding store was unreachable (network or store outage), not "
            "necessarily missing the binding. Check connectivity and the "
            "token, then retry. Nothing was spent.",
        ) from None
    matches = [
        b
        for b in (binding_from_row(r) for r in rows or [])
        if b.capability == slot
    ]
    distinct = {b.entity_id for b in matches}
    if not matches:
        raise GenerationRefused(
            BINDING_MISSING,
            slot,
            f"no vendor_binding with capability={slot}",
            f"Create a vendor_binding entity with capability={slot} "
            "(vendor, tool_namespace, credential_location, fallback, constraints).",
        )
    if len(distinct) > 1:
        raise GenerationRefused(
            BINDING_MISSING,
            slot,
            f"{len(distinct)} vendor_binding entities claim capability={slot}",
            "Merge or retire the duplicates so exactly one binding fills the slot.",
        )
    return matches[0]

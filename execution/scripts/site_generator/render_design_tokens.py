#!/usr/bin/env python3
"""
render_design_tokens.py — project the `design_system` Neotoma entity into a
per-product JSON mirror the static site generator reads at build time.

WHY THIS EXISTS: build_site.py (this directory's generator) must never call
Neotoma at build time — that is the whole point of the projection model this
repo already uses for docs/taxonomy.md, docs/agents/*.md, and
docs/positioning/. A design system is exactly the same shape of problem as
those: it is corrected in Neotoma by a design agent, so committing its values
as hand-authored JSON in this repo would recreate the exact drift-is-possible
failure the operator ruled against.

So: Neotoma stays canonical for design tokens. This script is the render
step. The output is a per-product JSON file under design_tokens/, each
carrying the source entity id and the observation id per field (same
freshness-stamp discipline as neotoma_mirror_lib.observation_ids_block),
checked into the repo as a build input like docs/taxonomy.md.

INPUT CONTRACT this generator's OUTPUT satisfies (consumed by build_site.py):
a JSON object with:
  - _source: {entity_id, entity_type, fetched_at}
  - _observation_ids: {field: observation_id, ...}  (or "unknown")
  - identity: {design_system_name, spacing_system, border_radius,
               positioning_principles, anti_patterns, scope} — the product's
               own visual argument, not a shared component skeleton
  - product: {color_palette, type_scale} — the product-specific tokens

The HTTP/env/snapshot plumbing comes from neotoma_mirror_lib.py, the same
shared implementation used by every other Neotoma-to-repo projection. This
keeps authentication, retries, and nested snapshot handling from drifting
between generators.

Usage:
    render_design_tokens.py <product>            # Neotoma -> disk
    render_design_tokens.py <product> --check     # exit 1 if disk differs
    render_design_tokens.py --check               # check ALL known products

Env: NEOTOMA_BASE_URL, NEOTOMA_BEARER_TOKEN (falls back to
~/.config/neotoma/.env). ATELES_<PRODUCT>_DESIGN_SYSTEM_ENTITY_ID overrides
the product's canonical entity id below.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = Path(__file__).resolve().parent / "design_tokens"

sys.path.insert(0, str(REPO_ROOT / "execution" / "scripts"))
from neotoma_mirror_lib import load_env, request, unwrap_snapshot  # noqa: E402

DEFAULT_DESIGN_SYSTEM_ENTITY_IDS = {
    "neotoma": "ent_746b1d7c717e7780e7943782",
    "ateles": "ent_9158c8b39e0f437fdb2a86de",
}

IDENTITY_FIELDS = (
    "design_system_name",
    "scope",
    "spacing_system",
    "border_radius",
    "positioning_principles",
    "anti_patterns",
)
PER_PRODUCT_FIELDS = ("color_palette", "type_scale")
KNOWN_PRODUCTS = ("ateles", "neotoma")


def _observation_ids(provenance: dict, fields) -> dict:
    return {f: provenance.get(f) or "unknown" for f in fields}


def _css_ready_type_scale(value: dict | None) -> dict:
    """Project prose-bearing family fields into valid CSS values.

    The identity records deliberately annotate retained choices with suffixes
    such as ``(KEPT)``. That note belongs to the entity's rationale, not to a
    ``font-family`` declaration; leaving it attached makes the whole CSS value
    invalid and silently falls back to Times.
    """
    result = dict(value or {})
    for key in ("heading_font_family", "body_font_family", "code_font_family"):
        if isinstance(result.get(key), str):
            result[key] = re.sub(r"\s+\((?:KEPT|NEW).*$", "", result[key]).strip()
    return result


def render(product: str, base_url: str, token: str, entity_id: str) -> dict:
    entity = request(f"{base_url}/entities/{entity_id}", token)
    snapshot, provenance = unwrap_snapshot(entity)

    identity = {f: snapshot.get(f) for f in IDENTITY_FIELDS}
    per_product = {f: snapshot.get(f) for f in PER_PRODUCT_FIELDS}
    per_product["type_scale"] = _css_ready_type_scale(per_product["type_scale"])

    doc = {
        "_source": {
            "entity_id": entity_id,
            "entity_type": "design_system",
            "product": product,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "_observation_ids": _observation_ids(
            provenance, IDENTITY_FIELDS + PER_PRODUCT_FIELDS
        ),
        "identity": identity,
        "product": per_product,
    }
    return doc


def _out_path(product: str) -> Path:
    return OUT_DIR / f"{product}.json"


def write(product: str, doc: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _out_path(product)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {path.relative_to(REPO_ROOT)}")


def check(product: str, doc: dict) -> bool:
    path = _out_path(product)
    if not path.exists():
        print(f"MISSING: {path.relative_to(REPO_ROOT)}")
        return False
    on_disk = json.loads(path.read_text())
    # Compare everything except the volatile fetched_at timestamp.
    on_disk_cmp = {
        **on_disk,
        "_source": {**on_disk.get("_source", {}), "fetched_at": None},
    }
    doc_cmp = {**doc, "_source": {**doc["_source"], "fetched_at": None}}
    if on_disk_cmp != doc_cmp:
        print(f"DRIFT: {path.relative_to(REPO_ROOT)} differs from Neotoma")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "product",
        nargs="?",
        help="product slug (e.g. ateles, neotoma); omit with --check to check all",
    )
    parser.add_argument(
        "--check", action="store_true", help="verify disk matches Neotoma"
    )
    args = parser.parse_args()

    base_url, token = load_env()

    products = [args.product] if args.product else list(KNOWN_PRODUCTS)

    ok = True
    for product in products:
        default_entity_id = DEFAULT_DESIGN_SYSTEM_ENTITY_IDS.get(product)
        if not default_entity_id:
            print(f"No canonical design-system entity configured for {product}")
            return 1
        env_key = f"ATELES_{product.upper()}_DESIGN_SYSTEM_ENTITY_ID"
        entity_id = os.environ.get(env_key, default_entity_id)
        doc = render(product, base_url, token, entity_id)
        if args.check:
            ok = check(product, doc) and ok
        else:
            write(product, doc)

    if args.check:
        if ok:
            print("design token mirror check OK — disk matches Neotoma")
            return 0
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

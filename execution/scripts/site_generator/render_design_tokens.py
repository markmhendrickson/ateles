#!/usr/bin/env python3
"""
render_design_tokens.py — project the `design_system` Neotoma entity into a
per-product JSON mirror the static site generator reads at build time.

WHY THIS EXISTS: build_site.py (this directory's generator) must never call
Neotoma at build time — that is the whole point of the projection model this
repo already uses for docs/taxonomy.md, docs/agents/*.md, and
docs/positioning/. A design system is exactly the same shape of problem as
those: it is corrected in Neotoma by a design agent (see design_system entity
ent_72e01f1008653b601be1a956, "Neotoma & Ateles Visual System" — corrected at
least twice already, including a 2026-09-22 reconciliation against drift two
independent build-landing-page runs had flagged and declined to fix
unilaterally), so committing its values as hand-authored JSON in this repo
would recreate the exact drift-is-possible failure the operator ruled against
today (the Neotoma anti-profile page example in the task brief this script
was built for).

So: Neotoma stays canonical for design tokens. This script is the render
step. The output is a per-product JSON file under design_tokens/, each
carrying the source entity id and the observation id per field (same
freshness-stamp discipline as neotoma_mirror_lib.observation_ids_block),
checked into the repo as a build input like docs/taxonomy.md.

INPUT CONTRACT this generator's OUTPUT satisfies (consumed by build_site.py):
a JSON object with:
  - _source: {entity_id, entity_type, fetched_at}
  - _observation_ids: {field: observation_id, ...}  (or "unknown")
  - shared: {spacing_system, border_radius, positioning_principles,
             anti_patterns, scope}  — tokens declared identical across every
             product this design_system entity covers
  - product: {color_palette, type_scale}  — the per-product theme slice for
             THIS run's product, selected from the entity's per-product keyed
             fields (see DESIGN_SYSTEM_ENTITY_ID's color_palette/type_scale
             shape, which nests neotoma/ateles under shared variable names)

The HTTP/env/snapshot plumbing comes from neotoma_mirror_lib.py, the same
shared implementation used by every other Neotoma-to-repo projection. This
keeps authentication, retries, and nested snapshot handling from drifting
between generators.

Usage:
    render_design_tokens.py <product>            # Neotoma -> disk
    render_design_tokens.py <product> --check     # exit 1 if disk differs
    render_design_tokens.py --check               # check ALL known products

Env: NEOTOMA_BASE_URL, NEOTOMA_BEARER_TOKEN (falls back to
~/.config/neotoma/.env). ATELES_DESIGN_SYSTEM_ENTITY_ID overrides the default
entity id below.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = Path(__file__).resolve().parent / "design_tokens"

sys.path.insert(0, str(REPO_ROOT / "execution" / "scripts"))
from neotoma_mirror_lib import load_env, request, unwrap_snapshot  # noqa: E402

# The single design_system entity covering both products (decision recorded
# on the entity itself 2026-09-22: "this entity remains ONE system covering
# both products", not two separate entities — spacing/radius are shared,
# color_palette/type_scale are keyed per-product on the same entity).
DEFAULT_DESIGN_SYSTEM_ENTITY_ID = "ent_72e01f1008653b601be1a956"

# Fields read from the entity. Shared fields apply to every product; the two
# per-product fields are dicts keyed by product slug on the live entity.
SHARED_FIELDS = (
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


def render(product: str, base_url: str, token: str, entity_id: str) -> dict:
    entity = request(f"{base_url}/entities/{entity_id}", token)
    snapshot, provenance = unwrap_snapshot(entity)

    shared = {f: snapshot.get(f) for f in SHARED_FIELDS}
    per_product = {}
    for f in PER_PRODUCT_FIELDS:
        val = snapshot.get(f) or {}
        # These fields carry a shared variable-name list plus per-product
        # sub-objects (see the entity's own `note`/`shared_variable_names`
        # keys). Select this product's slice; fall back to the whole object
        # if the entity is not yet keyed per-product (older shape).
        per_product[f] = val.get(product, val) if isinstance(val, dict) else val

    doc = {
        "_source": {
            "entity_id": entity_id,
            "entity_type": "design_system",
            "product": product,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "_observation_ids": _observation_ids(
            provenance, SHARED_FIELDS + PER_PRODUCT_FIELDS
        ),
        "shared": shared,
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

    entity_id = os.environ.get(
        "ATELES_DESIGN_SYSTEM_ENTITY_ID", DEFAULT_DESIGN_SYSTEM_ENTITY_ID
    )
    base_url, token = load_env()

    products = [args.product] if args.product else list(KNOWN_PRODUCTS)

    ok = True
    for product in products:
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

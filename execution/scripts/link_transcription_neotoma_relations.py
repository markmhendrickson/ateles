#!/usr/bin/env python3
"""
Create Neotoma REFERS_TO edges from an existing ``transcription`` entity to
``contact`` and/or ``feedback_analysis`` rows (same semantics as transcribe_audio.py).

Usage (from repo root, with .env sourcing NEOTOMA_BEARER_TOKEN)::

    execution/venv/bin/python execution/scripts/link_transcription_neotoma_relations.py \\
      --transcription-id ent_abc... \\
      --contact-entity-id ent_def... \\
      --contact-entity-id ent_ghi... \\
      --feedback-analysis-entity-id ent_jkl...

Uses prod CLI transport (``NEOTOMA_PROD_BASE_URL``, default http://localhost:3180).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "execution" / "scripts"))

from transcribe_audio import apply_transcription_neotoma_relationships  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(
        description="Neotoma REFERS_TO: transcription → contacts / feedback_analysis"
    )
    p.add_argument(
        "--transcription-id",
        required=True,
        help="Existing transcription entity_id (ent_…).",
    )
    p.add_argument(
        "--contact-entity-id",
        action="append",
        default=[],
        metavar="ENT_ID",
        help="Contact entity_id to link (repeatable).",
    )
    p.add_argument(
        "--feedback-analysis-entity-id",
        default=None,
        help="Optional feedback_analysis entity_id.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print one line per relationship attempt including successes.",
    )
    args = p.parse_args()
    tid = args.transcription_id.strip()
    if not tid.startswith("ent_"):
        print("transcription-id must start with ent_", file=sys.stderr)
        sys.exit(1)
    contacts = [
        c for c in args.contact_entity_id if isinstance(c, str) and c.startswith("ent_")
    ]
    fba = args.feedback_analysis_entity_id
    if fba is not None:
        fba = fba.strip()
        if fba and not fba.startswith("ent_"):
            print("feedback-analysis-entity-id must start with ent_", file=sys.stderr)
            sys.exit(1)
        if not fba:
            fba = None
    if not contacts and not fba:
        print(
            "Provide at least one --contact-entity-id or --feedback-analysis-entity-id",
            file=sys.stderr,
        )
        sys.exit(1)
    apply_transcription_neotoma_relationships(
        tid, contacts, fba, verbose=bool(args.verbose)
    )


if __name__ == "__main__":
    main()

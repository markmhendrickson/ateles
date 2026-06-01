#!/usr/bin/env python3
"""
One-off migration: Neotoma feedback from strategy/reference markdown into
$DATA_DIR/feedback/feedback.parquet. All parquet data lives in DATA_DIR (from
env or config), not in repo data/. Creates DATA_DIR/schemas/feedback_schema.json
and DATA_DIR/feedback/feedback.parquet.
"""

import json
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "execution" / "scripts"))
from config import get_data_dir

DATA_DIR = get_data_dir()
SCHEMAS_DIR = DATA_DIR / "schemas"
SCHEMA_TEMPLATE = Path(__file__).resolve().parent / "schemas" / "feedback_schema.json"
FEEDBACK_DIR = DATA_DIR / "feedback"
FEEDBACK_FILE = FEEDBACK_DIR / "feedback.parquet"


def main():
    if not SCHEMA_TEMPLATE.exists():
        raise SystemExit(f"Schema template not found: {SCHEMA_TEMPLATE}")

    with open(SCHEMA_TEMPLATE) as f:
        meta = json.load(f)
    columns = list(meta["schema"].keys())

    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    schema_dest = SCHEMAS_DIR / "feedback_schema.json"
    with open(schema_dest, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Wrote schema to {schema_dest}")

    today = date.today().isoformat()

    records = [
        {
            "feedback_id": str(uuid4())[:16],
            "source_name": "Dick Hardt",
            "source_type": "call",
            "topic": "Neotoma",
            "feedback_date": "2025-12-24",
            "summary": "Feedback from call with Dick Hardt (Hellō founder) regarding Neotoma concept, architecture, and execution. Skeptical about feasibility and execution capability; increased appreciation for need for decentralized/robust memory systems.",
            "key_themes": "Proactivity challenge (agents may not use MCP enough); schema feasibility unsolved, popular schemas could solve it; vector search scaling beyond 100k; interoperability as differentiator; execution capability (data storage/retrieval hard, blockchain experience not seen as transferable); market validation needed.",
            "implications": "Emphasize cross-platform interoperability and decentralized architecture; focus on popular schemas; address agent proactivity via MCP context; refine positioning to data systems expertise.",
            "full_content": None,
            "source_file_path": "strategy/reference/neotoma-feedback-dick-hardt-call.md",
            "created_date": today,
            "updated_date": today,
            "import_date": today,
            "import_source_file": "migrate_feedback_to_parquet",
        },
        {
            "feedback_id": str(uuid4())[:16],
            "source_name": "Ed Mcmanus",
            "source_type": "imessage",
            "topic": "Neotoma",
            "feedback_date": "2026-02-09",
            "summary": "Feedback on Neotoma value proposition and VC strategy. Portable and local as structural advantages closed providers cannot replicate; VC path possible with strong adoption even without revenue.",
            "key_themes": "Portable + local as barrier/strength; strong adoption can support VC raise (MIT, open source, no revenue); user stance: bootstrap to revenue default, VC possible if structured right, prefer to maintain control.",
            "implications": "Emphasize portable and local-first architecture; bootstrap-to-revenue remains default; adoption metrics matter for any future VC path.",
            "full_content": None,
            "source_file_path": "strategy/reference/neotoma-feedback-ed-mcmanus.md",
            "created_date": today,
            "updated_date": today,
            "import_date": today,
            "import_source_file": "migrate_feedback_to_parquet",
        },
    ]

    import pandas as pd

    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(records, columns=columns)
    df.to_parquet(FEEDBACK_FILE, index=False)
    print(f"Wrote {len(records)} feedback records to {FEEDBACK_FILE}")


if __name__ == "__main__":
    main()

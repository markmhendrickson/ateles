#!/usr/bin/env python3
"""
Generate website cache from Neotoma export only.

Wraps generate_posts_cache.py with default path data/tmp/neotoma_website_export.json.
Export file must exist; script exits with error if missing.
"""

import sys
from pathlib import Path

# Repo root: execution/scripts -> execution -> repo root
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_NEOTOMA_EXPORT = REPO_ROOT / "data" / "tmp" / "neotoma_website_export.json"


def main():
    from generate_posts_cache import main as posts_cache_main

    # Inject default export path if not already provided
    has_path = any(
        a == "--from-neotoma-json" or a.startswith("--from-neotoma-json=")
        for a in sys.argv[1:]
    )
    if not has_path:
        sys.argv = [
            sys.argv[0],
            "--from-neotoma-json",
            str(DEFAULT_NEOTOMA_EXPORT),
        ] + sys.argv[1:]
    posts_cache_main()


if __name__ == "__main__":
    main()

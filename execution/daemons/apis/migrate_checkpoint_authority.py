#!/usr/bin/env python3
"""Create a fresh v2 approval request for one approved legacy checkpoint."""

from __future__ import annotations

import argparse

from apis import (
    Notifier,
    _require_checkpoint_denial_store,
    migrate_checkpoint_authority,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint_id", help="Approved legacy checkpoint entity ID")
    args = parser.parse_args()

    _require_checkpoint_denial_store()
    replacement_id = migrate_checkpoint_authority(
        args.checkpoint_id,
        notifier=Notifier.from_neotoma(),
    )
    if not replacement_id:
        parser.error(
            "migration failed closed; the checkpoint was not released or consumed"
        )
    print(replacement_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

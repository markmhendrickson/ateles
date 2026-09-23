#!/usr/bin/env python3
"""Run the trusted canonical measurement without exposing child output."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import package_rule_inventory_inputs as inputs


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "rule inventory measurement failed before a safe verdict", file=sys.stderr
        )
        return 2
    trusted_root = Path(__file__).resolve().parents[2]
    renderer = trusted_root / "execution" / "scripts" / "render_rule_inventory.py"
    candidate_root = Path(argv[1])
    try:
        if renderer.is_symlink() or renderer.resolve(strict=True) != renderer:
            raise inputs.InputBoundaryError
        inputs.validate_inputs(candidate_root)
        candidate_root = candidate_root.resolve(strict=True)
        if not os.environ.get("NEOTOMA_BEARER_TOKEN"):
            print(
                "rule inventory equality unavailable: canonical read credential is missing",
                file=sys.stderr,
            )
            return 3
        if not os.environ.get("RULE_INVENTORY_CANONICAL_REPOSITORY_ROOTS"):
            print(
                "rule inventory equality unavailable: canonical repository roots are missing",
                file=sys.stderr,
            )
            return 3
        completed = subprocess.run(
            [
                sys.executable,
                str(renderer),
                "--repository-input-root",
                str(candidate_root),
                "--expected-output",
                str(candidate_root / "docs" / "foundation" / "rule_inventory.md"),
                "--check",
                "--require-complete-measurement",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            check=False,
        )
    except (OSError, inputs.InputBoundaryError):
        print(
            "rule inventory measurement failed before a safe verdict", file=sys.stderr
        )
        return 2

    messages = {
        0: "rule inventory matches the complete canonical measurement",
        1: "rule inventory differs from the complete canonical measurement",
        2: "rule inventory public-output safety check failed",
        3: "rule inventory equality unavailable: full measurement is incomplete",
    }
    known_returncode = completed.returncode in messages
    result = completed.returncode if known_returncode else 2
    message = messages.get(
        completed.returncode,
        "rule inventory measurement failed before a safe verdict",
    )
    stream = sys.stdout if result == 0 else sys.stderr
    print(message, file=stream)
    return result


if __name__ == "__main__":
    sys.exit(main(sys.argv))

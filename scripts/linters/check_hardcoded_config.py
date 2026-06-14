#!/usr/bin/env python3
"""
check_hardcoded_config.py — enforce the Ateles "Neotoma-canonical / env-sourced"
config rule in daemon and runtime code.

WHY THIS EXISTS (and why it is NOT redundant with the gitleaks PII scan):

  The gitleaks scan in .gitleaks.toml guards against *third-party PII leaking
  into a public repo*. It therefore DELIBERATELY allowlists the operator's own
  email and Google Calendar resource IDs (see the [allowlist] block) — those
  are not third-party PII, so gitleaks lets them through.

  But the Ateles architecture has a SEPARATE rule (docs/architecture.md,
  CLAUDE.md standing constraints): operator contact details, calendar IDs,
  recipients, IBANs, and other operator-specific config must be read from env
  (or parquet / Neotoma) at runtime — NEVER hardcoded — so the swarm is
  portable, versioned, and operator-agnostic. gitleaks does not enforce that.

  This linter closes that gap. It flags operator-specific config values that
  are baked into daemon/runtime code instead of being sourced from env/Neotoma.

SCOPE: lib/, execution/daemons/, execution/scripts/ Python files (the always-on
runtime). Tests and this linter itself are exempt.

SUPPRESSION: append `# config-source-ok: <reason>` to a line that legitimately
contains such a literal (e.g. an explicit env-default fallback that is reviewed
and intentional). Use sparingly.

Usage:
  python3 scripts/linters/check_hardcoded_config.py [file1.py file2.py ...]
  (no args → scans the default runtime dirs)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SUPPRESS = "config-source-ok"

# Default runtime trees to scan when no files are passed.
DEFAULT_DIRS = ("lib", "execution/daemons", "execution/scripts")

# Patterns for operator-specific config that must be env/Neotoma-sourced.
# Each entry: (compiled regex, short label, remediation hint).
CHECKS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r"[A-Za-z0-9._%+-]+@"
            r"(?:gmail|googlemail|outlook|hotmail|live|yahoo|ymail|icloud|me|"
            r"protonmail|proton|pm|gmx|aol|fastmail)\.[A-Za-z]{2,}",
            re.IGNORECASE,
        ),
        "operator/personal email literal",
        "read from OPERATOR_EMAIL (or a Neotoma person entity), not a literal",
    ),
    (
        re.compile(r"[A-Za-z0-9._-]+@group\.calendar\.google\.com", re.IGNORECASE),
        "Google Calendar resource ID literal",
        "read from COTINGA_CALENDAR_IDS / an env calendar list, not a literal",
    ),
    (
        re.compile(r"\b[A-Z]{2}[0-9]{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b"),
        "IBAN literal",
        "read from env or contacts.parquet / Neotoma payment_profile, never hardcode",
    ),
    (
        # Bitcoin bech32 (bc1...) addresses — unambiguous and what payment
        # profiles use. Real destination addresses must come from env, never a
        # literal (even in a docstring example — this is a public repo).
        re.compile(r"\bbc1[a-z0-9]{25,59}\b"),
        "Bitcoin address literal",
        "read from <PREFIX>_BTC_ADDRESS env (Neotoma payment_profile), use a "
        "<placeholder> in examples",
    ),
]

# Placeholder / example local-parts that are obviously not real config.
PLACEHOLDER = re.compile(
    r"^(?:you|your|user|name|email|me|someone|admin|test|foo|bar|john\.doe|"
    r"jane\.doe|noreply|no-reply|example|placeholder)$",
    re.IGNORECASE,
)


def _is_placeholder_email(match: str) -> bool:
    local = match.split("@", 1)[0]
    return bool(PLACEHOLDER.match(local))


def _iter_files(args: list[str]) -> list[Path]:
    if args:
        return [Path(a) for a in args if a.endswith(".py")]
    files: list[Path] = []
    for d in DEFAULT_DIRS:
        files.extend(Path(d).rglob("*.py"))
    return files


def _exempt(path: Path) -> bool:
    name = path.name
    parts = path.parts
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if "tests" in parts:
        return True
    # The linter must not flag its own pattern strings.
    if name == "check_hardcoded_config.py":
        return True
    return False


def scan_file(path: Path) -> list[tuple[int, str, str, str]]:
    violations: list[tuple[int, str, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return violations
    for lineno, line in enumerate(text.splitlines(), 1):
        if SUPPRESS in line:
            continue
        for regex, label, hint in CHECKS:
            for m in regex.finditer(line):
                val = m.group(0)
                if label.startswith("operator") and _is_placeholder_email(val):
                    continue
                violations.append((lineno, label, val, hint))
    return violations


def main(argv: list[str]) -> int:
    files = _iter_files(argv)
    total = 0
    for path in files:
        if not path.exists() or _exempt(path):
            continue
        for lineno, label, val, hint in scan_file(path):
            if total == 0:
                print(
                    "❌ CONFIG-SOURCING VIOLATION: operator-specific config "
                    "hardcoded in runtime code.",
                    file=sys.stderr,
                )
                print(
                    "   Per docs/architecture.md + CLAUDE.md, these must be "
                    "sourced from env / parquet / Neotoma — not baked in.\n",
                    file=sys.stderr,
                )
            total += 1
            print(f"  {path}:{lineno}: {label} → {val!r}", file=sys.stderr)
            print(f"      fix: {hint}", file=sys.stderr)
    if total:
        print(
            f"\n{total} violation(s). If a literal is a reviewed, intentional "
            f"env-default fallback, append `# {SUPPRESS}: <reason>` to the line.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

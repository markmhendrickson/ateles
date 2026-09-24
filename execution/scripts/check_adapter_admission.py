#!/usr/bin/env python3
"""Run the AD-21..AD-34 adapter-admission conformance fixture (ateles#1191).

`docs/foundation/conformance_suite.md#adapter-admission` specifies one reference ("sixth") adapter,
satisfying all six admission obligations (AD-21..AD-26 green), and six negative variants — each
required to turn exactly its foundation-mapped row(s) red. This is the primary, and today the only,
invoke surface: wired into `scripts/lint.sh` and `.github/workflows/foundation-checks.yml` beside the
other `check_foundation_*.py` checks, never a standalone CLI brand.

Filter vocabulary (positional or `--filter`, both accepted; positional wins if both given):
    adapter-admission   all rows (default)
    AD-21               one row by id
    3                   one obligation's mapped rows (its variant + reference row)

Exit codes: 0 all selected rows green and no self-check/runtime/selection failure; 1 otherwise,
including `RUNTIME_MISSING` (the #1190 adapter runtime is not importable — never a soft skip-green)
and `EMPTY_SELECTION` (the filter matched nothing). See `execution/conformance/adapter_admission/README.md`
for the three expected-output examples and the error-hint table.

Usage:
    check_adapter_admission.py [--filter SELECTOR] [SELECTOR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from execution.conformance.adapter_admission.runner import FILTER_ALL, format_report, run  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("selector_positional", nargs="?", default=None, metavar="SELECTOR")
    ap.add_argument("--filter", dest="selector_flag", default=None, metavar="SELECTOR")
    args = ap.parse_args(argv)

    selector = args.selector_positional or args.selector_flag or FILTER_ALL

    report = run(selector)
    output = format_report(report)
    if output:
        print(output)
    row_count = len(report.rows)
    failure_count = len(report.failures)
    print(f"adapter-admission: {row_count} row(s) checked, {failure_count} failure(s)")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())

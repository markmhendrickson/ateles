#!/usr/bin/env python3
"""The `Merge gate` column in conformance.md must be true, not decorative.

PR #944 (ateles#929) wired `check_foundation_decision_78.py` into
`foundation-checks.yml` after it sat registered in
`conformance.md#mechanical-checks-on-this-directory` but absent from the
merge-gating lane — the "mechanism that does not bind" defect
(`principles.md` #1). The qa lens on that PR found the fix itself unguarded:
nothing asserted that a row's `Merge gate` cell, once it names a job, actually
corresponds to a step in that job invoking the script named in `Runs`. A
future edit that deletes the invoking step, or that edits the cell to name a
job that never calls the script, would leave the table lying about CI and
nothing here would fail.

This file closes that gap two ways:

* `test_real_tree_merge_gate_claims_resolve_to_workflow_steps` walks every row
  of the real table on the real workflow file and asserts every non-`—`
  `Merge gate` cell names a job that has a step invoking the row's script via
  `python3`. Rows that correctly read `—` (the three unwired contract rows,
  the advisory undefined-word-candidates row, and Plan decision citations,
  which lives only in the workflow's `paths:` filter) are asserted to KEEP
  reading `—` here rather than skipped — a row wrongly flipped to claim a job
  is exactly the drift this test exists to catch, and skipping it would blind
  the test to it.
* `test_fails_when_merge_gated_checker_step_is_removed` and
  `test_fails_when_merge_gate_cell_claims_the_wrong_job` are planted-negative
  tests against synthetic tables/workflows: proof this check can go red on
  the thing it watches (`principles.md` #4 — a test that cannot fail is
  decoration).

Stdlib only. Skips gracefully, per `TestPathFilterCoversThisFile`'s pattern in
`test_decision_state.py`, when the corpus or workflow file is absent on a
branch — a check that cannot find what it reads is not a violation of it.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFORMANCE = REPO_ROOT / "docs" / "foundation" / "conformance.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "foundation-checks.yml"

TABLE_HEADER = "| Check | Runs | Merge gate | What fails |"
TABLE_END_MARKER = "## Direction of truth per class of record"

# Rows whose `Merge gate` cell is correctly `—` on the real corpus today, and
# why. A row moving off this list (because a script landed and got wired) is
# fine — the real-tree test only demands wired rows resolve, and unwired rows
# are free to leave this list once they are actually wired. A row's cell
# flipping to claim a job WITHOUT an invoking step existing is what the
# planted negatives catch; this list is not a waiver for that.
EXPECTED_DASH_CHECKS = {
    "Undefined-word candidates",  # advisory only; shares the vocabulary exit code and never fails it
    "Workflow tables",  # contract; no script on disk yet
    "Data-model tables",  # contract; no script on disk yet
    "Rule coverage",  # contract; no script on disk yet
    "Plan decision citations",  # path-filtered only — no step invokes it
}


class MergeGateRow:
    def __init__(self, check: str, runs: str, gate: str) -> None:
        self.check = check
        self.runs = runs
        self.gate = gate

    @property
    def is_dash(self) -> bool:
        stripped = self.gate.strip()
        return stripped.startswith("`—`") or stripped.startswith("—")

    @property
    def job_id(self) -> str | None:
        m = re.match(r"`[^`]+`\s*\(`([^`]+)`\s*job\)", self.gate.strip())
        return m.group(1) if m else None

    @property
    def script_name(self) -> str | None:
        """The first `execution/scripts/*.py` filename in the Runs cell.

        Falls back to any `.py` path in the cell (e.g. `Reading-list budget`'s
        `execution/daemons/apis/test_foundation.py`, invoked by pytest rather
        than the checker-script idiom) so every non-`—` row has a script to
        resolve, not only the `execution/scripts/` ones.
        """
        m = re.search(r"`execution/scripts/([^`\s]+\.py)", self.runs)
        if m:
            return m.group(1)
        m = re.search(r"`(?:[\w./-]+/)?([\w.-]+\.py)`", self.runs)
        return m.group(1) if m else None


def parse_merge_gate_table(conformance_text: str) -> list[MergeGateRow]:
    """Parse the mechanical-checks table's Check/Runs/Merge gate columns.

    Splits each row on the FIRST three `|`-delimited cells only (leaving
    'What fails' — the fourth column — untouched, since it freely contains
    escaped pipes and backticks that a naive split would misparse).
    """
    if TABLE_HEADER not in conformance_text:
        raise AssertionError(
            f"{TABLE_HEADER!r} not found in conformance.md — table structure changed"
        )
    start = conformance_text.index(TABLE_HEADER)
    if TABLE_END_MARKER not in conformance_text:
        raise AssertionError(f"{TABLE_END_MARKER!r} not found in conformance.md")
    end = conformance_text.index(TABLE_END_MARKER, start)
    table_text = conformance_text[start:end]

    rows: list[MergeGateRow] = []
    for line in table_text.splitlines():
        if not line.startswith("|"):
            continue
        if line.startswith("|---"):
            continue
        if line == TABLE_HEADER:
            continue
        # Split into at most 4 cells: leading '', check, runs, gate, rest...
        parts = line.split("|", 4)
        # parts[0] is '' (before the leading pipe)
        if len(parts) < 4:
            continue
        check = parts[1].strip()
        runs = parts[2].strip()
        gate = parts[3].strip()
        if not check:
            continue
        rows.append(MergeGateRow(check, runs, gate))
    return rows


def parse_job_blocks(workflow_text: str) -> dict[str, str]:
    """Split the `jobs:` section into per-job-id text blocks.

    A job header is a line indented exactly two spaces ending in `:` — e.g.
    `  checkers:`. Everything up to the next such header (or EOF) is that
    job's block, including all its steps.
    """
    if "\njobs:" not in workflow_text:
        raise AssertionError("no top-level 'jobs:' key found in the workflow file")
    jobs_text = workflow_text[workflow_text.index("\njobs:") :]
    header_re = re.compile(r"^  ([a-zA-Z0-9_-]+):\s*$", re.M)
    headers = list(header_re.finditer(jobs_text))
    blocks: dict[str, str] = {}
    for i, h in enumerate(headers):
        job_id = h.group(1)
        block_start = h.end()
        block_end = headers[i + 1].start() if i + 1 < len(headers) else len(jobs_text)
        blocks[job_id] = jobs_text[block_start:block_end]
    return blocks


def job_invokes_script_via_python3(job_block: str, script_name: str) -> bool:
    """True if some step's `run:` block in this job invokes the script.

    Recognizes two idioms, both scanned per `run:` block (not the whole job
    block at once) so a step that merely MENTIONS a script name in an
    unrelated comment cannot false-positive:

    1. The checker-script idiom every `checkers`-job step in this workflow
       uses: `f=execution/scripts/<script>` followed by `python3 "$f"` (or a
       direct `python3 execution/scripts/<script>` in the same block).
    2. The pytest idiom `foundation-tests` uses: the script named directly as
       a `pytest`/`python -m pytest` argument in the same run block (with or
       without a `uv run` / `python3` prefix).
    """
    if script_name not in job_block:
        return False
    run_blocks = re.findall(r"run:\s*\|\n((?:[ \t]+.*\n?)*)", job_block)
    for run_block in run_blocks:
        if script_name not in run_block:
            continue
        # Idiom 1: f=execution/scripts/<script> ... python3 "$f"
        if f"f=execution/scripts/{script_name}" in run_block and re.search(
            r'python3\s+"\$f"', run_block
        ):
            return True
        # Idiom 1b: direct python3 execution/scripts/<script> invocation.
        if re.search(
            rf"python3\s+[^\n]*execution/scripts/{re.escape(script_name)}",
            run_block,
        ):
            return True
        # Idiom 2: pytest/python -m pytest naming the file directly. The
        # invocation commonly line-continues with trailing `\` before the
        # file arguments (see foundation-tests' `uv run ... pytest \`), so
        # match across a bounded window rather than requiring the same line.
        if re.search(
            rf"(pytest|python3?\s+-m\s+pytest)[\s\S]{{0,200}}?{re.escape(script_name)}",
            run_block,
        ):
            return True
    return False


class TestRealTreeMergeGateClaimsResolveToWorkflowSteps(unittest.TestCase):
    """Every non-`—` Merge gate cell must name a job that really invokes it."""

    def setUp(self) -> None:
        if not CONFORMANCE.is_file():
            self.skipTest(f"{CONFORMANCE} absent on this branch")
        if not WORKFLOW.is_file():
            self.skipTest(f"{WORKFLOW} absent on this branch")
        self.conformance_text = CONFORMANCE.read_text(encoding="utf-8")
        self.workflow_text = WORKFLOW.read_text(encoding="utf-8")

    def test_wired_rows_resolve_to_a_real_invoking_step(self) -> None:
        rows = parse_merge_gate_table(self.conformance_text)
        self.assertTrue(rows, "parsed zero rows from the mechanical-checks table")
        job_blocks = parse_job_blocks(self.workflow_text)

        failures: list[str] = []
        for row in rows:
            if row.is_dash:
                continue
            job_id = row.job_id
            script = row.script_name
            if job_id is None:
                failures.append(
                    f"{row.check!r}: Merge gate cell {row.gate!r} is not `—` but "
                    "names no `(`<job>` job)` — cannot resolve to a step"
                )
                continue
            if script is None:
                failures.append(
                    f"{row.check!r}: Merge gate claims job {job_id!r} but Runs cell "
                    f"{row.runs!r} names no execution/scripts/*.py script to check"
                )
                continue
            if job_id not in job_blocks:
                failures.append(
                    f"{row.check!r}: Merge gate claims job {job_id!r}, which does "
                    f"not exist in {WORKFLOW.name} (jobs present: "
                    f"{sorted(job_blocks)})"
                )
                continue
            if not job_invokes_script_via_python3(job_blocks[job_id], script):
                failures.append(
                    f"{row.check!r}: Merge gate claims job {job_id!r} invokes "
                    f"{script}, but no step in that job's block calls it via python3"
                )

        self.assertFalse(failures, "\n".join(failures))

    def test_dash_rows_are_exactly_the_expected_unwired_set(self) -> None:
        """A row silently flipping to `—` (or off it) is itself worth seeing.

        This does not gate merge — the real-tree test above is the control —
        but pins today's known-unwired set so a change to it is visible in
        the diff of this test rather than only in conformance.md's prose.
        """
        rows = parse_merge_gate_table(self.conformance_text)
        dash_checks = {row.check for row in rows if row.is_dash}
        self.assertEqual(dash_checks, EXPECTED_DASH_CHECKS)


# ---------------------------------------------------------------------------
# Planted negatives: synthetic conformance.md / workflow pairs proving the
# checks above go RED on the failures they exist to catch.
# ---------------------------------------------------------------------------

CONFORMANCE_TEMPLATE = """\
# Conformance

## Mechanical checks on this directory

| Check | Runs | Merge gate | What fails |
|---|---|---|---|
| Decision 78 ruling | `execution/scripts/check_foundation_decision_78.py`, in `scripts/lint.sh` | {gate} | something |

## Direction of truth per class of record
"""

WORKFLOW_WITH_STEP = """\
name: foundation checks
on:
  pull_request:
    paths:
      - "docs/foundation/**"
  workflow_dispatch:
jobs:
  checkers:
    name: foundation corpus checkers
    runs-on: ubuntu-latest
    steps:
      - name: Decision 78 ruling — register and adapters section agree
        run: |
          f=execution/scripts/check_foundation_decision_78.py
          [ -f "$f" ] || { echo "::notice::$f absent on this branch — skipped"; exit 0; }
          python3 "$f"
  foundation-tests:
    name: foundation binding tests
    runs-on: ubuntu-latest
    steps:
      - name: Run foundation tests
        run: |
          echo noop
"""

WORKFLOW_WITHOUT_STEP = """\
name: foundation checks
on:
  pull_request:
    paths:
      - "docs/foundation/**"
  workflow_dispatch:
jobs:
  checkers:
    name: foundation corpus checkers
    runs-on: ubuntu-latest
    steps:
      - name: Anchors — every intra-foundation link resolves
        run: |
          f=execution/scripts/check_foundation_anchors.py
          [ -f "$f" ] || { echo "::notice::$f absent on this branch — skipped"; exit 0; }
          python3 "$f"
  foundation-tests:
    name: foundation binding tests
    runs-on: ubuntu-latest
    steps:
      - name: Run foundation tests
        run: |
          echo noop
"""


def write_pair(root: Path, conformance: str, workflow: str) -> tuple[Path, Path]:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True, exist_ok=True)
    conf_path = fdir / "conformance.md"
    conf_path.write_text(conformance, encoding="utf-8")
    wf_dir = root / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    wf_path = wf_dir / "foundation-checks.yml"
    wf_path.write_text(workflow, encoding="utf-8")
    return conf_path, wf_path


class TestPlantedNegatives(unittest.TestCase):
    def test_wired_pair_is_green(self) -> None:
        """Sanity: the happy-path synthetic pair must NOT fail, or the
        negatives below would be meaningless (a checker that always fails
        proves nothing either)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conf_path, wf_path = write_pair(
                Path(tmp),
                CONFORMANCE_TEMPLATE.format(
                    gate="`foundation-checks.yml` (`checkers` job)"
                ),
                WORKFLOW_WITH_STEP,
            )
            rows = parse_merge_gate_table(conf_path.read_text(encoding="utf-8"))
            job_blocks = parse_job_blocks(wf_path.read_text(encoding="utf-8"))
            row = rows[0]
            self.assertTrue(
                job_invokes_script_via_python3(job_blocks[row.job_id], row.script_name)
            )

    def test_fails_when_merge_gated_checker_step_is_removed(self) -> None:
        """Removing the invoking step while the cell still claims it wired
        must be caught by name."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conf_path, wf_path = write_pair(
                Path(tmp),
                CONFORMANCE_TEMPLATE.format(
                    gate="`foundation-checks.yml` (`checkers` job)"
                ),
                WORKFLOW_WITHOUT_STEP,  # step removed; cell still claims it
            )
            rows = parse_merge_gate_table(conf_path.read_text(encoding="utf-8"))
            job_blocks = parse_job_blocks(wf_path.read_text(encoding="utf-8"))
            row = rows[0]
            self.assertEqual(row.check, "Decision 78 ruling")
            invoked = job_invokes_script_via_python3(
                job_blocks[row.job_id], row.script_name
            )
            self.assertFalse(
                invoked,
                "expected job_invokes_script_via_python3 to report False when "
                "the invoking step is removed — it reported True, so this "
                "check cannot catch a removed step",
            )

    def test_fails_when_merge_gate_cell_claims_the_wrong_job(self) -> None:
        """Cell renamed to point at a job that never invokes the script."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conf_path, wf_path = write_pair(
                Path(tmp),
                CONFORMANCE_TEMPLATE.format(
                    # claims the foundation-tests job, which never calls this script
                    gate="`foundation-checks.yml` (`foundation-tests` job)"
                ),
                WORKFLOW_WITH_STEP,
            )
            rows = parse_merge_gate_table(conf_path.read_text(encoding="utf-8"))
            job_blocks = parse_job_blocks(wf_path.read_text(encoding="utf-8"))
            row = rows[0]
            self.assertEqual(row.job_id, "foundation-tests")
            invoked = job_invokes_script_via_python3(
                job_blocks[row.job_id], row.script_name
            )
            self.assertFalse(
                invoked,
                "expected False when the cell claims a job that does not "
                "invoke the script — it reported True",
            )

    def test_fails_when_gate_names_a_job_that_does_not_exist(self) -> None:
        """A typo'd or renamed job id in the Merge gate cell must not read
        as wired just because SOME job exists."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conf_path, wf_path = write_pair(
                Path(tmp),
                CONFORMANCE_TEMPLATE.format(
                    gate="`foundation-checks.yml` (`checkerz` job)"
                ),
                WORKFLOW_WITH_STEP,
            )
            rows = parse_merge_gate_table(conf_path.read_text(encoding="utf-8"))
            job_blocks = parse_job_blocks(wf_path.read_text(encoding="utf-8"))
            row = rows[0]
            self.assertNotIn(row.job_id, job_blocks)


if __name__ == "__main__":
    unittest.main()

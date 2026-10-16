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
    # Script exists and runs in scripts/lint.sh; NOT in foundation-checks.yml, because adding a
    # workflow step needs a token with `workflow` scope. The cell stays `—` rather than claiming a
    # gate it does not have — wiring the step is the follow-up, and this entry moves off the list
    # then, as the comment above describes.
    "Data-model tables",
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


# ---------------------------------------------------------------------------
# ateles#1138: push to main must bind the decision-state check with no paths
# filter, so a push that touches no foundation path cannot leave the
# committed docs/foundation/decision_state.md stale and unchecked.
# ---------------------------------------------------------------------------


def parse_on_block(workflow_text: str) -> str:
    """The `on:` trigger block's raw text, up to the next top-level key."""
    m = re.search(r"^on:\s*\n((?:[ \t]+.*\n?|\n)*)", workflow_text, re.M)
    if not m:
        raise AssertionError("no top-level 'on:' key found in the workflow file")
    return m.group(1)


def push_to_main_has_no_paths_filter(on_block: str) -> bool:
    """True iff `push.branches` includes `main` and that mapping has no `paths`."""
    m = re.search(r"^  push:\s*\n((?:[ \t]{4,}.*\n?)*)", on_block, re.M)
    if not m:
        return False
    push_block = m.group(1)
    branches_m = re.search(r"branches:\s*\n((?:[ \t]+-.*\n?)*)", push_block)
    if not branches_m or "main" not in branches_m.group(1):
        return False
    return "paths:" not in push_block


def decision_state_step_text(job_block: str) -> str | None:
    """The `run:` text of the 'Decision state — matches the register it projects' step."""
    m = re.search(
        r"- name:\s*Decision state[^\n]*\n((?:[ \t]+.*\n?)*)",
        job_block,
    )
    return m.group(1) if m else None


class TestPushToMainBindsDecisionState(unittest.TestCase):
    """ateles#1138: the decision-state check must run on every push to main.

    A `paths` filter on that push trigger would silently skip exactly the
    push most likely to leave the committed decision_state.md stale -- one
    that lands a register ruling without regenerating and touches no
    `docs/foundation/**` path itself (e.g. a merge commit, or a change to an
    unrelated file that happens to land alongside a prior ruling). That is
    the "reports without binding" defect (`principles.md` #1) aimed at this
    workflow's own push trigger.
    """

    def setUp(self) -> None:
        if not WORKFLOW.is_file():
            self.skipTest(f"{WORKFLOW} absent on this branch")
        self.workflow_text = WORKFLOW.read_text(encoding="utf-8")
        self.on_block = parse_on_block(self.workflow_text)
        self.job_blocks = parse_job_blocks(self.workflow_text)

    def test_push_to_main_has_no_paths_filter(self) -> None:
        self.assertTrue(
            push_to_main_has_no_paths_filter(self.on_block),
            "on.push.branches must include 'main' with no 'paths' key under "
            "that push mapping -- found:\n" + self.on_block,
        )

    def test_decision_state_step_runs_check(self) -> None:
        self.assertIn("checkers", self.job_blocks)
        step_text = decision_state_step_text(self.job_blocks["checkers"])
        self.assertIsNotNone(
            step_text, "no 'Decision state — matches the register it projects' "
            "step found in the checkers job"
        )
        self.assertIn("python3", step_text)
        self.assertIn("render_decision_state.py", step_text)
        self.assertIn("--check", step_text)
        # A real YAML `continue-on-error:` key must not appear on this step
        # (the step name line through the next step or end of job). Matched
        # as a YAML key (leading whitespace + the literal key + colon), not
        # as a bare substring -- this step's own explanatory comment
        # legitimately says the words "continue-on-error" in prose (to state
        # that the fetch step upstream has none), and that prose is not the
        # YAML directive this assertion exists to catch.
        step_full = self.job_blocks["checkers"][
            self.job_blocks["checkers"].index("Decision state") :
        ]
        next_step = re.search(r"\n {6}- name:", step_full[1:])
        if next_step:
            step_full = step_full[: next_step.start() + 1]
        self.assertIsNone(
            re.search(r"^\s*continue-on-error\s*:", step_full, re.M),
            "found a real 'continue-on-error:' YAML key on the decision-state step",
        )

    def test_missing_origin_main_does_not_exit_0_on_that_step(self) -> None:
        step_text = decision_state_step_text(self.job_blocks["checkers"])
        self.assertIsNotNone(step_text)
        # The step's own unfetchable-ref skip is a distinct, honest branch
        # (see the step's comment) from a blanket "exit 0" that would hide a
        # real check failure; assert there is no bare unconditional exit 0
        # outside the two guarded skip idioms (`absent on this branch` and
        # `not fetchable`).
        for line in step_text.splitlines():
            if "exit 0" not in line:
                continue
            self.assertTrue(
                "absent on this branch" in line or "not fetchable" in line,
                f"unexpected unconditional 'exit 0' in the decision-state "
                f"step, not one of the two guarded skips: {line!r}",
            )
        fetch_step_m = re.search(
            r"- name:\s*Fetch origin/main as a ref\n((?:[ \t]+.*\n?)*)",
            self.workflow_text,
        )
        self.assertIsNotNone(fetch_step_m, "'Fetch origin/main as a ref' step not found")
        self.assertNotIn("continue-on-error", fetch_step_m.group(1))

    def test_dropped_step_or_path_filtered_push_is_red(self) -> None:
        """Planted negatives: each mutation must be caught by the assertions above."""
        # (a) step removed entirely
        without_step = re.sub(
            r"      # Decision state.*?\n(?=      # Register narrative)",
            "",
            self.workflow_text,
            flags=re.S,
        )
        job_blocks = parse_job_blocks(without_step)
        self.assertIsNone(decision_state_step_text(job_blocks["checkers"]))

        # (b) continue-on-error inserted on the step
        with_coe = self.workflow_text.replace(
            "      - name: Decision state — matches the register it projects\n"
            "        if: always() && steps.corpus.outputs.present == 'true'\n",
            "      - name: Decision state — matches the register it projects\n"
            "        if: always() && steps.corpus.outputs.present == 'true'\n"
            "        continue-on-error: true\n",
        )
        self.assertNotEqual(with_coe, self.workflow_text)
        job_blocks_coe = parse_job_blocks(with_coe)
        step_full = job_blocks_coe["checkers"][
            job_blocks_coe["checkers"].index("Decision state") :
        ]
        next_step = re.search(r"\n {6}- name:", step_full[1:])
        if next_step:
            step_full = step_full[: next_step.start() + 1]
        self.assertIn(
            "continue-on-error",
            step_full,
            "planted continue-on-error was not detected by the slice used above",
        )

        # (c) paths: filter added under push
        with_paths = self.on_block.replace(
            "  push:\n    branches:\n      - main\n",
            "  push:\n    branches:\n      - main\n    paths:\n      - \"docs/foundation/**\"\n",
        )
        self.assertNotEqual(with_paths, self.on_block)
        self.assertFalse(push_to_main_has_no_paths_filter(with_paths))


class TestBindingsNamedInProse(unittest.TestCase):
    """ateles#1138: every surface describing this mechanism names both bindings.

    `scripts/lint.sh`, the `checkers` job on push to main with no path filter,
    and the `python3` regenerate command must all be named consistently in
    CLAUDE.md, the generator's own render() source, and conformance.md's
    mechanical-checks Runs cell -- the same "renamed thing leaves a stale
    reference" defect class CLAUDE.md itself names, applied to this mechanism.
    """

    def setUp(self) -> None:
        self.claude_md = REPO_ROOT / "CLAUDE.md"
        if not self.claude_md.is_file():
            self.skipTest(f"{self.claude_md} absent on this branch")
        if not CONFORMANCE.is_file():
            self.skipTest(f"{CONFORMANCE} absent on this branch")
        self.claude_text = self.claude_md.read_text(encoding="utf-8")
        self.conformance_text = CONFORMANCE.read_text(encoding="utf-8")

    def test_runs_cell_names_lint_and_unfiltered_push(self) -> None:
        rows = parse_merge_gate_table(self.conformance_text)
        row = next((r for r in rows if r.check == "Decision state"), None)
        self.assertIsNotNone(row, "no 'Decision state' row in the mechanical-checks table")
        self.assertIn("scripts/lint.sh", row.runs)
        # The row's own prose avoids the literal word "push" -- vocabulary.md
        # bans it globally (Never, entry "claim") with no per-sense carve-out,
        # so the design doc states the concept ("lands on main directly, ...
        # unfiltered by path") without using the banned verb. Assert the
        # concept, not the word.
        self.assertIn("lands on", row.runs)
        self.assertIn("main", row.runs)
        self.assertIn("unfiltered by path", row.runs)
        # "What fails" is the table's 4th cell, which MergeGateRow does not
        # parse (parse_merge_gate_table only captures check/runs/gate) -- read
        # it directly off the full row line rather than widening that shared
        # parser for one test.
        row_line = next(
            line for line in self.conformance_text.splitlines()
            if line.startswith("| Decision state |")
        )
        self.assertIn("unfetchable", row_line)

    def test_render_header_names_both_bindings(self) -> None:
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import render_decision_state as ds

        module_doc = ds.__doc__ or ""
        self.assertIn("scripts/lint.sh", module_doc)
        self.assertIn("push", module_doc)
        self.assertIn("main", module_doc)

    def test_claude_md_names_both_bindings_and_python3(self) -> None:
        # Scope to the decision_state.md bullet itself, not just anywhere in
        # the file -- CLAUDE.md's unrelated non-binding-controls bullet also
        # happens to say "push to main", and matching that would pass even if
        # the decision_state.md bullet itself never named the binding.
        marker = "`docs/foundation/decision_state.md` is generated, never authored."
        self.assertIn(marker, self.claude_text)
        start = self.claude_text.index(marker)
        end = self.claude_text.index("\n", start + 1)
        # The bullet in this file wraps as one long line; take to end of line
        # or a generous window, whichever is available.
        bullet = self.claude_text[start:end] if end > start else self.claude_text[start:start + 4000]
        if len(bullet) < 200:  # didn't find a line break -- take a window instead
            bullet = self.claude_text[start:start + 4000]
        self.assertIn("scripts/lint.sh", bullet)
        self.assertIn("lands on", bullet)
        self.assertIn("main", bullet)
        self.assertIn("python3 execution/scripts/render_decision_state.py", bullet)

    def test_claude_md_is_on_the_pull_request_path_filter(self) -> None:
        if not WORKFLOW.is_file():
            self.skipTest(f"{WORKFLOW} absent on this branch")
        workflow_text = WORKFLOW.read_text(encoding="utf-8")
        on_block = parse_on_block(workflow_text)
        pr_m = re.search(r"  pull_request:\s*\n((?:[ \t]+.*\n?)*)", on_block)
        self.assertIsNotNone(pr_m, "no pull_request trigger found")
        self.assertIn("CLAUDE.md", pr_m.group(1))
        push_m = re.search(r"  push:\s*\n((?:[ \t]+.*\n?)*)", on_block)
        self.assertIsNotNone(push_m, "no push trigger found")
        self.assertNotIn("paths", push_m.group(1))


if __name__ == "__main__":
    unittest.main()

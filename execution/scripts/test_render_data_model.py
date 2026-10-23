#!/usr/bin/env python3
"""Fail-then-pass coverage for render_data_model.py.

`principles.md` #4: a test that cannot fail on the thing it watches is decoration. The defect this
checker exists to catch is a governance type the foundation names and `data_model.md#concepts` does
not declare — the state `origin/main` was in for `agent_policy` and `swarm_roster` — so the first
test below reconstructs exactly that state and asserts the checker reports BOTH types and exits 1.

The other tests pin the two behaviours that decide whether this is a control or a report:

* **Exit 2 is not exit 0.** Every path where an input is missing or self-inconsistent returns "did
  not run", distinct from both "clean" and "found a defect". A checker that exited 0 when its source
  enumeration moved would report a pass for a check that never ran, which is the defect the corpus
  names as a mechanism that does not bind.
* **The expected set is read, not hardcoded.** A governance type added to WM-22's enumeration is
  enforced with no edit to the checker — asserted with a synthetic type name that appears nowhere in
  the real corpus, so the test cannot pass by accident of the real set.

Stdlib only. Skips gracefully where the corpus is absent, per the pattern in
`test_foundation_mechanical_checks_ci.py`.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "execution" / "scripts" / "render_data_model.py"
FOUNDATION = REPO_ROOT / "docs" / "foundation"
DATA_MODEL = FOUNDATION / "data_model.md"
SUITE = FOUNDATION / "conformance_suite.md"


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--check", "--root", str(root)],
        capture_output=True,
        text=True,
    )


class RenderDataModelCheck(unittest.TestCase):
    def setUp(self) -> None:
        if not (SCRIPT.is_file() and DATA_MODEL.is_file() and SUITE.is_file()):
            self.skipTest("checker or corpus absent on this branch")
        self.data_model = DATA_MODEL.read_text(encoding="utf-8")
        self.suite = SUITE.read_text(encoding="utf-8")

    def _tree(self, data_model: str, suite: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "docs" / "foundation").mkdir(parents=True)
        (tmp / "docs" / "foundation" / "data_model.md").write_text(data_model, encoding="utf-8")
        (tmp / "docs" / "foundation" / "conformance_suite.md").write_text(suite, encoding="utf-8")
        return tmp

    def test_real_tree_is_clean(self) -> None:
        """Every governance type the suite enumerates has a concepts row today."""
        proc = _run(REPO_ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_red_when_a_governance_type_has_no_row(self) -> None:
        """The state origin/main was in: the rows deleted, the check must name both types.

        This is what the check looked like red. Removing the two rows from the concepts table
        reproduces the pre-existing corpus exactly as far as this checker can see it.
        """
        stripped = "\n".join(
            line
            for line in self.data_model.splitlines()
            if not line.startswith("| agent behavioural rule | `agent_policy` |")
            and not line.startswith("| roster | `swarm_roster` |")
        )
        proc = _run(self._tree(stripped, self.suite))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("agent_policy", proc.stderr)
        self.assertIn("swarm_roster", proc.stderr)

    def test_prose_mention_is_not_a_declaration(self) -> None:
        """A type named only in prose is still undeclared.

        `agent_policy` was cited across the corpus the whole time it had no row, so a checker that
        searched the document rather than the table's `Entity type` column would have reported the
        real gap as covered.
        """
        stripped = "\n".join(
            line
            for line in self.data_model.splitlines()
            if not line.startswith("| agent behavioural rule | `agent_policy` |")
        )
        self.assertIn("`agent_policy`", stripped)  # still mentioned in prose
        proc = _run(self._tree(stripped, self.suite))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("agent_policy", proc.stderr)

    def test_new_type_in_the_enumeration_is_enforced_without_editing_the_checker(self) -> None:
        """The expected set comes from the corpus, so a type added to WM-22 binds immediately."""
        suite = self.suite.replace(
            "`swarm_roster`, the registry, `intake_rule` —",
            "`swarm_roster`, the registry, `intake_rule`, `synthetic_absent_type` —",
        ).replace("the eight governance types", "the nine governance types")
        self.assertNotIn("synthetic_absent_type", self.data_model)
        proc = _run(self._tree(self.data_model, suite))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("synthetic_absent_type", proc.stderr)

    def test_exit_2_when_the_enumeration_is_gone(self) -> None:
        suite = "\n".join(
            line for line in self.suite.splitlines() if not line.startswith("| WM-22 |")
        )
        proc = _run(self._tree(self.data_model, suite))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("DID NOT RUN", proc.stderr)

    def test_exit_2_when_the_count_and_the_enumeration_disagree(self) -> None:
        """A shrunk enumeration must not silently shrink what is enforced."""
        suite = self.suite.replace("the eight governance types", "the twelve governance types")
        proc = _run(self._tree(self.data_model, suite))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("DID NOT RUN", proc.stderr)

    def test_exit_2_when_the_concepts_markers_are_gone(self) -> None:
        proc = _run(
            self._tree(
                self.data_model.replace("<!-- rendered: data_model concepts -->", ""), self.suite
            )
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the operator rulings embedded in the rule inventory."""

import unittest

import render_rule_inventory as inventory


class RuleInventoryRulingsTest(unittest.TestCase):
    def test_operator_rulings_are_complete_and_exact(self) -> None:
        self.assertEqual(
            set(inventory.PROPOSAL_RULINGS),
            {f"P{number}" for number in range(1, 11)},
        )
        self.assertEqual(inventory.PROPOSAL_RULINGS["P3"].status, "DUPLICATE")
        self.assertEqual(inventory.PROPOSAL_RULINGS["P10"].status, "QUARANTINED")
        self.assertEqual(
            inventory.PROPOSAL_RULINGS["P5"].rule,
            "Request operator review only when technical gates are clear and "
            "operator approval is the sole remaining gate",
        )

    def test_generalization_dispositions_preserve_specific_rules(self) -> None:
        self.assertEqual(
            {key: ruling.status for key, ruling in inventory.GENERALIZATION_RULINGS.items()},
            {"G1": "ACCEPTED", "G2": "DUPLICATE", "G3": "DUPLICATE"},
        )
        self.assertIn("--body-file", inventory.GENERALIZATION_RULINGS["G1"].note)

    def test_rendered_section_distinguishes_ruling_from_enforcement(self) -> None:
        rendered = inventory.render_rulings_section()
        self.assertIn("Operator rulings (2026-09-21)", rendered)
        self.assertIn("does not make an accepted rule binding", rendered)
        self.assertIn("P10", rendered)
        self.assertIn("exact stylistic tell and scope", rendered)

    def test_check_ignores_only_the_measurement_date(self) -> None:
        first = "**Measured:** 2026-09-19.\ncount: 10\n"
        next_day = "**Measured:** 2026-09-21.\ncount: 10\n"
        changed = "**Measured:** 2026-09-21.\ncount: 11\n"
        self.assertEqual(
            inventory.strip_volatile_measurement_date(first),
            inventory.strip_volatile_measurement_date(next_day),
        )
        self.assertNotEqual(
            inventory.strip_volatile_measurement_date(first),
            inventory.strip_volatile_measurement_date(changed),
        )

    def test_portable_path_hides_home_encoded_project_directory(self) -> None:
        path = "~/.claude/projects/-Users-example-repos-ateles/memory/rule.md"
        self.assertEqual(
            inventory._portable(path),
            "~/.claude/projects/<project>/memory/rule.md",
        )


if __name__ == "__main__":
    unittest.main()

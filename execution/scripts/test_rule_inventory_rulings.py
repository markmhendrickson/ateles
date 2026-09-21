"""Regression tests for the operator rulings embedded in the rule inventory."""

import json
import tempfile
import unittest
from pathlib import Path

import render_rule_inventory as inventory


class RuleInventoryRulingsTest(unittest.TestCase):
    def test_operator_rulings_are_complete_and_exact(self) -> None:
        expected = {
            "P1": (
                "ACCEPTED",
                "`docs/foundation/github.md`",
                "Code-host review semantics belong in the code-host mapping.",
            ),
            "P2": (
                "ACCEPTED",
                "`agent_policy`",
                "A generic rule for authors and reviewers of guards.",
            ),
            "P3": (
                "DUPLICATE",
                "`agent_policy` (`R-ae9bca`)",
                "Already captured by the dispatch rule; create no second rule.",
            ),
            "P4": (
                "ACCEPTED",
                "`agent_policy`",
                "PR shepherding behaviour; workflow declarations still own their "
                "step lists.",
            ),
            "P5": (
                "ACCEPTED",
                "`agent_policy`",
                "Narrowed by the operator; an earlier request would misstate "
                "readiness.",
            ),
            "P6": (
                "ACCEPTED",
                "`docs/foundation/gmail.md`",
                "This is the mail adapter's per-thread operation, not a general "
                "preference.",
            ),
            "P7": (
                "ACCEPTED",
                "`docs/foundation/adapters.md`",
                "A watcher resumption invariant shared across import adapters.",
            ),
            "P8": (
                "ACCEPTED",
                "`docs/foundation/adapters.md`",
                "A source-dedup invariant shared across import adapters.",
            ),
            "P9": (
                "ACCEPTED",
                "`task_policy`",
                "The recap presentation is an operator preference, not public "
                "prompt text.",
            ),
            "P10": (
                "QUARANTINED",
                "none",
                "No rule is created until the exact stylistic tell and scope are "
                "supplied.",
            ),
        }
        actual = {
            key: (ruling.status, ruling.home, ruling.note)
            for key, ruling in inventory.PROPOSAL_RULINGS.items()
        }
        self.assertEqual(actual, expected)
        self.assertEqual(
            inventory.PROPOSAL_RULINGS["P5"].rule,
            "Request operator review only when technical gates are clear and "
            "operator approval is the sole remaining gate",
        )

    def test_generalization_dispositions_preserve_specific_rules(self) -> None:
        expected = {
            "G1": (
                "ACCEPTED",
                "`agent_policy`",
                "Keep the concrete `--body-file` rule beside the general "
                "shell-injection rule.",
            ),
            "G2": (
                "DUPLICATE",
                "`agent_policy` (`R-680852`)",
                "Fold into the existing read-back rule; preserve the concrete "
                "daemon sequence.",
            ),
            "G3": (
                "DUPLICATE",
                "foundation consent rule (`R-fba8d`)",
                "Already captured; preserve the concrete Gmail gate and its tests.",
            ),
        }
        actual = {
            key: (ruling.status, ruling.home, ruling.note)
            for key, ruling in inventory.GENERALIZATION_RULINGS.items()
        }
        self.assertEqual(actual, expected)
        self.assertIn("--body-file", inventory.GENERALIZATION_RULINGS["G1"].note)

    def test_every_target_home_names_a_live_rule_kind(self) -> None:
        self.assertEqual(set(inventory.TARGET_HOME), set(inventory.KIND_LABELS))

    def test_questions_tool_rule_is_separate_from_status_updates(self) -> None:
        self.assertEqual(
            inventory.classify(
                "Give status updates unprompted: what moved and what is blocked."
            ),
            ["status_update_unprompted"],
        )
        self.assertEqual(
            inventory.classify(
                "Pose open decisions through the harness questions tool with "
                "labeled options."
            ),
            ["decisions_via_questions_tool"],
        )

    def test_needs_split_is_bound_at_eight_statement_boundary(self) -> None:
        def statements(openings: list[str]) -> list[inventory.Statement]:
            return [
                inventory.Statement("Codex", "private", f"L{index}", opening)
                for index, opening in enumerate(openings, 1)
            ]

        seven_distinct = statements(
            [f"Always report status variant-{index} before continuing" for index in range(7)]
        )
        eight_distinct = statements(
            [f"Always report status variant-{index} before continuing" for index in range(8)]
        )
        eight_same = statements(["Always report the same status opening now"] * 8)

        self.assertFalse(
            inventory.Cluster("status_update_unprompted", "ignored", seven_distinct)
            .needs_split
        )
        self.assertTrue(
            inventory.Cluster("status_update_unprompted", "ignored", eight_distinct)
            .needs_split
        )
        self.assertFalse(
            inventory.Cluster("status_update_unprompted", "ignored", eight_same)
            .needs_split
        )

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

    def test_statement_value_is_never_emitted(self) -> None:
        statement = inventory.Statement(
            store="private",
            location="private",
            locator="private",
            text="Always deploy Client Codename to client-app-production",
        )
        self.assertEqual(statement.safe_text, inventory.WITHHELD)

    def test_public_location_uses_store_kind_not_private_path_components(self) -> None:
        cases = {
            "Claude Code project memory": "~/.claude/projects/<project>/memory/<file>",
            "Skills (user root)": "~/.claude/skills/<skill>/SKILL.md",
            "agent_policy entities": "<entity>",
            "foundation reference repo": "~/repos/<reference>/<file>",
        }
        source = "/Users/Operator/Client-Codename/person-daemon/private.md"
        for store, expected in cases.items():
            with self.subTest(store=store):
                self.assertEqual(
                    inventory.public_statement_location(store, source), expected
                )

    def test_render_redacts_every_dynamic_metadata_surface(self) -> None:
        sensitive = {
            "OperatorIdentity",
            "ClientCodename",
            "client-app-production",
            "person-daemon",
            "/Users/private",
            "private.example.internal",
        }
        statement = inventory.Statement(
            store="OperatorIdentity/private store",
            location="/Users/private/ClientCodename/person-daemon.md",
            locator="client-app-production",
            text="Always use private.example.internal for ClientCodename",
            kind="never_stash",
            last_modified="OperatorIdentity",
        )
        cluster = inventory.Cluster(
            kind="never_stash",
            label="ClientCodename person-daemon",
            statements=[statement],
        )
        store = inventory.Store(
            name="OperatorIdentity/private store",
            location="/Users/private/ClientCodename",
            populated=1,
            statements=1,
            last_modified="ClientCodename",
            reachable="client-app-production",
            reach_note="person-daemon at private.example.internal",
            note="ClientCodename",
            read_ok=False,
            read_error="/Users/private failed for OperatorIdentity",
        )
        rendered = inventory.render([cluster], [store], [], [statement])
        for value in sensitive:
            with self.subTest(value=value):
                self.assertNotIn(value, rendered)

    def test_public_render_is_honest_about_withheld_statement_bodies(self) -> None:
        statement = inventory.Statement(
            store="Codex",
            location="/private/rules.md",
            locator="L9",
            text="Never reveal this private value",
            kind="git_never_stash",
        )
        advisory = inventory.Statement(
            store=statement.store,
            location=statement.location,
            locator="L2",
            text="Prefer not to reveal this mechanism",
            kind="git_never_stash",
        )
        cluster = inventory.Cluster(
            kind="git_never_stash",
            label="ignored",
            statements=[statement, advisory],
        )
        rendered = inventory.render([cluster], [], [], [statement])
        self.assertNotIn("| Statement |", rendered)
        self.assertIn("statement bodies are deliberately absent", rendered)
        self.assertIn("--private-diagnostics", rendered)

    def test_private_diagnostics_expose_locators_but_never_statement_values(self) -> None:
        statement = inventory.Statement(
            store="OperatorIdentity/private store",
            location="/Users/private/ClientCodename/rules.md",
            locator="client-app-production",
            text="Never reveal private.example.internal",
            kind="git_never_stash",
        )
        advisory = inventory.Statement(
            store=statement.store,
            location=statement.location,
            locator="L2",
            text="Prefer not to reveal this mechanism",
            kind="git_never_stash",
        )
        cluster = inventory.Cluster(
            kind="git_never_stash",
            label="ignored",
            statements=[statement, advisory],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "rule-locators.json"
            inventory.write_private_diagnostics(target, [cluster])
            payload = json.loads(target.read_text())
            serialized = json.dumps(payload)
            self.assertIn(statement.location, serialized)
            self.assertIn(statement.locator, serialized)
            self.assertNotIn(statement.text, serialized)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

        with self.assertRaises(ValueError):
            inventory.write_private_diagnostics(
                inventory.REPO_ROOT / "private-rule-locators.json", [cluster]
            )

    def test_current_measurements_are_derived_in_narrative(self) -> None:
        statements = [
            inventory.Statement(
                "Codex", "private", "L1", "Never use this mechanism"
            ),
            inventory.Statement(
                "Codex", "private", "L2", "Prefer not to use this mechanism"
            ),
        ]
        cluster = inventory.Cluster("git_never_stash", "ignored", statements)
        stores = [
            inventory.Store(
                "ateles/CLAUDE.md checkout copies",
                "private",
                populated=11,
                distinct_versions=2,
            ),
            inventory.Store(
                "neotoma/AGENTS.md checkout copies",
                "private",
                populated=13,
                distinct_versions=3,
            ),
        ]
        rendered = inventory.render([cluster], stores, [], statements)
        self.assertIn("1 rows rather than 2", rendered)
        self.assertIn("11 copies of `ateles/CLAUDE.md` in 2 distinct versions", rendered)
        self.assertIn("13 copies of `neotoma/AGENTS.md` in 3 distinct versions", rendered)

    def test_merge_gating_workflow_runs_full_inventory_check_fail_closed(self) -> None:
        workflow = (
            inventory.REPO_ROOT / ".github/workflows/foundation-checks.yml"
        ).read_text()
        self.assertIn('NEOTOMA_BEARER_TOKEN: ${{ secrets.NEOTOMA_BEARER_TOKEN }}', workflow)
        self.assertIn("Rule inventory — full measured output matches", workflow)
        self.assertIn("python3 \"$f\" --check", workflow)
        self.assertIn("--require-complete-measurement", workflow)
        self.assertIn("rule inventory equality unavailable: NEOTOMA_BEARER_TOKEN", workflow)
        self.assertIn("readiness check will name the unread entity store kinds", workflow)
        self.assertIn("canonical measurement runner must expose every required store kind", workflow)
        self.assertNotIn("continue-on-error: true\n        run: python3 execution/scripts/render_rule_inventory.py --check", workflow)

    def test_measurement_readiness_names_only_safe_store_kinds(self) -> None:
        stores = [inventory.Store(name, "private") for name in inventory.PUBLIC_STORE_NAMES]
        target = next(store for store in stores if store.name == "agent_policy entities")
        target.read_ok = False
        target.read_error = "/Users/private ClientCodename token=secret"
        missing, unread = inventory.measurement_readiness(stores)
        self.assertEqual(missing, [])
        self.assertEqual(unread, ["agent_policy entities"])
        self.assertNotIn("ClientCodename", json.dumps([missing, unread]))

    def test_public_store_label_drops_repository_owner_namespace(self) -> None:
        self.assertEqual(
            inventory.public_store_name("OperatorIdentity/foundation repo"),
            "foundation reference repo",
        )

    def test_json_redacts_every_dynamic_metadata_surface(self) -> None:
        sensitive = {
            "OperatorIdentity",
            "ClientCodename",
            "client-app-production",
            "person-daemon",
            "/Users/private",
            "private.example.internal",
        }
        statement = inventory.Statement(
            store="OperatorIdentity/private store",
            location="/Users/private/ClientCodename/person-daemon.md",
            locator="client-app-production",
            text="Always use private.example.internal for ClientCodename",
            kind="never_stash",
            last_modified="OperatorIdentity",
        )
        cluster = inventory.Cluster(
            kind="never_stash",
            label="ClientCodename person-daemon",
            statements=[statement],
        )
        store = inventory.Store(
            name="OperatorIdentity/private store",
            location="/Users/private/ClientCodename",
            populated=1,
            statements=1,
            last_modified="ClientCodename",
            reachable="client-app-production",
            reach_note="person-daemon at private.example.internal",
            note="ClientCodename",
            read_ok=False,
            read_error="/Users/private failed for OperatorIdentity",
        )
        rendered = json.dumps(
            inventory.public_payload([cluster], [store], [], [statement])
        )
        for value in sensitive:
            with self.subTest(value=value):
                self.assertNotIn(value, rendered)


if __name__ == "__main__":
    unittest.main()

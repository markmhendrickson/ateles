"""Regression tests for the operator rulings embedded in the rule inventory."""

import json
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

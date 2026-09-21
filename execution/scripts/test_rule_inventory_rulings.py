"""Regression tests for the operator rulings embedded in the rule inventory."""

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import render_rule_inventory as inventory

EXPECTED_PROPOSAL_RULINGS = {
    "P1": (
        "A retraction posted as a COMMENT does not clear an APPROVED review; "
        "the approval stands until it is formally dismissed",
        "ACCEPTED",
        "`docs/foundation/github.md`",
        "Code-host review semantics belong in the code-host mapping.",
    ),
    "P2": (
        "A prose-matching guard fires on text that names its own rule, so a "
        "document describing a rule trips the gate that enforces it",
        "ACCEPTED",
        "`agent_policy`",
        "A generic rule for authors and reviewers of guards.",
    ),
    "P3": (
        "Durable work goes to a dispatched agent, never a harness task chip — "
        "a chip is not an entity, so it is unclaimable and invisible to the swarm",
        "DUPLICATE",
        "`agent_policy` (`R-ae9bca`)",
        "Already captured by the dispatch rule; create no second rule.",
    ),
    "P4": (
        "Monitoring does not end at merge: carry a change through release and "
        "deployment until it is confirmed live on every instance that needs it",
        "ACCEPTED",
        "`agent_policy`",
        "PR shepherding behaviour; workflow declarations still own their step lists.",
    ),
    "P5": (
        "Request operator review only when technical gates are clear and "
        "operator approval is the sole remaining gate",
        "ACCEPTED",
        "`agent_policy`",
        "Narrowed by the operator; an earlier request would misstate readiness.",
    ),
    "P6": (
        "Stage a reply at the END of its thread, having first checked the "
        "external system for the thread's latest message",
        "ACCEPTED",
        "`docs/foundation/gmail.md`",
        "This is the mail adapter's per-thread operation, not a general preference.",
    ),
    "P7": (
        "On resuming an interrupted watcher, import everything that arrived "
        "during the gap — not only what arrives afterward",
        "ACCEPTED",
        "`docs/foundation/adapters.md`",
        "A watcher resumption invariant shared across import adapters.",
    ),
    "P8": (
        "Check durable storage for an already-imported source before importing it again",
        "ACCEPTED",
        "`docs/foundation/adapters.md`",
        "A source-dedup invariant shared across import adapters.",
    ),
    "P9": (
        "Produce an internal recap for the operator covering the work done, "
        "distinct from any outward-facing recap",
        "ACCEPTED",
        "`task_policy`",
        "The recap presentation is an operator preference, not public prompt text.",
    ),
    "P10": (
        "Avoid a named stylistic tell in generated prose because it reads as machine-written",
        "QUARANTINED",
        "none",
        "No rule is created until the exact stylistic tell and scope are supplied.",
    ),
}

EXPECTED_GENERALIZATION_RULINGS = {
    "G1": (
        "Never interpolate untrusted or code-bearing text into a shell command; "
        "write it to a file and pass the path",
        "ACCEPTED",
        "`agent_policy`",
        "Keep the concrete `--body-file` rule beside the general shell-injection rule.",
    ),
    "G2": (
        "Any deployment step is unverified until read back from the thing that now runs",
        "DUPLICATE",
        "`agent_policy` (`R-680852`)",
        "Fold into the existing read-back rule; preserve the concrete daemon sequence.",
    ),
    "G3": (
        "Any outward, irreversible action needs per-action approval, and approval "
        "never carries forward",
        "DUPLICATE",
        "foundation consent rule (`R-fba8d`)",
        "Already captured; preserve the concrete Gmail gate and its tests.",
    ),
}


class RuleInventoryRulingsTest(unittest.TestCase):
    def assert_public_store_metadata_withheld(
        self, store: inventory.Store, *private_values: str
    ) -> None:
        payload = inventory.public_payload([], [store], [], [])
        self.assertNotIn("read_error", payload["stores"][0])
        serialized = json.dumps(payload)
        for value in (*private_values, store.read_error):
            if value:
                with self.subTest(private_value=value):
                    self.assertNotIn(value, serialized)

    def assert_read_error_leak_mutant_rejected(
        self, store: inventory.Store, *private_values: str
    ) -> None:
        payload = inventory.public_payload([], [store], [], [])
        payload["stores"][0]["read_error"] = store.read_error
        serialized = json.dumps(payload)
        with self.assertRaises(AssertionError):
            for value in (*private_values, store.read_error):
                if value:
                    self.assertNotIn(value, serialized)

    def assert_rulings_exact(
        self,
        actual: dict[str, inventory.RuleRuling],
        expected: dict[str, tuple[str, str, str, str]],
    ) -> None:
        self.assertEqual(
            {
                key: (ruling.rule, ruling.status, ruling.home, ruling.note)
                for key, ruling in actual.items()
            },
            expected,
        )

    def test_operator_rulings_are_complete_and_exact(self) -> None:
        self.assert_rulings_exact(inventory.PROPOSAL_RULINGS, EXPECTED_PROPOSAL_RULINGS)

    def test_generalization_dispositions_preserve_specific_rules(self) -> None:
        self.assert_rulings_exact(
            inventory.GENERALIZATION_RULINGS, EXPECTED_GENERALIZATION_RULINGS
        )
        self.assertIn("--body-file", inventory.GENERALIZATION_RULINGS["G1"].note)

    def test_exact_ruling_guard_rejects_each_governed_field_mutation(self) -> None:
        for field in ("rule", "status", "home", "note"):
            with self.subTest(ruling="P8", field=field):
                mutated = dict(inventory.PROPOSAL_RULINGS)
                mutated["P8"] = replace(
                    mutated["P8"], **{field: getattr(mutated["P8"], field) + " MUTANT"}
                )
                with self.assertRaises(AssertionError):
                    self.assert_rulings_exact(mutated, EXPECTED_PROPOSAL_RULINGS)

        mutated = dict(inventory.GENERALIZATION_RULINGS)
        mutated["G2"] = replace(mutated["G2"], rule=mutated["G2"].rule + " MUTANT")
        with self.assertRaises(AssertionError):
            self.assert_rulings_exact(mutated, EXPECTED_GENERALIZATION_RULINGS)

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
        self.assertIn(
            "RULE_INVENTORY_CANONICAL_REPOSITORY_ROOTS: "
            "${{ vars.RULE_INVENTORY_CANONICAL_REPOSITORY_ROOTS }}",
            workflow,
        )
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

    def test_canonical_repository_instruction_roots_are_measured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            first = base / "repo-one"
            second = base / "repo-two"
            for root in (first, second):
                subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (first / "CLAUDE.md").write_text(
                "- **Never `git stash`.** Keep durable work visible.\n"
            )
            (second / "AGENTS.md").write_text(
                "- **Always verify writes.** Read the changed field back.\n"
            )
            (second / "CLAUDE.md").symlink_to("AGENTS.md")
            (second / ".cursorrules").write_text(
                "Never bypass the repository's verification gate.\n"
            )

            statements, store = inventory.read_canonical_repository_instruction_roots(
                os.pathsep.join((str(first), str(second)))
            )

        self.assertTrue(store.read_ok)
        self.assertEqual(store.populated, 2)
        self.assertIn("3 root instruction file(s)", store.note)
        self.assertEqual(store.statements, len(statements))
        self.assertGreater(store.statements, 0)
        self.assertEqual(
            {statement.store for statement in statements},
            {"Canonical repository instruction roots"},
        )
        self.assertEqual(
            inventory.public_store_location(store.name, store.location),
            "~/repos/<canonical-roots>",
        )
        self.assertEqual(
            inventory.public_statement_location(
                store.name, statements[0].location
            ),
            "~/repos/<canonical-root>/<instruction-file>",
        )

    def test_missing_canonical_repository_root_fails_closed_without_leaking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            good = base / "repo-good"
            subprocess.run(["git", "init", "--quiet", str(good)], check=True)
            good.joinpath("CLAUDE.md").write_text(
                "Never expose ClientCodename value ent_private_123.\n"
            )
            missing = base / "ClientCodename-ent_private_123-missing"

            statements, store = inventory.read_canonical_repository_instruction_roots(
                os.pathsep.join((str(good), str(missing)))
            )

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        self.assertIn(str(missing), store.read_error)
        missing_kinds, unread_kinds = inventory.measurement_readiness([store])
        self.assertIn("Canonical repository instruction roots", unread_kinds)
        public = json.dumps(inventory.public_payload([], [store], [], []))
        for secret in (
            str(missing),
            "ClientCodename",
            "ent_private_123",
            store.read_error,
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, public)
        self.assertIn("Canonical repository instruction roots", public)

    def test_relative_canonical_repository_root_fails_closed(self) -> None:
        configured = "ClientCodename-ent_private_123-relative/repository"
        statements, store = inventory.read_canonical_repository_instruction_roots(
            configured
        )

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        self.assertIn("not absolute", store.read_error)
        private_values = (configured, "ClientCodename", "ent_private_123")
        self.assert_public_store_metadata_withheld(store, *private_values)
        self.assert_read_error_leak_mutant_rejected(store, *private_values)

    def test_symlinked_canonical_repository_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            primary = base / "primary"
            subprocess.run(["git", "init", "--quiet", str(primary)], check=True)
            configured = base / "ClientCodename-ent_private_123-root"
            configured.symlink_to(primary, target_is_directory=True)

            statements, store = inventory.read_canonical_repository_instruction_roots(
                str(configured)
            )

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        self.assertIn("unavailable", store.read_error)
        private_values = (str(configured), "ClientCodename", "ent_private_123")
        self.assert_public_store_metadata_withheld(store, *private_values)
        self.assert_read_error_leak_mutant_rejected(store, *private_values)

    def test_repository_instruction_symlink_escape_fails_closed_without_leaking(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            configured = base / "configured-root"
            subprocess.run(["git", "init", "--quiet", str(configured)], check=True)
            outside = base / "ClientCodename-ent_private_123.md"
            outside.write_text("Never expose this private instruction value.\n")
            candidate = configured / "AGENTS.md"
            candidate.symlink_to(outside)

            statements, store = inventory.read_canonical_repository_instruction_roots(
                str(configured)
            )

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        self.assertIn("leaves its canonical root", store.read_error)
        private_values = (
            str(configured),
            str(candidate),
            str(outside),
            "ClientCodename",
            "ent_private_123",
        )
        self.assert_public_store_metadata_withheld(store, *private_values)
        self.assert_read_error_leak_mutant_rejected(store, *private_values)

    def test_malformed_canonical_repository_root_fails_closed_without_leaking(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            configured = Path(tmpdir) / "ClientCodename-ent_private_123"
            subprocess.run(["git", "init", "--quiet", str(configured)], check=True)
            configured.joinpath("CLAUDE.md").write_text(
                "Never expose this private instruction value.\n"
            )
            malformed = str(configured) + "\x00"

            statements, store = (
                inventory.read_canonical_repository_instruction_roots(malformed)
            )

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        public = json.dumps(inventory.public_payload([], [store], [], []))
        self.assertNotIn("ClientCodename", public)
        self.assertNotIn("ent_private_123", public)

    def test_unreadable_repository_instruction_fails_closed_without_leaking(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            configured = Path(tmpdir) / "configured-root"
            subprocess.run(["git", "init", "--quiet", str(configured)], check=True)
            instruction = configured / "CLAUDE.md"
            instruction.write_text("Never expose ClientCodename private value.\n")

            with mock.patch.object(
                Path,
                "read_bytes",
                autospec=True,
                side_effect=PermissionError("ClientCodename unreadable"),
            ):
                statements, store = (
                    inventory.read_canonical_repository_instruction_roots(
                        str(configured)
                    )
                )

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        self.assertIn("PermissionError", store.read_error)
        public = json.dumps(inventory.public_payload([], [store], [], []))
        self.assertNotIn("ClientCodename", public)

    def test_external_symlinked_git_directory_is_not_a_primary_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            metadata_owner = base / "metadata-owner"
            subprocess.run(
                ["git", "init", "--quiet", str(metadata_owner)], check=True
            )
            configured = base / "configured-root"
            configured.mkdir()
            configured.joinpath(".git").symlink_to(
                metadata_owner / ".git", target_is_directory=True
            )
            configured.joinpath("AGENTS.md").write_text(
                "Never bypass verification for ClientCodename.\n"
            )

            statements, store = inventory.read_canonical_repository_instruction_roots(
                str(configured)
            )

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        self.assertIn("not a primary clone", store.read_error)

    def test_fabricated_git_directory_is_not_a_primary_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            configured = Path(tmpdir) / "configured-root"
            configured.joinpath(".git").mkdir(parents=True)
            configured.joinpath("CLAUDE.md").write_text(
                "Never bypass verification.\n"
            )

            statements, store = inventory.read_canonical_repository_instruction_roots(
                str(configured)
            )

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        self.assertIn("not a primary clone", store.read_error)

    def test_external_common_git_directory_is_not_a_primary_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            configured = base / "configured-root"
            metadata_owner = base / "metadata-owner"
            subprocess.run(["git", "init", "--quiet", str(configured)], check=True)
            subprocess.run(
                ["git", "init", "--quiet", str(metadata_owner)], check=True
            )
            configured.joinpath(".git", "commondir").write_text(
                str(metadata_owner / ".git") + "\n"
            )
            configured.joinpath("AGENTS.md").write_text(
                "Never bypass verification.\n"
            )

            statements, store = inventory.read_canonical_repository_instruction_roots(
                str(configured)
            )

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        self.assertIn("not a primary clone", store.read_error)

    def test_git_worktree_is_not_a_primary_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            primary = base / "primary"
            subprocess.run(["git", "init", "--quiet", str(primary)], check=True)
            primary.joinpath("tracked.txt").write_text("fixture\n")
            subprocess.run(["git", "-C", str(primary), "add", "tracked.txt"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(primary),
                    "-c",
                    "user.name=Rule Inventory Test",
                    "-c",
                    "user.email=rule-inventory@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            configured = base / "worktree"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(primary),
                    "worktree",
                    "add",
                    "--quiet",
                    "--detach",
                    str(configured),
                ],
                check=True,
            )
            configured.joinpath("AGENTS.md").write_text("Never bypass verification.\n")

            statements, store = inventory.read_canonical_repository_instruction_roots(
                str(configured)
            )

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        self.assertIn("not a primary clone", store.read_error)

    def test_unconfigured_canonical_repository_roots_are_unread_not_empty(self) -> None:
        statements, store = inventory.read_canonical_repository_instruction_roots("")

        self.assertEqual(statements, [])
        self.assertFalse(store.read_ok)
        self.assertEqual(store.populated, 0)
        self.assertIn("required", store.read_error)

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

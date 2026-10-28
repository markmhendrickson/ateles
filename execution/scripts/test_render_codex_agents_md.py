"""Tests for execution/scripts/render_codex_agents_md.py.

The renderer supplies the Codex fallback instructions. Its `--check` command
reports full-text drift for the target explicitly supplied by its caller.
CI runs this suite against temporary targets; it neither checks nor regenerates
the installed Codex-home file. Installed-target freshness requires a separate
invocation against that target. These tests exercise the command's failure modes:

* the literal state found on 2026-09-13 — an `AGENTS.md` that exists and is
  empty, which is how a Codex session came to run with no rules at all;
* a rendered copy that has LOST a safety rule (``NEVER `git stash` ``), which is
  what a hand-maintained copy does over time;
* `CLAUDE.md` GAINING a rule the rendered copy lacks, which is the realistic
  drift direction — the source moves and the copy does not;
* ongoing constraints hidden under inventory/history headings, which must survive verbatim.

Every test writes to a temporary `--out`, never the operator's real Codex home.

Run: python3 execution/scripts/test_render_codex_agents_md.py
"""

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).resolve().parent / "render_codex_agents_md.py"
REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

EXIT_OK = 0
EXIT_LOST = 1
EXIT_USAGE = 2


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


class TestRender(unittest.TestCase):
    def test_render_then_check_passes(self) -> None:
        """The rendered file carries every standing rule in the real CLAUDE.md.

        CI exercises this round-trip with the actual repository source and a
        temporary output. Mutation tests separately prove drift is rejected;
        this test does not establish an installed-target freshness gate.
        """
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            rendered = run("--out", str(out))
            self.assertEqual(rendered.returncode, EXIT_OK, rendered.stderr)
            self.assertTrue(out.exists())

            checked = run("--check", "--out", str(out))
            self.assertEqual(
                checked.returncode,
                EXIT_OK,
                f"parity check failed against the real CLAUDE.md:\n{checked.stdout}",
            )
            self.assertIn("OK:", checked.stdout)

    def test_rendered_file_carries_the_hard_constraints(self) -> None:
        """The rules that have cost real work are present by name.

        Named explicitly rather than counted: a count passes while the one rule
        that matters is the one missing.
        """
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            text = out.read_text(encoding="utf-8")
            for rule in (
                "NEVER `git stash`",
                "One worktree, one agent",
                "Verify before asserting",
                "Merge stays gated",
                "Proceed with your recommendation",
                "End every turn with the decisions",
                "Capture reusable operator feedback as a standing rule",
                "Advance fully verified mechanical gates automatically",
                "Escalate choices by material operator outcome",
                "Maintain a live session workboard",
            ):
                self.assertIn(rule, text, f"missing standing rule: {rule}")

    def test_routine_deployment_is_distinct_from_product_release(self) -> None:
        """Provider deployment records must not recreate the retired consent gate."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            for text in (
                CLAUDE_MD.read_text(encoding="utf-8"),
                out.read_text(encoding="utf-8"),
            ):
                self.assertIn("hosted-instance deploys", text)
                self.assertIn("deployment_configuration", text)
                self.assertIn("operator", text)
                self.assertNotIn(
                    "Production or client-instance deployment remains operator-gated",
                    text,
                )

    def test_harness_context_tracks_the_opt_in_live_path(self) -> None:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            self.assertEqual(run("--out", str(out)).returncode, EXIT_OK)
            text = out.read_text()
            self.assertIn("install_codex_hooks.py", text)
            self.assertIn("/hooks trust step", text)
            self.assertIn(
                "proves neither installation, trust nor recipient-path delivery", text
            )
            self.assertNotIn("**in Codex nothing does.**", text)
            self.assertNotIn("as of this writing it carries session-anchoring", text)


class TestCheckFails(unittest.TestCase):
    def test_empty_agents_md_fails(self) -> None:
        """The literal 2026-09-13 state: the file exists and carries nothing."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            out.write_text("", encoding="utf-8")
            result = run("--check", "--out", str(out))
            self.assertEqual(result.returncode, EXIT_LOST)
            self.assertIn("empty", result.stdout)

    def test_absent_agents_md_fails(self) -> None:
        """No load point at all is a failure, not a skip."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            result = run("--check", "--out", str(out))
            self.assertEqual(result.returncode, EXIT_LOST)
            self.assertIn("does not exist", result.stdout)

    def test_lost_safety_rule_is_named(self) -> None:
        """A rendered copy that dropped the never-stash rule fails, by name."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            kept = [
                line
                for line in out.read_text(encoding="utf-8").splitlines(True)
                if "NEVER `git stash`" not in line
            ]
            out.write_text("".join(kept), encoding="utf-8")

            result = run("--check", "--out", str(out))
            self.assertEqual(result.returncode, EXIT_LOST)
            self.assertIn("git stash", result.stdout)

    def test_new_rule_upstream_is_detected(self) -> None:
        """The realistic drift: CLAUDE.md gains a rule, the copy does not.

        Uses a temporary --source so the real CLAUDE.md is never touched.
        """
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))

            source = Path(tmp) / "CLAUDE.md"
            source.write_text(
                CLAUDE_MD.read_text(encoding="utf-8")
                + "\n## New standing requirements\n\n- **Never run a migration against prod without a dry "
                "run.** Planted.\n",
                encoding="utf-8",
            )

            result = run("--check", "--out", str(out), "--source", str(source))
            self.assertEqual(result.returncode, EXIT_LOST)
            self.assertIn("migration against prod", result.stdout)

    def test_unreadable_source_exits_2_not_1(self) -> None:
        """ "Could not run" must never read as "passed" — or as a finding."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            result = run(
                "--check",
                "--out",
                str(out),
                "--source",
                str(Path(tmp) / "absent.md"),
            )
            self.assertEqual(result.returncode, EXIT_USAGE)


class TestOmissions(unittest.TestCase):
    """Regression coverage for constraints once lost through section omissions."""

    def test_ongoing_constraints_in_historical_sections_survive(self):
        requirements = (
            "encrypted snapshots must NOT live here",
            "Same private-repo rule applies to neotoma (public product) + openclaw (public fork): their operator secrets materialize from `ateles-private`, never committed to those repos.",
        )
        source = CLAUDE_MD.read_text()
        for requirement in requirements:
            self.assertIn(requirement, source)
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            generated = run("--out", str(out))
            self.assertEqual(generated.returncode, EXIT_OK, generated.stderr)
            self.assertEqual(run("--out", str(out), "--check").returncode, EXIT_OK)
            original = out.read_text()
            for requirement in requirements:
                self.assertTrue(
                    requirement in original,
                    f"Missing ongoing source clause: {requirement}",
                )
            for requirement in requirements:
                with self.subTest(requirement=requirement):
                    out.write_text(original.replace(requirement, "[removed]"))
                    self.assertEqual(
                        run("--out", str(out), "--check").returncode, EXIT_LOST
                    )
            out.write_text(original)
            self.assertEqual(run("--out", str(out), "--check").returncode, EXIT_OK)

    def test_complete_actual_source_survives_without_section_classification(self):
        # Independent witness: compare the raw canonical source, not the
        # projector's own expected result. Covers paragraphs and table rows,
        # including reservations hidden under an inventory/history heading.
        source = CLAUDE_MD.read_text().strip()
        self.assertTrue(source)
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            generated = run("--out", str(out))
            self.assertEqual(generated.returncode, EXIT_OK, generated.stderr)
            self.assertTrue(
                source in out.read_text(),
                f"Complete canonical source missing ({len(source.encode())} bytes)",
            )

    def test_source_content_survives_every_formerly_omitted_heading(self):
        headings = (
            "Key entity IDs",
            "Current phase blockers (Phase 5–6)",
            "Recently resolved",
            "Swarm governance layer",
        )
        for heading in headings:
            with self.subTest(heading=heading), TemporaryDirectory() as tmp:
                source, out = Path(tmp) / "CLAUDE.md", Path(tmp) / "AGENTS.md"
                # Deliberately has no bold rule lead: identity checks alone
                # cannot establish preservation of a paragraph or table row.
                raw = f"# Fixture source\n\n## {heading}\n\nA continuing restriction remains binding here.\n"
                source.write_text(raw)
                generated = run("--source", str(source), "--out", str(out))
                self.assertEqual(generated.returncode, EXIT_OK, generated.stderr)
                self.assertTrue(
                    raw.strip() in out.read_text(),
                    f"Source content lost under {heading}",
                )
                self.assertEqual(
                    run(
                        "--source", str(source), "--out", str(out), "--check"
                    ).returncode,
                    EXIT_OK,
                )


class TestManagedRegionContract(unittest.TestCase):
    def test_preserves_user_bytes_outside_markers(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            generated = out.read_bytes()
            prefix, suffix = b"User preamble\r\n\r\n", b"\r\nUser suffix\r\n"
            out.write_bytes(prefix + generated + suffix)
            result = run("--out", str(out))
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            self.assertEqual(out.read_bytes(), prefix + generated + suffix)

    def test_malformed_or_unmanaged_target_is_not_overwritten(self):
        for target in (
            "user notes only",
            "<!-- BEGIN GENERATED: ateles standing rules -->",
            "<!-- END GENERATED: ateles standing rules -->",
            "<!-- END GENERATED: ateles standing rules -->\n<!-- BEGIN GENERATED: ateles standing rules -->",
            "<!-- BEGIN GENERATED: ateles standing rules -->\n<!-- BEGIN GENERATED: ateles standing rules -->\n<!-- END GENERATED: ateles standing rules -->",
        ):
            with self.subTest(target=target), TemporaryDirectory() as tmp:
                out = Path(tmp) / "AGENTS.md"
                out.write_text(target)
                before = out.read_bytes()
                result = run("--out", str(out))
                self.assertEqual(result.returncode, EXIT_USAGE, result.stdout)
                self.assertEqual(out.read_bytes(), before)

    def test_source_body_change_with_same_rule_identity_fails(self):
        # A body change must fail even when the rule identity is unchanged.
        sys.path.insert(0, str(SCRIPT.parent))
        import render_codex_agents_md as mod

        with TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "CLAUDE.md", Path(tmp) / "AGENTS.md"
            source.write_text(
                "- **Never hardcode secrets, IBANs, or contact details** — always read from env or parquet.\n"
            )
            self.assertEqual(
                mod.main(["--source", str(source), "--out", str(out)]), EXIT_OK
            )
            self.assertEqual(
                mod.main(["--check", "--source", str(source), "--out", str(out)]),
                EXIT_OK,
            )
            source.write_text(
                source.read_text().replace(
                    "always read from env or parquet",
                    "use only a runtime source; never substitute a default",
                )
            )
            self.assertEqual(
                mod.main(["--check", "--source", str(source), "--out", str(out)]),
                EXIT_LOST,
            )

    def test_render_uses_supplied_source_limiting_clauses(self):
        with TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "CLAUDE.md", Path(tmp) / "AGENTS.md"
            clause = (
                "A technical choice does not authorize a public send or access grant."
            )
            source.write_text(
                "# Session rules\n\n- **Proceed automatically.** Choose a representation. "
                + clause
                + "\n"
            )
            result = run("--source", str(source), "--out", str(out))
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            self.assertIn(clause, out.read_text())


class TestProjectionBoundaries(unittest.TestCase):
    def test_every_managed_line_mutation_is_detected(self):
        with TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "CLAUDE.md", Path(tmp) / "AGENTS.md"
            clauses = [
                "Choose equivalent representations automatically.",
                "Public sends require the existing consent gate.",
                "Never renew a consumed exception.",
                "An unknown grant is restrictive, not authorization.",
            ]
            source.write_text(
                "# Generic standing instructions\n\n"
                + "\n".join(
                    f"- **Rule {i}.** {clause}" for i, clause in enumerate(clauses)
                )
            )
            self.assertEqual(
                run("--source", str(source), "--out", str(out)).returncode, EXIT_OK
            )
            original = out.read_text()
            for clause in clauses:
                with self.subTest(clause=clause):
                    out.write_text(original.replace(clause, "[removed]"))
                    result = run("--source", str(source), "--out", str(out), "--check")
                    self.assertEqual(result.returncode, EXIT_LOST)
            out.write_text(original)
            self.assertEqual(
                run("--source", str(source), "--out", str(out), "--check").returncode,
                EXIT_OK,
            )

    def test_new_constraint_in_historical_section_changes_the_projection(self):
        with TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "CLAUDE.md", Path(tmp) / "AGENTS.md"
            source.write_text(CLAUDE_MD.read_text())
            self.assertEqual(
                run("--source", str(source), "--out", str(out)).returncode, EXIT_OK
            )
            addition = (
                "A newly added restriction under a historical heading must survive."
            )
            source.write_text(
                source.read_text().replace(
                    "## Recently resolved", "## Recently resolved\n\n" + addition
                )
            )
            self.assertEqual(
                run("--source", str(source), "--out", str(out), "--check").returncode,
                EXIT_LOST,
            )
            self.assertEqual(
                run("--source", str(source), "--out", str(out)).returncode, EXIT_OK
            )
            self.assertTrue(
                addition in out.read_text(), "New source restriction was lost"
            )
            self.assertEqual(
                run("--source", str(source), "--out", str(out), "--check").returncode,
                EXIT_OK,
            )

    def test_legacy_machine_header_migrates_without_touching_human_suffix(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import render_codex_agents_md as mod

        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            run("--out", str(out))
            current = out.read_bytes()
            suffix = b"\nHuman suffix\r\n"
            out.write_bytes((mod.LEGACY_HEADER + "\n").encode() + current + suffix)
            self.assertEqual(run("--out", str(out), "--check").returncode, EXIT_LOST)
            self.assertEqual(run("--out", str(out)).returncode, EXIT_OK)
            self.assertEqual(out.read_bytes(), current + suffix)
            self.assertEqual(run("--out", str(out), "--check").returncode, EXIT_OK)

    def test_legacy_header_after_human_prefix_refuses_ambiguous_migration(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import render_codex_agents_md as mod

        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            self.assertEqual(run("--out", str(out)).returncode, EXIT_OK)
            current = out.read_bytes()
            human = b"Human prefix must remain exact\r\n"
            original = human + (mod.LEGACY_HEADER + "\n").encode() + current
            out.write_bytes(original)
            for mode in ((), ("--check",)):
                with self.subTest(mode=mode):
                    result = run("--out", str(out), *mode)
                    self.assertEqual(result.returncode, EXIT_USAGE)
                    self.assertIn(
                        "legacy generated header follows user content", result.stderr
                    )
                    self.assertEqual(out.read_bytes(), original)

    def test_exact_legacy_header_without_markers_does_not_establish_ownership(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import render_codex_agents_md as mod

        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            original = (
                mod.LEGACY_HEADER + "\nHuman notes without a managed region\r\n"
            ).encode()
            out.write_bytes(original)
            for mode in ((), ("--check",)):
                with self.subTest(mode=mode):
                    self.assertEqual(
                        run("--out", str(out), *mode).returncode, EXIT_USAGE
                    )
                    self.assertEqual(out.read_bytes(), original)

    def test_empty_source_and_symlink_target_fail_closed(self):
        with TemporaryDirectory() as tmp:
            source, out, real = (
                Path(tmp) / "CLAUDE.md",
                Path(tmp) / "AGENTS.md",
                Path(tmp) / "real.md",
            )
            source.write_text("")
            self.assertEqual(
                run("--source", str(source), "--out", str(out)).returncode, EXIT_USAGE
            )
            self.assertFalse(out.exists())
            real.write_text("human notes")
            out.symlink_to(real)
            for mode in ((), ("--check",)):
                self.assertEqual(run("--out", str(out), *mode).returncode, EXIT_USAGE)
                self.assertEqual(real.read_text(), "human notes")


class TestRecoveredStandingConstraints(unittest.TestCase):
    def test_current_source_limits_reach_target_and_mutations_fail(self):
        clauses = (
            "ent_30a399394effd7eab935d440",
            "A display-selected Primary plan creates no authority, binding, ownership or planning level",
            "Report Working now only from a held lease plus observed activity",
            "Awaiting your decision only from an open operator checkpoint",
            "do not create a cached parallel workboard/status store",
            "ent_82b64b6c4104843e43853666",
            "merge into the closest existing policy rather than duplicating it",
            "Never persist enforceable operating rules in harness memory files",
            "read back the active canonical result",
            "ent_1c0cbb99d2c8011358ff1dc3",
            "architecture, security or new functionality alone does not require human consent",
            "Review cannot expand authority, renew consumed/expired exceptions or holds",
            "current-bound admission/recovery, review/CI/App/merge/deployment/release/send gates",
            "ent_00a75e37c4db0cdaa5d90f98",
            "fully mechanical and live-cross-checked",
            "unless a governing rule reserves sign-off",
            "A stored status, self-report or single unchecked measurement is insufficient",
            "this rule creates no authority, revives no exception and releases no held task",
        )
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "AGENTS.md"
            self.assertEqual(run("--out", str(out)).returncode, EXIT_OK)
            original = out.read_text()
            for clause in clauses:
                with self.subTest(clause=clause):
                    self.assertIn(clause, original)
                    out.write_text(original.replace(clause, "[removed]"))
                    self.assertEqual(
                        run("--out", str(out), "--check").returncode, EXIT_LOST
                    )
            out.write_text(original)
            self.assertEqual(run("--out", str(out), "--check").returncode, EXIT_OK)


if __name__ == "__main__":
    unittest.main(verbosity=2)

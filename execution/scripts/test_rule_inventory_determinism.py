"""The canonical rule-inventory check must be satisfiable by a complete measurement.

Measured failure this guards (2026-09-25): a complete canonical measurement could
not match a committed ``rule_inventory.md`` even minutes after regenerating it,
because (a) repository-sourced stores were dated by the packaged copies' mtime,
which is always the day of the run; (b) the checkout-copy counts follow the
host's worktree set, which the swarm changes minute to minute; and (c) nothing
separated those host-dependent values from the compared rule content.

Every test here builds a synthetic host (a fake ``$HOME`` with the harness
stores, a candidate git repository, and cached entity payloads) so the
measurement runs offline and never reads the real machine.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import package_rule_inventory_inputs as inputs
import render_rule_inventory as renderer

COMMIT_DATE = "2026-01-02"
LATER_DAY_EPOCH = 1_600_000_000  # 2020-09-13: a run on some other day


def _git(cwd: Path, *args: str, when: str | None = None) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="inventory test",
        GIT_AUTHOR_EMAIL="inventory@example.com",
        GIT_COMMITTER_NAME="inventory test",
        GIT_COMMITTER_EMAIL="inventory@example.com",
    )
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    completed = subprocess.run(
        ["git", "-C", str(cwd), "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return completed.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# Named literally, not via the renderer's constant, so the fixture itself does
# not depend on the change under test.
CHECKOUT_COPY_STORES = frozenset(
    {"ateles/CLAUDE.md checkout copies", "neotoma/AGENTS.md checkout copies"}
)

RULE = "- **NEVER `git stash`** in any form — the stash stack is shared; WIP-commit.\n"


class InventoryHost:
    """A synthetic canonical measurement host, entirely inside a temp dir."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home"
        self.repos = self.home / "repos"
        self.source = base / "candidate"
        self.cache = base / "entity-cache"
        self.canonical_root = self.repos / "tools"
        self._build_home()
        self._build_source()
        self._build_cache()

    # -- fixture ---------------------------------------------------------
    def _build_home(self) -> None:
        ateles = self.repos / "ateles"
        ateles.mkdir(parents=True)
        _git(ateles, "init", "-q", "-b", "main")
        _write(ateles / "CLAUDE.md", RULE)
        _git(ateles, "add", "-A")
        _git(ateles, "commit", "-q", "-m", "init", when=f"{COMMIT_DATE}T12:00:00+00:00")
        _write(self.repos / "neotoma" / "AGENTS.md", RULE)
        _write(self.repos / "foundation" / "README.md", RULE)
        self.canonical_root.mkdir(parents=True)
        _git(self.canonical_root, "init", "-q", "-b", "main")
        _write(self.canonical_root / "CLAUDE.md", RULE)
        _write(self.home / ".claude" / "CLAUDE.md", RULE)
        _write(self.home / ".claude" / "projects" / "p" / "memory" / "m.md", RULE)
        _write(self.home / ".claude" / "skills" / "s" / "SKILL.md", RULE)
        _write(self.home / ".codex" / "AGENTS.md", RULE)
        _write(self.home / ".cursor" / "rules" / "r.md", RULE)
        (self.home / ".openclaw" / "agents").mkdir(parents=True)

    def _build_source(self) -> None:
        self.source.mkdir()
        _git(self.source, "init", "-q", "-b", "main")
        _write(self.source / "CLAUDE.md", RULE)
        _write(self.source / "docs" / "foundation" / "rule_inventory.md", "stage 0\n")
        _write(
            self.source / "lib" / "daemon_runtime" / "agent_loader.py",
            'filt = snap.get("agent_sub")\n',
        )
        _write(self.source / ".claude" / "skills" / "x" / "SKILL.md", RULE)
        _write(
            self.source / ".claude" / "hooks" / "guard.py",
            '"""Never run git stash in any form; this guard blocks it."""\n',
        )
        _git(self.source, "add", "-A")
        _git(
            self.source,
            "commit",
            "-q",
            "-m",
            "rules",
            when=f"{COMMIT_DATE}T12:00:00+00:00",
        )

    def _build_cache(self) -> None:
        rows = {
            "standing_rule": [
                {
                    "entity_id": "ent_fixture",
                    "snapshot": {"instruction": "Never use git stash; WIP-commit."},
                    "last_observation_at": "2026-01-03T00:00:00Z",
                }
            ],
            "agent_policy": [],
            "task_policy": [],
        }
        for entity_type, entities in rows.items():
            _write(self.cache / f"{entity_type}.json", json.dumps({"entities": entities}))

    # -- operations ------------------------------------------------------
    @contextlib.contextmanager
    def environment(self):
        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                renderer.CANONICAL_REPOSITORY_ROOTS_ENV: str(self.canonical_root),
            },
        ):
            yield

    def measure(self, repository_input_root: Path) -> str:
        with self.environment():
            ent_statements, ent_stores = renderer.read_entities(
                self.cache, repository_input_root
            )
            file_statements, file_stores = renderer.read_file_stores(
                self.home, repository_input_root
            )
        statements = ent_statements + file_statements
        clusters, unclassified = renderer.build_clusters(statements)
        return renderer.render(
            clusters, ent_stores + file_stores, unclassified, statements
        )

    def file_stores(self, repository_input_root: Path) -> dict[str, renderer.Store]:
        with self.environment():
            _, stores = renderer.read_file_stores(self.home, repository_input_root)
        return {store.name: store for store in stores}

    def commit_inventory(self) -> None:
        """Generate the inventory from the candidate and commit it, as a PR would."""
        _write(
            self.source / "docs" / "foundation" / "rule_inventory.md",
            self.measure(self.source),
        )
        _git(self.source, "add", "-A")
        _git(self.source, "commit", "-q", "-m", "inventory")

    def package(self, name: str, *, run_epoch: int | None = None) -> Path:
        destination = self.base / name
        inputs.package_inputs(self.source, destination)
        inputs.validate_inputs(destination)
        if run_epoch is not None:
            for path in destination.rglob("*"):
                if path.is_file():
                    os.utime(path, (run_epoch, run_epoch))
        return destination

    def check(self, data: Path) -> tuple[int, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [
            "render_rule_inventory.py",
            "--repository-input-root",
            str(data),
            "--expected-output",
            str(data / "docs" / "foundation" / "rule_inventory.md"),
            "--check",
            "--require-complete-measurement",
            "--cache",
            str(self.cache),
        ]
        with (
            self.environment(),
            mock.patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = renderer.main()
        return result, stderr.getvalue()

    def churn_worktrees(self) -> None:
        """What the swarm does between two runs: worktrees come and go."""
        ateles = self.repos / "ateles"
        for slug in ("a", "b", "c"):
            _git(ateles, "worktree", "add", "-q", str(self.repos / f"ateles-wt-{slug}"))
        _git(ateles, "worktree", "remove", str(self.repos / "ateles-wt-b"))
        # A worktree whose CLAUDE.md drifted: a new distinct version.
        _write(self.repos / "ateles-wt-c" / "CLAUDE.md", RULE + "- **Drifted** copy.\n")

    def age_non_git_stores(self) -> None:
        """A touch, a checkout, a copy: mtimes move while no rule changes."""
        for path in (
            self.home / ".codex" / "AGENTS.md",
            self.home / ".claude" / "projects" / "p" / "memory" / "m.md",
            self.repos / "neotoma" / "AGENTS.md",
            self.canonical_root / "CLAUDE.md",
        ):
            os.utime(path, (LATER_DAY_EPOCH, LATER_DAY_EPOCH))


class CheckIsSatisfiableTest(unittest.TestCase):
    def test_two_runs_with_worktree_churn_between_them_both_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = InventoryHost(Path(directory))
            host.commit_inventory()
            first = host.package("run-1")
            self.assertEqual(host.check(first), (0, ""))
            before = host.measure(first)

            # Minutes later: worktrees churned, other stores touched, and the
            # packager ran again (fresh mtimes, here a different day's).
            host.churn_worktrees()
            host.age_non_git_stores()
            second = host.package("run-2", run_epoch=LATER_DAY_EPOCH)
            after = host.measure(second)
            # Validate the instrument: the churn really changed the measurement.
            self.assertNotEqual(before, after)
            self.assertEqual(host.check(second), (0, ""))

    def test_a_real_rule_change_still_fails_the_check(self) -> None:
        """The exclusions must not blind the check to rule drift."""
        with tempfile.TemporaryDirectory() as directory:
            host = InventoryHost(Path(directory))
            host.commit_inventory()
            _write(
                host.home / ".codex" / "AGENTS.md",
                RULE + "- **Never force-push** to a shared branch.\n",
            )
            result, stderr = host.check(host.package("run"))
            self.assertEqual(result, 1)
            self.assertIn("stale", stderr)


class RepositoryDatesTest(unittest.TestCase):
    GIT_DATED = ("ateles/CLAUDE.md", "Skills (ateles repo)", "Claude Code hooks (ateles)")

    def test_git_sourced_dates_are_stable_across_repackaging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = InventoryHost(Path(directory))
            packaged = [
                host.package("run-1"),
                host.package("run-2", run_epoch=LATER_DAY_EPOCH),
            ]
            for data in packaged:
                manifest = json.loads((data / inputs.MANIFEST).read_text())
                with self.subTest(data=data.name):
                    self.assertEqual(
                        {record.get("last_commit_date") for record in manifest["files"]},
                        {COMMIT_DATE},
                    )
                    stores = host.file_stores(data)
                    for name in self.GIT_DATED:
                        self.assertEqual(stores[name].last_modified, COMMIT_DATE, name)
                        self.assertEqual(stores[name].date_source, "git commit", name)

    def test_shallow_clone_records_no_date_rather_than_a_wrong_one(self) -> None:
        """A depth-1 clone would date every file to its head commit."""
        with tempfile.TemporaryDirectory() as directory:
            host = InventoryHost(Path(directory))
            _git(host.source, "commit", "-q", "--allow-empty", "-m", "later",
                 when="2026-03-04T12:00:00+00:00")
            shallow = Path(directory) / "shallow"
            _git(
                Path(directory),
                "clone",
                "-q",
                "--depth",
                "1",
                f"file://{host.source}",
                str(shallow),
            )
            data = Path(directory) / "shallow-data"
            inputs.package_inputs(shallow, data)
            inputs.validate_inputs(data)
            manifest = json.loads((data / inputs.MANIFEST).read_text())
            self.assertEqual(
                {record.get("last_commit_date") for record in manifest["files"]},
                {None},
            )
            stores = host.file_stores(data)
            for name in self.GIT_DATED:
                self.assertEqual(stores[name].last_modified, "", name)

    def test_malformed_recorded_date_is_rejected_at_the_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = InventoryHost(Path(directory))
            data = host.package("run")
            manifest_path = data / inputs.MANIFEST
            original = json.loads(manifest_path.read_text())
            for bad in ("2026-13-40", "yesterday", 20260102, "2026-01-02 /private"):
                with self.subTest(bad=bad):
                    mutant = {
                        **original,
                        "files": [
                            {**original["files"][0], "last_commit_date": bad},
                            *original["files"][1:],
                        ],
                    }
                    manifest_path.write_text(json.dumps(mutant))
                    with self.assertRaises(inputs.InputBoundaryError) as raised:
                        inputs.validate_inputs(data)
                    self.assertEqual(raised.exception.reason, "malformed_manifest")


class ComparedSectionTest(unittest.TestCase):
    """What --check compares excludes host-dependent values, and only those."""

    def stores(self, *, copies: int, versions: int, day: str, codex_statements: int):
        stores = [
            renderer.Store(name=name, location="safe", last_modified=day)
            for name in sorted(renderer.REQUIRED_MEASUREMENT_STORES)
        ]
        for store in stores:
            if store.name in CHECKOUT_COPY_STORES:
                store.populated = copies
                store.distinct_versions = versions
            if store.name == "Codex":
                store.statements = codex_statements
        return stores

    def rendered(self, stores) -> str:
        statement = renderer.Statement(
            "ateles/CLAUDE.md", "CLAUDE.md", "L1", "Never use git stash."
        )
        clusters, unclassified = renderer.build_clusters([statement])
        return renderer.render(clusters, stores, unclassified, [statement])

    def check(self, committed: str, measured_stores) -> int:
        return self.check_with_stderr(committed, measured_stores)[0]

    def check_with_stderr(self, committed: str, measured_stores) -> tuple[int, str]:
        statement = renderer.Statement(
            "ateles/CLAUDE.md", "CLAUDE.md", "L1", "Never use git stash."
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "docs" / "foundation" / "rule_inventory.md"
            _write(output, committed)
            argv = [
                "render_rule_inventory.py",
                "--repository-input-root",
                str(root),
                "--expected-output",
                str(output),
                "--check",
                "--require-complete-measurement",
            ]
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    renderer, "read_entities", return_value=([statement], [])
                ),
                mock.patch.object(
                    renderer, "read_file_stores", return_value=([], measured_stores)
                ),
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                return renderer.main(), stderr.getvalue()

    def test_host_dependent_counts_and_dates_are_not_compared(self) -> None:
        committed = self.rendered(
            self.stores(copies=145, versions=23, day="2026-09-23", codex_statements=7)
        )
        churned = self.stores(
            copies=162, versions=26, day="2026-09-25", codex_statements=7
        )
        self.assertEqual(self.check(committed, churned), 0)

    def test_informational_blocks_hold_the_host_dependent_values(self) -> None:
        text = self.rendered(
            self.stores(copies=145, versions=23, day="2026-09-23", codex_statements=7)
        )
        compared = renderer.comparable_text(text)
        for host_value in ("145", "23 distinct versions", "2026-09-23"):
            self.assertIn(host_value, text)
            self.assertNotIn(host_value, compared)
        self.assertIn("host-dependent", compared)

    def test_a_compared_count_still_fails(self) -> None:
        committed = self.rendered(
            self.stores(copies=145, versions=23, day="2026-09-23", codex_statements=7)
        )
        drifted = self.stores(
            copies=145, versions=23, day="2026-09-23", codex_statements=8
        )
        self.assertEqual(self.check(committed, drifted), 1)

    def test_compared_content_cannot_hide_inside_an_informational_block(self) -> None:
        stores = self.stores(copies=145, versions=23, day="2026-09-23", codex_statements=7)
        committed = self.rendered(stores)
        # A committed file that wraps the stores table in markers, so its
        # compared projection no longer contains the table, must not match.
        start = committed.index("| Store | Location |")
        end = committed.index(renderer.INFORMATIONAL_START, start)
        hidden = (
            committed[:start]
            + renderer.INFORMATIONAL_START
            + "\n"
            + committed[start:end]
            + renderer.INFORMATIONAL_END
            + "\n"
            + committed[end:]
        )
        self.assertNotEqual(renderer.comparable_text(committed), renderer.comparable_text(hidden))
        self.assertEqual(self.check(hidden, stores), 1)


class CommittedInformationalBlockTest(unittest.TestCase):
    """Only the VALUE cells of an informational block are exempt (PR #1279 review).

    Masking a block wholesale let a committed file carry arbitrary text inside
    one -- unscreened and uncompared -- and still match. Everything in a block
    except its host-dependent values must equal the measurement, and the
    committed file is screened as well as the fresh render.
    """

    DAY = "2026-09-23"

    # The same synthetic render and check as ComparedSectionTest, without
    # inheriting (and so re-running) its tests.
    stores = ComparedSectionTest.stores
    rendered = ComparedSectionTest.rendered
    check = ComparedSectionTest.check
    check_with_stderr = ComparedSectionTest.check_with_stderr

    def committed_and_stores(self):
        stores = self.stores(copies=145, versions=23, day=self.DAY, codex_statements=7)
        return self.rendered(stores), stores

    @staticmethod
    def insert_after(text: str, anchor: str, line: str) -> str:
        start = text.index(renderer.INFORMATIONAL_START)
        at = text.index(anchor, start) + len(anchor)
        at = text.index("\n", at) + 1
        return text[:at] + line + "\n" + text[at:]

    def test_the_unmodified_committed_render_matches(self) -> None:
        committed, stores = self.committed_and_stores()
        self.assertEqual(self.check(committed, stores), 0)

    def test_a_row_added_to_the_dates_table_fails(self) -> None:
        committed, stores = self.committed_and_stores()
        # Well-formed by the table's own grammar, so only the row count and
        # store names being compared can catch it.
        tampered = self.insert_after(
            committed, "|---|---|---|---|", f"| Codex | — | {self.DAY} | git commit |"
        )
        self.assertEqual(self.check(tampered, stores), 1)

    def test_prose_or_a_rule_heading_added_to_a_block_fails(self) -> None:
        committed, stores = self.committed_and_stores()
        for label, line in {
            "fabricated rule heading": "### R-deadbe: never merge on a Friday",
            "a private path and an id": "Source: ~/repos/private-client/notes.md, ent_c0ffee5eedbeef",
        }.items():
            with self.subTest(label=label):
                tampered = self.insert_after(
                    committed, "**Informational, not compared", line
                )
                self.assertEqual(self.check(tampered, stores), 1)

    def test_a_line_added_to_the_copy_measurement_block_fails(self) -> None:
        committed, stores = self.committed_and_stores()
        second = committed.index(
            renderer.INFORMATIONAL_START,
            committed.index(renderer.INFORMATIONAL_END) + 1,
        )
        at = committed.index("\n", second) + 1
        tampered = committed[:at] + "Also measured: 3 copies elsewhere.\n" + committed[at:]
        self.assertEqual(self.check(tampered, stores), 1)

    def test_a_value_cell_outside_its_vocabulary_is_compared(self) -> None:
        committed, stores = self.committed_and_stores()
        row = f"| Codex | — | {self.DAY} | file modification time |"
        self.assertIn(row, committed)
        for label, replacement in {
            "date source": f"| Codex | — | {self.DAY} | copied from a private note |",
            "date cell": "| Codex | — | last Tuesday | file modification time |",
            "count cell on a compared row": f"| Codex | 4 | {self.DAY} | file modification time |",
        }.items():
            with self.subTest(label=label):
                tampered = committed.replace(row, replacement)
                self.assertEqual(self.check(tampered, stores), 1)

    def test_pii_shaped_text_in_a_committed_block_fails_the_screen(self) -> None:
        committed, stores = self.committed_and_stores()
        tampered = self.insert_after(
            committed,
            "**Informational, not compared",
            "Contact someone@private-mail.net or +34 612 345 678.",
        )
        result, stderr = self.check_with_stderr(tampered, stores)
        self.assertEqual(result, 2)
        self.assertIn("COMMITTED INVENTORY SCREEN FAILED", stderr)

    def test_pii_shaped_value_in_a_masked_cell_fails(self) -> None:
        committed, stores = self.committed_and_stores()
        # A phone number in the one cell whose integer is host-dependent: the
        # mask must not accept it as a count, and the screen must see it.
        row = next(
            line
            for line in committed.splitlines()
            if line.startswith("| ateles/CLAUDE.md checkout copies | 145 |")
        )
        tampered = committed.replace(
            row, row.replace("| 145 |", "| 612345678901 |")
        )
        self.assertNotEqual(self.check(tampered, stores), 0)

    def test_value_cells_alone_may_differ(self) -> None:
        committed, _ = self.committed_and_stores()
        # A shallow CI checkout: no git history, so repository dates are
        # absent (rendered as a dash), on another host with other counts.
        shallow = self.stores(copies=9, versions=2, day="", codex_statements=7)
        for store in shallow:
            store.date_source = "git commit"
        self.assertEqual(self.check(committed, shallow), 0)


class CanonicalRootsRuleTest(unittest.TestCase):
    """The documented definition binds: a root outside it is UNREAD."""

    def read(self, host: InventoryHost, configured: Path | str):
        with host.environment():
            return renderer.read_canonical_repository_instruction_roots(str(configured))

    def test_primary_clone_directly_under_repos_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = InventoryHost(Path(directory))
            statements, store = self.read(host, host.canonical_root)
            self.assertTrue(store.read_ok, store.read_error)
            self.assertEqual((store.populated, store.statements), (1, len(statements)))
            self.assertGreater(store.statements, 0)

    def test_roots_outside_the_definition_are_unread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = InventoryHost(Path(directory))
            ateles = host.repos / "ateles"
            _git(ateles, "worktree", "add", "-q", str(host.repos / "ateles-linked"))
            nested = host.repos / "group" / "inner"
            nested.mkdir(parents=True)
            _git(nested, "init", "-q")
            neotoma = host.repos / "neotoma"
            _git(neotoma, "init", "-q")
            cases = {
                "linked worktree": host.repos / "ateles-linked",
                "not directly under ~/repos": nested,
                "dedicated store (ateles)": ateles,
                "dedicated store (neotoma)": neotoma,
            }
            for label, root in cases.items():
                with self.subTest(label=label):
                    statements, store = self.read(host, root)
                    self.assertFalse(store.read_ok)
                    self.assertEqual(statements, [])


if __name__ == "__main__":
    unittest.main()

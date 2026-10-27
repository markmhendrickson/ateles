"""The repo-declared review scope: globs, the file format, the path-selected panel.

Operator ruling 2026-10-27 (agent_policy ent_d0f1a840e549b3b299f62397).

Run: pytest execution/daemons/apis/test_review_scope.py -v
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parents[1]), str(_HERE.parents[2])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import review_scope as rs  # noqa: E402
from review_panel import LENSES  # noqa: E402

REPO_ROOT = _HERE.parents[2]

GOOD = {
    "version": 1,
    "full_panel_paths": ["execution/daemons/**", "pay/*.py"],
    "reduced_paths": ["lib/**", "site/**"],
    "ux_paths": ["site/**"],
    "lens_scopes": {"arch": ["lib/**"]},
}


def _load(obj) -> rs.ScopeLoad:
    text = obj if isinstance(obj, str) else json.dumps(obj)
    payload = {"type": "file", "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
    return rs.load_from_contents_payload(payload, source="o/r@main:.github/review_scope.json")


class TestGlobs:
    @pytest.mark.parametrize(
        "pattern,path,expected",
        [
            ("a/**", "a/b/c.py", True),
            ("a/**", "a", False),
            ("a/**", "ab/c.py", False),
            ("a/", "a/b.py", True),
            ("a/*.py", "a/b.py", True),
            ("a/*.py", "a/b/c.py", False),
            ("**/x.py", "x.py", True),
            ("**/x.py", "p/q/x.py", True),
            ("**/x.py", "p/q/xx.py", False),
            ("x.py", "x.py", True),
            ("x.py", "sub/x.py", False),  # no basename-only matching
            ("docs/**/*.md", "docs/a/b/c.md", True),
            ("docs/**/*.md", "docs/c.md", True),
            ("a?c", "abc", True),
            ("a?c", "a/c", False),
            ("a.b", "aXb", False),  # a dot is literal
            ("a/**", "./a/b.py", True),
        ],
    )
    def test_matching(self, pattern, path, expected):
        globs = rs.compile_globs([pattern])
        assert (rs.first_match(path, globs) is not None) is expected

    @pytest.mark.parametrize("bad", ["", "/abs/**", "a\\b", "a\x00"])
    def test_unusable_globs_are_refused(self, bad):
        with pytest.raises(rs.ScopeError):
            rs.compile_globs([bad])


class TestFormat:
    def test_a_good_file_loads(self):
        load = _load(GOOD)
        assert load.scope is not None and load.error == ""
        assert load.scope.full_panel_paths == ("execution/daemons/**", "pay/*.py")
        assert load.scope.declares("arch") and not load.scope.declares("ux")

    @pytest.mark.parametrize(
        "obj,fragment",
        [
            ("{nope", "not valid JSON"),
            ([1], "not an object"),
            ({**GOOD, "version": 2}, "version"),
            ({**GOOD, "version": True}, "version"),
            ({**GOOD, "version": "1"}, "version"),
            ({k: v for k, v in GOOD.items() if k != "full_panel_paths"}, "full_panel_paths"),
            ({**GOOD, "full_panel_paths": []}, "empty"),
            ({k: v for k, v in GOOD.items() if k != "reduced_paths"}, "reduced_paths is missing"),
            ({**GOOD, "reduced_paths": []}, "reduced_paths is empty"),
            ({**GOOD, "reduced_paths": "lib/**"}, "list of strings"),
            ({**GOOD, "full_panel_paths": "execution/**"}, "list of strings"),
            ({**GOOD, "full_panel_paths": [1]}, "list of strings"),
            ({**GOOD, "ux_paths": "site"}, "list of strings"),
            ({**GOOD, "lens_scopes": []}, "object"),
            ({**GOOD, "lens_scopes": {"nobody": ["a/**"]}}, "unknown lens"),
            ({**GOOD, "lens_scopes": {"arch": []}}, "empty"),
            ({**GOOD, "full_panel_pathz": ["a"]}, "unrecognised"),
            ({**GOOD, "full_panel_paths": ["/etc/**"]}, "glob"),
        ],
    )
    def test_an_unusable_file_loads_nothing_and_says_why(self, obj, fragment):
        load = _load(obj)
        assert load.scope is None
        assert fragment in load.error

    def test_a_payload_that_is_not_a_readable_file_loads_nothing(self):
        for payload in (None, [], {"type": "dir"}, {"type": "file"}, {"content": 5},
                        {"content": "!!!", "encoding": "utf-8"}):
            assert rs.load_from_contents_payload(payload, source="s").scope is None

    def test_an_oversized_file_loads_nothing(self):
        big = json.dumps({**GOOD, "full_panel_paths": ["a/" + "x" * rs.MAX_SCOPE_BYTES]})
        assert "larger than" in _load(big).error

    def test_known_lenses_match_the_panel_registry(self):
        assert rs.KNOWN_LENSES == frozenset(lens.lens for lens in LENSES)


class TestPathPanel:
    def test_a_plain_change_is_arch(self):
        d = rs.select_path_panel(["lib/a.py"], _load(GOOD))
        assert (d.full, d.lens) == (False, "arch")

    def test_a_change_wholly_on_ux_paths_is_ux(self):
        d = rs.select_path_panel(["site/a.html", "site/x.html"], _load(GOOD))
        assert (d.full, d.lens) == (False, "ux")
        assert "site/a.html" in d.reason

    def test_a_ui_file_beside_a_code_file_is_arch_not_ux(self):
        """Item 6: ux only when EVERY changed file is on ux_paths."""
        d = rs.select_path_panel(["lib/a.py", "site/x.html"], _load(GOOD))
        assert (d.full, d.lens) == (False, "arch")

    def test_a_rename_into_a_ux_path_from_code_is_arch(self):
        d = rs.select_path_panel(["site/x.html", "lib/old_x.py"], _load(GOOD))
        assert (d.full, d.lens) == (False, "arch")

    def test_a_risky_change_is_the_full_panel_even_with_ui_files(self):
        d = rs.select_path_panel(["site/x.html", "pay/send.py"], _load(GOOD))
        assert d.full and "pay/send.py" in d.reason and "pay/*.py" in d.reason

    @pytest.mark.parametrize("files", [[], [""]])
    def test_no_changed_files_is_the_full_panel(self, files):
        d = rs.select_path_panel(files, _load(GOOD))
        assert d.full and "empty" in d.reason

    def test_no_usable_scope_is_the_full_panel(self):
        d = rs.select_path_panel(["lib/a.py"], _load("{bad"))
        assert d.full and "could not be used" in d.reason

    def test_the_description_names_the_mode_and_the_source(self):
        text = rs.select_path_panel(["lib/a.py"], _load(GOOD)).describe()
        assert text.startswith("Panel: one lens (arch), chosen by changed path.")
        assert ".github/review_scope.json" in text
        assert "->" not in text
        assert "--lenses" in text and "not required" in text

    def test_a_full_panel_description_has_no_skipped_lens_note(self):
        text = rs.select_path_panel(["pay/send.py"], _load(GOOD)).describe()
        assert text.startswith("Panel: full five-lens panel, chosen by changed path.")
        assert "not required" not in text

    def test_the_scope_file_itself_is_always_full_panel(self):
        d = rs.select_path_panel([".github/review_scope.json"], _load(GOOD))
        assert d.full and "scope file itself" in d.reason
        d = rs.select_path_panel([".GitHub/Review_Scope.json"], _load(GOOD))
        assert d.full

    def test_several_matches_are_counted(self):
        d = rs.select_path_panel(["pay/a.py", "pay/b.py", "pay/c.py"], _load(GOOD))
        assert "(and 2 more changed files on full-panel paths)" in d.reason
        d = rs.select_path_panel(["pay/a.py", "pay/b.py"], _load(GOOD))
        assert "(and 1 more changed file on full-panel paths)" in d.reason
        d = rs.select_path_panel(["pay/a.py"], _load(GOOD))
        assert "more changed" not in d.reason


class TestThisRepoDeclaresItsOwnScope:
    """Ateles's own `.github/review_scope.json` must load and must put the
    machinery that gates merges on the full panel."""

    def _scope(self) -> rs.ReviewScope:
        text = (REPO_ROOT / rs.SCOPE_PATH).read_text()
        return rs.parse_scope(text)

    @pytest.mark.parametrize(
        "path",
        [
            "execution/scripts/approve_pr_as_app.py",
            "execution/scripts/post_lens_verdict.py",
            "execution/daemons/apis/review_panel.py",
            "execution/daemons/apis/review_carry.py",
            "execution/daemons/apis/review_scope.py",
            "execution/daemons/apis/lens_authors.py",
            "execution/daemons/apis/swarm_dispatch.py",
            "execution/daemons/apis/apis.py",
            ".claude/hooks/gmail_send_gate.py",
            ".github/workflows/ateles-tests.yml",
            ".github/review_scope.json",
            "lib/github_app_token.py",
            "lib/daemon_runtime/checkout_drift.py",
            "execution/scripts/secrets_lib.py",
            "execution/scripts/secrets_publish.py",
        ],
    )
    def test_the_gate_and_credential_machinery_is_full_panel(self, path):
        assert self._scope().full_panel_trigger([path]) is not None, path

    def test_an_ordinary_docs_change_is_not(self):
        scope = self._scope()
        assert scope.full_panel_trigger(["docs/guide/how_to.md"]) is None

    def test_every_declared_lens_scope_is_a_known_lens(self):
        assert set(self._scope().lens_scopes) <= rs.KNOWN_LENSES


class TestCaseAndUnicodeInsensitiveMatching:
    """Item 3: daemons run from case-insensitive macOS checkouts."""

    @pytest.mark.parametrize(
        "path",
        ["Execution/Daemons/x.py", "EXECUTION/DAEMONS/X.PY", "execution/daemons/X.py"],
    )
    def test_case_variants_match(self, path):
        globs = rs.compile_globs(["execution/daemons/**"])
        assert rs.first_match(path, globs) is not None

    def test_a_dot_directory_in_any_case_matches(self):
        globs = rs.compile_globs([".claude/**"])
        assert rs.first_match(".Claude/hooks/x.py", globs) is not None

    def test_a_glob_written_in_upper_case_matches_lower_case_paths(self):
        globs = rs.compile_globs(["Execution/Daemons/**"])
        assert rs.first_match("execution/daemons/x.py", globs) is not None

    def test_composed_and_decomposed_forms_match(self):
        composed = "docs/caf\u00e9/a.md"
        decomposed = "docs/cafe\u0301/a.md"
        assert rs.first_match(decomposed, rs.compile_globs(["docs/caf\u00e9/**"])) is not None
        assert rs.first_match(composed, rs.compile_globs(["docs/cafe\u0301/**"])) is not None

    def test_a_different_name_still_does_not_match(self):
        assert rs.first_match("execution/daemon/x.py", rs.compile_globs(["execution/daemons/**"])) is None

    def test_touches_lens_shares_the_matcher(self):
        scope = rs.parse_scope(json.dumps({**GOOD, "lens_scopes": {"arch": ["Lib/**"]}}))
        assert scope.touches_lens("arch", ["LIB/x.py"]) == "LIB/x.py"
        assert scope.touches_lens("arch", ["libs/x.py"]) is None

    def test_full_panel_trigger_and_ux_use_the_same_matcher(self):
        scope = rs.parse_scope(json.dumps(GOOD))
        assert scope.full_panel_trigger(["EXECUTION/DAEMONS/x.py"]) is not None
        assert scope.all_on_ux_paths(["SITE/x.html"]) is not None


class TestThisRepoScopeGaps:
    """Item 5: paths the first review round found missing from Ateles's file."""

    def _scope(self):
        return rs.parse_scope((REPO_ROOT / rs.SCOPE_PATH).read_text())

    @pytest.mark.parametrize(
        "path",
        [
            "scripts/lint.sh",
            "scripts/anything/new_tool.py",
            "execution/evals/some_eval/runner.py",
            ".codex/hooks.json",
            "execution/scripts/install_codex_hooks.py",
            "execution/scripts/verify_gh_identity.py",
            "execution/scripts/trigger_swarm_pr.py",
            "execution/scripts/render_daemon_plist.py",
            "execution/scripts/lanius_sweep.py",
            "execution/scripts/triage_backfill_sweep.py",
            "execution/scripts/check_foundation_anchors.py",
            "execution/scripts/security_action_items/render_action_items.py",
            "execution/local_llm/serve.py",
            "conftest.py",
            "execution/scripts/test_approve_pr_as_app.py",
            "execution/scripts/test_post_lens_verdict.py",
            "execution/scripts/sub/dir/rotate_key_helper.py",
            "execution/scripts/sub/dir/my_secret_tool.py",
            "Execution/Daemons/apis/apis.py",
            ".Claude/hooks/gmail_send_gate.py",
            ".CODEX/hooks.json",
        ],
    )
    def test_is_full_panel(self, path):
        assert self._scope().full_panel_trigger([path]) is not None, path

    def test_codex_is_in_the_security_scope(self):
        assert self._scope().touches_lens("security", [".codex/hooks.json"]) is not None


class TestUnclearClassificationTakesTheFullPanel:
    """Operator rule update: the reduced route applies only to a change confined
    to application code, tests, copy, UI or docs outside the risky paths; when
    the classification is unclear, the full panel applies."""

    def test_a_file_on_no_list_is_unclear_and_full(self):
        d = rs.select_path_panel(["LICENSE"], _load(GOOD))
        assert d.full and "unclear" in d.reason and "LICENSE" in d.reason

    def test_one_unclear_file_among_reduced_ones_makes_the_whole_change_full(self):
        d = rs.select_path_panel(["lib/a.py", "tools/odd.bin", "other/x"], _load(GOOD))
        assert d.full and "(and 1 more)" in d.reason

    def test_a_ux_only_path_counts_as_classified(self):
        scope = rs.parse_scope(json.dumps({**GOOD, "reduced_paths": ["lib/**"], "ux_paths": ["site/**"]}))
        d = rs.select_path_panel(["site/x.html"], rs.ScopeLoad(scope, "s"))
        assert (d.full, d.lens) == (False, "ux")

    def test_a_scope_whose_lists_match_nothing_never_selects_one_lens(self):
        scope = rs.parse_scope(json.dumps({**GOOD, "reduced_paths": ["never/**"], "ux_paths": []}))
        d = rs.select_path_panel(["lib/a.py"], rs.ScopeLoad(scope, "s"))
        assert d.full

    def test_a_full_panel_path_beats_a_reduced_path(self):
        scope = rs.parse_scope(json.dumps({**GOOD, "reduced_paths": ["execution/**"]}))
        d = rs.select_path_panel(["execution/daemons/x.py"], rs.ScopeLoad(scope, "s"))
        assert d.full and "full_panel_paths" in d.reason


class TestThisRepoCoversEveryRiskCategory:
    def _scope(self):
        return rs.parse_scope((REPO_ROOT / rs.SCOPE_PATH).read_text())

    @pytest.mark.parametrize(
        "path",
        [
            # rule and policy text
            "CLAUDE.md", "AGENTS.md", "sub/dir/AGENTS.md", "apps/x/CLAUDE.md",
            ".claude/skills/x/SKILL.md", "docs/policies/agent-data-rules.md",
            "execution/scripts/render_rule_inventory.py", "lib/daemon_runtime/agent_loader.py",
            "scripts/revert_auto_policy.py", ".claude/hooks/rule_injection_gate.py",
            # dependency manifests and lockfiles
            "requirements.txt", "requirements-dev.txt", "execution/daemons/apis/requirements.txt",
            "apps/x/package.json", "apps/x/package-lock.json", "web/pnpm-lock.yaml",
            "pyproject.toml", "sub/pyproject.toml", "uv.lock", "sub/uv.lock", "Brewfile", "Brewfile.lock.json",
            # deployment, release, publication
            "deploy/fly-runner/fly.toml", "fly.toml", "apps/y/fly.prod.toml", "Dockerfile",
            "apps/y/Dockerfile.prod", "docker-compose.yml", "deploy/cloud/docker-compose.yml",
            "execution/scripts/publish_site.py", "scripts/update_readme_release_status.py",
            "execution/daemons/phoenicurus-release/publish.py", "tools/deploy_it.sh",
            ".github/workflows/ateles-release.yml", "com.example.job.plist",
            # hooks
            ".claude/hooks/x.py", ".codex/hooks.json", "scripts/git-hooks/pre-commit",
            "sub/hooks/post-merge", ".pre-commit-config.yaml",
            # data writes, schema, migrations
            "db/migrations/001_init.sql", "execution/scripts/migrate_agent_policy_edges.py",
            "tools/run_migration.py", "tools/policy_writer.py",
            "apps/task-dashboard/src/schemaData.ts", "execution/scripts/register_generation_record_schema.py",
            # credentials, sign-in, authorization
            "lib/credential_scrub.py", "apps/task-dashboard/server/auth.ts", "x/oauth_client.py",
            "tokens.json", ".env", ".env.production",
            # external sending, payment
            "execution/daemons/monedula/handlers/wise_transfer.py", "lib/approval/email_channel.py",
            "x/send_gmail.py", "lib/notify/notifier.py", "x/payment_journal.py",
        ],
    )
    def test_is_full_panel(self, path):
        assert self._scope().full_panel_trigger([path]) is not None, path

    @pytest.mark.parametrize(
        "path",
        [
            "docs/guide/how_to.md",
            "README.md",
            "apps/task-dashboard/src/Console.tsx",
            "apps/task-dashboard/src/Console.test.tsx",
            "execution/scripts/local_whisper.py",
            "execution/scripts/hallucination_filter.py",
            "execution/scripts/test_hallucination_filter.py",
        ],
    )
    def test_ordinary_application_code_docs_and_ui_stay_on_the_reduced_route(self, path):
        scope = self._scope()
        assert scope.full_panel_trigger([path]) is None, path
        assert scope.unclassified([path]) == [], path

    def test_an_unlisted_file_is_unclear_for_this_repo(self):
        scope = self._scope()
        assert scope.full_panel_trigger(["LICENSE"]) is None
        assert scope.unclassified(["LICENSE"]) == ["LICENSE"]


class TestReducedPathsAreAnExplicitList:
    """Round 3: `execution/scripts/**` was too wide a reduced route (it held
    data-writing scripts). A script not named is unclear, so it takes the full panel."""

    def _scope(self):
        return rs.parse_scope((REPO_ROOT / rs.SCOPE_PATH).read_text())

    @pytest.mark.parametrize(
        "path",
        [
            "execution/scripts/import_audio_from_desktop.py",
            "execution/scripts/link_vocabulary_terms.py",
            "execution/scripts/workout_import/importer.py",
            "execution/scripts/stream_transcript.py",
            "execution/scripts/package_rule_inventory_inputs.py",
            "execution/scripts/brand_new_tool.py",
        ],
    )
    def test_a_script_that_is_not_listed_is_unclear_and_takes_the_full_panel(self, path):
        scope = self._scope()
        assert scope.unclassified([path]) == [path]
        d = rs.select_path_panel([path], rs.ScopeLoad(scope, "s"))
        assert d.full

    def test_a_brand_new_script_beside_a_safe_one_still_takes_the_full_panel(self):
        d = rs.select_path_panel(
            ["execution/scripts/hallucination_filter.py", "execution/scripts/brand_new_tool.py"],
            rs.ScopeLoad(self._scope(), "s"),
        )
        assert d.full and "brand_new_tool.py" in d.reason

    def test_the_dashboard_build_config_is_full_panel(self):
        assert self._scope().full_panel_trigger(["apps/task-dashboard/vite.config.ts"]) is not None
        assert self._scope().unclassified(["apps/task-dashboard/vite.config.ts"]) != []

    def test_the_one_lens_reasons_gloss_the_scope_lists(self):
        scope = rs.parse_scope(json.dumps(GOOD))
        arch = rs.select_path_panel(["lib/a.py"], rs.ScopeLoad(scope, "s"))
        assert "the paths this repo marks as safe for one lens" in arch.reason
        ux = rs.select_path_panel(["site/a.html"], rs.ScopeLoad(scope, "s"))
        assert "the user-facing paths this repo marks as safe for one lens" in ux.reason
        unclear = rs.select_path_panel(["odd/x.bin"], rs.ScopeLoad(scope, "s"))
        assert "the paths this repo marks as safe for one lens" in unclear.reason

    def test_the_one_lens_note_says_review_comment(self):
        text = rs.select_path_panel(["lib/a.py"], _load(GOOD)).describe()
        assert "post an objection as a review comment on the current head" in text

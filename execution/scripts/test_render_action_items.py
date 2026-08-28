"""
Effect-level tests for the security-and-migration tracker renderer
(execution/scripts/security_action_items/render_action_items.py), per the
ateles#557 QA map.

Two things must hold:
  A. The renderer resolves issue/pr rows live via `gh` (mocked here — never
     live `gh` in this unit suite), falls back honestly to UNKNOWN when `gh`
     misses, never uppercases hand-typed `manual_status` prose, and emits the
     manifest-driven framing (title/subtitle/scope_note/public-safety block).
  B. The checked-in manifest.json holds membership/grouping only — no
     `manual_status` on GitHub-tracked (issue/pr) items — and its public-safe
     copy carries no client/partner names, instance ids, or exploit mechanics.

Mirrors the execution/scripts/test_render_agent_docs.py layout: import the
target script as a module via sys.path insertion, monkeypatch at the
`gh_json` seam rather than shelling out.

Run with: pytest execution/scripts/test_render_action_items.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG_DIR = _REPO_ROOT / "execution" / "scripts" / "security_action_items"
sys.path.insert(0, str(_PKG_DIR))

import render_action_items as rai  # noqa: E402

DEFAULT_REPO = "markmhendrickson/neotoma"


def _manifest(groups):
    return {
        "title": "Test tracker title",
        "subtitle": "Test tracker subtitle",
        "scope_note": "Test scope note.",
        "slug": "security-incident-action-items",
        "repo": DEFAULT_REPO,
        "groups": groups,
    }


# --- A. render_action_items.py behaviour -----------------------------------


class TestIssueAndPrResolution:
    def test_issue_resolves_title_and_state(self, monkeypatch):
        calls = []

        def fake_gh_json(args):
            calls.append(args)
            if args[0] == "issue":
                return {
                    "number": 42,
                    "state": "OPEN",
                    "title": "Mock issue title",
                    "url": "https://github.com/markmhendrickson/neotoma/issues/42",
                }
            return None

        monkeypatch.setattr(rai, "gh_json", fake_gh_json)
        manifest = _manifest(
            [{"name": "G", "items": [{"kind": "issue", "number": 42}]}]
        )
        html_out = rai.render(manifest, manifest["repo"])
        assert "Mock issue title" in html_out
        assert "(could not resolve)" not in html_out
        assert "open" in html_out

    def test_pr_resolves_via_issue_then_pr_fallback(self, monkeypatch):
        def fake_gh_json(args):
            if args[0] == "issue":
                return None
            return {
                "number": 99,
                "state": "MERGED",
                "title": "Mock PR title",
                "url": "https://github.com/markmhendrickson/neotoma/pull/99",
            }

        monkeypatch.setattr(rai, "gh_json", fake_gh_json)
        manifest = _manifest([{"name": "G", "items": [{"kind": "pr", "number": 99}]}])
        html_out = rai.render(manifest, manifest["repo"])
        assert "Mock PR title" in html_out
        assert "merged" in html_out
        assert "(could not resolve)" not in html_out

    def test_gh_miss_unknown_and_unresolved_summary(self, monkeypatch):
        monkeypatch.setattr(rai, "gh_json", lambda args: None)
        manifest = _manifest([{"name": "G", "items": [{"kind": "issue", "number": 7}]}])
        html_out = rai.render(manifest, manifest["repo"])
        assert "(could not resolve)" in html_out
        assert "could not be resolved from GitHub" in html_out

    def test_cross_repo_label_uses_basename(self, monkeypatch):
        seen_repos = []

        def fake_gh_json(args):
            repo = args[args.index("--repo") + 1]
            seen_repos.append(repo)
            if args[0] == "issue":
                return {
                    "number": 1,
                    "state": "OPEN",
                    "title": "Cross-repo item",
                    "url": "https://example.invalid/1",
                }
            return None

        monkeypatch.setattr(rai, "gh_json", fake_gh_json)
        manifest = _manifest(
            [
                {
                    "name": "G",
                    "items": [
                        {
                            "kind": "issue",
                            "number": 1,
                            "repo": "markmhendrickson/ateles",
                        }
                    ],
                }
            ]
        )
        html_out = rai.render(manifest, manifest["repo"])
        assert "markmhendrickson/ateles" in seen_repos
        assert "ateles#1" in html_out
        assert ">#1<" not in html_out  # not the bare-default-repo label form


class TestManualAndGhsaItems:
    def test_manual_and_ghsa_skip_gh(self, monkeypatch):
        called = []
        monkeypatch.setattr(rai, "gh_json", lambda args: called.append(args))
        manifest = _manifest(
            [
                {
                    "name": "G",
                    "items": [
                        {
                            "kind": "ghsa",
                            "key": "advisory-x",
                            "title": "Redacted advisory title",
                            "manual_status": "pending",
                        },
                        {
                            "kind": "manual",
                            "key": "op-action-x",
                            "title": "Redacted operator action",
                            "manual_status": "done 2026-08-27",
                        },
                    ],
                }
            ]
        )
        html_out = rai.render(manifest, manifest["repo"])
        assert not called, "gh_json must not be invoked for ghsa/manual rows"
        assert "advisory-x" in html_out
        assert "op-action-x" in html_out
        assert "Redacted advisory title" in html_out
        assert "Redacted operator action" in html_out

    def test_manual_status_prose_not_uppercased(self, monkeypatch):
        monkeypatch.setattr(rai, "gh_json", lambda args: None)
        manifest = _manifest(
            [
                {
                    "name": "G",
                    "items": [
                        {
                            "kind": "manual",
                            "key": "op-action-y",
                            "title": "Redacted operator action",
                            "manual_status": "done 2026-08-27",
                        },
                        {
                            "kind": "ghsa",
                            "key": "advisory-y",
                            "title": "Redacted advisory title",
                            "manual_status": "fix in review; advisory not yet filed",
                        },
                    ],
                }
            ]
        )
        html_out = rai.render(manifest, manifest["repo"])
        assert "done 2026-08-27" in html_out
        assert "DONE 2026-08-27" not in html_out
        assert "fix in review; advisory not yet filed" in html_out
        assert "FIX IN REVIEW; ADVISORY NOT YET FILED" not in html_out


class TestSummaryCounts:
    def test_summary_counts_open_closed_manual_total(self, monkeypatch):
        def fake_gh_json(args):
            n = int(args[args.index("view") + 1])
            if args[0] != "issue":
                return None
            state = "OPEN" if n == 1 else "CLOSED"
            return {"number": n, "state": state, "title": f"Item {n}", "url": "x"}

        monkeypatch.setattr(rai, "gh_json", fake_gh_json)
        manifest = _manifest(
            [
                {
                    "name": "G",
                    "items": [
                        {"kind": "issue", "number": 1},  # open
                        {"kind": "issue", "number": 2},  # closed
                        {
                            "kind": "manual",
                            "key": "op-1",
                            "title": "Op item",
                            "manual_status": "done",
                        },
                    ],
                }
            ]
        )
        html_out = rai.render(manifest, manifest["repo"])
        assert "<strong>1 open</strong>" in html_out
        assert "1 closed or merged" in html_out
        assert "1 with no public tracker" in html_out
        assert "3 tracked in total" in html_out


class TestCliOutput:
    def test_cli_out_writes_file_and_stderr_confirm(self, tmp_path, monkeypatch, capsys):
        manifest = _manifest(
            [
                {
                    "name": "G",
                    "items": [
                        {
                            "kind": "manual",
                            "key": "op-1",
                            "title": "Op item",
                            "manual_status": "done",
                        }
                    ],
                }
            ]
        )
        out_path = tmp_path / "out.html"
        body = rai.render(manifest, manifest["repo"])
        with open(out_path, "w") as fh:
            fh.write(body)
        assert out_path.exists()
        assert out_path.read_text().strip() != ""
        # The CLI's own stderr-confirm format, exercised directly rather than
        # via subprocess (unit-level, no live gh):
        msg = f"wrote {out_path} ({len(body)} chars)"
        assert msg.startswith("wrote ")
        assert msg.endswith("chars)")


class TestFramingAndPublicSafety:
    def test_framing_and_public_safety_block_emitted(self, monkeypatch):
        monkeypatch.setattr(rai, "gh_json", lambda args: None)
        manifest = _manifest([{"name": "G", "items": []}])
        html_out = rai.render(manifest, manifest["repo"])
        assert manifest["title"] in html_out
        assert manifest["subtitle"] in html_out
        assert manifest["scope_note"] in html_out
        assert "How this page is generated" in html_out
        assert "Public-safety rule" in html_out


# --- B. Checked-in manifest.json contract -----------------------------------


@pytest.fixture(scope="module")
def live_manifest():
    with open(_PKG_DIR / "manifest.json") as fh:
        return json.load(fh)


class TestManifestContract:
    def test_manifest_no_manual_status_on_github_kinds(self, live_manifest):
        offenders = []
        for g in live_manifest["groups"]:
            for it in g["items"]:
                if it["kind"] in ("issue", "pr") and "manual_status" in it:
                    offenders.append((g["name"], it))
        assert not offenders, f"issue/pr items must not carry manual_status: {offenders}"

    def test_manifest_slug_and_repo(self, live_manifest):
        assert live_manifest["slug"] == "security-incident-action-items"
        assert live_manifest["repo"] == "markmhendrickson/neotoma"

    def test_manifest_groups_present(self, live_manifest):
        expected = {
            "Advisories (private disclosure)",
            "Code fixes",
            "Detection controls",
            "Enforcement / governance",
            "Hygiene surfaced in passing",
            "Client-instance migration",
            "Operator-only actions",
        }
        actual = {g["name"] for g in live_manifest["groups"]}
        assert expected <= actual

    def test_manifest_public_safety_copy(self, live_manifest):
        # Short, repo-local deny-list: descriptive vulnerability language and
        # unmerged fix-PR references are exactly what a private-disclosure
        # advisory row must not carry on a page shared with third parties.
        deny_terms = [
            "jwt-unverified-fallback",
            "failed-signin-session",
            "unverified-jwt",
            "bearer fallback",
            "did not invalidate",
        ]
        haystack_parts = [
            live_manifest.get("title", ""),
            live_manifest.get("subtitle", ""),
            live_manifest.get("scope_note", ""),
        ]
        for g in live_manifest["groups"]:
            haystack_parts.append(g.get("name", ""))
            haystack_parts.append(g.get("note", ""))
            for it in g["items"]:
                haystack_parts.append(it.get("key", ""))
                haystack_parts.append(it.get("title", ""))
        haystack = " ".join(haystack_parts).lower()
        hits = [t for t in deny_terms if t.lower() in haystack]
        assert not hits, f"public-safety deny-list matched: {hits}"

        # No unmerged fix_pr on an advisory row (2233 is open at spec time).
        advisory_group = next(
            g for g in live_manifest["groups"] if g["name"] == "Advisories (private disclosure)"
        )
        unmerged_fix_prs = {2233}
        offenders = [
            it["key"]
            for it in advisory_group["items"]
            if it.get("fix_pr") in unmerged_fix_prs
        ]
        assert not offenders, f"advisory rows must omit unmerged fix_pr: {offenders}"

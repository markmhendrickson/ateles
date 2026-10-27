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

    def test_a_ui_change_is_ux(self):
        d = rs.select_path_panel(["lib/a.py", "site/x.html"], _load(GOOD))
        assert (d.full, d.lens) == (False, "ux")
        assert "site/x.html" in d.reason

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
        assert "panel mode: path" in text and "one lens (arch)" in text
        assert ".github/review_scope.json" in text


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

"""
Effect tests for version_lines.py: a release candidate must carry every version
line the product check (neotoma tests/unit/llms_txt_claims.test.ts) pins to
package.json, not only the two files `npm version` writes (neotoma#2947).

Run with:
  pytest execution/daemons/phoenicurus-release/test_version_lines.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import version_lines  # noqa: E402

README = (
    "# Neotoma\n\nIntro.\n\n"
    "Neotoma v0.31.0 provides 67 MCP tools and 116 REST paths in its OpenAPI spec. "
    "They cover largely the same capabilities.\n"
)
LIMITS = "# Known limitations\n\nApplies to `neotoma@0.31.0`. Every entry has a tracking issue.\n"


def _repo(tmp_path: Path, *, npm_version: str = "0.32.0", readme=README, limits=LIMITS) -> Path:
    """A product checkout as the old preparation left it: npm bumped, prose not."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "package.json").write_text(json.dumps({"name": "neotoma", "version": npm_version}))
    (tmp_path / "package-lock.json").write_text(
        json.dumps({"version": npm_version, "packages": {"": {"version": npm_version}}})
    )
    if readme is not None:
        (tmp_path / "README.md").write_text(readme)
    if limits is not None:
        (tmp_path / "docs" / "known_limitations.md").write_text(limits)
    return tmp_path


def test_npm_bump_alone_is_reported_incomplete(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert version_lines.main(["--repo", str(repo), "--version", "v0.32.0", "--check"]) == 1
    err = capsys.readouterr().err
    assert "README.md: states '0.31.0'" in err
    assert "docs/known_limitations.md: states '0.31.0'" in err


def test_writes_every_prose_line_and_verifies(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert version_lines.main(["--repo", str(repo), "--version", "v0.32.0"]) == 0
    readme = (repo / "README.md").read_text()
    assert "Neotoma v0.32.0 provides 67 MCP tools and 116 REST paths" in readme
    assert "They cover largely the same capabilities." in readme
    assert "Applies to `neotoma@0.32.0`." in (repo / "docs/known_limitations.md").read_text()
    out = capsys.readouterr().out
    assert "wrote README.md" in out and "wrote docs/known_limitations.md" in out
    assert version_lines.verify(repo, "v0.32.0") == []


def test_rerun_is_a_no_op(tmp_path):
    repo = _repo(tmp_path)
    version_lines.write_prose_lines(repo, "v0.32.0")
    assert version_lines.write_prose_lines(repo, "v0.32.0") == []


def test_missing_line_stops_without_writing_anything(tmp_path, capsys):
    repo = _repo(tmp_path, limits="# Known limitations\n\nNo applicability line.\n")
    assert version_lines.main(["--repo", str(repo), "--version", "v0.32.0"]) == 1
    assert "docs/known_limitations.md: expected exactly one version line" in capsys.readouterr().err
    assert "v0.31.0 provides" in (repo / "README.md").read_text()  # untouched


def test_unbumped_package_json_is_reported(tmp_path, capsys):
    repo = _repo(tmp_path, npm_version="0.31.0")
    assert version_lines.main(["--repo", str(repo), "--version", "v0.32.0"]) == 1
    err = capsys.readouterr().err
    assert "package.json: version is '0.31.0'" in err
    assert 'packages[""].version' in err


def test_product_check_without_node_modules_fails_closed(tmp_path, capsys):
    repo = _repo(tmp_path)
    rc = version_lines.main(["--repo", str(repo), "--version", "v0.32.0", "--product-check"])
    assert rc == 1
    assert "PRODUCT CHECK FAILED" in capsys.readouterr().err


def test_rejects_non_semver(tmp_path):
    with pytest.raises(version_lines.VersionLineError):
        version_lines.write_prose_lines(_repo(tmp_path), "latest")

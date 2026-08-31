"""Tests for the Neotoma REST-path linter (ateles#606).

The linter is the repo-wide guard replacing whack-a-mole fixes: four
independent modules independently used the MCP tool name `retrieve_entities`
as a REST path, and each was found only after it caused a production symptom.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import check_neotoma_rest_paths as lint  # noqa: E402


def _scan(tmp_path, body: str):
    p = tmp_path / "mod.py"
    p.write_text(body)
    return lint.scan(p)


def test_flags_fstring_base_url_path(tmp_path):
    hits = _scan(tmp_path, 'resp = post(f"{NEOTOMA_BASE_URL}/retrieve_entities", json=b)\n')
    assert len(hits) == 1
    assert hits[0][1] == "retrieve_entities"
    assert "/entities/query" in hits[0][2]


def test_flags_bare_path_passed_to_helper(tmp_path):
    hits = _scan(tmp_path, 'data = await _post("retrieve_entities", body, bearer)\n')
    assert len(hits) == 1


def test_flags_leading_slash_form(tmp_path):
    assert len(_scan(tmp_path, 'url = "/retrieve_entities"\n')) == 1


def test_accepts_the_correct_route(tmp_path):
    assert _scan(tmp_path, 'post(f"{NEOTOMA_BASE_URL}/entities/query", json=b)\n') == []


def test_ignores_prose_in_a_comment(tmp_path):
    body = "# NOTE: /retrieve_entities 404s; use /entities/query instead.\n"
    assert _scan(tmp_path, body) == []


def test_suppression_marker_silences_a_line(tmp_path):
    body = f'url = "/retrieve_entities"  # {lint.SUPPRESS}: 404 probe fixture\n'
    assert _scan(tmp_path, body) == []


def test_flags_other_mcp_tool_names_used_as_routes(tmp_path):
    hits = _scan(tmp_path, 'get(f"{NEOTOMA_BASE_URL}/retrieve_related_entities")\n')
    assert len(hits) == 1
    assert "related" in hits[0][2]


def test_test_files_are_exempt(tmp_path):
    p = tmp_path / "test_thing.py"
    p.write_text('url = "/retrieve_entities"\n')
    assert lint.is_exempt(p) is True


def test_real_tree_is_clean():
    """The repo itself must have no violations — this is the guard's point."""
    repo = Path(__file__).resolve().parents[2]
    import os

    cwd = os.getcwd()
    try:
        os.chdir(repo)
        assert lint.main([]) == 0
    finally:
        os.chdir(cwd)

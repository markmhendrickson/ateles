"""Unit tests for lib/daemon_runtime/artifact_contract.py (ateles#1155)."""

from __future__ import annotations

from artifact_contract import (
    InvalidRef,
    ParsedRef,
    artifact_gate_reason,
    body_shape_for_dispatch,
    classify_artifact_body,
    infer_body_shape,
    looks_like_pr_or_commit_ref,
    parse_artifact_header,
    parse_github_ref,
)


def test_parse_last_multiline_match_wins():
    text = (
        "[cicada] pull_request_link: https://github.com/o/r/pull/1\n"
        "noise\n"
        "[cicada] pull_request_link: https://github.com/o/r/pull/2\n"
    )
    h = parse_artifact_header(text, agent="cicada", artifact_kind="pull_request_link")
    assert h is not None
    assert "pull/2" in h.body
    assert h.matched_line.startswith("[cicada] pull_request_link:")


def test_parse_case_insensitive_agent_and_kind():
    text = "[Cicada] Pull_Request_Link: #42"
    h = parse_artifact_header(text, agent="cicada", artifact_kind="pull_request_link")
    assert h is not None
    assert h.body == "#42"


def test_parse_wrong_agent_or_kind_returns_none():
    assert (
        parse_artifact_header(
            "[pavo] pull_request_link: #1",
            agent="cicada",
            artifact_kind="pull_request_link",
        )
        is None
    )
    assert (
        parse_artifact_header(
            "[cicada] acceptance_criteria: yes",
            agent="cicada",
            artifact_kind="pull_request_link",
        )
        is None
    )


def test_classify_blocked_empty_valid():
    assert classify_artifact_body("BLOCKED") == "blocked"
    assert classify_artifact_body("BLOCKED — missing context") == "blocked"
    assert classify_artifact_body("BLOCKED - ascii") == "blocked"
    assert classify_artifact_body("   ") == "empty"
    assert classify_artifact_body("") == "empty"
    assert classify_artifact_body("https://github.com/o/r/pull/1") == "valid"


def test_looks_like_pr_or_commit_and_eng_spec():
    assert looks_like_pr_or_commit_ref(
        "https://github.com/markmhendrickson/ateles/pull/999"
    )
    assert looks_like_pr_or_commit_ref("PR #12")
    assert looks_like_pr_or_commit_ref("a" * 40)
    assert not looks_like_pr_or_commit_ref("ENG_SPEC_SECTION authored for x")
    assert not looks_like_pr_or_commit_ref("just prose")
    assert infer_body_shape("ENG_SPEC_SECTION foo") == "eng_spec_section"
    assert infer_body_shape("https://github.com/o/r/pull/1") == "pr_or_commit"


def test_parse_github_ref_happy_and_invalid():
    repo = "markmhendrickson/ateles"
    ok = parse_github_ref(
        "https://github.com/markmhendrickson/ateles/pull/999",
        dispatch_repo=repo,
    )
    assert isinstance(ok, ParsedRef)
    assert ok.kind == "pr" and ok.number == 999

    foreign = parse_github_ref(
        "https://evil.example/o/r/pull/1",
        dispatch_repo=repo,
    )
    assert isinstance(foreign, InvalidRef) and foreign.code == "invalid_ref_shape"

    cross = parse_github_ref(
        "https://github.com/other/repo/pull/1",
        dispatch_repo=repo,
    )
    assert isinstance(cross, InvalidRef)

    meta = parse_github_ref("PR #1; rm -rf /", dispatch_repo=repo)
    assert isinstance(meta, InvalidRef)

    bare = parse_github_ref("#42", dispatch_repo=None)
    assert isinstance(bare, InvalidRef)


def test_body_shape_for_dispatch_cicada():
    assert body_shape_for_dispatch(role="cicada", dispatch_mode=None) == frozenset(
        {"pr_or_commit"}
    )
    assert "eng_spec_section" in body_shape_for_dispatch(
        role="cicada", dispatch_mode="ordered_spec"
    )
    assert "eng_spec_section" in body_shape_for_dispatch(
        role="cicada", dispatch_mode="eng_lens"
    )


def test_cause_builders_emit_all_six_codes():
    for code in (
        "missing_header",
        "empty_body",
        "blocked",
        "unresolvable_ref",
        "invalid_ref_shape",
        "wrong_body_for_dispatch",
    ):
        reason = artifact_gate_reason(code, role="cicada", kind="pull_request_link")
        assert reason.startswith(f"[ARTIFACT_GATE] {code}")
        assert "role=cicada" in reason
        assert "kind=pull_request_link" in reason

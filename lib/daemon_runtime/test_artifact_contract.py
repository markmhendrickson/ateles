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

    anchored = parse_github_ref("#42", dispatch_repo=repo)
    assert isinstance(anchored, ParsedRef)
    assert anchored.canonical == f"{repo}#42"


def test_short_sha_is_a_ref_attempt_not_prose_and_never_resolved():
    """The distinction decides which cause code the operator sees.

    An abbreviated SHA answered the right question unverifiably
    (`invalid_ref_shape`); prose answered a different question
    (`wrong_body_for_dispatch`). A prefix can be ambiguous, so this gate never
    resolves one even when a repo anchors it: only a full 40-character SHA is a
    commit ref.
    """
    assert looks_like_pr_or_commit_ref("a1b2c3d")
    assert infer_body_shape("a1b2c3d") == "pr_or_commit"

    repo = "markmhendrickson/ateles"
    for dispatch_repo in (None, repo):
        short = parse_github_ref("a1b2c3d", dispatch_repo=dispatch_repo)
        assert isinstance(short, InvalidRef)
        assert short.code == "invalid_ref_shape"
    assert "40-character" in parse_github_ref("a1b2c3d", dispatch_repo=repo).reason
    # 39 hex is still a prefix.
    assert isinstance(parse_github_ref("a" * 39, dispatch_repo=repo), InvalidRef)

    full = parse_github_ref("a" * 40, dispatch_repo=repo)
    assert isinstance(full, ParsedRef) and full.kind == "sha" and full.sha == "a" * 40


def test_pr_url_needs_a_known_repo_to_be_checked_against():
    """A full PR URL for ANY public repo used to be accepted with no repo known.

    Bare #N and SHA refs were already refused without an expected repo; the URL
    branch skipped the owner/repo comparison when there was none, so a foreign
    PR URL resolved and the task went DONE.
    """
    url = "https://github.com/some-other-org/other-repo/pull/7"
    no_repo = parse_github_ref(url, dispatch_repo=None)
    assert isinstance(no_repo, InvalidRef) and no_repo.code == "invalid_ref_shape"
    assert "dispatch_repo" in no_repo.reason
    # A malformed dispatch_repo is not a known repo either.
    for bad in ("", "no-slash", "a/b/c", "/x", "x/"):
        got = parse_github_ref(url, dispatch_repo=bad)
        assert isinstance(got, InvalidRef), bad

    other = parse_github_ref(url, dispatch_repo="markmhendrickson/ateles")
    assert isinstance(other, InvalidRef) and "cross-repo" in other.reason

    ok = parse_github_ref(
        "https://github.com/markmhendrickson/ateles/pull/7",
        dispatch_repo="MarkMHendrickson/Ateles",
    )
    assert isinstance(ok, ParsedRef) and ok.number == 7


def test_prose_accepting_contracts_accept_a_link_bearing_body():
    """All 14 non-Cicada contracts accept only `prose`; a URL body is still text.

    `infer_body_shape` labels any http(s) URL `pr_or_commit`, so without this a
    valid "[regulus] docs_diff_or_no_change_note: <PR URL>" was refused as
    `wrong_body_for_dispatch`.
    """
    from artifact_contract import ARTIFACT_CONTRACTS

    non_cicada = [c for c in ARTIFACT_CONTRACTS if c.role != "cicada"]
    assert len(non_cicada) == 14
    for c in non_cicada:
        accepted = body_shape_for_dispatch(role=c.role, dispatch_mode=None)
        assert {"prose", "pr_or_commit", "eng_spec_section"} <= accepted, c.role
    # Cicada stays narrowed.
    assert body_shape_for_dispatch(role="cicada", dispatch_mode=None) == frozenset(
        {"pr_or_commit"}
    )


def test_eng_spec_content_check():
    from artifact_contract import eng_spec_has_content

    for bare in ("ENG_SPEC_SECTION", "ENG_SPEC_SECTION:", "ENG_SPEC_SECTION —", "  eng_spec_section - "):
        assert not eng_spec_has_content(bare), bare
    assert eng_spec_has_content("ENG_SPEC_SECTION authored for ateles#1155")
    assert eng_spec_has_content("ENG_SPEC_SECTION: added section 3")


def test_header_parse_is_linear_on_whitespace_heavy_output():
    """80k newlines / spaces used to be quadratic (`^(\\s*\\[` retried from every line)."""
    import time

    agent, kind = "cicada", "pull_request_link"
    for text in (
        "\n" * 80_000,
        ("\n \t" * 30_000) + "no header",
        f"[{agent}] " + " " * 80_000,
        "x\n" * 40_000 + "\n" * 40_000,
    ):
        start = time.perf_counter()
        assert parse_artifact_header(text, agent=agent, artifact_kind=kind) is None
        assert time.perf_counter() - start < 2.0, "header parse is not linear"

    text = ("noise\n" * 200_000) + "[cicada] pull_request_link: #5\n"
    start = time.perf_counter()
    h = parse_artifact_header(text, agent=agent, artifact_kind=kind)
    assert time.perf_counter() - start < 2.0
    assert h is not None and h.body == "#5", "cap dropped the closing header"


def test_header_scan_is_capped_to_the_tail(monkeypatch):
    """Only the tail of huge output is scanned; the closing header still wins."""
    import artifact_contract

    monkeypatch.setattr(artifact_contract, "_MAX_SCAN_CHARS", 500)
    early = "[cicada] pull_request_link: #1\n"
    closing = "[cicada] pull_request_link: #2\n"
    assert (
        parse_artifact_header(
            early + "noise\n" * 500, agent="cicada", artifact_kind="pull_request_link"
        )
        is None
    ), "scanned past the cap"
    h = parse_artifact_header(
        early + "noise\n" * 500 + closing, agent="cicada", artifact_kind="pull_request_link"
    )
    assert h is not None and h.body == "#2"


def test_header_body_does_not_span_lines():
    h = parse_artifact_header(
        "[cicada] pull_request_link:\n#5\n", agent="cicada", artifact_kind="pull_request_link"
    )
    assert h is not None and h.body == ""


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


def test_every_cause_has_real_hint_and_next_step_text():
    """No placeholder copy: each code says what happened and what to do."""
    from artifact_contract import CAUSE_HINTS, CAUSE_NEXT_STEPS

    codes = set(CAUSE_HINTS)
    assert codes == set(CAUSE_NEXT_STEPS)
    assert {"record_not_saved", "blocked", "unresolvable_ref"} <= codes
    for code in codes:
        reason = artifact_gate_reason(code, role="cicada", kind="pull_request_link")
        assert "[COPY" not in reason
        assert CAUSE_HINTS[code] in reason and f"Next: {CAUSE_NEXT_STEPS[code]}" in reason
    # An honest agent BLOCKED must read differently from a gate failure, and a
    # persistence miss must tell the operator not to re-dispatch.
    assert "not a gate failure" in CAUSE_HINTS["blocked"]
    assert "Do not re-dispatch" in CAUSE_NEXT_STEPS["record_not_saved"]
    assert "exists" in CAUSE_NEXT_STEPS["record_not_saved"]


def test_cause_builders_emit_all_codes():
    for code in (
        "missing_header",
        "empty_body",
        "blocked",
        "unresolvable_ref",
        "invalid_ref_shape",
        "wrong_body_for_dispatch",
        "record_not_saved",
    ):
        reason = artifact_gate_reason(code, role="cicada", kind="pull_request_link")
        assert reason.startswith(f"[ARTIFACT_GATE] {code}")
        assert "role=cicada" in reason
        assert "kind=pull_request_link" in reason

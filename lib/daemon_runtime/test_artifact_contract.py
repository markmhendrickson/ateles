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
        # BLOCKED lane, not the retry lane: a real commit may exist behind it.
        assert short.code == "ref_unverifiable"
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
    # Cannot be checked, so it is neither accepted nor judged wrong.
    assert isinstance(no_repo, InvalidRef) and no_repo.code == "ref_unverifiable"
    assert "no repo" in no_repo.reason
    # A malformed dispatch_repo is not a known repo either.
    for bad in ("", "no-slash", "a/b/c", "/x", "x/"):
        got = parse_github_ref(url, dispatch_repo=bad)
        assert isinstance(got, InvalidRef), bad

    other = parse_github_ref(url, dispatch_repo="markmhendrickson/ateles")
    # A real PR exists in the wrong repo: BLOCKED lane, so a retry cannot duplicate it.
    assert isinstance(other, InvalidRef) and other.code == "ref_unverifiable"
    assert "cross-repo" in other.reason

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


ALL_CODES = (
    "missing_header",
    "empty_body",
    "blocked",
    "unresolvable_ref",
    "invalid_ref_shape",
    "wrong_body_for_dispatch",
    "record_not_saved",
    "ref_unverifiable",
    "ref_check_unavailable",
)


def test_every_cause_has_real_hint_and_next_step_text():
    """No placeholder copy: each code says what happened and what to do."""
    from artifact_contract import CAUSE_HINTS, CAUSE_NEXT_STEPS

    assert set(CAUSE_HINTS) == set(CAUSE_NEXT_STEPS) == set(ALL_CODES)
    for code in ALL_CODES:
        reason = artifact_gate_reason(
            code, role="cicada", kind="pull_request_link", task_id="ent_t1"
        )
        assert "[COPY" not in reason and "{record_cmd}" not in reason
        assert CAUSE_HINTS[code] in reason
        assert "\nNext: " in reason, "the next step must be on its own line"


def test_next_steps_state_which_lane_the_task_is_in():
    """FAILED codes are auto-retried by the watchdog; the hint must say so.

    A hint that says "re-dispatch" when the watchdog already has invites a
    second run; BLOCKED codes must say nothing retries them.
    """
    from artifact_contract import WATCHDOG_RETRIED

    for code in ALL_CODES:
        nxt = artifact_gate_reason(code, role="cicada", task_id="t").split("Next: ", 1)[1]
        if code in WATCHDOG_RETRIED:
            assert "re-runs it automatically" in nxt, code
            assert "you do not need to re-dispatch" in nxt, code
        else:
            assert "re-runs it automatically" not in nxt, code
            assert "not retried" in nxt.lower() or "NOT retried" in nxt, code
    assert WATCHDOG_RETRIED == {
        "missing_header", "empty_body", "wrong_body_for_dispatch",
        "invalid_ref_shape", "unresolvable_ref",
    }


def test_reasons_show_offered_ref_and_expected_repo_and_the_command():
    reason = artifact_gate_reason(
        "invalid_ref_shape", role="cicada", kind="pull_request_link",
        task_id="ent_t1", offered="https://github.com/o/r/pull/7", expected_repo=None,
    )
    assert 'offered="https://github.com/o/r/pull/7"' in reason
    assert "expected_repo=none recorded on the task" in reason
    assert (
        "neotoma corrections create --entity-id ent_t1 --entity-type task "
        "--field-name result --corrected-value 'https://github.com/o/r/pull/7'"
    ) in reason
    assert "--field-name status --corrected-value done" in reason
    with_repo = artifact_gate_reason(
        "invalid_ref_shape", role="cicada", offered="x", expected_repo="a/b"
    )
    assert "expected_repo=a/b" in with_repo


def test_record_not_saved_wording_is_role_shape_aware():
    """A prose deliverable has no PR: the PR wording would be false for it."""
    pr = artifact_gate_reason("record_not_saved", role="cicada", ref_based=True)
    prose = artifact_gate_reason("record_not_saved", role="regulus", ref_based=False)
    assert "PR" in pr and "second PR" in pr
    assert "PR" not in prose and "redo the work" in prose
    assert "Do not re-dispatch" in pr and "Do not re-dispatch" in prose


def test_verbatim_body_is_on_its_own_line_and_not_a_double_negative():
    reason = artifact_gate_reason(
        "blocked", role="cicada", verbatim_body="need repo access"
    )
    lines = reason.split("\n")
    assert lines[1] == 'Agent said: "need repo access"'
    assert lines[2].startswith("Next: ")
    assert "not a gate failure" not in reason, "said once, in the alert head"


def test_trim_on_word_boundary():
    from artifact_contract import trim_on_word

    assert trim_on_word("short", 80) == "short"
    text = "alpha beta gamma delta epsilon zeta"
    out = trim_on_word(text, 14)
    assert out.endswith("…") and out.rstrip("…") in ("alpha beta", "alpha beta gamma")
    assert "gam" not in out.replace("gamma", "")  # never a partial word
    long_word = "x" * 300
    assert len(trim_on_word(long_word, 50)) <= 51


def test_cause_builders_emit_all_codes():
    for code in ALL_CODES:
        reason = artifact_gate_reason(code, role="cicada", kind="pull_request_link")
        assert reason.startswith(f"[ARTIFACT_GATE] {code}")
        assert "role=cicada" in reason
        assert "kind=pull_request_link" in reason


# ── tri-state resolver classification ────────────────────────────────────────


def test_gh_failure_classification_is_tri_state():
    """Only a clear not-found is "absent"; every other failure is "unavailable"."""
    from artifact_contract import classify_gh_ref_failure as c

    assert c(0, "") == "exists"
    # Definitive not-found: GitHub answered.
    assert c(1, "GraphQL: Could not resolve to a PullRequest with the number of 9. (repository.pullRequest)") == "absent"
    assert c(1, "no pull requests found for branch") == "absent"
    assert c(1, "gh: Not Found (HTTP 404)") == "absent"
    assert c(1, "gh: No commit found for SHA: abc (HTTP 422)") == "absent"
    # Could not check: says nothing about the ref.
    for stderr in (
        "gh: API rate limit exceeded for user ID 1. (HTTP 403)",
        "HTTP 429: secondary rate limit",
        "gh: Bad credentials (HTTP 401)",
        "gh: Server Error (HTTP 502)",
        "error connecting to api.github.com: connection reset",
        "GraphQL: Could not resolve to a Repository with the name 'o/r'.",
        "something unexpected",
        "",
    ):
        assert c(1, stderr) == "unavailable", stderr
    # A rate limit that also contains the word 404 must not read as absent.
    assert c(1, "HTTP 403: API rate limit exceeded (see docs, not HTTP 404)") == "unavailable"


# ── boundaries: dispatch_repo, ASCII, BLOCKED variants, grammar ──────────────


def test_dispatch_repo_must_be_safe_for_gh_argv():
    from artifact_contract import _split_dispatch_repo

    assert _split_dispatch_repo("markmhendrickson/ateles") == ("markmhendrickson", "ateles")
    for bad in ("--flag/x", "a/--b", "a b/c", "a/b c", "a/b;rm", "../x", "a/..", "a/b/c", "$x/y", "-o/r", "é/r"):
        assert _split_dispatch_repo(bad) is None, bad


def test_pr_url_and_shorthand_are_ascii_only():
    """`\\d` used to match other scripts' digits, which int() then accepts."""
    arabic = "\u0661\u0662\u0663"
    repo = "markmhendrickson/ateles"
    got = parse_github_ref(f"https://github.com/{repo}/pull/{arabic}", dispatch_repo=repo)
    assert isinstance(got, InvalidRef)
    got = parse_github_ref(f"#{arabic}", dispatch_repo=repo)
    assert isinstance(got, InvalidRef)
    # IGNORECASE without re.ASCII folds a few non-ASCII letters onto ASCII ones
    # (long s -> "s"), so a look-alike scheme would otherwise match.
    lookalike = f"http\u017f://github.com/{repo}/pull/7"
    got = parse_github_ref(lookalike, dispatch_repo=repo)
    assert isinstance(got, InvalidRef), "accepted a look-alike scheme"


def test_stop_and_ask_variants_are_all_blocked():
    for body in (
        "BLOCKED", "blocked", "BLOCKED — need X", "BLOCKED – need X", "BLOCKED - need X",
        "BLOCKED: need X", "BLOCKED  need X", "BLOCKED (missing repo)", "BLOCKED.",
        "BLOCKED, waiting on X", "Blocked; need X", "BLOCKED\u2014need X",
    ):
        assert classify_artifact_body(body) == "blocked", body
    for body in ("BLOCKED_BY_X", "unblocked the queue", "blockedness", "PR #1 (was BLOCKED)"):
        assert classify_artifact_body(body) == "valid", body


def test_header_grammar_vs_the_old_regex():
    """The old Anthus regex used `\\s` everywhere. Documented difference: a header
    is one line, so a newline between the tag and the kind no longer matches. Every
    other Unicode whitespace still does."""
    def parse(text):
        return parse_artifact_header(text, agent="cicada", artifact_kind="pull_request_link")

    for gap in (" ", "\t", "\u00a0", "\u3000", "\x0c", "\u2003"):
        h = parse(f"[cicada]{gap}pull_request_link:{gap}#5")
        assert h is not None and h.body == "#5", repr(gap)
        assert parse(f"{gap}[cicada] pull_request_link: #5") is not None, repr(gap)
    # The deliberate difference.
    assert parse("[cicada]\npull_request_link: #5") is None
    assert parse("[cicada] pull_request_link\n: #5") is None


def test_mode_gated_shapes_come_from_the_table_not_a_role_special_case():
    """The table is the enforced source: no `role == "cicada"` branch."""
    import inspect
    import artifact_contract
    from artifact_contract import ARTIFACT_CONTRACTS

    assert "cicada" not in inspect.getsource(artifact_contract.body_shape_for_dispatch)
    cicada = next(c for c in ARTIFACT_CONTRACTS if c.role == "cicada")
    assert cicada.mode_gated_shapes == frozenset({"eng_spec_section"})
    assert cicada.accepted_body_shapes >= cicada.mode_gated_shapes
    for mode, expect_spec in ((None, False), ("ordered_spec", True), ("eng_lens", True)):
        got = body_shape_for_dispatch(role="cicada", dispatch_mode=mode)
        assert ("eng_spec_section" in got) is expect_spec, mode
        assert "pr_or_commit" in got
    # Every declared shape is reachable somewhere in the table's own terms.
    for c in ARTIFACT_CONTRACTS:
        assert c.mode_gated_shapes <= c.accepted_body_shapes, c.role

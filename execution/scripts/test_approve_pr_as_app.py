"""Tests for approve_pr_as_app.py — panel-gated App approval.

Bootstrap mode (agent_policy `ent_d0f1a840e549b3b299f62397`): a session
approves a PR as the swarm's App only when every required review lens has
cleared the PR's CURRENT head. These tests mock every GitHub call and the
App's review submission, and cover the refusal paths the tool exists to
enforce: a missing lens, a verdict left on a stale head, a `[BLOCKING]`
finding, a red required check, a required lens the caller tried to narrow
away, and the pre-submit TOCTOU window (head moved, PR closed/merged,
read-back mismatch).

For `test_a_red_check_refuses_and_breaking_the_check_reader_fails_the_test`
(CLAUDE.md "a test that cannot fail on the thing it watches is decoration"):
the check-state assertion is deliberately broken mid-suite and confirmed to
go red before being restored, and the finding is noted in the PR body.

Run: pytest execution/scripts/test_approve_pr_as_app.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

_SCRIPTS = Path(__file__).resolve().parent
_DAEMON_DIR = _SCRIPTS.parents[0] / "daemons" / "apis"
_REPO_ROOT = _SCRIPTS.parents[1]
for _p in (str(_REPO_ROOT), str(_SCRIPTS), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import approve_pr_as_app as target  # noqa: E402
import swarm_dispatch  # noqa: E402
from review_panel import LENSES, select_panel  # noqa: E402

HEAD = "a" * 40
OLD_HEAD = "b" * 40
REPO = "owner/repo"
PR = 42
AUTHOR = "some-human-author"
PARENT_ISSUE = 7

# Changed files that match NOTHING in review_panel's diff_patterns, so the
# derived floor is exactly the always-on lenses (pm, qa) unless a test adds
# gate_contributors, pending-gate inheritance, or --lenses. Verified against
# the live registry (not just eyeballed) by
# TestRequiredLensesAreDerivedNotHandTyped.test_neutral_files_fixture_is_actually_neutral
# — docs/ and scripts/ paths are NOT neutral (ux's diff_patterns match both),
# which is why this fixture deliberately avoids them. Security-relevant files
# are defined separately below for the "still requires security" test.
NEUTRAL_FILES = ["a_random_file.txt", "lib/some_util.py"]
SECURITY_RELEVANT_FILES = ["execution/daemons/apis/auth/token_exchange.py"]


def _lens_comment_body(lens: str, agent: str, *, head: str = HEAD, verdict: str = "SIGNED_OFF", extra: str = "") -> str:
    marker = swarm_dispatch.compose_lens_review_marker(lens, head)
    header_name = {
        "pm": "Pavo",
        "arch": "Waxwing",
        "qa": "Phoenicurus",
        "security": "Falco",
    }.get(lens, agent.capitalize())
    body = f"{marker}\n**\U0001f916 {header_name} — Ateles swarm, {lens} review**\n**{verdict}**\n"
    if extra:
        body += f"\n{extra}\n"
    return body


def _comment(id_: int, body: str) -> dict:
    return {
        "id": id_,
        "body": body,
        "html_url": f"https://github.com/{REPO}/pull/{PR}#issuecomment-{id_}",
    }


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or (payload if isinstance(payload, str) else str(payload))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]

    def json(self):
        return self._payload


class _FakeClient:
    """Records calls and serves canned GitHub responses keyed by URL suffix.

    `pr_state`/`pr_head`/`pr_merged_at` are mutable so a test can simulate
    the PR changing state BETWEEN the tool's initial verification and its
    pre-submit re-fetch inside `submit_app_approval` (the TOCTOU window) —
    `pr_get_count` lets a test flip that state only on the SECOND `pulls/{n}`
    GET, mirroring "the state was fine when we checked, then it moved."
    """

    def __init__(
        self,
        *,
        comments: list[dict],
        check_runs: list[dict],
        combined_state: str = "success",
        legacy_status_total_count: int = 1,
        changed_files: list[str] | None = None,
        parent_issue_comments: list[dict] | None = None,
        pr_author: str = AUTHOR,
        pr_state: str = "open",
        pr_merged_at: str | None = None,
        pr_head_after_first_get: str | None = None,
        review_readback: dict | None = None,
    ):
        self.comments = comments
        self.check_runs = check_runs
        self.combined_state = combined_state
        self.legacy_status_total_count = legacy_status_total_count
        self.changed_files = changed_files if changed_files is not None else NEUTRAL_FILES
        self.parent_issue_comments = parent_issue_comments or []
        self.pr_author = pr_author
        self.pr_state = pr_state
        self.pr_merged_at = pr_merged_at
        self.pr_head_after_first_get = pr_head_after_first_get
        self.review_readback = review_readback
        self.posted: list[dict] = []
        self.pr_get_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _pr_payload(self) -> dict:
        self.pr_get_count += 1
        head = HEAD
        if self.pr_get_count > 1 and self.pr_head_after_first_get is not None:
            head = self.pr_head_after_first_get
        return {
            "head": {"sha": head},
            "number": PR,
            "body": f"Closes #{PARENT_ISSUE}",
            "user": {"login": self.pr_author},
            "state": self.pr_state,
            "merged_at": self.pr_merged_at,
        }

    async def get(self, url, headers=None, params=None):
        if url.endswith(f"/pulls/{PR}"):
            return _FakeResponse(self._pr_payload())
        if url.endswith(f"/pulls/{PR}/files"):
            page = (params or {}).get("page", 1)
            rows = [{"filename": f} for f in self.changed_files]
            return _FakeResponse(rows if page == 1 else [])
        if url.endswith(f"/issues/{PR}/comments"):
            page = (params or {}).get("page", 1)
            return _FakeResponse(self.comments if page == 1 else [])
        if url.endswith(f"/issues/{PARENT_ISSUE}/comments"):
            page = (params or {}).get("page", 1)
            return _FakeResponse(self.parent_issue_comments if page == 1 else [])
        if url.endswith("/status"):
            return _FakeResponse(
                {"state": self.combined_state, "total_count": self.legacy_status_total_count}
            )
        if url.endswith("/check-runs"):
            return _FakeResponse({"check_runs": self.check_runs})
        if "/reviews/" in url:
            # read-back of a just-posted review
            if self.review_readback is not None:
                return _FakeResponse(self.review_readback)
            return _FakeResponse(
                {
                    "id": 999,
                    "state": "APPROVED",
                    "commit_id": HEAD,
                    "user": {"login": "ateles-agents[bot]", "type": "Bot"},
                }
            )
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url, json=None, headers=None):
        assert url.endswith(f"/pulls/{PR}/reviews")
        self.posted.append({"json": json, "headers": headers})
        return _FakeResponse({"id": 999, "state": "APPROVED"})


def _green_checks() -> list[dict]:
    return [{"name": "ateles-tests", "status": "completed", "conclusion": "success"}]


def _install_client(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)


def _install_app_mint(monkeypatch, *, token: str | None = "fake-installation-token") -> None:
    monkeypatch.setenv("ATELES_REVIEWER_APP_ID", "12345")
    monkeypatch.setenv("ATELES_REVIEWER_APP_PRIVATE_KEY", "fake-pem-not-a-real-key")

    async def _fake_mint(repo, client):
        return token

    monkeypatch.setattr(swarm_dispatch, "_mint_reviewer_app_installation_token", _fake_mint)
    monkeypatch.setattr(target, "_mint_reviewer_app_installation_token", _fake_mint)


def _all_clear_comments(lenses: list[str]) -> list[dict]:
    return [
        _comment(i, _lens_comment_body(lens, target.LENS_AGENTS[lens]))
        for i, lens in enumerate(lenses)
    ]


# `run()` no longer takes an explicit lens list — the floor is derived, and
# the caller-supplied list can only ADD to it. Every test below that wants
# {pm, arch, qa, security} to ALL be required passes them all as `--lenses`
# additions on top of the (pm, qa)-only derived floor for NEUTRAL_FILES, so
# the derivation itself is exercised on every call rather than bypassed.
ALL_FOUR = ["pm", "arch", "qa", "security"]


@pytest.mark.asyncio
class TestAllLensesClear:
    async def test_dry_run_reports_pass_and_submits_nothing(self, monkeypatch):
        client = _FakeClient(comments=_all_clear_comments(ALL_FOUR), check_runs=_green_checks())
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, ALL_FOUR, apply=False)

        assert code == 0
        assert client.posted == []

    async def test_apply_approves_as_the_app(self, monkeypatch):
        client = _FakeClient(comments=_all_clear_comments(ALL_FOUR), check_runs=_green_checks())
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 0
        assert len(client.posted) == 1
        posted = client.posted[0]["json"]
        assert posted["event"] == "APPROVE"
        assert posted["commit_id"] == HEAD
        for lens in ALL_FOUR:
            assert lens in posted["body"]


@pytest.mark.asyncio
class TestMissingLensRefuses:
    async def test_apply_refuses_and_submits_nothing(self, monkeypatch):
        # security never commented at all.
        present = [lens for lens in ALL_FOUR if lens != "security"]
        client = _FakeClient(comments=_all_clear_comments(present), check_runs=_green_checks())
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1
        assert client.posted == []


@pytest.mark.asyncio
class TestOldHeadVerdictRefuses:
    async def test_verdict_on_stale_head_does_not_count(self, monkeypatch):
        comments = _all_clear_comments([lens for lens in ALL_FOUR if lens != "qa"])
        # qa reviewed an OLD head, not the current one.
        comments.append(
            _comment(
                99,
                _lens_comment_body("qa", "phoenicurus", head=OLD_HEAD),
            )
        )
        client = _FakeClient(comments=comments, check_runs=_green_checks())
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1
        assert client.posted == []


@pytest.mark.asyncio
class TestBlockingFindingRefuses:
    async def test_blocking_marker_in_body_refuses_despite_clear_token(self, monkeypatch):
        comments = _all_clear_comments([lens for lens in ALL_FOUR if lens != "security"])
        # security's own token says SIGNED_OFF but the body also carries a
        # [BLOCKING] finding — sign_off_is_warranted must refuse this.
        comments.append(
            _comment(
                77,
                _lens_comment_body(
                    "security",
                    "falco",
                    extra="[BLOCKING] SSRF: unvalidated redirect target reaches the fetch sink.",
                ),
            )
        )
        client = _FakeClient(comments=comments, check_runs=_green_checks())
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1
        assert client.posted == []


@pytest.mark.asyncio
class TestRedCheckRefuses:
    async def test_apply_refuses_on_a_failing_required_check(self, monkeypatch):
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=[{"name": "ateles-tests", "status": "completed", "conclusion": "failure"}],
            combined_state="failure",
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1
        assert client.posted == []

    async def test_a_red_check_refuses_and_breaking_the_check_reader_fails_the_test(
        self, monkeypatch
    ):
        """CLAUDE.md: a test must fail on the thing it watches.

        This test's assertion is `checks_green is False` on a failing check
        run. To prove that is a real assertion and not decoration, this test
        body temporarily monkeypatches `evaluate_checks` to always report
        green (simulating the bug this tool exists to prevent — approving
        past a red check) and confirms the SAME assertion then fails, before
        restoring the real function and confirming it passes again. Noted in
        the PR body per the operator's instruction for at least one refusal
        test.
        """
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=[{"name": "ateles-tests", "status": "completed", "conclusion": "failure"}],
            combined_state="failure",
        )
        _install_client(monkeypatch, client)

        real_evaluate_checks = target.evaluate_checks

        async def _always_green(client_, *, repo, head_sha):
            outcomes, _ = await real_evaluate_checks(client_, repo=repo, head_sha=head_sha)
            return outcomes, True  # bug: reports green regardless of outcomes

        monkeypatch.setattr(target, "evaluate_checks", _always_green)
        code_with_bug = await target.run(REPO, PR, ALL_FOUR, apply=False)
        assert code_with_bug == 0, "sanity check itself failed to simulate the bug"

        monkeypatch.setattr(target, "evaluate_checks", real_evaluate_checks)
        code_fixed = await target.run(REPO, PR, ALL_FOUR, apply=False)
        assert code_fixed == 1


@pytest.mark.asyncio
class TestChecksApiOnlyRepoDoesNotFalseFail:
    """PR #1255 live dry run: `commits/.../status` returns
    `state=pending, total_count=0` for a repo that posts only check-runs
    (ateles-tests.yml is a check-run, never a legacy commit status). Reading
    that "pending" literally would refuse every PR in this repo regardless of
    how green its check-runs are. total_count==0 must be read as "no legacy
    statuses to weigh in", not as a live pending status."""

    async def test_zero_legacy_statuses_with_green_check_runs_passes(self, monkeypatch):
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=_green_checks(),
            combined_state="pending",
            legacy_status_total_count=0,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 0
        assert len(client.posted) == 1

    async def test_zero_legacy_statuses_and_zero_check_runs_still_fails_closed(
        self, monkeypatch
    ):
        """No signal of any kind (no legacy statuses, no check-runs) must NOT
        read as green — that is "CI has not run", not "CI passed"."""
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=[],
            combined_state="pending",
            legacy_status_total_count=0,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1
        assert client.posted == []


@pytest.mark.asyncio
class TestDryRunNeverSubmits:
    async def test_dry_run_never_submits_even_when_everything_passes(self, monkeypatch):
        client = _FakeClient(comments=_all_clear_comments(ALL_FOUR), check_runs=_green_checks())
        _install_client(monkeypatch, client)
        mint_called = False

        async def _fail_if_called(repo, client_):
            nonlocal mint_called
            mint_called = True
            return "should-not-be-used"

        monkeypatch.setattr(target, "_mint_reviewer_app_installation_token", _fail_if_called)

        code = await target.run(REPO, PR, ALL_FOUR, apply=False)

        assert code == 0
        assert client.posted == []
        assert mint_called is False

    async def test_dry_run_never_submits_when_something_fails(self, monkeypatch):
        present = [lens for lens in ALL_FOUR if lens != "security"]
        client = _FakeClient(comments=_all_clear_comments(present), check_runs=_green_checks())
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, ALL_FOUR, apply=False)

        assert code == 1
        assert client.posted == []


# ── Required-lens derivation: floor comes from review_panel, never a flag ───


class TestRequiredLensesFixtureIsValid:
    """Validates the instrument (CLAUDE.md: "validate the instrument before
    believing the measurement") — an earlier version of the NEUTRAL_FILES
    fixture used docs/ and execution/scripts/ paths, which DO match
    accipiter's (ux) diff_patterns, so every test built on "neutral" derived
    to (pm, qa, ux) instead of (pm, qa) and every assertion in the class
    below was silently wrong until this was caught. Asserted against the
    live registry directly, not against this tool's own derivation, so a
    regression here can't be masked by both sides drifting together."""

    def test_neutral_files_fixture_is_actually_neutral(self):
        panel = select_panel(gate_contributors=set(), changed_files=NEUTRAL_FILES, max_panel=6)
        assert sorted(lens.lens for lens in panel) == ["pm", "qa"]

    def test_security_relevant_files_fixture_actually_pulls_in_security(self):
        panel = select_panel(
            gate_contributors=set(), changed_files=SECURITY_RELEVANT_FILES, max_panel=6
        )
        assert "security" in {lens.lens for lens in panel}


@pytest.mark.asyncio
class TestRequiredLensesAreDerivedNotHandTyped:
    async def test_neutral_diff_derives_only_the_always_on_lenses(self, monkeypatch):
        """No --lenses at all: the floor for a diff matching nothing in
        review_panel's diff_patterns, with no linked-issue gate contributors,
        is exactly the always-on lenses (pm, qa) — never a hand-typed
        pm/arch/qa/security default."""
        client = _FakeClient(
            comments=_all_clear_comments(["pm", "qa"]),
            check_runs=_green_checks(),
            changed_files=NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)

        lenses, required = await target.resolve_lenses(
            client, repo=REPO, pr=PR, pr_body="Closes #7", extra_lenses=[]
        )

        assert sorted(lenses) == ["pm", "qa"]
        assert sorted(r.lens for r in required) == ["pm", "qa"]

    async def test_security_relevant_diff_derives_security_without_a_flag(self, monkeypatch):
        client = _FakeClient(
            comments=_all_clear_comments(["pm", "qa", "security"]),
            check_runs=_green_checks(),
            changed_files=SECURITY_RELEVANT_FILES,
        )
        _install_client(monkeypatch, client)

        lenses, required = await target.resolve_lenses(
            client, repo=REPO, pr=PR, pr_body="Closes #7", extra_lenses=[]
        )

        assert "security" in lenses
        assert "security" in {r.lens for r in required}

    async def test_lenses_flag_cannot_remove_a_derived_lens(self, monkeypatch):
        """`--lenses` only ever ADDS. Passing an unrelated lens must not drop
        `security` from a security-relevant diff's derived floor."""
        client = _FakeClient(
            comments=_all_clear_comments(["pm", "qa", "security"]),
            check_runs=_green_checks(),
            changed_files=SECURITY_RELEVANT_FILES,
        )
        _install_client(monkeypatch, client)

        lenses, _ = await target.resolve_lenses(
            client, repo=REPO, pr=PR, pr_body="Closes #7", extra_lenses=["legal"]
        )

        assert "security" in lenses
        assert "legal" in lenses  # the addition still lands

    async def test_apply_refuses_when_flag_tries_to_narrow_away_security(self, monkeypatch):
        """The operator's own scenario: `--lenses pm` on a PR touching
        security-relevant paths must STILL require security — it cannot be
        narrowed away, and since security never commented, this must refuse."""
        client = _FakeClient(
            comments=_all_clear_comments(["pm", "qa"]),  # security absent
            check_runs=_green_checks(),
            changed_files=SECURITY_RELEVANT_FILES,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ["pm"], apply=True)

        assert code == 1
        assert client.posted == []

    async def test_gate_contributor_on_parent_issue_pulls_in_its_lens(self, monkeypatch):
        """A lens that pre-registered a review_expectation on the linked
        issue is required even with a neutral diff — mirrors
        `swarm_dispatch._preregistered_expectations`'s marker exactly."""
        expectation_comment = {
            "id": 1,
            "body": (
                f"**{swarm_dispatch.EXPECTATION_MARKER} (arch)** — what waxwing "
                "will verify: contract-first layering."
            ),
        }
        client = _FakeClient(
            comments=_all_clear_comments(["pm", "qa", "arch"]),
            check_runs=_green_checks(),
            changed_files=NEUTRAL_FILES,
            parent_issue_comments=[expectation_comment],
        )
        _install_client(monkeypatch, client)

        lenses, required = await target.resolve_lenses(
            client, repo=REPO, pr=PR, pr_body=f"Closes #{PARENT_ISSUE}", extra_lenses=[]
        )

        assert "arch" in lenses
        assert "arch" in {r.lens for r in required}

    @pytest.mark.asyncio
    async def test_derivation_matches_select_panel_directly(self, monkeypatch):
        """Lock-step test: `derive_required_lenses`'s own selection over a
        diff must not diverge from calling `review_panel.select_panel`
        directly over the SAME inputs it derives (changed files + gate
        contributors from the linked issue). This is the test the operator
        asked for: it fails if this tool's derivation logic is ever
        rewritten to diverge from the dispatcher's `select_panel` call,
        even though both currently call the identical imported function —
        a future edit that wraps, filters, or reorders `derive_required_
        lenses`'s call to `select_panel` would break this before it could
        silently ship."""
        for files in (NEUTRAL_FILES, SECURITY_RELEVANT_FILES):
            client = _FakeClient(
                comments=[],
                check_runs=[],
                changed_files=files,
                parent_issue_comments=[],
            )
            required = await target.derive_required_lenses(
                client, repo=REPO, pr=PR, pr_body=f"Closes #{PARENT_ISSUE}"
            )
            expected = {
                lens.lens
                for lens in select_panel(gate_contributors=set(), changed_files=files, max_panel=6)
            }
            assert {r.lens for r in required} == expected


# ── Pre-submit TOCTOU closure: mirrors _emit_formal_review ──────────────────


@pytest.mark.asyncio
class TestPreSubmitReVerification:
    async def test_head_moved_between_check_and_submit_refuses(self, monkeypatch):
        """The head that cleared the panel and the head about to be approved
        must be the SAME head. Simulates a push landing during the tool's own
        (multi-call) evaluation window."""
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=_green_checks(),
            pr_head_after_first_get=OLD_HEAD,  # moved by the time submit re-fetches
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1
        assert client.posted == []

    async def test_pr_closed_before_submit_refuses(self, monkeypatch):
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=_green_checks(),
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        # Flip PR to closed only after the tool's first read (the dry-run-
        # equivalent evaluation phase), so the initial head/lens/check pass
        # sees it open and only the pre-submit re-fetch inside
        # submit_app_approval sees it closed.
        real_pr_payload = client._pr_payload

        def _closed_after_first(self=client):
            payload = real_pr_payload()
            if self.pr_get_count > 1:
                payload["state"] = "closed"
            return payload

        client._pr_payload = _closed_after_first

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1
        assert client.posted == []

    async def test_pr_merged_before_submit_refuses(self, monkeypatch):
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=_green_checks(),
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        real_pr_payload = client._pr_payload

        def _merged_after_first(self=client):
            payload = real_pr_payload()
            if self.pr_get_count > 1:
                payload["merged_at"] = "2026-09-25T00:00:00Z"
            return payload

        client._pr_payload = _merged_after_first

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1
        assert client.posted == []

    async def test_readback_mismatch_refuses_and_reports_not_confirmed(self, monkeypatch):
        """The POST's own response is never trusted as proof — only the
        independent GET read-back is. A read-back that disagrees on state,
        commit, or author must refuse even though the POST itself succeeded."""
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=_green_checks(),
            review_readback={
                "id": 999,
                "state": "COMMENTED",  # not APPROVED — mismatch
                "commit_id": HEAD,
                "user": {"login": "ateles-agents[bot]", "type": "Bot"},
            },
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        # The POST itself is allowed to have happened (that's the point of
        # this test — only the independent read-back decides success), but
        # the tool must refuse to report a landed approval on a mismatch.
        assert code == 1

    async def test_readback_wrong_commit_refuses(self, monkeypatch):
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=_green_checks(),
            review_readback={
                "id": 999,
                "state": "APPROVED",
                "commit_id": OLD_HEAD,  # wrong commit
                "user": {"login": "ateles-agents[bot]", "type": "Bot"},
            },
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1

    async def test_readback_author_is_pr_author_refuses(self, monkeypatch):
        """If the read-back reviewer login matches the PR author, this would
        be a self-approval — must refuse even if everything else matches."""
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=_green_checks(),
            review_readback={
                "id": 999,
                "state": "APPROVED",
                "commit_id": HEAD,
                "user": {"login": AUTHOR, "type": "User"},
            },
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1

    async def test_readback_non_bot_user_type_refuses(self, monkeypatch):
        """The App's reviews land as a bot identity. A read-back showing a
        non-bot user type is not the App's own approval and must refuse."""
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=_green_checks(),
            review_readback={
                "id": 999,
                "state": "APPROVED",
                "commit_id": HEAD,
                "user": {"login": "someone-else", "type": "User"},
            },
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1

    async def test_self_approval_422_refuses(self, monkeypatch):
        """GitHub's own 422 for approving your own PR must be recognized and
        refused explicitly, mirroring `_emit_formal_review`'s same-login
        handling."""

        class _SelfApprovalClient(_FakeClient):
            async def post(self, url, json=None, headers=None):
                assert url.endswith(f"/pulls/{PR}/reviews")
                self.posted.append({"json": json, "headers": headers})
                return _FakeResponse(
                    {"message": "Can not approve your own pull request"},
                    status_code=422,
                    text="Can not approve your own pull request",
                )

        client = _SelfApprovalClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=_green_checks(),
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 1


class TestLensAgentsRegistry:
    def test_lens_agents_match_review_panel_registry(self):
        """Kept in lock-step with review_panel.LENSES per CLAUDE.md's
        operator-specific-config rule: this dict must never silently drift
        from the single source of truth for lens->agent."""
        registry = {lens.lens: lens.agent for lens in LENSES}
        for lens, agent in target.LENS_AGENTS.items():
            assert registry.get(lens) == agent, f"{lens}: {agent} != {registry.get(lens)}"

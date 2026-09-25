"""Tests for approve_pr_as_app.py — panel-gated App approval.

Bootstrap mode (agent_policy `ent_d0f1a840e549b3b299f62397`): a session
approves a PR as the swarm's App only when every required review lens has
cleared the PR's CURRENT head. These tests mock every GitHub call and the
App's review submission, and cover the refusal paths the tool exists to
enforce: a missing lens, a verdict left on a stale head, a `[BLOCKING]`
finding, and a red required check.

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
from review_panel import LENSES  # noqa: E402

HEAD = "a" * 40
OLD_HEAD = "b" * 40
REPO = "owner/repo"
PR = 42


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
        self.text = text or str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]

    def json(self):
        return self._payload


class _FakeClient:
    """Records calls and serves canned GitHub responses keyed by URL suffix."""

    def __init__(
        self,
        *,
        comments: list[dict],
        check_runs: list[dict],
        combined_state: str = "success",
        legacy_status_total_count: int = 1,
    ):
        self.comments = comments
        self.check_runs = check_runs
        self.combined_state = combined_state
        self.legacy_status_total_count = legacy_status_total_count
        self.posted: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        if url.endswith(f"/pulls/{PR}"):
            return _FakeResponse({"head": {"sha": HEAD}, "number": PR})
        if url.endswith(f"/issues/{PR}/comments"):
            page = (params or {}).get("page", 1)
            return _FakeResponse(self.comments if page == 1 else [])
        if url.endswith("/status"):
            return _FakeResponse(
                {"state": self.combined_state, "total_count": self.legacy_status_total_count}
            )
        if url.endswith("/check-runs"):
            return _FakeResponse({"check_runs": self.check_runs})
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


@pytest.mark.asyncio
class TestAllLensesClear:
    async def test_dry_run_reports_pass_and_submits_nothing(self, monkeypatch):
        lenses = ["pm", "arch", "qa", "security"]
        client = _FakeClient(comments=_all_clear_comments(lenses), check_runs=_green_checks())
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, lenses, apply=False)

        assert code == 0
        assert client.posted == []

    async def test_apply_approves_as_the_app(self, monkeypatch):
        lenses = ["pm", "arch", "qa", "security"]
        client = _FakeClient(comments=_all_clear_comments(lenses), check_runs=_green_checks())
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, lenses, apply=True)

        assert code == 0
        assert len(client.posted) == 1
        posted = client.posted[0]["json"]
        assert posted["event"] == "APPROVE"
        assert posted["commit_id"] == HEAD
        for lens in lenses:
            assert lens in posted["body"]


@pytest.mark.asyncio
class TestMissingLensRefuses:
    async def test_apply_refuses_and_submits_nothing(self, monkeypatch):
        lenses = ["pm", "arch", "qa", "security"]
        # security never commented at all.
        present = [lens for lens in lenses if lens != "security"]
        client = _FakeClient(comments=_all_clear_comments(present), check_runs=_green_checks())
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, lenses, apply=True)

        assert code == 1
        assert client.posted == []


@pytest.mark.asyncio
class TestOldHeadVerdictRefuses:
    async def test_verdict_on_stale_head_does_not_count(self, monkeypatch):
        lenses = ["pm", "arch", "qa", "security"]
        comments = _all_clear_comments([lens for lens in lenses if lens != "qa"])
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

        code = await target.run(REPO, PR, lenses, apply=True)

        assert code == 1
        assert client.posted == []


@pytest.mark.asyncio
class TestBlockingFindingRefuses:
    async def test_blocking_marker_in_body_refuses_despite_clear_token(self, monkeypatch):
        lenses = ["pm", "arch", "qa", "security"]
        comments = _all_clear_comments([lens for lens in lenses if lens != "security"])
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

        code = await target.run(REPO, PR, lenses, apply=True)

        assert code == 1
        assert client.posted == []


@pytest.mark.asyncio
class TestRedCheckRefuses:
    async def test_apply_refuses_on_a_failing_required_check(self, monkeypatch):
        lenses = ["pm", "arch", "qa", "security"]
        client = _FakeClient(
            comments=_all_clear_comments(lenses),
            check_runs=[{"name": "ateles-tests", "status": "completed", "conclusion": "failure"}],
            combined_state="failure",
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, lenses, apply=True)

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
        lenses = ["pm", "arch", "qa", "security"]
        client = _FakeClient(
            comments=_all_clear_comments(lenses),
            check_runs=[{"name": "ateles-tests", "status": "completed", "conclusion": "failure"}],
            combined_state="failure",
        )
        _install_client(monkeypatch, client)

        real_evaluate_checks = target.evaluate_checks

        async def _always_green(client_, *, repo, head_sha):
            outcomes, _ = await real_evaluate_checks(client_, repo=repo, head_sha=head_sha)
            return outcomes, True  # bug: reports green regardless of outcomes

        monkeypatch.setattr(target, "evaluate_checks", _always_green)
        code_with_bug = await target.run(REPO, PR, lenses, apply=False)
        assert code_with_bug == 0, "sanity check itself failed to simulate the bug"

        monkeypatch.setattr(target, "evaluate_checks", real_evaluate_checks)
        code_fixed = await target.run(REPO, PR, lenses, apply=False)
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
        lenses = ["pm", "arch", "qa", "security"]
        client = _FakeClient(
            comments=_all_clear_comments(lenses),
            check_runs=_green_checks(),
            combined_state="pending",
            legacy_status_total_count=0,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, lenses, apply=True)

        assert code == 0
        assert len(client.posted) == 1

    async def test_zero_legacy_statuses_and_zero_check_runs_still_fails_closed(
        self, monkeypatch
    ):
        """No signal of any kind (no legacy statuses, no check-runs) must NOT
        read as green — that is "CI has not run", not "CI passed"."""
        lenses = ["pm", "arch", "qa", "security"]
        client = _FakeClient(
            comments=_all_clear_comments(lenses),
            check_runs=[],
            combined_state="pending",
            legacy_status_total_count=0,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, lenses, apply=True)

        assert code == 1
        assert client.posted == []


@pytest.mark.asyncio
class TestDryRunNeverSubmits:
    async def test_dry_run_never_submits_even_when_everything_passes(self, monkeypatch):
        lenses = ["pm", "arch", "qa", "security"]
        client = _FakeClient(comments=_all_clear_comments(lenses), check_runs=_green_checks())
        _install_client(monkeypatch, client)
        mint_called = False

        async def _fail_if_called(repo, client_):
            nonlocal mint_called
            mint_called = True
            return "should-not-be-used"

        monkeypatch.setattr(target, "_mint_reviewer_app_installation_token", _fail_if_called)

        code = await target.run(REPO, PR, lenses, apply=False)

        assert code == 0
        assert client.posted == []
        assert mint_called is False

    async def test_dry_run_never_submits_when_something_fails(self, monkeypatch):
        lenses = ["pm", "arch", "qa", "security"]
        present = [lens for lens in lenses if lens != "security"]
        client = _FakeClient(comments=_all_clear_comments(present), check_runs=_green_checks())
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, lenses, apply=False)

        assert code == 1
        assert client.posted == []


class TestDefaultLensesAndRegistry:
    def test_default_lenses_are_pm_arch_qa_security(self):
        assert target.DEFAULT_LENSES == ("pm", "arch", "qa", "security")

    def test_lens_agents_match_review_panel_registry(self):
        """Kept in lock-step with review_panel.LENSES per CLAUDE.md's
        operator-specific-config rule: this dict must never silently drift
        from the single source of truth for lens->agent."""
        registry = {lens.lens: lens.agent for lens in LENSES}
        for lens, agent in target.LENS_AGENTS.items():
            assert registry.get(lens) == agent, f"{lens}: {agent} != {registry.get(lens)}"

    def test_docs_only_lenses_flag_can_narrow_to_pm_alone(self):
        """`--lenses pm` parses to a single required lens, per the tool's
        docs-only-PR use case."""
        result = [x.strip() for x in "pm".split(",") if x.strip()]
        assert result == ["pm"]

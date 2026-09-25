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
BASE_REF = "main"
REQUIRED_CHECK = "gitleaks (secrets + PII)"

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
        protection_rsc: dict | None = None,
        branch_payload: dict | int | None = None,
        rules: list | int | None = None,
        jobs: dict[int, dict] | None = None,
        runners: list[dict] | None = None,
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
        # Branch-protection / scheduling surfaces (operator ruling
        # 2026-09-25, "not run (no runner)"). Defaults mirror the live repo:
        # the dedicated protection endpoint 404s for a non-admin token, the
        # branch summary requires only gitleaks, no ruleset requires a check,
        # and the runner list is unreadable (None -> 403).
        self.protection_rsc = protection_rsc
        self.branch_payload = (
            branch_payload
            if branch_payload is not None
            else {
                "protected": True,
                "protection": {
                    "enabled": True,
                    "required_status_checks": {
                        "contexts": [REQUIRED_CHECK],
                        "checks": [{"context": REQUIRED_CHECK, "app_id": 15368}],
                    },
                },
            }
        )
        self.rules = rules if rules is not None else []
        self.jobs = jobs or {}
        self.runners = runners
        self.posted: list[dict] = []
        self.pr_get_count = 0
        self.get_urls: list[str] = []

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
            "base": {"ref": BASE_REF},
        }

    async def get(self, url, headers=None, params=None):
        self.get_urls.append(url)
        page = (params or {}).get("page", 1)
        if url.endswith(f"/branches/{BASE_REF}/protection/required_status_checks"):
            if self.protection_rsc is None:
                return _FakeResponse({"message": "Not Found"}, status_code=404)
            return _FakeResponse(self.protection_rsc)
        if url.endswith(f"/rules/branches/{BASE_REF}"):
            if isinstance(self.rules, int):
                return _FakeResponse({"message": "error"}, status_code=self.rules)
            return _FakeResponse(self.rules if page == 1 else [])
        if url.endswith(f"/branches/{BASE_REF}"):
            if isinstance(self.branch_payload, int):
                return _FakeResponse({"message": "error"}, status_code=self.branch_payload)
            return _FakeResponse(self.branch_payload)
        if "/actions/jobs/" in url:
            job_id = int(url.rsplit("/", 1)[1])
            if job_id not in self.jobs:
                return _FakeResponse({"message": "Not Found"}, status_code=404)
            return _FakeResponse(self.jobs[job_id])
        if url.endswith("/actions/runners"):
            if self.runners is None:
                return _FakeResponse({"message": "forbidden"}, status_code=403)
            return _FakeResponse(
                {"total_count": len(self.runners), "runners": self.runners if page == 1 else []}
            )
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

        async def _always_green(client_, *, repo, head_sha, **kwargs):
            outcomes, _ = await real_evaluate_checks(client_, repo=repo, head_sha=head_sha, **kwargs)
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


# ── Empty-lens-set bypass (round-2 Falco finding on PR #1266) ───────────────
#
# `review_panel.select_panel(..., max_panel=0)` returns `[]` — EVEN the
# `always=True` lenses (pm, qa) — because it caps the assembled panel with
# plain list slicing. With APIS_PANEL_MAX=0 (or unset-but-empty derivation
# from any other cause) plus green checks, the OLD code's
# `all(o.passed for o in [])` was vacuously True: zero lenses reviewed, tool
# reports PASS. This class covers the fix at every layer: input validation
# (`_validate_panel_max`), defense-in-depth union of the always-on floor in
# `derive_required_lenses`, and the hard empty-set refusal in `run()` that
# does not depend on either of the other two.


class TestAlwaysRequiredLensesRegistry:
    def test_always_required_lenses_are_taken_from_the_registry(self):
        """Must be derived from LENSES, not a hard-coded name list — if a
        future lens is marked always=True, this must pick it up with no
        code change here."""
        expected = {lens.lens for lens in LENSES if lens.always}
        got = {lens.lens for lens in target._always_required_lenses()}
        assert got == expected
        assert got == {"pm", "qa"}  # documents today's registry state


class TestPanelMaxValidation:
    @pytest.mark.parametrize("raw", ["0", "-1", "1"])
    def test_panel_max_at_or_below_always_floor_refuses(self, raw):
        with pytest.raises(RuntimeError, match="APIS_PANEL_MAX"):
            target._validate_panel_max(raw)

    def test_panel_max_equal_to_always_floor_count_is_allowed(self):
        assert target._validate_panel_max("2") == 2

    def test_panel_max_above_floor_is_allowed(self):
        assert target._validate_panel_max("6") == 6

    def test_non_integer_panel_max_refuses(self):
        with pytest.raises(RuntimeError, match="APIS_PANEL_MAX"):
            target._validate_panel_max("not-a-number")


@pytest.mark.asyncio
class TestPanelMaxZeroOrNegativeNeverApproves:
    """The operator's exact scenario: APIS_PANEL_MAX=0, =-1, and =1 must
    each refuse (never silently keep only a truncated floor and never
    approve) — verified through the full `run()` path, not just the
    validator in isolation."""

    @pytest.mark.parametrize("panel_max", ["0", "-1", "1"])
    async def test_apply_refuses_and_submits_nothing(self, monkeypatch, panel_max):
        monkeypatch.setenv("APIS_PANEL_MAX", panel_max)
        # Comments that would clear EVERY lens if the panel were somehow
        # still assembled — proves the refusal is the panel-size guard
        # itself, not merely "no lens commented."
        client = _FakeClient(
            comments=_all_clear_comments(list(target.LENS_AGENTS)),
            check_runs=_green_checks(),
            changed_files=NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, [], apply=True)

        assert code == 1
        assert client.posted == []

    @pytest.mark.parametrize("panel_max", ["0", "-1", "1"])
    async def test_dry_run_reports_failure_not_pass(self, monkeypatch, panel_max):
        """Even without --apply, a misconfigured APIS_PANEL_MAX must report
        FAIL, never a vacuous PASS over zero lenses."""
        monkeypatch.setenv("APIS_PANEL_MAX", panel_max)
        client = _FakeClient(
            comments=_all_clear_comments(list(target.LENS_AGENTS)),
            check_runs=_green_checks(),
            changed_files=NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, [], apply=False)

        assert code == 1


@pytest.mark.asyncio
class TestEmptyRequiredLensSetAlwaysRefuses:
    """The hard, independent guard in `run()`: whatever the reason
    `resolve_lenses` ever returns an empty list, `run()` must refuse before
    any lens/check evaluation runs — this must not depend on
    `_validate_panel_max` or the always-on union existing at all."""

    async def test_run_refuses_when_resolve_lenses_returns_empty(self, monkeypatch):
        client = _FakeClient(comments=[], check_runs=_green_checks())
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        async def _empty_resolve_lenses(client_, *, repo, pr, pr_body, extra_lenses):
            return [], []

        monkeypatch.setattr(target, "resolve_lenses", _empty_resolve_lenses)

        code = await target.run(REPO, PR, [], apply=True)

        assert code == 1
        assert client.posted == []

    async def test_empty_lens_set_refusal_precedes_any_lens_evaluation(self, monkeypatch):
        """The refusal must happen BEFORE evaluate_lens is ever called —
        proves this is a hard gate, not a side effect of an empty loop
        happening to produce an empty outcomes list."""
        client = _FakeClient(comments=[], check_runs=_green_checks())
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        evaluate_lens_called = False
        real_evaluate_lens = target.evaluate_lens

        async def _tracking_evaluate_lens(*a, **kw):
            nonlocal evaluate_lens_called
            evaluate_lens_called = True
            return await real_evaluate_lens(*a, **kw)

        monkeypatch.setattr(target, "evaluate_lens", _tracking_evaluate_lens)

        async def _empty_resolve_lenses(client_, *, repo, pr, pr_body, extra_lenses):
            return [], []

        monkeypatch.setattr(target, "resolve_lenses", _empty_resolve_lenses)

        code = await target.run(REPO, PR, [], apply=True)

        assert code == 1
        assert evaluate_lens_called is False


@pytest.mark.asyncio
class TestOtherEmptyOrShrunkLensPaths:
    """Additional paths that could shrink or empty the lens list, per the
    operator's request to scan beyond APIS_PANEL_MAX specifically."""

    async def test_zero_changed_files_still_derives_the_always_on_floor(self, monkeypatch):
        """A PR with zero changed files (e.g. an empty commit, or a diff
        GitHub reports as having no files) must not derive an empty panel —
        select_panel still selects always-on lenses when changed_files=[]."""
        client = _FakeClient(
            comments=_all_clear_comments(["pm", "qa"]),
            check_runs=_green_checks(),
            changed_files=[],
        )
        _install_client(monkeypatch, client)

        lenses, required = await target.resolve_lenses(
            client, repo=REPO, pr=PR, pr_body="Closes #7", extra_lenses=[]
        )

        assert sorted(lenses) == ["pm", "qa"]
        assert sorted(r.lens for r in required) == ["pm", "qa"]

    async def test_select_panel_raising_propagates_as_a_refusal_not_a_silent_empty_panel(
        self, monkeypatch
    ):
        """If `select_panel` itself raises (a future signature change, a bad
        input it does not tolerate), `derive_required_lenses` must propagate
        that as a failure — never catch it and silently substitute an empty
        or partial panel."""
        client = _FakeClient(
            comments=_all_clear_comments(["pm", "qa"]),
            check_runs=_green_checks(),
            changed_files=NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        def _raising_select_panel(*a, **kw):
            raise RuntimeError("simulated select_panel failure")

        monkeypatch.setattr(target, "select_panel", _raising_select_panel)

        code = await target.run(REPO, PR, [], apply=True)

        assert code == 1
        assert client.posted == []

class TestAlwaysOnUnionIsDefenseInDepth:
    def test_always_on_union_survives_even_if_select_panel_returns_empty(self):
        """Defense in depth, independent of `_validate_panel_max`: even a
        `select_panel` call that returns `[]` for some reason other than
        `max_panel` (a future behaviour change) must not leave
        `derive_required_lenses`'s result empty — the always-on floor is
        unioned in regardless of what `select_panel` returned."""
        always = target._always_required_lenses()
        by_name: dict = {lens.lens: lens for lens in always}
        # Simulate select_panel returning nothing extra.
        for lens in ():  # empty "panel" from select_panel
            by_name.setdefault(lens.lens, lens)
        assert {lens.lens for lens in by_name.values()} == {lens.lens for lens in always}
        assert by_name  # never empty


class TestLensAgentsRegistry:
    def test_lens_agents_match_review_panel_registry(self):
        """Kept in lock-step with review_panel.LENSES per CLAUDE.md's
        operator-specific-config rule: this dict must never silently drift
        from the single source of truth for lens->agent."""
        registry = {lens.lens: lens.agent for lens in LENSES}
        for lens, agent in target.LENS_AGENTS.items():
            assert registry.get(lens) == agent, f"{lens}: {agent} != {registry.get(lens)}"


# ── Unschedulable non-required checks: "not run (no runner)" ───────────────
#
# Operator ruling 2026-09-25: until a self-hosted runner exists for a check, a
# check that branch protection does NOT require and that no online runner can
# schedule is reported as "not run (no runner)" instead of failing the gate.
# The ruling's fail-closed bounds are each pinned below. The two positive
# tests (not-run passes) are red on origin/main, where every queued check
# blocks; the negative tests pin behaviour main already had and that this
# change must not weaken.

INVENTORY = "canonical rule inventory"
INVENTORY_JOB_ID = 108014743705
INVENTORY_LABELS = ["self-hosted", "macOS", "canonical-rule-inventory"]
NOT_RUN = "not run (no runner)"


def _inventory_run(*, status: str = "queued", conclusion: str | None = None) -> dict:
    return {"id": INVENTORY_JOB_ID, "name": INVENTORY, "status": status, "conclusion": conclusion}


def _job(labels: list[str], *, status: str = "queued") -> dict[int, dict]:
    return {INVENTORY_JOB_ID: {"id": INVENTORY_JOB_ID, "status": status, "labels": labels}}


def _runner(labels: list[str], *, status: str = "online") -> dict:
    return {"id": 1, "name": "r1", "status": status, "labels": [{"name": n} for n in labels]}


def _required_gitleaks_run() -> dict:
    return {"id": 1, "name": REQUIRED_CHECK, "status": "completed", "conclusion": "success"}


def _inventory_client(**overrides) -> _FakeClient:
    kwargs = dict(
        comments=_all_clear_comments(ALL_FOUR),
        check_runs=[_required_gitleaks_run(), _inventory_run()],
        jobs=_job(INVENTORY_LABELS),
        runners=[],
    )
    kwargs.update(overrides)
    return _FakeClient(**kwargs)


@pytest.mark.asyncio
class TestUnschedulableNonRequiredCheckIsNotRun:
    async def test_not_required_queued_no_matching_runner_is_not_run_and_gate_passes(
        self, monkeypatch, capsys
    ):
        # Runner list readable: one online runner with other labels, and one
        # runner WITH the labels but offline — neither can schedule the job.
        client = _inventory_client(
            runners=[
                _runner(["self-hosted", "Linux", "X64"]),
                _runner(INVENTORY_LABELS, status="offline"),
            ]
        )
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, ALL_FOUR, apply=False)
        out = capsys.readouterr().out

        assert code == 0
        assert NOT_RUN in out
        assert "no online runner has all of its labels" in out
        assert "checks: GREEN" in out

    async def test_apply_names_the_not_run_check_in_the_approval_body(self, monkeypatch):
        client = _inventory_client()
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 0
        assert len(client.posted) == 1
        body = client.posted[0]["json"]["body"]
        lines = [ln for ln in body.splitlines() if INVENTORY in ln]
        assert len(lines) == 1
        assert NOT_RUN in lines[0]
        assert "not required by branch protection on `main`" in lines[0]

    async def test_runner_list_unreadable_and_allowlisted_is_not_run(self, monkeypatch, capsys):
        client = _inventory_client(runners=None)  # 403, as for a non-admin token
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, ALL_FOUR, apply=False)
        out = capsys.readouterr().out

        assert code == 0
        assert NOT_RUN in out
        assert "known-unprovisioned allowlist" in out

    async def test_readable_protection_endpoint_is_honoured(self, monkeypatch, capsys):
        client = _inventory_client(
            protection_rsc={"contexts": [REQUIRED_CHECK], "checks": []},
            branch_payload=500,  # must not be consulted once the protection API answered
        )
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, ALL_FOUR, apply=False)

        assert code == 0
        assert NOT_RUN in capsys.readouterr().out


@pytest.mark.asyncio
class TestUnschedulableCheckFailClosedBounds:
    async def _assert_blocks(self, monkeypatch, capsys, client: _FakeClient) -> None:
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)
        code = await target.run(REPO, PR, ALL_FOUR, apply=True)
        out = capsys.readouterr().out
        assert code == 1
        assert client.posted == []
        assert NOT_RUN not in out

    async def test_required_queued_check_blocks(self, monkeypatch, capsys):
        branch = {
            "protected": True,
            "protection": {
                "enabled": True,
                "required_status_checks": {"contexts": [REQUIRED_CHECK, INVENTORY]},
            },
        }
        await self._assert_blocks(monkeypatch, capsys, _inventory_client(branch_payload=branch))

    async def test_check_required_by_a_ruleset_blocks(self, monkeypatch, capsys):
        rules = [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": INVENTORY}]},
            }
        ]
        await self._assert_blocks(monkeypatch, capsys, _inventory_client(rules=rules))

    async def test_matching_online_runner_means_pending_blocks(self, monkeypatch, capsys):
        # Superset of the job's labels, online: the job CAN be scheduled.
        runners = [_runner(["self-hosted", "macOS", "ARM64", "canonical-rule-inventory"])]
        await self._assert_blocks(monkeypatch, capsys, _inventory_client(runners=runners))

    async def test_failure_conclusion_always_blocks(self, monkeypatch, capsys):
        client = _inventory_client(
            check_runs=[
                _required_gitleaks_run(),
                _inventory_run(status="completed", conclusion="failure"),
            ],
            runners=None,
        )
        await self._assert_blocks(monkeypatch, capsys, client)

    async def test_runner_list_unreadable_and_not_allowlisted_blocks(self, monkeypatch, capsys):
        client = _inventory_client(jobs=_job(["self-hosted", "Linux", "gpu-box"]), runners=None)
        await self._assert_blocks(monkeypatch, capsys, client)

    async def test_protection_read_failure_blocks(self, monkeypatch, capsys):
        # Protection API 404 (no admin) AND branch summary unreadable.
        await self._assert_blocks(monkeypatch, capsys, _inventory_client(branch_payload=500))

    async def test_protected_branch_with_redacted_protection_blocks(self, monkeypatch, capsys):
        client = _inventory_client(branch_payload={"protected": True})
        await self._assert_blocks(monkeypatch, capsys, client)

    async def test_ruleset_read_failure_blocks(self, monkeypatch, capsys):
        await self._assert_blocks(monkeypatch, capsys, _inventory_client(rules=403))

    async def test_github_hosted_labels_never_qualify(self, monkeypatch, capsys):
        for labels in (["ubuntu-latest"], ["self-hosted", "ubuntu-24.04"]):
            client = _inventory_client(jobs=_job(labels))
            await self._assert_blocks(monkeypatch, capsys, client)

    async def test_in_progress_check_never_qualifies(self, monkeypatch, capsys):
        client = _inventory_client(
            check_runs=[_required_gitleaks_run(), _inventory_run(status="in_progress")]
        )
        await self._assert_blocks(monkeypatch, capsys, client)

    async def test_unreadable_job_blocks(self, monkeypatch, capsys):
        await self._assert_blocks(monkeypatch, capsys, _inventory_client(jobs={}))

    async def test_removing_the_allowlist_entry_restores_enforcement(self, monkeypatch, capsys):
        monkeypatch.setattr(target, "KNOWN_UNPROVISIONED_RUNNER_LABEL_SETS", frozenset())
        await self._assert_blocks(monkeypatch, capsys, _inventory_client(runners=None))

    async def test_every_check_not_run_is_no_signal_and_blocks(self, monkeypatch, capsys):
        client = _inventory_client(check_runs=[_inventory_run()], legacy_status_total_count=0)
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)
        code = await target.run(REPO, PR, ALL_FOUR, apply=True)
        assert code == 1
        assert client.posted == []


class TestAllowlistContents:
    def test_allowlist_holds_only_the_inventory_label_set(self):
        assert target.KNOWN_UNPROVISIONED_RUNNER_LABEL_SETS == frozenset(
            {frozenset(lbl.casefold() for lbl in INVENTORY_LABELS)}
        )

    def test_allowlist_matches_the_workflow_runs_on(self):
        workflow = (_REPO_ROOT / ".github" / "workflows" / "canonical-rule-inventory.yml").read_text()
        assert "runs-on: [self-hosted, macOS, canonical-rule-inventory]" in workflow


@pytest.mark.asyncio
class TestNoSchedulingReadsWhenNothingIsPending:
    async def test_all_completed_head_makes_no_protection_or_runner_calls(self, monkeypatch):
        client = _FakeClient(comments=_all_clear_comments(ALL_FOUR), check_runs=_green_checks())
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, ALL_FOUR, apply=False)

        assert code == 0
        assert not [
            u
            for u in client.get_urls
            if "/branches/" in u or "/actions/" in u
        ]

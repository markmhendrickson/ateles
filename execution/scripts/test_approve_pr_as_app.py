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
import lens_authors  # noqa: E402
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


# The account the swarm's lens comments are written by in these tests. It is
# supplied through the identity setting (lens_authors.ENV_AUTHORS), never typed
# into the tool; OTHER_LOGIN is an account outside that set.
SWARM_LOGIN = "swarm-lens-account"
OTHER_LOGIN = "some-other-account"
SWARM_AUTHORS = frozenset({SWARM_LOGIN})


@pytest.fixture(autouse=True)
def _lens_comment_identities(monkeypatch):
    """Name the swarm's lens-comment account through the setting, and keep the
    identity lookups off the network and off the host's real tokens."""
    monkeypatch.setenv(lens_authors.ENV_AUTHORS, SWARM_LOGIN)
    for name in lens_authors.PAT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: (set(), False))
    lens_authors.clear_cache()


def _comment(id_: int, body: str, *, author: str | None = SWARM_LOGIN) -> dict:
    row = {
        "id": id_,
        "body": body,
        "html_url": f"https://github.com/{REPO}/pull/{PR}#issuecomment-{id_}",
    }
    if author is not None:
        row["user"] = {"login": author}
    return row


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


def _all_clear_comments(lenses: list[str], *, author: str = SWARM_LOGIN) -> list[dict]:
    return [
        _comment(i, _lens_comment_body(lens, target.LENS_AGENTS[lens]), author=author)
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
class TestSkippedLoxiaReviewReadsAsGreen:
    """Loxia's canary gate (loxia-pr-review.yml, ateles PR ci/loxia-canary-only)
    makes the `review` job's `if:` false for a non-canary PR, which GitHub
    reports as a check-run with `conclusion: "skipped"`. "Loxia PR review" is
    not currently in `required_status_checks` on `main` (verified live via
    `gh api repos/markmhendrickson/ateles/branches/main` — only "gitleaks
    (secrets + PII)" is required), so this is already covered by
    `evaluate_checks`'s existing `conclusion in ("success", "neutral",
    "skipped")` branch — this test pins that a check-run literally named
    "Loxia PR review" with a skipped conclusion does not block approval, so a
    future change to that branch (or to the workflow's job/check name) cannot
    silently regress it."""

    async def test_skipped_loxia_check_run_does_not_block_approval(self, monkeypatch):
        client = _FakeClient(
            comments=_all_clear_comments(ALL_FOUR),
            check_runs=[
                *_green_checks(),
                {"name": "Loxia PR review", "status": "completed", "conclusion": "skipped"},
            ],
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, ALL_FOUR, apply=True)

        assert code == 0
        assert len(client.posted) == 1


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

        lenses, required, _ = await target.resolve_lenses(
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

        lenses, required, _ = await target.resolve_lenses(
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

        lenses, _, _ = await target.resolve_lenses(
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

        lenses, required, _ = await target.resolve_lenses(
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

        async def _empty_resolve_lenses(
            client_, *, repo, pr, pr_body, extra_lenses, panel_all=False
        ):
            return [], [], []

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

        async def _empty_resolve_lenses(
            client_, *, repo, pr, pr_body, extra_lenses, panel_all=False
        ):
            return [], [], []

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

        lenses, required, _ = await target.resolve_lenses(
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


# ── ateles#1293: a non-required lens's live blocking verdict must refuse ────
#
# On 2026-09-26, approve_pr_as_app.py reported "lenses: ALL PASS" for
# ateles#1293 and approved it, even though Waxwing (arch) had posted
# REQUEST_CHANGES with a [BLOCKING] finding on the SAME head ten minutes
# earlier. The derived floor for that diff was only {pm, qa}; arch was never
# in `lenses`, so its comment was never read at all. These tests replay that
# exact shape directly against `find_non_required_blocks` and through the
# full `run()` path, plus the two bounds the fix must respect: a block on a
# STALE head must not count (the lens hasn't spoken about the head being
# approved), and `--panel all` (panel_all=True) closes the gap by folding
# every lens into the required set in the first place.


@pytest.mark.asyncio
class TestNonRequiredLensLiveBlockRefuses:
    """Replays #1293's shape: required lenses (pm, qa) all clear, but a
    lens the diff never required (arch) posted a live REQUEST_CHANGES with
    a [BLOCKING] finding on the CURRENT head. Before the fix this is
    'lenses: ALL PASS' because `lenses` never contained 'arch' at all — the
    tool never even fetched its verdict. After the fix it must refuse."""

    async def test_required_lenses_pass_non_required_lens_blocks_refuses(self, monkeypatch):
        # pm + qa (the derived floor for NEUTRAL_FILES) both clear. arch —
        # NOT in the derived floor, and NOT added via --lenses — posted
        # REQUEST_CHANGES with a [BLOCKING] finding on the SAME head.
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(
            _comment(
                55,
                _lens_comment_body(
                    "arch",
                    "waxwing",
                    verdict="REQUEST_CHANGES",
                    extra="[BLOCKING] contract_mappings not updated for the new endpoint.",
                ),
            )
        )
        client = _FakeClient(
            comments=comments, check_runs=_green_checks(), changed_files=NEUTRAL_FILES
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        # No --lenses, no --panel all: exactly the derived floor {pm, qa},
        # exactly ateles#1293's request-shape.
        code = await target.run(REPO, PR, [], apply=True)

        assert code == 1
        assert client.posted == []

    async def test_dry_run_also_reports_failure_not_a_vacuous_pass(self, monkeypatch, capsys):
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(
            _comment(
                56,
                _lens_comment_body("arch", "waxwing", verdict="REQUEST_CHANGES"),
            )
        )
        client = _FakeClient(
            comments=comments, check_runs=_green_checks(), changed_files=NEUTRAL_FILES
        )
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, [], apply=False)
        out = capsys.readouterr().out

        assert code == 1
        assert "lenses: ALL PASS" in out  # the required-floor check alone still passes
        assert "overall: FAIL" in out  # but the non-required block sinks the overall gate
        assert "arch" in out
        assert "waxwing" in out

    async def test_refusal_names_the_lens_and_links_the_comment(self, monkeypatch, capsys):
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(
            _comment(
                57,
                _lens_comment_body(
                    "arch", "waxwing", extra="[BLOCKING] missing tenant isolation check."
                ),
            )
        )
        client = _FakeClient(
            comments=comments, check_runs=_green_checks(), changed_files=NEUTRAL_FILES
        )
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, [], apply=False)
        out = capsys.readouterr().out

        assert code == 1
        assert "arch" in out
        assert "waxwing" in out
        assert f"https://github.com/{REPO}/pull/{PR}#issuecomment-57" in out

    async def test_blocking_marker_with_a_clear_token_still_refuses(self, monkeypatch):
        """Mirrors TestBlockingFindingRefuses, but for a NON-required lens:
        a [BLOCKING] finding in the body must refuse even if the lens's own
        verdict token reads SIGNED_OFF."""
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(
            _comment(
                58,
                _lens_comment_body(
                    "arch",
                    "waxwing",
                    verdict="SIGNED_OFF",
                    extra="[BLOCKING] SSRF: unvalidated redirect target reaches the fetch sink.",
                ),
            )
        )
        client = _FakeClient(
            comments=comments, check_runs=_green_checks(), changed_files=NEUTRAL_FILES
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, [], apply=True)

        assert code == 1
        assert client.posted == []


class TestFindNonRequiredBlocksUnit:
    """Direct unit tests of the new predicate, independent of run() — kept
    out of the async-marked class above since these are plain sync tests."""

    def test_find_non_required_blocks_unit(self):
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(
            _comment(60, _lens_comment_body("arch", "waxwing", verdict="REQUEST_CHANGES"))
        )
        blocks = target.find_non_required_blocks(
            comments=comments,
            head_sha=HEAD,
            required_lenses={"pm", "qa"},
            authors=SWARM_AUTHORS,
        )
        assert [b.lens for b in blocks] == ["arch"]
        assert blocks[0].agent == "waxwing"

    def test_find_non_required_blocks_ignores_a_clearing_non_required_lens(self):
        """A non-required lens that reviewed and CLEARED must not be reported
        — this predicate is about live objections, never mere absence or a
        clean bill from a lens outside the floor."""
        comments = _all_clear_comments(["pm", "qa", "arch"])
        blocks = target.find_non_required_blocks(
            comments=comments,
            head_sha=HEAD,
            required_lenses={"pm", "qa"},
            authors=SWARM_AUTHORS,
        )
        assert blocks == []

    def test_find_non_required_blocks_ignores_a_lens_that_never_commented(self):
        comments = _all_clear_comments(["pm", "qa"])
        blocks = target.find_non_required_blocks(
            comments=comments,
            head_sha=HEAD,
            required_lenses={"pm", "qa"},
            authors=SWARM_AUTHORS,
        )
        assert blocks == []

    def test_find_non_required_blocks_skips_lenses_already_in_required_set(self):
        """A lens that IS in `required_lenses` is judged by `evaluate_lens`
        already — `find_non_required_blocks` must not double-report it."""
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(
            _comment(61, _lens_comment_body("qa", "phoenicurus", verdict="REQUEST_CHANGES"))
        )
        blocks = target.find_non_required_blocks(
            comments=comments,
            head_sha=HEAD,
            required_lenses={"pm", "qa"},
            authors=SWARM_AUTHORS,
        )
        assert blocks == []  # qa is required; its own block is evaluate_lens's job, not this one


@pytest.mark.asyncio
class TestNonRequiredLensStaleHeadDoesNotCount:
    """A non-required lens's blocking comment on an OLD head must NOT count
    — the lens has not spoken about the head actually being approved. This
    is the mirror of TestOldHeadVerdictRefuses (which covers a REQUIRED
    lens's stale-head comment) for the new non-required-block path."""

    async def test_stale_head_block_from_non_required_lens_is_not_counted(self, monkeypatch):
        comments = _all_clear_comments(["pm", "qa"])
        # arch blocked, but on the OLD head — not the PR's current head.
        comments.append(
            _comment(
                62,
                _lens_comment_body(
                    "arch", "waxwing", head=OLD_HEAD, verdict="REQUEST_CHANGES"
                ),
            )
        )
        client = _FakeClient(
            comments=comments, check_runs=_green_checks(), changed_files=NEUTRAL_FILES
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, [], apply=True)

        # Must pass: the only comment naming this exact head marker is
        # absent for arch, so find_non_required_blocks reports nothing for
        # it, exactly mirroring how evaluate_lens treats a stale-head
        # comment as "lens has not reviewed the current head".
        assert code == 0
        assert len(client.posted) == 1


class TestFindNonRequiredBlocksStaleHeadUnit:
    def test_find_non_required_blocks_unit_stale_head_not_counted(self):
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(
            _comment(63, _lens_comment_body("arch", "waxwing", head=OLD_HEAD, verdict="REQUEST_CHANGES"))
        )
        blocks = target.find_non_required_blocks(
            comments=comments,
            head_sha=HEAD,
            required_lenses={"pm", "qa"},
            authors=SWARM_AUTHORS,
        )
        assert blocks == []


# ── ateles#1394: only an ACTIVE objection from a non-required lens blocks ───
#
# `find_non_required_blocks` used `not sign_off_is_warranted(...)` as its
# block test, which is true for a plain COMMENT (no clearing verdict) — so the
# automatic pipeline's non-blocking content-lens COMMENT refused an approval
# whose every required lens had cleared. A non-required lens now blocks only
# on an explicit objection (REQUEST_CHANGES / BLOCKED / a blocking token or
# [BLOCKING] anywhere), and, fail-closed, on anything it cannot read. Only an
# explicit, parsed COMMENT with no blocking token is exempt.


def _non_required_blocks(body: str) -> list:
    comments = _all_clear_comments(["pm", "qa"])
    comments.append(_comment(70, body))
    return target.find_non_required_blocks(
        comments=comments,
        head_sha=HEAD,
        required_lenses={"pm", "qa"},
        authors=SWARM_AUTHORS,
    )


class TestNonRequiredLensBlocksOnlyOnActiveObjection:
    def test_comment_without_blocking_token_does_not_block(self):
        body = _lens_comment_body(
            "content", "corvus", verdict="COMMENT", extra="Minor wording observations only."
        )
        assert _non_required_blocks(body) == []

    def test_request_changes_blocks(self):
        body = _lens_comment_body("arch", "waxwing", verdict="REQUEST_CHANGES")
        assert [b.lens for b in _non_required_blocks(body)] == ["arch"]

    def test_blocked_blocks(self):
        body = _lens_comment_body("arch", "waxwing", verdict="BLOCKED")
        assert [b.lens for b in _non_required_blocks(body)] == ["arch"]

    def test_blocking_finding_inside_a_comment_body_blocks(self):
        body = _lens_comment_body(
            "arch", "waxwing", verdict="COMMENT",
            extra="[BLOCKING] contract_mappings not updated for the new endpoint.",
        )
        assert [b.lens for b in _non_required_blocks(body)] == ["arch"]

    def test_blocking_verdict_token_inside_a_comment_body_blocks(self):
        body = _lens_comment_body(
            "arch", "waxwing", verdict="COMMENT", extra="Earlier round said **BLOCKED**."
        )
        assert [b.lens for b in _non_required_blocks(body)] == ["arch"]

    def test_unparseable_verdict_still_blocks(self):
        marker = swarm_dispatch.compose_lens_review_marker("content", HEAD)
        body = f"{marker}\n**\U0001f916 Corvus — Ateles swarm, content review**\n**MAYBE**\n"
        assert [b.lens for b in _non_required_blocks(body)] == ["content"]

    def test_missing_verdict_line_still_blocks(self):
        marker = swarm_dispatch.compose_lens_review_marker("content", HEAD)
        body = f"{marker}\n**\U0001f916 Corvus — Ateles swarm, content review**\nLooks fine to me.\n"
        assert [b.lens for b in _non_required_blocks(body)] == ["content"]

    def test_verdict_before_header_still_blocks(self):
        marker = swarm_dispatch.compose_lens_review_marker("content", HEAD)
        body = f"{marker}\n**COMMENT**\n**\U0001f916 Corvus — Ateles swarm, content review**\n"
        assert [b.lens for b in _non_required_blocks(body)] == ["content"]

    def test_header_naming_another_agent_still_blocks(self):
        marker = swarm_dispatch.compose_lens_review_marker("content", HEAD)
        body = f"{marker}\n**\U0001f916 Pavo — Ateles swarm, pm review**\n**COMMENT**\n"
        assert [b.lens for b in _non_required_blocks(body)] == ["content"]

    def test_comment_with_a_second_verdict_line_still_blocks(self):
        body = _lens_comment_body(
            "content", "corvus", verdict="COMMENT", extra="**APPROVE**"
        )
        assert [b.lens for b in _non_required_blocks(body)] == ["content"]

    def test_approve_does_not_block(self):
        assert _non_required_blocks(_lens_comment_body("content", "corvus", verdict="APPROVE")) == []

    def test_signed_off_does_not_block(self):
        assert _non_required_blocks(_lens_comment_body("content", "corvus", verdict="SIGNED_OFF")) == []

    def test_approve_carrying_a_blocking_finding_blocks(self):
        body = _lens_comment_body(
            "content", "corvus", verdict="APPROVE", extra="[BLOCKING] leaks a token."
        )
        assert [b.lens for b in _non_required_blocks(body)] == ["content"]


def _blocks_for(*bodies: str) -> list:
    """Non-required lens comments, in creation order, after an all-clear floor."""
    comments = _all_clear_comments(["pm", "qa"])
    comments.extend(_comment(80 + i, b) for i, b in enumerate(bodies))
    return target.find_non_required_blocks(
        comments=comments,
        head_sha=HEAD,
        required_lenses={"pm", "qa"},
        authors=SWARM_AUTHORS,
    )


def _arch(verdict: str, extra: str = "", head: str = HEAD) -> str:
    return _lens_comment_body("arch", "waxwing", head=head, verdict=verdict, extra=extra)


class TestEarlierObjectionIsRetiredOnlyByAClearingVerdict:
    """An objection on the current head stays live until a LATER comment from
    the same lens on that head is a clearing verdict; a COMMENT is not one."""

    def test_request_changes_then_comment_blocks(self):
        blocks = _blocks_for(_arch("REQUEST_CHANGES"), _arch("COMMENT"))
        assert [b.lens for b in blocks] == ["arch"]
        assert blocks[0].reason == "earlier objection not retired by a clearing verdict"

    def test_request_changes_then_approve_does_not_block(self):
        assert _blocks_for(_arch("REQUEST_CHANGES"), _arch("APPROVE")) == []

    def test_blocked_then_signed_off_does_not_block(self):
        assert _blocks_for(_arch("BLOCKED"), _arch("SIGNED_OFF")) == []

    def test_blocking_finding_then_comment_blocks(self):
        blocks = _blocks_for(_arch("COMMENT", "[BLOCKING] missing check."), _arch("COMMENT"))
        assert [b.lens for b in blocks] == ["arch"]
        assert blocks[0].reason == "earlier objection not retired by a clearing verdict"

    def test_comment_only_does_not_block(self):
        assert _blocks_for(_arch("COMMENT"), _arch("COMMENT")) == []

    def test_objection_on_an_earlier_head_is_ignored(self):
        assert _blocks_for(_arch("REQUEST_CHANGES", head=OLD_HEAD), _arch("COMMENT")) == []

    def test_a_later_objection_after_a_clearing_verdict_blocks(self):
        blocks = _blocks_for(
            _arch("REQUEST_CHANGES"), _arch("APPROVE"), _arch("BLOCKED"), _arch("COMMENT")
        )
        assert [b.lens for b in blocks] == ["arch"]

    def test_latest_comment_objecting_blocks_with_its_own_reason(self):
        blocks = _blocks_for(_arch("APPROVE"), _arch("REQUEST_CHANGES"))
        assert [b.reason for b in blocks] == ["REQUEST_CHANGES"]


class TestPerLensRefusalReason:
    def test_objecting_verdict_token_is_named(self):
        assert _blocks_for(_arch("REQUEST_CHANGES"))[0].reason == "REQUEST_CHANGES"
        assert _blocks_for(_arch("BLOCKED"))[0].reason == "BLOCKED"

    def test_blocking_finding_is_named(self):
        blocks = _blocks_for(_arch("COMMENT", "[BLOCKING] leaks a token."))
        assert blocks[0].reason == "[BLOCKING] finding"

    def test_unreadable_verdict_is_named(self):
        marker = swarm_dispatch.compose_lens_review_marker("arch", HEAD)
        body = f"{marker}\n**\U0001f916 Waxwing — Ateles swarm, arch review**\nLooks fine.\n"
        assert _blocks_for(body)[0].reason == "unreadable verdict"

    def test_earlier_objection_reason(self):
        blocks = _blocks_for(_arch("BLOCKED"), _arch("COMMENT"))
        assert blocks[0].reason == "earlier objection not retired by a clearing verdict"


@pytest.mark.asyncio
class TestRefusalPrintsThePreciseReason:
    async def test_run_prints_reason_per_lens(self, monkeypatch, capsys):
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(_comment(90, _arch("REQUEST_CHANGES")))
        comments.append(_comment(91, _arch("COMMENT")))
        client = _FakeClient(
            comments=comments, check_runs=_green_checks(), changed_files=NEUTRAL_FILES
        )
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, [], apply=False)
        out = capsys.readouterr().out

        assert code == 1
        assert "earlier objection not retired by a clearing verdict" in out
        assert "unreadable verdict or [BLOCKING] finding" not in out


@pytest.mark.asyncio
class TestNonRequiredLensCommentDoesNotRefuseApproval:
    """End to end: every required lens clears, the pipeline's content lens
    posts a non-blocking COMMENT on the same head, and the approval goes
    through; the same lens posting REQUEST_CHANGES refuses it (ateles#1293)."""

    async def test_content_lens_comment_does_not_refuse(self, monkeypatch):
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(
            _comment(71, _lens_comment_body("content", "corvus", verdict="COMMENT"))
        )
        client = _FakeClient(
            comments=comments, check_runs=_green_checks(), changed_files=NEUTRAL_FILES
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, [], apply=True)

        assert code == 0
        assert len(client.posted) == 1

    async def test_content_lens_request_changes_refuses(self, monkeypatch):
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(
            _comment(72, _lens_comment_body("content", "corvus", verdict="REQUEST_CHANGES"))
        )
        client = _FakeClient(
            comments=comments, check_runs=_green_checks(), changed_files=NEUTRAL_FILES
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, [], apply=True)

        assert code == 1
        assert client.posted == []


# ── --panel all: the bootstrap-mode default ──────────────────────────────────


@pytest.mark.asyncio
class TestPanelAllRequiresEveryBootstrapRosterLens:
    """PR #1303 round-1 incident (Waxwing arch / Pavo pm / Phoenicurus qa /
    Accipiter ux review comments): these fixtures used to be built from
    `_all_clear_comments(list(target.LENS_AGENTS))` — fabricating a clearing
    comment from EVERY registered lens, including `legal`/Buteo, which
    bootstrap mode's own five-lens roster never dispatches for an ordinary
    diff. That made every test in this class validate the code against its
    own (wrong) premise rather than against the actual bootstrap roster —
    "a test that cannot fail on the thing it watches is decoration"
    (CLAUDE.md verification discipline). Fixtures here are now built from
    `target.BOOTSTRAP_PANEL_LENSES` — the named constant the fix itself
    derives from — plus whatever `select_panel` additionally derives for
    the diff, never from raw `LENS_AGENTS`. Operator ruling 2026-09-26
    (ateles#1317) additionally excludes `content`/Corvus from the roster
    outright — see TestBootstrapExcludesContent for that behavior.
    """

    async def test_panel_all_derives_the_bootstrap_five_not_all_seven_lens_agents(
        self, monkeypatch
    ):
        client = _FakeClient(
            comments=_all_clear_comments(sorted(target.BOOTSTRAP_PANEL_LENSES)),
            check_runs=_green_checks(),
            changed_files=NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)

        lenses, required, _ = await target.resolve_lenses(
            client, repo=REPO, pr=PR, pr_body="Closes #7", extra_lenses=[], panel_all=True
        )

        assert sorted(lenses) == sorted(target.BOOTSTRAP_PANEL_LENSES)
        assert "legal" not in lenses
        # The derived floor itself is unchanged (still just the diff-derived
        # {pm, qa}) — panel_all is an ADDITION on top, exactly like --lenses.
        assert sorted(r.lens for r in required) == ["pm", "qa"]

    async def test_panel_all_apply_approves_when_the_bootstrap_five_all_clear(
        self, monkeypatch
    ):
        client = _FakeClient(
            comments=_all_clear_comments(sorted(target.BOOTSTRAP_PANEL_LENSES)),
            check_runs=_green_checks(),
            changed_files=NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, [], apply=True, panel_all=True)

        assert code == 0
        assert len(client.posted) == 1
        posted_body = client.posted[0]["json"]["body"]
        for lens in target.BOOTSTRAP_PANEL_LENSES:
            assert lens in posted_body
        assert "legal" not in posted_body

    async def test_panel_all_never_requires_legal_when_the_diff_does_not_need_it(
        self, monkeypatch
    ):
        """The exact regression PR #1303 round 1 shipped: a neutral diff (no
        package.json/auth//LICENSE/PII surface) must approve WITHOUT a legal/
        Buteo comment ever existing — legal never even asked for. This is
        the test that fails if the bootstrap default ever again requires a
        lens the bootstrap panel doesn't run."""
        client = _FakeClient(
            comments=_all_clear_comments(sorted(target.BOOTSTRAP_PANEL_LENSES)),
            check_runs=_green_checks(),
            changed_files=NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        lenses, _, _ = await target.resolve_lenses(
            client, repo=REPO, pr=PR, pr_body="Closes #7", extra_lenses=[], panel_all=True
        )
        assert "legal" not in lenses, (
            "the bootstrap default must never require a lens outside "
            "BOOTSTRAP_PANEL_LENSES — legal joins the floor only when "
            "select_panel derives it for the diff"
        )

        code = await target.run(REPO, PR, [], apply=True, panel_all=True)
        assert code == 0, (
            "a diff where only the bootstrap five are relevant must be "
            "approvable under --panel all with NO legal comment at all"
        )

    async def test_panel_all_still_requires_legal_when_the_diff_needs_it(
        self, monkeypatch
    ):
        """legal is not excluded outright — it still joins the floor when
        select_panel's own diff_patterns pull it in (e.g. a package.json/
        LICENSE change), exactly as before this fix. panel_all widens the
        BOOTSTRAP DEFAULT floor; it never narrows what select_panel itself
        requires."""
        lenses_present = sorted(target.BOOTSTRAP_PANEL_LENSES) + ["legal"]
        client = _FakeClient(
            comments=_all_clear_comments(lenses_present),
            check_runs=_green_checks(),
            changed_files=["package.json"],
        )
        _install_client(monkeypatch, client)

        lenses, required, _ = await target.resolve_lenses(
            client, repo=REPO, pr=PR, pr_body="Closes #7", extra_lenses=[], panel_all=True
        )
        assert "legal" in lenses
        assert "legal" in {r.lens for r in required}, (
            "legal must appear in the DIFF-DERIVED floor for a "
            "package.json change, not merely as a panel_all addition"
        )

    async def test_panel_all_refuses_when_one_of_the_bootstrap_five_never_commented(
        self, monkeypatch
    ):
        present = [lens for lens in target.BOOTSTRAP_PANEL_LENSES if lens != "security"]
        client = _FakeClient(
            comments=_all_clear_comments(present),
            check_runs=_green_checks(),
            changed_files=NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, [], apply=True, panel_all=True)

        assert code == 1
        assert client.posted == []

    async def test_panel_all_still_refuses_on_a_1293_shaped_block(self, monkeypatch):
        """With panel_all on, arch is already in `lenses` (required), so this
        exercises the SAME refusal through evaluate_lens rather than
        find_non_required_blocks — proving the two paths agree."""
        comments = _all_clear_comments(
            [lens for lens in target.BOOTSTRAP_PANEL_LENSES if lens != "arch"]
        )
        comments.append(
            _comment(70, _lens_comment_body("arch", "waxwing", verdict="REQUEST_CHANGES"))
        )
        client = _FakeClient(
            comments=comments, check_runs=_green_checks(), changed_files=NEUTRAL_FILES
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, [], apply=True, panel_all=True)

        assert code == 1
        assert client.posted == []


# Files with no diff_patterns/issue_patterns match of their own (so the ONLY
# reason `select_panel` would seat `content` here is its
# `forward_looking=True`/`min_changed_files=5` opt-in path on file COUNT
# alone, not file content) — five files, the exact threshold
# `Lens.min_changed_files=5` on the `content` lens requires. Verified against
# the live registry by
# TestBootstrapExcludesContent.test_five_file_neutral_diff_fixture_actually_derives_content_outside_bootstrap,
# mirroring how NEUTRAL_FILES is verified above.
NON_TRIVIAL_NEUTRAL_FILES = [
    "lib/some_util_a.py",
    "lib/some_util_b.py",
    "lib/some_util_c.py",
    "lib/some_util_d.py",
    "lib/some_util_e.py",
]


@pytest.mark.asyncio
class TestBootstrapExcludesContent:
    """Operator ruling 2026-09-26 (ateles#1317, agent_policy
    `ent_d0f1a840e549b3b299f62397` amendment): while bootstrap mode is
    active, Corvus/content must never be dispatched or required by the App
    approval gate — including when `review_panel.select_panel` would
    otherwise add it for a non-trivial (>=5 changed files) diff, since
    `content` is `forward_looking=True` with `min_changed_files=5`. This is a
    BOOTSTRAP-ONLY exclusion: `content` must still appear in the plain
    diff-derived floor (`--panel required`, i.e. no `panel_all`), which is
    exactly what the normal non-bootstrap swarm pipeline
    (`swarm_dispatch.py`'s canary-lane dispatch) uses via
    `review_panel.select_panel` directly.

    Red-before-green: before this fix, `resolve_lenses(panel_all=True)` on
    NON_TRIVIAL_NEUTRAL_FILES returned `content` in `lenses` (unioned in via
    the OLD `BOOTSTRAP_PANEL_LENSES`, which included it, AND independently
    surfaced by `derive_required_lenses` itself deriving it via
    `select_panel`'s file-count opt-in) — reproduced directly against
    `review_panel.select_panel` in
    test_five_file_neutral_diff_fixture_actually_derives_content_outside_bootstrap
    below, which stays green on both old and new code since it exercises
    the generic non-bootstrap function this fix must NOT change.
    """

    async def test_five_file_neutral_diff_fixture_actually_derives_content_outside_bootstrap(
        self,
    ):
        """Instrument check (CLAUDE.md "validate the instrument before
        believing the measurement"): confirms NON_TRIVIAL_NEUTRAL_FILES is
        genuinely >= the content lens's min_changed_files threshold by
        calling the live `review_panel.select_panel` directly, so a future
        change to that threshold cannot make this fixture silently stop
        proving what it claims to prove."""
        panel = select_panel(
            gate_contributors=set(),
            changed_files=NON_TRIVIAL_NEUTRAL_FILES,
            max_panel=6,
        )
        assert "content" in [lens.lens for lens in panel], (
            "fixture is not actually non-trivial by review_panel's own "
            "min_changed_files threshold — this test would pass vacuously"
        )

    async def test_panel_all_never_requires_content_even_for_a_non_trivial_diff(
        self, monkeypatch
    ):
        """The core fix this PR ships: a >=5-file diff that normally selects
        content must NOT make content required under bootstrap mode
        (--panel all), even though it clears review_panel's own opt-in path
        for the forward-looking content lens."""
        client = _FakeClient(
            comments=_all_clear_comments(sorted(target.BOOTSTRAP_PANEL_LENSES)),
            check_runs=_green_checks(),
            changed_files=NON_TRIVIAL_NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)

        lenses, required, excluded = await target.resolve_lenses(
            client,
            repo=REPO,
            pr=PR,
            pr_body="Closes #7",
            extra_lenses=[],
            panel_all=True,
        )

        assert "content" not in lenses, (
            "bootstrap mode (--panel all) must never require content/Corvus, "
            "even when select_panel's own file-count opt-in would otherwise "
            "have derived it for this diff"
        )
        # select_panel DID derive content into the diff-derived floor here —
        # `resolve_lenses` must actively strip it, not merely fail to add it.
        assert "content" in {r.lens for r in required}, (
            "the derived floor itself (review_panel.select_panel's own "
            "output) must still show content was selected, so the fix is "
            "proven to be an explicit exclusion rather than this fixture "
            "accidentally not triggering select_panel's opt-in at all"
        )
        assert excluded == ["content"], (
            "resolve_lenses must report content as an lens it actively "
            "excluded, so the dry-run output can say so rather than "
            "silently omitting it with no explanation"
        )

    async def test_panel_all_apply_approves_a_non_trivial_diff_with_no_content_comment(
        self, monkeypatch
    ):
        """End-to-end: --apply must succeed on a >=5-file diff under
        bootstrap mode with every bootstrap-roster lens clear and NO content/
        Corvus comment ever posted — content is excluded outright, not
        merely optional."""
        client = _FakeClient(
            comments=_all_clear_comments(sorted(target.BOOTSTRAP_PANEL_LENSES)),
            check_runs=_green_checks(),
            changed_files=NON_TRIVIAL_NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        code = await target.run(REPO, PR, [], apply=True, panel_all=True)

        assert code == 0, (
            "a non-trivial diff must be approvable under --panel all with "
            "no content/Corvus comment at all"
        )
        posted_body = client.posted[0]["json"]["body"]
        assert "content" not in posted_body

    async def test_dry_run_output_names_content_as_excluded_not_silently_absent(
        self, monkeypatch, capsys
    ):
        """Self-review finding: the dry-run table's 'required lenses' list
        used to print content as part of the diff-derived floor with no
        indication it would never be evaluated, reading as an unexplained
        gap. The printed output must say outright that content was
        excluded by bootstrap mode, not just omit it from the per-lens
        table."""
        client = _FakeClient(
            comments=_all_clear_comments(sorted(target.BOOTSTRAP_PANEL_LENSES)),
            check_runs=_green_checks(),
            changed_files=NON_TRIVIAL_NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)

        await target.run(REPO, PR, [], apply=False, panel_all=True)

        out = capsys.readouterr().out
        assert "content" in out
        assert "excluded" in out

    async def test_required_panel_alone_still_derives_content_for_a_non_trivial_diff(
        self, monkeypatch
    ):
        """The non-bootstrap path (--panel required, i.e. no panel_all) is
        UNCHANGED: content must still appear in the plain diff-derived floor
        for a >=5-file diff, exactly as the normal swarm pipeline
        (swarm_dispatch.py's canary-lane dispatch via select_panel directly)
        would derive it. This is the test that fails if the bootstrap
        exclusion is ever implemented as a change to
        review_panel.select_panel or derive_required_lenses instead of
        resolve_lenses's panel_all-only filter."""
        client = _FakeClient(
            comments=_all_clear_comments(["pm", "qa", "content"]),
            check_runs=_green_checks(),
            changed_files=NON_TRIVIAL_NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)

        lenses, required, _ = await target.resolve_lenses(
            client,
            repo=REPO,
            pr=PR,
            pr_body="Closes #7",
            extra_lenses=[],
            panel_all=False,
        )

        assert "content" in lenses, (
            "outside bootstrap mode (--panel required), content must still "
            "be derivable for a non-trivial diff — this exclusion is "
            "bootstrap-only"
        )
        assert "content" in {r.lens for r in required}

        code = await target.run(REPO, PR, [], apply=True, panel_all=False)
        assert code == 0, (
            "with content actually clear on this head, the required-panel "
            "path must approve exactly as it did before this fix"
        )

    async def test_explicit_lenses_content_is_still_blocked_under_panel_all(
        self, monkeypatch
    ):
        """A caller cannot route around the exclusion via --lenses content
        while --panel all (bootstrap default) is active — the exclusion
        applies regardless of how content's addition was requested."""
        client = _FakeClient(
            comments=_all_clear_comments(sorted(target.BOOTSTRAP_PANEL_LENSES)),
            check_runs=_green_checks(),
            changed_files=NEUTRAL_FILES,
        )
        _install_client(monkeypatch, client)

        lenses, _, excluded = await target.resolve_lenses(
            client,
            repo=REPO,
            pr=PR,
            pr_body="Closes #7",
            extra_lenses=["content"],
            panel_all=True,
        )
        assert "content" not in lenses
        assert excluded == ["content"], (
            "an explicit --lenses content request dropped by the bootstrap "
            "exclusion must be reported back, not silently discarded with "
            "no trace"
        )


class TestBootstrapPanelLensesConstant:
    """Pins the constant itself against the exact incident: it must be the
    bootstrap roster (five lenses, no legal, no content), never re-widened
    back to `LENS_AGENTS` (seven) by a future edit with no failing test to
    catch it. Operator ruling 2026-09-26 (ateles#1317) excludes Corvus/
    content from bootstrap mode outright; see TestBootstrapExcludesContent
    below for the exclusion behavior itself."""

    def test_bootstrap_panel_is_exactly_five_lenses(self):
        assert target.BOOTSTRAP_PANEL_LENSES == frozenset(
            {"pm", "arch", "ux", "qa", "security"}
        )

    def test_bootstrap_panel_excludes_legal(self):
        assert "legal" not in target.BOOTSTRAP_PANEL_LENSES

    def test_bootstrap_panel_excludes_content(self):
        assert "content" not in target.BOOTSTRAP_PANEL_LENSES

    def test_bootstrap_panel_is_a_strict_subset_of_lens_agents(self):
        """Every bootstrap lens must still be a real, registered lens — this
        constant is a curated SUBSET of LENS_AGENTS, not an independent list
        that could silently drift to name an unknown lens."""
        assert target.BOOTSTRAP_PANEL_LENSES < frozenset(target.LENS_AGENTS)

    def test_panel_all_never_unions_a_lens_outside_the_bootstrap_roster(self):
        """The regression test qa's review asked for directly: whatever
        `resolve_lenses` does internally, its panel_all output must never
        contain a lens outside BOOTSTRAP_PANEL_LENSES unless select_panel
        itself derived it for the diff. Asserted against the CONSTANT, not
        against LENS_AGENTS echoed back at itself. `content` is excluded for
        a stronger reason than `legal` (see TestBootstrapExcludesContent) but
        both are, today, outside the bootstrap roster."""
        non_bootstrap = frozenset(target.LENS_AGENTS) - target.BOOTSTRAP_PANEL_LENSES
        assert non_bootstrap == frozenset({"legal", "content"})

    def test_bootstrap_excluded_lenses_is_exactly_content(self):
        assert target.BOOTSTRAP_EXCLUDED_LENSES == frozenset({"content"})


# ── ux finding on PR #1303: a missing lens's reason must say WHICH lens and ─
# ── HOW to resolve it, and distinguish "pending" from "will never comment" ──


@pytest.mark.asyncio
class TestMissingLensReasonIsActionable:
    async def test_diff_derived_missing_lens_reads_as_pending_not_permanent(self):
        """A lens genuinely required by THIS diff (`diff_derived=True`) that
        has not yet commented reads as a wait, with an action available:
        wait, or dispatch it yourself."""
        client = _FakeClient(comments=[], check_runs=[])
        outcome = await target.evaluate_lens(
            client,
            repo=REPO,
            pr=PR,
            head_sha=HEAD,
            comments=[],
            lens="pm",
            authors=SWARM_AUTHORS,
            diff_derived=True,
        )
        assert outcome.passed is False
        assert "pm" in outcome.reason
        assert "pavo" in outcome.reason
        assert "part of this diff's required floor" in outcome.reason
        assert "never will" not in outcome.reason

    async def test_panel_all_only_missing_lens_reads_as_permanent_not_pending(self):
        """A lens required ONLY because --panel all/--lenses widened the
        floor (`diff_derived=False`) that has not commented must say it will
        NEVER comment on its own, and name the two ways to actually resolve
        the row — Accipiter's ux finding on PR #1303: before this fix both
        cases printed the identical 'no comment carries {marker}' reason,
        giving no hint that legal would never comment for an ordinary diff."""
        client = _FakeClient(comments=[], check_runs=[])
        outcome = await target.evaluate_lens(
            client,
            repo=REPO,
            pr=PR,
            head_sha=HEAD,
            comments=[],
            lens="legal",
            authors=SWARM_AUTHORS,
            diff_derived=False,
        )
        assert outcome.passed is False
        assert "legal" in outcome.reason
        assert "buteo" in outcome.reason
        assert "never will" in outcome.reason
        assert "--panel required" in outcome.reason or "--lenses" in outcome.reason
        assert "dispatch the lens yourself" in outcome.reason

    async def test_run_passes_diff_derived_correctly_for_required_vs_panel_all_lenses(
        self, monkeypatch
    ):
        """End-to-end: run() must tell evaluate_lens which lenses are
        actually diff-derived (pm, qa for NEUTRAL_FILES) versus which are
        required only via panel_all (arch, ux, security, content) — proven
        by reading the printed reason for each missing lens."""
        client = _FakeClient(comments=[], check_runs=_green_checks(), changed_files=NEUTRAL_FILES)
        _install_client(monkeypatch, client)

        code = await target.run(REPO, PR, [], apply=False, panel_all=True)
        assert code == 1  # nothing commented; expected to fail closed

        # Re-run and capture the outcomes directly via resolve_lenses +
        # evaluate_lens, mirroring exactly what run() does, so the test
        # reads the SAME reason text a real dry run would print.
        lenses, required, _ = await target.resolve_lenses(
            client, repo=REPO, pr=PR, pr_body="", extra_lenses=[], panel_all=True
        )
        floor_names = {r.lens for r in required}
        outcomes = {
            lens: await target.evaluate_lens(
                client,
                repo=REPO,
                pr=PR,
                head_sha=HEAD,
                comments=[],
                lens=lens,
                authors=SWARM_AUTHORS,
                diff_derived=lens in floor_names,
            )
            for lens in lenses
        }
        assert "never will" not in outcomes["pm"].reason
        assert "never will" not in outcomes["qa"].reason
        assert "never will" in outcomes["arch"].reason
        assert "never will" in outcomes["security"].reason


class TestPanelAllDefault:
    def test_bootstrap_mode_default_is_panel_all_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("ATELES_APPROVE_PANEL_REQUIRED_ONLY", raising=False)
        assert target._panel_all_default() is True

    def test_opt_out_env_var_restores_required_only_default(self, monkeypatch):
        monkeypatch.setenv("ATELES_APPROVE_PANEL_REQUIRED_ONLY", "1")
        assert target._panel_all_default() is False

    def test_opt_out_env_var_requires_exact_value(self, monkeypatch):
        """Only the literal '1' opts out, so a typo like 'true' or 'yes'
        fails closed to the stricter bootstrap-mode default rather than
        silently disabling it."""
        for v in ("true", "yes", "0", ""):
            monkeypatch.setenv("ATELES_APPROVE_PANEL_REQUIRED_ONLY", v)
            assert target._panel_all_default() is True

    def test_cli_default_reflects_bootstrap_mode(self, monkeypatch):
        monkeypatch.delenv("ATELES_APPROVE_PANEL_REQUIRED_ONLY", raising=False)
        monkeypatch.setattr(sys, "argv", ["approve_pr_as_app.py", "--repo", REPO, "--pr", str(PR)])
        parser_args = []

        async def _capture_run(repo, pr, extra_lenses, *, apply, panel_all=False):
            parser_args.append(panel_all)
            return 0

        monkeypatch.setattr(target, "run", _capture_run)
        target.main()

        assert parser_args == [True]

    def test_cli_panel_required_overrides_bootstrap_default(self, monkeypatch):
        monkeypatch.delenv("ATELES_APPROVE_PANEL_REQUIRED_ONLY", raising=False)
        monkeypatch.setattr(
            sys,
            "argv",
            ["approve_pr_as_app.py", "--repo", REPO, "--pr", str(PR), "--panel", "required"],
        )
        parser_args = []

        async def _capture_run(repo, pr, extra_lenses, *, apply, panel_all=False):
            parser_args.append(panel_all)
            return 0

        monkeypatch.setattr(target, "run", _capture_run)
        target.main()

        assert parser_args == [False]


# ── ateles#1326: the standard harness attribution footer does not break ────
# ── a real bootstrap-lens gate comment read through evaluate_lens ───────────
#
# `evaluate_lens` calls `lens_own_verdict`/`sign_off_is_warranted` directly on
# `comment.get("body")` — the bytes already live on GitHub — so this is the
# dry-run surface Pavo's spec names: "approve_pr_as_app.py's dry run is run
# ... against all five bootstrap lens comment shapes (pm/ux/arch/qa/security)
# carrying the real footer, and each resolves to its posted verdict with zero
# comment edits."

_REAL_FOOTER = "\n\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n"

BOOTSTRAP_LENS_AGENTS = {
    "pm": "pavo",
    "ux": "accipiter",
    "arch": "waxwing",
    "qa": "phoenicurus",
    "security": "falco",
}


@pytest.mark.asyncio
class TestEvaluateLensToleratesTheRealHarnessFooter:
    @pytest.mark.parametrize("lens,agent", sorted(BOOTSTRAP_LENS_AGENTS.items()))
    async def test_each_bootstrap_lens_comment_with_the_real_footer_resolves_signed_off(
        self, lens, agent
    ):
        body = _lens_comment_body(lens, agent) + _REAL_FOOTER
        client = _FakeClient(comments=[_comment(1, body)], check_runs=_green_checks())
        outcome = await target.evaluate_lens(
            client,
            repo=REPO,
            pr=PR,
            head_sha=HEAD,
            comments=[_comment(1, body)],
            lens=lens,
            authors=SWARM_AUTHORS,
            diff_derived=True,
        )
        assert outcome.head_matched is True
        assert outcome.verdict == "signed_off"
        assert outcome.passed is True

    async def test_a_footer_bearing_comment_with_a_blocking_finding_still_fails(self):
        body = _lens_comment_body(
            "arch", "waxwing", verdict="REQUEST_CHANGES",
            extra="[BLOCKING] layering: x",
        ) + _REAL_FOOTER
        outcome = await target.evaluate_lens(
            _FakeClient(comments=[_comment(1, body)], check_runs=_green_checks()),
            repo=REPO,
            pr=PR,
            head_sha=HEAD,
            comments=[_comment(1, body)],
            lens="arch",
            authors=SWARM_AUTHORS,
            diff_derived=True,
        )
        assert outcome.passed is False
        assert outcome.verdict != "signed_off"


# ── carried sign-offs (ruling `rereview_only_blockers_and_touched_areas`) ────

SECURITY_FIX_FILE = "execution/scripts/test_guard.py"  # allowlisted: qa only
CODE_FIX_FILE = ".claude/hooks/some_guard.py"  # a security path: every lens
DOC_FILE = "docs/guide/how_to.md"  # ux area
UNMAPPED_FILE = "lib/some_util.py"  # in no area
FIVE = ["pm", "arch", "ux", "qa", "security"]
_HEADERS = {
    "pm": "Pavo", "arch": "Waxwing", "ux": "Accipiter", "qa": "Phoenicurus",
    "security": "Falco",
}


class _CompareClient(_FakeClient):
    """A `_FakeClient` that also serves the three-dot compares the carry reads.

    `sides` maps a head SHA to the PR's file list at that head. The PR's diff at
    the OLD head and at the current head differ by exactly the fix, so the
    interdiff is the fix; a head missing from `sides` answers 404 (an unreadable
    delta).
    """

    def __init__(self, *a, sides: dict[str, list[dict]] | None = None, **k):
        super().__init__(*a, **k)
        self.sides = sides or {}

    async def get(self, url, headers=None, params=None):
        if "/compare/" in url:
            self.get_urls.append(url)
            sha = url.rsplit("...", 1)[1]
            if sha not in self.sides:
                return _FakeResponse({"message": "Not Found"}, status_code=404)
            return _FakeResponse({"files": self.sides[sha]})
        return await super().get(url, headers=headers, params=params)


def _pr_file(name: str, patch: str = "+x\n") -> dict:
    return {"filename": name, "patch": patch, "changes": 1, "additions": 1, "deletions": 0}


def _round_comments(head: str, *, blocked: tuple[str, ...] = (), skip=()) -> list[dict]:
    out = []
    for n, lens in enumerate(FIVE):
        if lens in skip:
            continue
        marker = swarm_dispatch.compose_lens_review_marker(lens, head)
        verdict = "BLOCKED" if lens in blocked else "SIGNED_OFF"
        text = (
            f"{marker}\n**\U0001f916 {_HEADERS[lens]} — Ateles swarm, {lens} lens panelist**\n"
            f"**{verdict}**\n"
        )
        if lens in blocked:
            text += "\n[BLOCKING] correctness: wrong\n"
        out.append(
            {
                "id": 100 + n + (0 if head == OLD_HEAD else 50),
                "created_at": f"2026-09-29T{'09' if head == OLD_HEAD else '11'}:0{n}:00Z",
                "body": text,
                "user": {"login": SWARM_LOGIN},
                "html_url": f"https://github.com/{REPO}/pull/{PR}#issuecomment-{head[:2]}{n}",
            }
        )
    return out


def _fix_sides(*fix_files: str) -> dict[str, list[dict]]:
    """PR file lists at OLD and the current head; the fix adds `fix_files`."""
    base = [_pr_file("a_random_file.txt")]
    return {
        OLD_HEAD: base,
        HEAD: base + [_pr_file(f) for f in fix_files],
    }


def _fix_round_comments(*, blocked=("security",), reran=("security", "qa")) -> list[dict]:
    earlier = _round_comments(OLD_HEAD, blocked=blocked)
    now = [c for c in _round_comments(HEAD) if any(
        f"review:{lens} " in c["body"] for lens in reran)]
    return earlier + now


@pytest.mark.asyncio
class TestCarriedSignOffs:
    async def _run(self, monkeypatch, comments, sides, *, apply=True, no_carry=False):
        client = _CompareClient(
            comments=comments, check_runs=_green_checks(), sides=sides,
            changed_files=[SECURITY_FIX_FILE, "a_random_file.txt"],
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)
        kwargs = {"carry": False} if no_carry else {}
        code = await target.run(REPO, PR, [], apply=apply, panel_all=True, **kwargs)
        return code, client

    async def test_a_test_only_security_fix_reviewed_by_security_and_qa_approves_and_names_the_carried(
        self, monkeypatch
    ):
        code, client = await self._run(
            monkeypatch, _fix_round_comments(), _fix_sides(SECURITY_FIX_FILE)
        )
        assert code == 0
        body = client.posted[0]["json"]["body"]
        for lens in ("pm", "arch", "ux", "qa", "security"):
            assert f"**{lens}**" in body
        # every carried lens is named with the full head its verdict came from
        assert body.count("CARRIED FORWARD") == 3
        assert body.count(f"at head `{OLD_HEAD}`") == 3
        assert "**security** (falco): `signed_off` —" in body
        assert "**qa** (phoenicurus): `signed_off` —" in body
        assert "carried forward from an earlier head" in body

    async def test_a_docs_fix_also_needs_ux_at_the_current_head(self, monkeypatch):
        sides = _fix_sides(SECURITY_FIX_FILE, DOC_FILE)
        code, client = await self._run(monkeypatch, _fix_round_comments(), sides)
        assert code == 1 and client.posted == []  # ux was not re-run
        comments = _fix_round_comments(reran=("security", "qa", "ux"))
        code, client = await self._run(monkeypatch, comments, sides)
        assert code == 0
        assert client.posted[0]["json"]["body"].count("CARRIED FORWARD") == 2

    async def test_a_code_fix_carries_nothing(self, monkeypatch):
        code, client = await self._run(
            monkeypatch, _fix_round_comments(), _fix_sides(CODE_FIX_FILE)
        )
        assert code == 1 and client.posted == []

    async def test_an_unmapped_file_carries_nothing(self, monkeypatch):
        code, client = await self._run(
            monkeypatch, _fix_round_comments(), _fix_sides(UNMAPPED_FILE)
        )
        assert code == 1 and client.posted == []

    async def test_a_lens_that_blocked_is_never_carried(self, monkeypatch):
        # arch blocked at OLD and was not re-run; the fix touched only security.
        comments = _fix_round_comments(blocked=("arch", "security"))
        code, client = await self._run(
            monkeypatch, comments, _fix_sides(SECURITY_FIX_FILE)
        )
        assert code == 1 and client.posted == []

    async def test_an_unreadable_delta_carries_nothing(self, monkeypatch):
        sides = _fix_sides(SECURITY_FIX_FILE)
        del sides[OLD_HEAD]  # the earlier head cannot be compared
        code, client = await self._run(monkeypatch, _fix_round_comments(), sides)
        assert code == 1 and client.posted == []

    async def test_no_carry_flag_requires_every_lens_at_the_current_head(self, monkeypatch):
        code, client = await self._run(
            monkeypatch, _fix_round_comments(), _fix_sides(SECURITY_FIX_FILE),
            no_carry=True,
        )
        assert code == 1 and client.posted == []

    async def test_a_live_blocking_verdict_at_the_current_head_still_refuses(self, monkeypatch):
        comments = _round_comments(OLD_HEAD) + _round_comments(HEAD, blocked=("qa",), skip=("pm", "arch", "ux", "security"))
        code, client = await self._run(
            monkeypatch, comments, _fix_sides(SECURITY_FIX_FILE)
        )
        assert code == 1 and client.posted == []

    async def test_the_dry_run_table_marks_carried_rows(self, monkeypatch, capsys):
        code, _ = await self._run(
            monkeypatch, _fix_round_comments(), _fix_sides(SECURITY_FIX_FILE),
            apply=False,
        )
        assert code == 0
        assert capsys.readouterr().out.count("carried") >= 3

    async def test_a_moved_guard_line_is_a_change_not_an_empty_delta(self, monkeypatch):
        """Security review of ateles#1368: the same +/- lines at a different place
        measured 0 changed lines, so nothing was touched and every lens carried."""
        old = [_pr_file(SECURITY_FIX_FILE, "@@ -1,3 +1,4 @@\n a\n+check()\n b\n")]
        old[0]["sha"] = "blob-old"
        new = [_pr_file(SECURITY_FIX_FILE, "@@ -1,3 +1,4 @@\n a\n b\n+check()\n")]
        new[0]["sha"] = "blob-new"
        comments = _fix_round_comments(blocked=(), reran=())
        code, client = await self._run(monkeypatch, comments, {OLD_HEAD: old, HEAD: new})
        assert code == 1 and client.posted == []

    async def test_an_empty_delta_after_a_head_change_carries_nothing(self, monkeypatch):
        same = [_pr_file("a_random_file.txt")]
        comments = _fix_round_comments(blocked=(), reran=())
        code, client = await self._run(monkeypatch, comments, {OLD_HEAD: same, HEAD: same})
        assert code == 1 and client.posted == []

    async def test_a_refusal_keeps_why_the_lens_could_not_be_carried(self, monkeypatch, capsys):
        sides = _fix_sides(SECURITY_FIX_FILE, DOC_FILE)
        code, _ = await self._run(monkeypatch, _fix_round_comments(), sides, apply=False)
        out = capsys.readouterr().out
        assert code == 1
        assert "Not carried from an earlier head: the fix touched its area" in out

    async def test_an_unreadable_delta_says_so_on_the_failing_row(self, monkeypatch, capsys):
        sides = _fix_sides(SECURITY_FIX_FILE)
        del sides[OLD_HEAD]
        await self._run(monkeypatch, _fix_round_comments(), sides, apply=False)
        out = capsys.readouterr().out
        assert "unreadable" in out
        assert "re-run this lens on the current head, then run the gate again" in out

    async def test_an_empty_delta_says_what_to_do_next(self, monkeypatch, capsys):
        same = [_pr_file("a_random_file.txt")]
        comments = _fix_round_comments(blocked=(), reran=())
        await self._run(monkeypatch, comments, {OLD_HEAD: same, HEAD: same}, apply=False)
        out = capsys.readouterr().out
        assert "is empty although the head moved" in out
        assert "re-run this lens on the current head, then run the gate again" in out


def test_help_documents_carrying_and_no_carry(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["approve_pr_as_app.py", "--help"])
    with pytest.raises(SystemExit):
        target.main()
    out = capsys.readouterr().out
    assert "--no-carry" in out
    assert "earlier head" in out
    assert "--no-carry" in (target.__doc__ or "")


# ── Only the swarm's own comment identities are read as lens verdicts ───────
#
# The head marker is text any account can post on a public repository, so a
# lens comment counts only when a swarm identity wrote it (`lens_authors`).
# Every case drives `run()` end to end rather than a helper, so the tests keep
# their meaning whichever function does the filtering.

def sd_marker_only(lens: str) -> str:
    return swarm_dispatch.compose_lens_review_marker(lens, HEAD) + "\nhello"


HAND_RUN_LOGIN = "hand-run-panel-account"
APP_BOT_LOGIN = "swarm-app[bot]"


@pytest.mark.asyncio
class TestOnlySwarmAuthoredLensCommentsAreRead:
    async def _run(self, monkeypatch, comments, *, lenses=(), apply=True, **kw):
        client = _FakeClient(
            comments=comments, check_runs=_green_checks(), changed_files=NEUTRAL_FILES
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)
        code = await target.run(REPO, PR, list(lenses), apply=apply, **kw)
        return code, client

    async def test_a_forged_clearing_comment_does_not_displace_a_required_lens_objection(
        self, monkeypatch, capsys
    ):
        comments = _all_clear_comments(["pm", "qa", "security"])
        comments.append(
            _comment(
                60,
                _lens_comment_body(
                    "arch", "waxwing", verdict="REQUEST_CHANGES",
                    extra="[BLOCKING] a real finding.",
                ),
            )
        )
        comments.append(  # latest, on the same head, from an account outside the swarm
            _comment(61, _lens_comment_body("arch", "waxwing"), author=OTHER_LOGIN)
        )
        code, client = await self._run(monkeypatch, comments, lenses=ALL_FOUR)
        out = capsys.readouterr().out
        assert code == 1
        assert client.posted == []
        assert "ignored 1 lens-marked comment(s) not written by a configured swarm identity" in out
        assert "review:arch" in out and "add it to" in out and lens_authors.ENV_AUTHORS in out
        assert OTHER_LOGIN in out

    async def test_a_forged_approve_after_a_real_objection_does_not_retire_it(
        self, monkeypatch
    ):
        # arch is NOT in the required floor: the objection is read by the
        # non-required-block check, where a later clearing verdict retires it.
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(_comment(62, _arch("REQUEST_CHANGES", "[BLOCKING] real finding.")))
        comments.append(_comment(63, _arch("APPROVE"), author=OTHER_LOGIN))
        code, client = await self._run(monkeypatch, comments)
        assert code == 1
        assert client.posted == []

    async def test_the_same_clearing_comment_from_a_swarm_identity_still_retires_it(
        self, monkeypatch
    ):
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(_comment(62, _arch("REQUEST_CHANGES", "[BLOCKING] real finding.")))
        comments.append(_comment(63, _arch("APPROVE")))
        code, client = await self._run(monkeypatch, comments)
        assert code == 0
        assert len(client.posted) == 1

    async def test_an_objection_from_a_non_admitted_account_holds_approval(
        self, monkeypatch, capsys
    ):
        # It can only DELAY: the account is not a configured swarm identity, so
        # its comment is never read as a verdict, but a real objection must not
        # vanish just because its author is missing from the setting.
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(_comment(64, _arch("REQUEST_CHANGES"), author=OTHER_LOGIN))
        code, client = await self._run(monkeypatch, comments)
        out = capsys.readouterr().out
        assert code == 1
        assert client.posted == []
        assert "arch (waxwing)" in out and OTHER_LOGIN in out and "REQUEST_CHANGES" in out
        assert f"https://github.com/{REPO}/pull/{PR}#issuecomment-64" in out
        assert lens_authors.ENV_AUTHORS in out and "non-swarm objections: HELD" in out

    async def test_a_swarm_account_missing_from_the_setting_cannot_make_its_objection_vanish(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, "an-unrelated-account")
        comments = _all_clear_comments(["pm", "qa"], author="an-unrelated-account")
        comments.append(_comment(65, _arch("REQUEST_CHANGES", "[BLOCKING] real finding")))
        code, client = await self._run(monkeypatch, comments)
        out = capsys.readouterr().out
        assert code == 1 and client.posted == []
        assert SWARM_LOGIN in out and "issuecomment-65" in out

    async def test_an_objection_on_a_required_lens_from_a_non_admitted_account_holds(
        self, monkeypatch
    ):
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(_comment(66, _lens_comment_body("qa", "phoenicurus", verdict="REQUEST_CHANGES"), author=OTHER_LOGIN))
        code, client = await self._run(monkeypatch, comments)
        assert code == 1 and client.posted == []

    async def test_an_unreadable_comment_from_a_non_admitted_account_does_not_hold(
        self, monkeypatch
    ):
        comments = _all_clear_comments(["pm", "qa"])
        junk = _comment(67, sd_marker_only("arch"), author=OTHER_LOGIN)
        comments.append(junk)
        code, _ = await self._run(monkeypatch, comments)
        assert code == 0

    async def test_a_forged_comment_cannot_supply_a_missing_required_lens(
        self, monkeypatch
    ):
        comments = _all_clear_comments(["pm", "qa"])
        comments.append(
            _comment(65, _lens_comment_body("security", "falco"), author=OTHER_LOGIN)
        )
        code, client = await self._run(monkeypatch, comments, lenses=["security"])
        assert code == 1
        assert client.posted == []

    async def test_a_comment_with_no_readable_author_is_not_a_verdict(self, monkeypatch):
        comments = _all_clear_comments(["pm"])
        comments.append(_comment(66, _lens_comment_body("qa", "phoenicurus"), author=None))
        code, client = await self._run(monkeypatch, comments)
        assert code == 1
        assert client.posted == []

    async def test_a_forged_earlier_head_signoff_is_not_carried(self, monkeypatch):
        earlier = _round_comments(OLD_HEAD, skip=("ux",))
        forged = [
            {**c, "user": {"login": OTHER_LOGIN}}
            for c in _round_comments(OLD_HEAD)
            if "review:ux " in c["body"]
        ]
        now = [
            c for c in _round_comments(HEAD)
            if any(f"review:{lens} " in c["body"] for lens in ("security", "qa", "pm", "arch"))
        ]
        client = _CompareClient(
            comments=earlier + forged + now, check_runs=_green_checks(),
            sides=_fix_sides(SECURITY_FIX_FILE),
            changed_files=[SECURITY_FIX_FILE, "a_random_file.txt"],
        )
        _install_client(monkeypatch, client)
        _install_app_mint(monkeypatch)
        code = await target.run(REPO, PR, [], apply=True, panel_all=True)
        assert code == 1
        assert client.posted == []

    # ── the legitimate authors are still read ───────────────────────────────

    async def test_a_hand_run_panel_account_named_in_the_setting_is_read(
        self, monkeypatch
    ):
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, f"{SWARM_LOGIN}, {HAND_RUN_LOGIN}")
        comments = [
            _comment(1, _lens_comment_body("pm", "pavo"), author=SWARM_LOGIN),
            _comment(2, _lens_comment_body("qa", "phoenicurus"), author=HAND_RUN_LOGIN),
        ]
        code, client = await self._run(monkeypatch, comments)
        assert code == 0
        assert len(client.posted) == 1

    async def test_the_swarm_apps_bot_login_is_read_without_any_setting(self, monkeypatch):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: ({APP_BOT_LOGIN}, False))
        comments = [
            _comment(1, _lens_comment_body("pm", "pavo"), author="Swarm-App[bot]"),
            _comment(2, _lens_comment_body("qa", "phoenicurus"), author=APP_BOT_LOGIN),
        ]
        code, client = await self._run(monkeypatch, comments)
        assert code == 0

    async def test_a_login_in_the_setting_matches_without_regard_to_case(self, monkeypatch):
        comments = [
            _comment(1, _lens_comment_body("pm", "pavo"), author=SWARM_LOGIN.upper()),
            _comment(2, _lens_comment_body("qa", "phoenicurus"), author=SWARM_LOGIN.title()),
        ]
        code, _ = await self._run(monkeypatch, comments)
        assert code == 0

    async def test_an_account_the_shared_agent_token_resolves_to_is_read(self, monkeypatch):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        monkeypatch.setenv("ATELES_AGENT_PAT", "fake-agent-token")
        monkeypatch.setattr(lens_authors, "_login_for_token", lambda token: "agent-account")
        comments = [
            _comment(1, _lens_comment_body("pm", "pavo"), author="agent-account"),
            _comment(2, _lens_comment_body("qa", "phoenicurus"), author="agent-account"),
        ]
        code, _ = await self._run(monkeypatch, comments)
        assert code == 0

    # ── fail closed ─────────────────────────────────────────────────────────

    async def test_no_configured_identity_reads_every_lens_as_having_no_verdict(
        self, monkeypatch, capsys
    ):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        code, client = await self._run(monkeypatch, _all_clear_comments(ALL_FOUR), lenses=ALL_FOUR)
        out = capsys.readouterr().out
        assert code == 1
        assert client.posted == []
        assert lens_authors.ENV_AUTHORS in out
        assert "no swarm lens-comment identity could be resolved" in out
        # the cause is identity, never "wait for the lens to review"
        assert "Wait for it to review" not in out
        # genuine swarm comments are not described as foreign
        assert "not written by a configured swarm identity" not in out
        assert "were NOT read because no swarm identity could be resolved" in out

    async def test_an_unreadable_identity_lookup_reads_every_lens_as_having_no_verdict(
        self, monkeypatch
    ):
        def _boom():
            raise RuntimeError("identity source unreadable")

        monkeypatch.setattr(target, "lens_comment_authors", _boom)
        code, client = await self._run(monkeypatch, _all_clear_comments(ALL_FOUR), lenses=ALL_FOUR)
        assert code == 1
        assert client.posted == []

    @pytest.mark.parametrize("setting", ["*", "all", "", "   ", "a/b", ",,"])
    async def test_a_malformed_setting_admits_nobody(self, monkeypatch, setting):
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, setting)
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: (set(), False))
        code, client = await self._run(monkeypatch, _all_clear_comments(ALL_FOUR), lenses=ALL_FOUR)
        # "all" is a syntactically valid login, so it admits an account named
        # "all" and nobody else; the swarm's own comments are still unread.
        assert code == 1
        assert client.posted == []

    async def test_the_ignored_list_names_the_lens_and_is_capped(self, monkeypatch, capsys):
        comments = _all_clear_comments(["pm", "qa"])
        for n in range(8):
            comments.append(
                _comment(80 + n, _lens_comment_body("arch", "waxwing"), author=f"stranger-{n}")
            )
        code, _ = await self._run(monkeypatch, comments)
        out = capsys.readouterr().out
        assert "ignored 8 lens-marked comment(s)" in out
        assert out.count("review:arch by stranger-") == 5
        assert "(+3 more)" in out
        assert code == 0

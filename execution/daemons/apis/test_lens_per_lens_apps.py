"""Per-lens GitHub Apps: a lens verdict is read only from its own App.

Operator ruling 2026-10-14: each review role approves, and posts its verdict,
as its own GitHub App. The reader side of that ruling is in `lens_authors`: once
a lens HAS an App, `review:<lens>` is admitted only from that App's bot login,
so one lens cannot post another's verdict and a shared account cannot speak for
a lens that has its own identity.

Run: pytest execution/daemons/apis/test_lens_per_lens_apps.py -v
"""

from __future__ import annotations

import pytest

import lens_authors
import swarm_dispatch as sd
from lib import github_app_token as gat

SWARM = "swarm-lens-account"  # named by the conftest through the setting
PM_APP = "ateles-reviewer-pm[bot]"
ARCH_APP = "ateles-reviewer-arch[bot]"
OPENER_APP = "ateles-pr-opener[bot]"
HEAD = "a" * 40

_LOGINS = {
    "reviewer_pm": PM_APP,
    "reviewer_arch": ARCH_APP,
    "pr_opener": OPENER_APP,
}


def _declare(monkeypatch, identity: str, *, usable: bool = True) -> None:
    prefix = gat.identity_env_prefix(identity)
    monkeypatch.setenv(f"{prefix}_ID", "1")
    if usable:
        monkeypatch.setenv(f"{prefix}_PRIVATE_KEY", "fake-key-not-used-for-signing")


@pytest.fixture
def logins(monkeypatch):
    """Resolve each App's bot login from a table, with no GitHub call."""

    def fake(identity):
        if identity not in _LOGINS:
            raise gat.AppTokenError("GET /app -> HTTP 401")
        return _LOGINS[identity]

    monkeypatch.setattr(gat, "identity_bot_login", fake)
    return _LOGINS


def _c(author: str | None, lens: str, extra: str = "") -> dict:
    row = {"body": f"<!-- review:{lens} commit={HEAD} -->\n**verdict**{extra}"}
    if author is not None:
        row["user"] = {"login": author}
    return row


def _admitted(authors, comment) -> bool:
    return lens_authors.is_swarm_comment(comment, authors)


class TestWithNoPerLensAppsNothingChanges:
    def test_resolution_is_a_plain_set_and_every_swarm_comment_is_read(self, monkeypatch):
        res = lens_authors.resolve()
        assert type(res.authors) is frozenset
        assert _admitted(res.authors, _c(SWARM, "pm"))
        assert _admitted(res.authors, _c(SWARM, "arch"))


class TestALensWithAnAppIsReadOnlyFromThatApp:
    def test_its_own_app_is_admitted(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        authors = lens_authors.resolve().authors
        assert _admitted(authors, _c(PM_APP, "pm"))

    def test_the_shared_swarm_account_can_no_longer_speak_for_it(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        authors = lens_authors.resolve().authors
        assert not _admitted(authors, _c(SWARM, "pm"))

    def test_a_lens_without_an_app_is_still_read_from_the_shared_account(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        authors = lens_authors.resolve().authors
        assert _admitted(authors, _c(SWARM, "qa"))

    def test_the_shared_account_still_writes_non_lens_state(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        authors = lens_authors.resolve().authors
        aggregation = {
            "user": {"login": SWARM},
            "body": f"<!-- vanellus-aggregation commit={HEAD} -->\n### review:pm\nnotes",
        }
        assert _admitted(authors, aggregation)


class TestOneLensCannotPostAnothersVerdict:
    def test_pm_app_cannot_post_arch(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        _declare(monkeypatch, "reviewer_arch")
        authors = lens_authors.resolve().authors
        assert _admitted(authors, _c(ARCH_APP, "arch"))
        assert not _admitted(authors, _c(PM_APP, "arch"))
        assert not _admitted(authors, _c(ARCH_APP, "pm"))

    def test_pm_app_cannot_post_the_verdict_of_a_lens_with_no_app(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        authors = lens_authors.resolve().authors
        # `qa` has no App; the pm App is not a shared identity, so it cannot
        # author qa's verdict either.
        assert not _admitted(authors, _c(PM_APP, "qa"))

    def test_a_comment_carrying_two_lens_markers_is_not_the_apps_own(self, monkeypatch, logins):
        # A reader that finds a marker by substring must not find arch's marker
        # inside a comment the pm App wrote.
        _declare(monkeypatch, "reviewer_pm")
        authors = lens_authors.resolve().authors
        smuggled = _c(PM_APP, "pm", extra=f"\n<!-- review:arch commit={HEAD} -->")
        assert not _admitted(authors, smuggled)

    def test_a_per_lens_app_cannot_write_the_aggregation_or_a_notice(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        authors = lens_authors.resolve().authors
        for body in (
            f"<!-- vanellus-aggregation commit={HEAD} -->",
            "<!-- swarm-run-confirmation -->",
            "<!-- review-deferred-until=x -->",
        ):
            assert not _admitted(authors, {"user": {"login": PM_APP}, "body": body})

    def test_scope_lens_comments_drops_the_impostor_and_keeps_the_owner(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        _declare(monkeypatch, "reviewer_arch")
        authors = lens_authors.resolve().authors
        real, fake = _c(ARCH_APP, "arch"), _c(PM_APP, "arch")
        assert lens_authors.scope_lens_comments([real, fake], authors) == [real]
        assert lens_authors.ignored_lens_comments([real, fake], authors) == [fake]


class TestFailClosed:
    def test_a_declared_app_with_an_unreadable_login_admits_nobody_for_that_lens(
        self, monkeypatch
    ):
        _declare(monkeypatch, "reviewer_pm")
        monkeypatch.setattr(gat, "identity_bot_login", lambda i: (_ for _ in ()).throw(gat.AppTokenError("x")))
        res = lens_authors.resolve()
        assert any("pm lens App" in f for f in res.failed)
        assert not res.complete
        assert not _admitted(res.authors, _c(SWARM, "pm"))
        assert _admitted(res.authors, _c(SWARM, "qa"))

    def test_a_declared_but_unusable_app_admits_nobody_for_that_lens(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm", usable=False)  # id, no key
        res = lens_authors.resolve()
        assert any("pm lens App" in f for f in res.failed)
        assert not _admitted(res.authors, _c(SWARM, "pm"))
        assert not _admitted(res.authors, _c(PM_APP, "pm"))

    def test_a_comment_with_no_readable_author_is_never_admitted(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        authors = lens_authors.resolve().authors
        assert not _admitted(authors, _c(None, "pm"))


class TestThePrOpenerIsNeverAVerdictAuthor:
    def test_even_when_the_setting_names_it(self, monkeypatch, logins):
        _declare(monkeypatch, "pr_opener")
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, f"{SWARM} {OPENER_APP}")
        authors = lens_authors.resolve().authors
        assert OPENER_APP in authors  # named by the operator...
        assert not _admitted(authors, _c(OPENER_APP, "pm"))  # ...yet never a lens author
        assert _admitted(authors, _c(SWARM, "pm"))

    def test_a_pr_opener_app_alone_keeps_every_other_identity_as_before(self, monkeypatch, logins):
        _declare(monkeypatch, "pr_opener")
        authors = lens_authors.resolve().authors
        assert _admitted(authors, _c(SWARM, "pm"))
        assert not _admitted(authors, _c(OPENER_APP, "arch"))


class TestTheDispatcherUsesTheSameRule:
    def test_swarm_dispatch_resolution_carries_the_per_lens_scope(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        authors = sd.lens_comment_authors()
        assert not lens_authors.is_swarm_comment(_c(SWARM, "pm"), authors)
        assert lens_authors.is_swarm_comment(_c(PM_APP, "pm"), authors)


class TestEachAgentActsAsItsOwnApp:
    @pytest.fixture
    def minted(self, monkeypatch):
        calls = []

        def fake_token(identity, repo=None):
            calls.append((identity, repo))
            return f"app-token-for-{identity}"

        monkeypatch.setattr(gat, "identity_token", fake_token)
        return calls

    def test_a_lens_agent_with_an_app_gets_its_app_token(self, monkeypatch, minted):
        _declare(monkeypatch, "reviewer_pm")
        monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_agent_pat_should_lose")
        assert sd._token_for_agent_on_repo("pavo", "o/r") == "app-token-for-reviewer_pm"
        assert minted == [("reviewer_pm", "o/r")]

    def test_the_builder_gets_the_pr_opener_app(self, monkeypatch, minted):
        _declare(monkeypatch, "pr_opener")
        assert sd._token_for_agent_on_repo("cicada", "o/r") == "app-token-for-pr_opener"

    def test_a_reviewer_never_gets_the_pr_opener_token(self, monkeypatch, minted):
        _declare(monkeypatch, "pr_opener")
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_shared")
        assert sd._token_for_agent_on_repo("pavo", "o/r") == "ghp_shared"
        assert minted == []

    def test_without_an_app_the_old_tiers_apply(self, monkeypatch, minted):
        monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_agent_pat")
        assert sd._token_for_agent_on_repo("pavo", "o/r") == "ghp_agent_pat"
        assert minted == []

    def test_a_declared_app_that_cannot_mint_gives_no_token_not_a_borrowed_one(
        self, monkeypatch
    ):
        _declare(monkeypatch, "reviewer_pm")
        monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_agent_pat")
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_shared")

        def refuse(identity, repo=None):
            raise gat.AppTokenError("HTTP 401")

        monkeypatch.setattr(gat, "identity_token", refuse)
        assert sd._token_for_agent_on_repo("pavo", "o/r") == ""

    def test_an_unrelated_agent_is_untouched(self, monkeypatch, minted):
        _declare(monkeypatch, "reviewer_pm")
        _declare(monkeypatch, "pr_opener")
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_shared")
        assert sd._token_for_agent_on_repo("lanius", "o/r") == "ghp_shared"
        assert minted == []

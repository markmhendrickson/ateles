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
            "body": f"<!-- vanellus-aggregation commit={HEAD} -->\n### pm\nnotes",
        }
        assert _admitted(authors, aggregation)
        # ...but not when it names a lens that has an App (that lens is read from
        # its own App wherever in the body it is named; see the class below).
        naming = {"user": {"login": SWARM}, "body": aggregation["body"] + "\nsee review:pm"}
        assert not _admitted(authors, naming)


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


# ── Which comment counts as which lens: author AND the whole body ────────────
#
# The class these tests pin: the decision to read a comment as a lens's verdict
# (or as the aggregation, or as a notice) is made on the author together with
# the full body, never on the author alone and never on the opening marker alone.
# Every case has a control that is admitted, so a pass means the instrument can
# say yes.

PAVO = "**\U0001f916 Pavo — Ateles swarm, pm review**\n**SIGNED_OFF**\nlooks right\n"
SECURITY_APP = "ateles-reviewer-security[bot]"
QA_APP = "ateles-reviewer-qa[bot]"
SWARM_APP = "ateles-agents[bot]"
NEW_HEAD = "c" * 40

_MARKER_TEXT = {
    "pm": f"<!-- review:pm commit={HEAD} -->",
    "qa": f"<!-- review:qa commit={HEAD} -->",
    "arch": f"<!-- review:arch commit={HEAD} -->",
    "content": f"<!-- review:content commit={HEAD} -->",
}


def _row(author: str, body: str, id_: int = 1) -> dict:
    return {
        "id": id_,
        "created_at": f"2026-10-14T10:{id_:02d}:00Z",
        "html_url": f"https://example.invalid/c/{id_}",
        "user": {"login": author},
        "body": body,
    }


@pytest.fixture
def pm_app(monkeypatch, logins):
    _declare(monkeypatch, "reviewer_pm")
    return lens_authors.resolve().authors


class TestASharedAccountNeverSpeaksForALensWithAnApp:
    def test_the_marker_after_the_verdict_is_still_pms(self, pm_app):
        # control: the same layout for a lens with no App is admitted.
        assert _admitted(pm_app, _row(SWARM, PAVO + "\n" + _MARKER_TEXT["qa"]))
        assert not _admitted(pm_app, _row(SWARM, PAVO + "\n" + _MARKER_TEXT["pm"]))

    def test_a_leading_marker_for_another_lens_does_not_launder_a_later_pm_marker(self, pm_app):
        assert _admitted(pm_app, _row(SWARM, _MARKER_TEXT["content"] + "\n" + PAVO))
        assert not _admitted(
            pm_app, _row(SWARM, _MARKER_TEXT["content"] + "\n" + PAVO + "\n" + _MARKER_TEXT["pm"])
        )

    @pytest.mark.parametrize(
        "variant",
        [
            "review:pm-extra",
            "review:pm-superseded",
            "REVIEW:PM",
            "review:pm.",
            "<!-- review:pm-superseded by=" + NEW_HEAD + " -->",
        ],
    )
    def test_spelling_variants_of_the_marker_are_refused_too(self, pm_app, variant):
        assert _admitted(pm_app, _row(SWARM, PAVO + "\nreview:qa"))
        assert not _admitted(pm_app, _row(SWARM, PAVO + "\n" + variant))

    def test_it_holds_when_the_lens_app_is_declared_but_unreadable(self, monkeypatch):
        _declare(monkeypatch, "reviewer_pm", usable=False)
        authors = lens_authors.resolve().authors
        assert _admitted(authors, _row(SWARM, PAVO + "\nreview:qa"))
        assert not _admitted(authors, _row(SWARM, PAVO + "\n" + _MARKER_TEXT["pm"]))
        assert not _admitted(authors, _row(SWARM, _MARKER_TEXT["pm"] + "\n" + PAVO))

    def test_no_reader_reads_the_forged_comment_as_pms_verdict(self, pm_app):
        forged = _row(SWARM, PAVO + "\n" + _MARKER_TEXT["pm"])
        scoped = lens_authors.scope_lens_comments([forged], pm_app)
        assert scoped == []
        assert sd.lens_records(scoped) == {}
        assert not sd.lens_comment_satisfies_presence(
            "".join(c["body"] for c in scoped), "pm", head_sha=HEAD
        )
        assert lens_authors.ignored_lens_comments([forged], pm_app) == [forged]

    def test_a_comment_that_is_not_lens_shaped_is_untouched(self, pm_app):
        plain = _row(SWARM, "thanks, merging after CI")
        assert lens_authors.scope_lens_comments([plain], pm_app) == [plain]


class TestALensAppSpeaksOnlyForItsOwnLens:
    @pytest.fixture
    def two_apps(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        _declare(monkeypatch, "reviewer_arch")
        return lens_authors.resolve().authors

    @pytest.mark.parametrize(
        "marker",
        [
            f"<!-- vanellus-aggregation commit={HEAD} -->",
            "<!-- review-deferred-until:2026-10-15T00:00:00Z -->",
            "<!-- swarm-run-confirmation -->",
            "<!-- pipeline-bypass-notice -->",
            "<!-- swarm-gate-waive-result -->",
            f"<!-- review:arch commit={HEAD} -->",
            f"<!-- review:arch-superseded by={NEW_HEAD} -->",
            "review:pm-extra",
            "review:content",
        ],
    )
    def test_no_other_swarm_marker_rides_inside_its_comment(self, two_apps, marker):
        own = _MARKER_TEXT["pm"] + "\n" + PAVO
        assert _admitted(two_apps, _row(PM_APP, own))  # control
        assert not _admitted(two_apps, _row(PM_APP, own + "\n" + marker))
        assert not _admitted(two_apps, _row(PM_APP, marker))

    def test_the_forged_aggregation_is_not_read_as_the_aggregation(self, two_apps):
        forged = _row(
            PM_APP,
            f"{_MARKER_TEXT['pm']}\n**\U0001f916 Vanellus — Ateles swarm, aggregation**\n"
            f"**SIGNED_OFF**\n<!-- vanellus-aggregation commit={HEAD} -->",
        )
        scoped = lens_authors.scope_lens_comments([forged], two_apps)
        assert scoped == []
        assert sd.latest_aggregation_comment(scoped, head_sha=HEAD) is None

    def test_its_own_head_marker_and_its_own_retired_marker_are_fine(self, two_apps):
        original = _MARKER_TEXT["pm"] + "\n" + PAVO
        retired = sd.compose_superseded_verdict(original, NEW_HEAD)
        assert _admitted(two_apps, _row(PM_APP, original))
        assert _admitted(two_apps, _row(PM_APP, retired))

    def test_a_quoted_other_lens_marker_in_prose_is_dropped_and_says_so(self, two_apps):
        # Fail closed, and the contract: a lens App's comment names no other lens.
        quoting = _MARKER_TEXT["pm"] + "\n" + PAVO + "\nunlike review:arch, this is fine"
        assert not _admitted(two_apps, _row(PM_APP, quoting))


class TestARetiredVerdictIsJudgedLikeTheVerdictItRetired:
    """`compose_superseded_verdict` puts a `>` banner and a superseded marker in
    front of the verbatim original. The decision for every author must be the
    same for both forms, through `is_swarm_comment` AND `lens_records`."""

    @staticmethod
    def _forms(lens: str, header: str) -> dict[str, str]:
        original = f"<!-- review:{lens} commit={HEAD} -->\n{header}"
        return {"original": original, "retired": sd.compose_superseded_verdict(original, NEW_HEAD)}

    @pytest.mark.parametrize("form", ["original", "retired"])
    @pytest.mark.parametrize(
        ("author", "lens", "admitted"),
        [
            (PM_APP, "pm", True),  # the lens App, its own lens
            (SWARM, "pm", False),  # a shared account, a lens that has an App
            (PM_APP, "qa", False),  # a lens App, someone else's lens
            (SWARM, "qa", True),  # a shared account, a lens with no App
        ],
    )
    def test_same_answer_in_both_forms(self, pm_app, form, author, lens, admitted):
        header = {
            "pm": PAVO,
            "qa": "**\U0001f916 Phoenicurus — Ateles swarm, qa review**\n**SIGNED_OFF**\nok\n",
        }[lens]
        body = self._forms(lens, header)[form]
        row = _row(author, body)
        assert _admitted(pm_app, row) is admitted
        records = sd.lens_records(lens_authors.scope_lens_comments([row], pm_app))
        if admitted:
            assert [r.cleared for r in records[lens]] == [True]
            assert [r.head for r in records[lens]] == [HEAD]
        else:
            assert records == {}

    def test_a_forged_banner_in_front_of_a_forged_signoff_is_not_carried_forward(self, pm_app):
        # The shared account writes the retirement banner itself, in front of a
        # forged sign-off, hoping the carry-forward reader takes it as pm's history.
        forged = _row(
            SWARM,
            f"> ⚠️ Superseded by commit `ccccccc` — this verdict no longer reflects the head.\n"
            f"<!-- review:pm-superseded by={NEW_HEAD} -->\n\n"
            f"<!-- review:pm commit={'d' * 40} -->\n{PAVO}",
        )
        assert not _admitted(pm_app, forged)
        assert sd.lens_records(lens_authors.scope_lens_comments([forged], pm_app)) == {}

    def test_a_banner_is_only_looked_past_when_it_is_a_real_retirement(self):
        quoted = "> just a quoted line\n<!-- review:pm commit=" + HEAD + " -->"
        assert lens_authors.leading_lens(quoted) == ""
        real = sd.compose_superseded_verdict(f"<!-- review:pm commit={HEAD} -->\nbody", NEW_HEAD)
        assert lens_authors.leading_lens(real) == "pm"
        assert lens_authors.leading_lens(f"<!-- review:pm commit={HEAD} -->") == "pm"


class TestTwoIdentitiesThatAreOneLoginAdmitNobody:
    def _check(self, authors, failed, *, lens_dead: list[str], lens_ok: list[str]):
        assert any("two identities resolve to one bot login" in f for f in failed), failed
        for lens in lens_dead:
            assert not _admitted(authors, _row(PM_APP, f"<!-- review:{lens} commit={HEAD} -->\n"))
            assert not _admitted(authors, _row(SWARM, f"<!-- review:{lens} commit={HEAD} -->\n"))
        for lens in lens_ok:
            assert _admitted(authors, _row(ARCH_APP, f"<!-- review:{lens} commit={HEAD} -->\n"))

    def test_control_distinct_logins_are_all_admitted(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        _declare(monkeypatch, "reviewer_arch")
        _declare(monkeypatch, "pr_opener")
        res = lens_authors.resolve()
        assert res.failed == ()
        assert _admitted(res.authors, _row(PM_APP, _MARKER_TEXT["pm"] + "\n" + PAVO))
        assert _admitted(res.authors, _row(ARCH_APP, _MARKER_TEXT["arch"] + "\n"))

    def test_the_pr_opener_and_a_lens_on_one_app(self, monkeypatch):
        for ident in ("reviewer_pm", "pr_opener"):
            _declare(monkeypatch, ident)
        monkeypatch.setattr(
            gat, "identity_bot_login", lambda i: PM_APP if i in ("reviewer_pm", "pr_opener") else ""
        )
        res = lens_authors.resolve()
        assert any("two identities resolve to one bot login" in f for f in res.failed)
        assert not _admitted(res.authors, _row(PM_APP, _MARKER_TEXT["pm"] + "\n" + PAVO))
        assert not _admitted(res.authors, _row(SWARM, _MARKER_TEXT["pm"] + "\n" + PAVO))

    def test_two_lenses_on_one_app_both_admit_nobody(self, monkeypatch):
        for ident in ("reviewer_pm", "reviewer_arch", "reviewer_security"):
            _declare(monkeypatch, ident)
        table = {"reviewer_pm": PM_APP, "reviewer_security": PM_APP, "reviewer_arch": ARCH_APP}
        monkeypatch.setattr(gat, "identity_bot_login", lambda i: table[i])
        res = lens_authors.resolve()
        self._check(res.authors, res.failed, lens_dead=["pm", "security"], lens_ok=["arch"])

    def test_a_lens_on_the_swarm_apps_own_login(self, monkeypatch, logins):
        _declare(monkeypatch, "reviewer_pm")
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: ({PM_APP}, False))
        res = lens_authors.resolve()
        assert any("two identities resolve to one bot login" in f for f in res.failed)
        assert not _admitted(res.authors, _row(PM_APP, _MARKER_TEXT["pm"] + "\n" + PAVO))
        assert not res.complete

    def test_the_collision_is_reported_without_printing_the_login(self, monkeypatch):
        for ident in ("reviewer_pm", "reviewer_arch"):
            _declare(monkeypatch, ident)
        monkeypatch.setattr(gat, "identity_bot_login", lambda i: PM_APP)
        res = lens_authors.resolve()
        gap = res.describe_gap()
        assert "reviewer_arch" in gap and "reviewer_pm" in gap
        assert PM_APP not in gap
        assert "github_app_token.py status" in gap


class TestAnUnreadableOpenerIsNeverAdmitted:
    def test_a_bot_named_by_the_setting_is_refused_while_the_opener_cannot_be_read(
        self, monkeypatch
    ):
        # The opener is declared but its login cannot be read; the operator also
        # named a bot in the setting, which might be that very App.
        _declare(monkeypatch, "pr_opener")
        monkeypatch.setattr(
            gat, "identity_bot_login", lambda i: (_ for _ in ()).throw(gat.AppTokenError("HTTP 401"))
        )
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, f"{SWARM} {OPENER_APP}")
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: ({SWARM_APP}, False))
        res = lens_authors.resolve()
        body = _MARKER_TEXT["pm"] + "\n" + PAVO
        assert any("PR-opener App" in f for f in res.failed)
        assert not _admitted(res.authors, _row(OPENER_APP, body))  # refused
        assert _admitted(res.authors, _row(SWARM, body))  # an ordinary account stays
        assert _admitted(res.authors, _row(SWARM_APP, body))  # the swarm App stays

    def test_control_a_readable_opener_leaves_other_bots_to_the_setting(self, monkeypatch, logins):
        _declare(monkeypatch, "pr_opener")
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, f"{SWARM} some-other[bot]")
        res = lens_authors.resolve()
        assert _admitted(res.authors, _row("some-other[bot]", _MARKER_TEXT["pm"] + "\n" + PAVO))


class TestScopedAuthorsKeepsItsScope:
    @pytest.fixture
    def scoped(self, pm_app):
        assert isinstance(pm_app, lens_authors.ScopedAuthors)
        return pm_app

    def test_every_derived_set_keeps_the_scope(self, scoped):
        other = frozenset({"someone-else"})
        derived = {
            "or": scoped | other,
            "ror": other | scoped,
            "and": scoped & scoped,
            "sub": scoped - other,
            "rsub": other | scoped - other,
            "xor": scoped ^ other,
            "union": scoped.union(other),
            "intersection": scoped.intersection(scoped),
            "difference": scoped.difference(other),
            "symmetric_difference": scoped.symmetric_difference(other),
            "copy": scoped.copy(),
        }
        for name, result in derived.items():
            assert isinstance(result, lens_authors.ScopedAuthors), name
            assert result.scope == scoped.scope, name

    def test_a_derived_set_still_refuses_the_shared_account_for_pm(self, scoped):
        widened = scoped | {"another-shared-account"}
        assert not _admitted(widened, _row(SWARM, _MARKER_TEXT["pm"] + "\n" + PAVO))
        assert _admitted(widened, _row(PM_APP, _MARKER_TEXT["pm"] + "\n" + PAVO))


class TestAProcessMissingItsLensAppVariablesRefusesLoudly:
    """`ATELES_EXPECTED_LENS_APPS` names the lenses that must have an App on this
    host. A process that has none of that lens's variables must not fall back to
    the shared identity: that would silently restore the pre-App trust model."""

    def test_the_shared_account_cannot_speak_for_an_expected_lens(self, monkeypatch, logins):
        monkeypatch.setenv(gat.ENV_EXPECTED_LENS_APPS, "pm")
        res = lens_authors.resolve()
        assert any("pm lens App" in f and "expected" in f for f in res.failed)
        assert not res.complete
        assert not _admitted(res.authors, _row(SWARM, _MARKER_TEXT["pm"] + "\n" + PAVO))
        assert _admitted(res.authors, _row(SWARM, _MARKER_TEXT["qa"] + "\n"))  # no roster entry

    def test_the_agent_gets_no_token_not_its_pat(self, monkeypatch):
        monkeypatch.setenv(gat.ENV_EXPECTED_LENS_APPS, "pm")
        monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_agent_pat")
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_shared")
        assert sd._token_for_agent_on_repo("pavo", "o/r") == ""
        assert sd._token_for_agent_on_repo("phoenicurus", "o/r") == "ghp_shared"  # not expected

    def test_without_the_setting_nothing_changes(self, monkeypatch):
        monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_agent_pat")
        assert sd._token_for_agent_on_repo("pavo", "o/r") == "ghp_agent_pat"

    def test_a_malformed_entry_expects_every_lens_and_is_named(self, monkeypatch):
        # The roster lists lenses that are REQUIRED to have an App, so a typo must
        # not drop that lens's protection: it fails closed, loudly.
        monkeypatch.setenv(gat.ENV_EXPECTED_LENS_APPS, "*, ../x, PM!")
        assert gat.expected_reviewer_lenses() == tuple(sorted(gat.KNOWN_REVIEWER_LENSES))
        problem = gat.expected_roster_problem()
        assert "not a lens name" in problem
        for entry in ("'*'", "'../x'"):
            assert entry in problem
        assert "an entry of 3 characters" in problem  # `PM!` has a character outside the safe set

    @pytest.mark.parametrize("value", ["pm;qa", "pm|qa", "pm,qa;", "content-lens", "pm qa*"])
    def test_a_typo_in_one_entry_does_not_switch_the_guard_off(self, monkeypatch, logins, value):
        monkeypatch.setenv(gat.ENV_EXPECTED_LENS_APPS, value)
        res = lens_authors.resolve()
        assert any("not a lens name" in f for f in res.failed), res.failed
        assert not res.complete
        # the shared account cannot speak for pm, which the typo may have meant
        assert not _admitted(res.authors, _row(SWARM, _MARKER_TEXT["pm"] + "\n" + PAVO))

    def test_a_malformed_entry_gives_every_lens_agent_no_token_and_says_why(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv(gat.ENV_EXPECTED_LENS_APPS, "pm;qa")
        monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_agent_pat")
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_shared")
        with caplog.at_level("ERROR"):
            assert sd._token_for_agent_on_repo("pavo", "o/r") == ""
            assert sd._token_for_agent_on_repo("phoenicurus", "o/r") == ""
        assert "not a lens name" in caplog.text
        assert "ghp_agent_pat" not in caplog.text and "ghp_shared" not in caplog.text

    def test_control_a_sound_roster_reports_no_problem(self, monkeypatch):
        monkeypatch.setenv(gat.ENV_EXPECTED_LENS_APPS, "pm, qa  ARCH")
        assert gat.expected_roster_problem() == ""
        assert gat.expected_reviewer_lenses() == ("arch", "pm", "qa")

    def test_a_pasted_secret_is_never_echoed_as_a_bad_entry(self, monkeypatch):
        monkeypatch.setenv(gat.ENV_EXPECTED_LENS_APPS, "pm, -----BEGIN-PRIVATE-KEY-----abcdefghijklmnopqrstuvwxyz0123456789")
        problem = gat.expected_roster_problem()
        assert "abcdefghijklmnopqrstuvwxyz" not in problem
        assert "characters" in problem


class TestHalfConfiguredAppsNeverFallThroughToAnotherCredential:
    """Tier 0 is gated on the App being DECLARED (any variable set), not on it
    being fully configured: a half-set App answers no token rather than falling
    through to the agent's PAT or the shared token."""

    def test_an_id_with_no_key_yields_no_token_for_a_lens_agent(self, monkeypatch):
        monkeypatch.setenv("ATELES_APP_REVIEWER_PM_ID", "1")
        monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_agent_pat")
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_shared")
        assert sd._token_for_agent_on_repo("pavo", "o/r") == ""

    def test_an_id_with_no_key_yields_no_token_for_the_builder(self, monkeypatch):
        monkeypatch.setenv("ATELES_APP_PR_OPENER_ID", "1")
        monkeypatch.setenv("CICADA_AGENT_PAT", "ghp_agent_pat")
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_shared")
        assert sd._token_for_agent_on_repo("cicada", "o/r") == ""

    def test_only_an_installation_id_is_declared_too(self, monkeypatch):
        monkeypatch.setenv("ATELES_APP_REVIEWER_PM_INSTALLATION_ID", "9")
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_shared")
        assert sd._token_for_agent_on_repo("pavo", "o/r") == ""

    def test_a_failed_mint_logs_no_key_material(self, monkeypatch, caplog):
        monkeypatch.setenv("ATELES_APP_REVIEWER_PM_ID", "1")
        monkeypatch.setenv("ATELES_APP_REVIEWER_PM_PRIVATE_KEY", "SENTINEL-KEY-MATERIAL")
        monkeypatch.setattr(
            gat, "identity_token", lambda i, r=None: (_ for _ in ()).throw(gat.AppTokenError("HTTP 401"))
        )
        with caplog.at_level("DEBUG"):
            assert sd._token_for_agent_on_repo("pavo", "o/r") == ""
        assert "SENTINEL-KEY-MATERIAL" not in caplog.text


class TestTheCopiedLensConstantsAreBoundToTheRegistry:
    def test_the_status_listing_covers_exactly_the_panel_lenses(self):
        from review_panel import LENSES

        assert set(gat.KNOWN_REVIEWER_LENSES) == {lens.lens for lens in LENSES}

    def test_every_panel_lens_name_round_trips_through_the_env_name(self):
        from review_panel import LENSES

        for lens in LENSES:
            assert gat._LENS_KEY_RE.fullmatch(lens.lens), lens.lens
            prefix = gat.reviewer_prefix(lens.lens)
            assert prefix == f"ATELES_APP_REVIEWER_{lens.lens.upper()}"

    def test_every_name_the_daemons_can_read_is_on_the_plist_allowlist(self):
        from review_panel import LENSES
        from lib import environment_allowlist as allow

        prefixes = [gat.pr_opener_prefix()] + [gat.reviewer_prefix(lens.lens) for lens in LENSES]
        for prefix in prefixes:
            for suffix in ("_ID", "_INSTALLATION_ID"):
                assert prefix + suffix in allow.ALLOWED_ENVIRONMENT_NAMES, prefix + suffix
        assert gat.ENV_EXPECTED_LENS_APPS in allow.ALLOWED_ENVIRONMENT_NAMES

    def test_no_key_name_is_allowlisted(self):
        from review_panel import LENSES
        from lib import environment_allowlist as allow

        for lens in LENSES:
            for suffix in ("_PRIVATE_KEY", "_PRIVATE_KEY_PATH"):
                assert gat.reviewer_prefix(lens.lens) + suffix not in allow.ALLOWED_ENVIRONMENT_NAMES
        assert not any(gat.is_app_key_variable(n) for n in allow.ALLOWED_ENVIRONMENT_NAMES)

    def test_the_pr_opener_agents_are_agents_the_roster_knows(self):
        # The builder set is a role list kept in the dispatcher; every name in it
        # must at least be a real agent so a rename cannot orphan it.
        from pathlib import Path

        roster = {p.stem for p in (Path(__file__).resolve().parents[3] / "docs" / "agents").glob("*.md")}
        assert sd._PR_OPENER_AGENTS <= roster


@pytest.mark.asyncio
class TestTheDispatcherDoesNotRepostAFallbackForALensWithAnApp:
    @staticmethod
    def _client(posted):
        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None, params=None):
                class _R:
                    def raise_for_status(s):
                        pass

                    def json(s):
                        return []

                return _R()

            async def post(self, url, json=None, headers=None):
                posted.append(json["body"])

                class _R:
                    def raise_for_status(s):
                        pass

                return _R()

        return _Client()

    async def _run(self, monkeypatch):
        import httpx
        from test_gate_sign_off_dispatch import _StubNotifier, _config, _trigger

        posted: list[str] = []
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: self._client(posted))
        monkeypatch.setattr(sd, "_token_for_repo", lambda repo: "fake-token")
        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        reviews = [("pm", "**APPROVE**"), ("qa", "**APPROVE**")]
        for _ in range(3):  # three sweep cycles
            await d._post_missing_panel_comments(
                _trigger(head_sha=HEAD), reviews, {"pm": "pavo", "qa": "phoenicurus"},
                reviewed_head=HEAD,
            )
        return posted

    async def test_control_with_no_app_the_fallback_is_posted(self, monkeypatch):
        posted = await self._run(monkeypatch)
        assert any(sd.compose_lens_review_marker("pm", HEAD) in b for b in posted)

    async def test_with_an_app_the_lens_is_left_pending_and_the_others_still_post(
        self, monkeypatch, logins, caplog
    ):
        _declare(monkeypatch, "reviewer_pm")
        with caplog.at_level("WARNING"):
            posted = await self._run(monkeypatch)
        assert not any(sd.compose_lens_review_marker("pm", HEAD) in b for b in posted)
        assert any(sd.compose_lens_review_marker("qa", HEAD) in b for b in posted)
        assert "pm has an App of its own" in caplog.text

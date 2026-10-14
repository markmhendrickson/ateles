"""Snapshot selection and transport evidence, without a local repository."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from design_basis_snapshot import DesignBasisReader
from foundation import DesignSnapshot, check_design_basis, design_basis_block

REPO = "owner/reviewed"
SHA = "a" * 40
TREE = "b" * 40
PATH = "docs/foundation/principles.md"
TEXT = f"Design basis: {PATH}#rule"


def entry(path=PATH, mode="100644", kind="blob"):
    return {"path": path, "mode": mode, "type": kind, "sha": "c" * 40}


def association(number=7, repo=REPO, state="open"):
    return {
        "event": "cross-referenced",
        "source": {
            "issue": {
                "html_url": f"https://github.com/{repo}/pull/{number}",
                "state": state,
                "pull_request": {},
            }
        },
    }


class GitHubFixture:
    def __init__(self, *, entries=None, events=None):
        self.repository = REPO
        self.entries = [entry()] if entries is None else entries
        self.events = [] if events is None else events
        self.calls = []
        self.overrides = {}
        self.branch_sha = SHA

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        prefix = f"/repos/{self.repository}/"
        assert str(request.url).startswith("https://api.github.com/")
        suffix = path.removeprefix(prefix)
        if suffix in self.overrides:
            value = self.overrides[suffix]
            if isinstance(value, Exception):
                raise value
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200, json=value)
        if suffix.startswith("issues/"):
            data = self.events
        elif suffix.startswith("pulls/"):
            data = {
                "number": int(suffix.split("/")[-1]),
                "state": "open",
                "base": {"repo": {"full_name": self.repository}},
                "head": {"sha": self.branch_sha},
            }
        elif suffix == "":
            data = {"full_name": self.repository, "default_branch": "main"}
        elif suffix == "commits/main":
            data = {"sha": self.branch_sha}
        elif suffix == f"git/commits/{SHA}":
            data = {"sha": SHA, "tree": {"sha": TREE}}
        elif suffix == f"git/trees/{TREE}":
            data = {"sha": TREE, "truncated": False, "tree": self.entries}
        else:
            return httpx.Response(404, json={"message": "private raw response"})
        return httpx.Response(200, json=data)

    def reader(self, repository=None):
        return DesignBasisReader(
            repository or self.repository,
            {"Authorization": "Bearer fixture-secret"},
            transport=httpx.MockTransport(self),
        )


@pytest.mark.asyncio
async def test_baseline_is_immutable_and_lookups_deduplicate():
    api = GitHubFixture()
    reader = api.reader()
    snapshot = await reader.select_issue(42)
    assert snapshot == DesignSnapshot(REPO, SHA, "issue_baseline")
    api.branch_sha = "d" * 40
    result = await reader.check(TEXT + " " + PATH + "#other " + TEXT)
    again = await reader.check(TEXT)
    assert result.ok and again.ok
    assert len(result.outcomes) == 2
    assert len([r for r in api.calls if "/git/trees/" in r.url.path]) == 1
    assert all("d" * 40 not in str(r.url) for r in api.calls)
    reader.select_pr_head("d" * 40)
    assert (await reader.check(TEXT)).status == "CANNOT_CHECK"
    assert any("d" * 40 in str(r.url) for r in api.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "explicit,events,status",
    [
        (None, [association()], "pr_head"),
        (7, [association(), association(8)], "pr_head"),
        (None, [association(), association(8)], "unresolved"),
        (9, [association()], "unresolved"),
        (7, [association(repo="other/tenant")], "unresolved"),
    ],
)
async def test_association_selection(explicit, events, status):
    api = GitHubFixture(events=events)
    reader = api.reader()
    snapshot = await reader.select_issue(42, explicit)
    assert snapshot.snapshot_kind == status
    result = await reader.check(TEXT)
    assert result.ok == (status == "pr_head")
    if status == "unresolved":
        assert result.status == "CANNOT_CHECK" and not result.missing
        assert snapshot.sha is None
        assert not any(r.url.path.endswith("/commits/main") for r in api.calls)


@pytest.mark.asyncio
async def test_associated_pr_membership_is_verified():
    api = GitHubFixture(events=[association()])
    api.overrides["pulls/7"] = {
        "number": 7,
        "state": "open",
        "base": {"repo": {"full_name": "other/tenant"}},
        "head": {"sha": SHA},
    }
    reader = api.reader()
    await reader.select_issue(42, 7)
    assert (await reader.check(TEXT)).status == "CANNOT_CHECK"
    assert not any("other/tenant" in str(r.url) for r in api.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response", [httpx.Response(401), httpx.Response(404), {}, [None]]
)
async def test_failed_association_lookup_is_never_no_association(response):
    api = GitHubFixture()
    api.overrides["issues/42/timeline"] = response
    reader = api.reader()
    await reader.select_issue(42)
    result = await reader.check(TEXT)
    assert result.status == "CANNOT_CHECK" and result.sha is None
    assert not result.missing and not result.ok
    assert len(api.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix,response",
    [
        (f"git/commits/{SHA}", httpx.Response(404)),
        (f"git/commits/{SHA}", httpx.Response(401)),
        (
            f"git/commits/{SHA}",
            httpx.Response(302, headers={"location": "https://untrusted.example/"}),
        ),
        (f"git/commits/{SHA}", httpx.ReadTimeout("fixture-secret private response")),
        (f"git/commits/{SHA}", httpx.Response(200, content=b"not json")),
        (f"git/commits/{SHA}", {"sha": "d" * 40}),
        (f"git/trees/{TREE}", {"sha": TREE, "tree": [], "truncated": True}),
        (f"git/trees/{TREE}", {"sha": TREE, "tree": [None], "truncated": False}),
        (
            f"git/trees/{TREE}",
            {"sha": TREE, "tree": [entry("../escape")], "truncated": False},
        ),
    ],
)
async def test_unknown_is_not_absence_and_diagnostics_are_safe(suffix, response):
    api = GitHubFixture()
    api.overrides[suffix] = response
    reader = api.reader()
    reader.select_pr_head(SHA)
    result = await reader.check(TEXT)
    assert result.status == "CANNOT_CHECK" and not result.ok and not result.missing
    prompt = design_basis_block(TEXT, where="the PR body", check=result)
    assert "retry" in prompt.lower()
    assert "not evidence of a missing document" in prompt
    assert "fixture-secret" not in prompt and "private response" not in prompt
    assert REPO in prompt and SHA in prompt and "reviewed PR head" in prompt
    assert len(api.calls) <= 2


@pytest.mark.asyncio
async def test_mixed_outcomes_precedence_and_fragment_display():
    api = GitHubFixture(entries=[entry(), entry("docs/foundation/link.md", "120000")])
    reader = api.reader()
    reader.select_pr_head(SHA)
    text = TEXT + " docs/foundation/absent.md#anchor docs/foundation/link.md"
    result = await reader.check(text)
    assert [o.status for o in result.outcomes] == ["present", "absent", "uncheckable"]
    assert result.status == "CANNOT_CHECK"
    assert result.missing == ("docs/foundation/absent.md",)
    assert result.outcomes[0].citation.endswith("#rule")
    assert result.outcomes[0].path == PATH
    assert not any("#" in str(r.url) for r in api.calls)


@pytest.mark.asyncio
async def test_absence_requires_complete_tree_and_local_presence_cannot_rescue(
    tmp_path, monkeypatch
):
    (tmp_path / "docs/foundation").mkdir(parents=True)
    (tmp_path / PATH).write_text("present locally")
    monkeypatch.setenv("ATELES_FOUNDATION_ROOT", str(tmp_path))
    reader = GitHubFixture(entries=[]).reader()
    reader.select_pr_head(SHA)
    result = await reader.check(TEXT)
    assert result.status == "INVALID" and result.missing == (PATH,)
    assert "correct the citation" in design_basis_block(TEXT, where="PR", check=result)


@pytest.mark.asyncio
async def test_bounds_and_no_auth_no_request(monkeypatch):
    api = GitHubFixture(events=[{"event": "commented"}] * 100)
    reader = api.reader()
    await reader.select_issue(42)
    assert len(api.calls) == 5
    assert (await reader.check(TEXT)).status == "CANNOT_CHECK"
    reader = api.reader()
    reader.headers = {}
    reader.select_pr_head(SHA)
    assert (await reader.check(TEXT)).status == "CANNOT_CHECK"
    assert len(api.calls) == 5
    monkeypatch.setattr("design_basis_snapshot._MAX_BYTES", 8)
    reader = api.reader()
    reader.select_pr_head(SHA)
    assert (await reader.check(TEXT)).status == "CANNOT_CHECK"


def test_evaluator_preserves_missing_malformed_and_declaration():
    assert check_design_basis(None).status == "MISSING"
    assert check_design_basis("not a citation").status == "INVALID"
    assert check_design_basis("no design applies — typo").ok
    assert check_design_basis(TEXT).status == "CANNOT_CHECK"

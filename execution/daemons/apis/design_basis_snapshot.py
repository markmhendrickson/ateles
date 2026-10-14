"""Invocation-local GitHub evidence for design citations; no repository writes.

The dispatcher supplies its authenticated, repository-scoped headers. Requests
stay on GitHub's API origin; citation text never becomes a URL or shell input.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import quote

import httpx

from foundation import (
    FOUNDATION_CITATION_RE,
    CitationOutcome,
    DesignBasisCheck,
    DesignSnapshot,
    check_design_basis,
)

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA = re.compile(r"[0-9a-f]{40}")
_MAX_BYTES = 4 * 1024 * 1024
_MAX_REQUESTS = 16


class SnapshotUnavailable(Exception):
    """Safe, actionable diagnostic, never a raw transport response."""


class DesignBasisReader:
    """One authorization context, one selected snapshot, bounded reads.

    Tree and path evidence is cached only on this invocation's reader. A full
    recursive tree proves absence; truncated/malformed trees prove nothing.
    """

    def __init__(self, repository: str, headers: dict[str, str], *, transport=None):
        self.repository = repository
        self.headers = headers
        self.transport = transport
        self.snapshot = DesignSnapshot(repository)
        self._requests = 0
        self._tree: dict[str, tuple[str, str]] | None = None
        self._tree_failure = ""
        self._paths: dict[str, tuple[str, str]] = {}

    async def _get(self, suffix: str, *, params=None):
        if not _REPOSITORY.fullmatch(self.repository) or any(
            part in (".", "..") for part in self.repository.split("/")
        ):
            raise SnapshotUnavailable(
                "Invalid repository identity; supply owner/repository."
            )
        if not self.headers.get("Authorization"):
            raise SnapshotUnavailable(
                "Repository credential unavailable; restore the configured repository access and retry."
            )
        self._requests += 1
        if self._requests > _MAX_REQUESTS:
            raise SnapshotUnavailable(
                "Snapshot request budget exhausted; retry with an explicit associated PR."
            )
        url = f"https://api.github.com/repos/{self.repository}/{suffix}"
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, transport=self.transport
            ) as client:
                async with client.stream(
                    "GET", url, headers=self.headers, params=params
                ) as response:
                    if response.status_code != 200:
                        raise SnapshotUnavailable(
                            f"GitHub snapshot lookup returned HTTP {response.status_code}; "
                            "verify repository access and the selected ref, then retry."
                        )
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > _MAX_BYTES:
                            raise SnapshotUnavailable(
                                "Snapshot response exceeds the read bound; retry with a bounded repository snapshot source."
                            )
                    return json.loads(chunks)
        except (httpx.HTTPError, TimeoutError):
            raise SnapshotUnavailable(
                "GitHub snapshot transport unavailable or timed out; retry the selected snapshot lookup."
            ) from None
        except (ValueError, UnicodeError):
            raise SnapshotUnavailable(
                "Malformed GitHub snapshot response; retry the selected snapshot lookup."
            ) from None

    def select_pr_head(self, sha: str) -> DesignSnapshot:
        """The review orchestrator supplies its selected head; never refresh it."""
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            self.snapshot = DesignSnapshot(
                self.repository,
                failure="Full reviewed PR head SHA unavailable; resolve the review head and retry.",
            )
        else:
            self.snapshot = DesignSnapshot(self.repository, sha, "pr_head")
        self._tree = None
        self._tree_failure = ""
        self._paths.clear()
        return self.snapshot

    async def select_issue(
        self, number: int, associated_pr: int | None = None
    ) -> DesignSnapshot:
        try:
            async with asyncio.timeout(30):
                return await self._select_issue(number, associated_pr)
        except (SnapshotUnavailable, TimeoutError) as exc:
            detail = (
                str(exc)
                if isinstance(exc, SnapshotUnavailable)
                else "Issue snapshot selection timed out; retry association lookup."
            )
            self.snapshot = DesignSnapshot(self.repository, failure=detail)
            return self.snapshot

    async def _select_issue(
        self, number: int, associated_pr: int | None
    ) -> DesignSnapshot:
        if type(number) is not int or number < 1:
            raise SnapshotUnavailable(
                "Invalid issue number; supply the issue repository and number."
            )
        candidates: set[int] = set()
        # Timeline cross-references are discovery, not trusted PR metadata.
        for page in range(1, 6):
            events = await self._get(
                f"issues/{number}/timeline", params={"per_page": 100, "page": page}
            )
            if not isinstance(events, list) or any(
                not isinstance(e, dict) for e in events
            ):
                raise SnapshotUnavailable(
                    "Malformed issue association response; retry association lookup."
                )
            for event in events:
                if event.get("event") != "cross-referenced":
                    continue
                source = event.get("source")
                issue = source.get("issue") if isinstance(source, dict) else None
                if not isinstance(issue, dict):
                    raise SnapshotUnavailable(
                        "Incomplete issue association metadata; retry association lookup."
                    )
                if "pull_request" not in issue:
                    continue
                url = issue.get("html_url", "")
                match = (
                    re.fullmatch(
                        r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)",
                        url,
                    )
                    if isinstance(url, str)
                    else None
                )
                if not match:
                    raise SnapshotUnavailable(
                        "Unverifiable associated PR identity; supply a verified PR in the issue repository."
                    )
                if match[1].lower() != self.repository.lower():
                    continue  # a reference from another repository is not membership
                if issue.get("state") == "open":
                    candidates.add(int(match[2]))
                elif issue.get("state") != "closed":
                    raise SnapshotUnavailable(
                        "Unknown associated PR state; retry association lookup."
                    )
            if len(events) < 100:
                break
        else:
            raise SnapshotUnavailable(
                "Incomplete issue association traversal; retry with complete association evidence."
            )
        if associated_pr is not None:
            if type(associated_pr) is not int or associated_pr not in candidates:
                raise SnapshotUnavailable(
                    "Explicit PR association is unverified; supply an open PR linked to this issue in the same repository."
                )
            candidates = {associated_pr}
        if len(candidates) > 1:
            raise SnapshotUnavailable(
                "Ambiguous associated PRs; explicitly select one of the linked open PRs and retry."
            )
        if candidates:
            pr_number = next(iter(candidates))
            pr = await self._get(f"pulls/{pr_number}")
            if not isinstance(pr, dict):
                raise SnapshotUnavailable(
                    "Malformed associated PR response; retry PR metadata lookup."
                )
            base = pr.get("base") or {}
            base_repo = base.get("repo") or {} if isinstance(base, dict) else {}
            head = pr.get("head") or {}
            if (
                pr.get("number") != pr_number
                or pr.get("state") != "open"
                or not isinstance(base_repo, dict)
                or str(base_repo.get("full_name", "")).lower()
                != self.repository.lower()
                or not isinstance(head, dict)
                or not _SHA.fullmatch(str(head.get("sha", "")))
            ):
                raise SnapshotUnavailable(
                    "Associated PR membership or head could not be verified; retry PR metadata in the issue repository."
                )
            return self.select_pr_head(head["sha"])
        repo = await self._get("")
        if (
            not isinstance(repo, dict)
            or str(repo.get("full_name", "")).lower() != self.repository.lower()
            or not isinstance(repo.get("default_branch"), str)
            or not repo["default_branch"]
        ):
            raise SnapshotUnavailable(
                "Default branch metadata unavailable; verify issue repository access and retry."
            )
        commit = await self._get(f"commits/{quote(repo['default_branch'], safe='')}")
        if not isinstance(commit, dict) or not _SHA.fullmatch(
            str(commit.get("sha", ""))
        ):
            raise SnapshotUnavailable(
                "Issue baseline SHA unavailable; retry default branch resolution."
            )
        self.snapshot = DesignSnapshot(self.repository, commit["sha"], "issue_baseline")
        return self.snapshot

    async def _read_tree(self) -> None:
        commit = await self._get(f"git/commits/{self.snapshot.sha}")
        if not isinstance(commit, dict) or commit.get("sha") != self.snapshot.sha:
            raise SnapshotUnavailable(
                "Selected commit could not be verified; verify repository access and retry this SHA."
            )
        tree = commit.get("tree")
        if not isinstance(tree, dict) or not _SHA.fullmatch(str(tree.get("sha", ""))):
            raise SnapshotUnavailable(
                "Malformed commit tree identity; retry the selected SHA."
            )
        result = await self._get(f"git/trees/{tree['sha']}", params={"recursive": "1"})
        if (
            not isinstance(result, dict)
            or result.get("sha") != tree["sha"]
            or result.get("truncated") is not False
            or not isinstance(result.get("tree"), list)
        ):
            raise SnapshotUnavailable(
                "Incomplete or malformed commit tree; retry with complete tree evidence at the selected SHA."
            )
        entries = {}
        for entry in result["tree"]:
            if not isinstance(entry, dict):
                raise SnapshotUnavailable(
                    "Malformed commit tree entry; retry tree lookup."
                )
            path, kind, mode = entry.get("path"), entry.get("type"), entry.get("mode")
            if (
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or any(p in ("", ".", "..") for p in path.split("/"))
                or path in entries
                or (kind, mode)
                not in {
                    ("blob", "100644"),
                    ("blob", "100755"),
                    ("blob", "120000"),
                    ("tree", "040000"),
                    ("commit", "160000"),
                }
            ):
                raise SnapshotUnavailable(
                    "Malformed commit tree entry; retry tree lookup."
                )
            entries[path] = (kind, mode)
        self._tree = entries

    async def check(self, text: str | None) -> DesignBasisCheck:
        citations = tuple(dict.fromkeys(FOUNDATION_CITATION_RE.findall(text or "")))
        if (
            citations
            and self.snapshot.sha
            and self._tree is None
            and not self._tree_failure
        ):
            try:
                async with asyncio.timeout(30):
                    await self._read_tree()
            except (SnapshotUnavailable, TimeoutError) as exc:
                self._tree_failure = (
                    str(exc)
                    if isinstance(exc, SnapshotUnavailable)
                    else "Commit tree lookup timed out; retry the selected SHA."
                )
        outcomes = []
        for citation in citations:
            path = citation.split("#", 1)[0]
            if path not in self._paths:
                failure = self.snapshot.failure or self._tree_failure
                if failure or self._tree is None:
                    value = (
                        "uncheckable",
                        failure
                        or "Snapshot unavailable; resolve a full SHA and retry.",
                    )
                else:
                    parts = path.split("/")
                    ancestors = ("/".join(parts[:i]) for i in range(1, len(parts)))
                    unsafe = any(
                        self._tree.get(p, ("tree", "040000"))[0] != "tree"
                        for p in ancestors
                    )
                    entry = self._tree.get(path)
                    if unsafe or (
                        entry is not None
                        and entry not in (("blob", "100644"), ("blob", "100755"))
                    ):
                        value = (
                            "uncheckable",
                            "Unsupported symlink, submodule or non-file path; cite a regular document in this repository.",
                        )
                    elif entry is not None:
                        value = (
                            "present",
                            "Verified regular file in the selected commit tree; anchor not checked.",
                        )
                    else:
                        value = (
                            "absent",
                            "Complete selected commit tree proves absence; correct the citation or add the document at this ref.",
                        )
                self._paths[path] = value
            status, detail = self._paths[path]
            outcomes.append(CitationOutcome(citation, path, status, detail))
        return check_design_basis(
            text, snapshot=self.snapshot, outcomes=tuple(outcomes)
        )

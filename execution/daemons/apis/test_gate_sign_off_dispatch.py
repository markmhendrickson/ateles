"""Dispatcher-side wiring for the lens-signed gate sign-off (ateles#795).

Covers the pieces of the amended ADR that live in `swarm_dispatch.py`,
separate from `IssueGateStore.sign_off` itself (see `test_gate_sign_off.py`):

- `sign_off_failure_class` — maps `gate_waive.SIGN_OFF_*` error classes to the
  public-safe vocabulary surfaced on the PR/issue, mirroring
  `review_failure_class`'s shape.
- `_surface_failed_sign_offs` — the operator-visible comment + notification
  for a sign_off failure, distinct from `_surface_denied_gate_writebacks`
  (that one is the LENS's own correct() being refused; this one is the
  DISPATCHER's signed write failing after a clean verdict).

Run: pytest execution/daemons/apis/test_gate_sign_off_dispatch.py -v
"""

from __future__ import annotations

import httpx
import pytest

from gate_waive import (
    SIGN_OFF_ENTITY_NOT_FOUND,
    SIGN_OFF_GATE_NOT_PENDING,
    SIGN_OFF_HEAD_MISMATCH,
    SIGN_OFF_NO_SIGNING_KEY,
    SIGN_OFF_SIGNING_FAILED,
    SIGN_OFF_VERIFY_FAILED,
)
from swarm_dispatch import (
    GATE_SIGN_OFF_FAILED_MARKER,
    DispatchConfig,
    SwarmDispatcher,
    sign_off_failure_class,
)


class _StubNotifier:
    def __init__(self):
        self.sent = []

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


def _config(**overrides):
    return DispatchConfig(
        **{
            "neotoma_token": "",
            "github_token": "",
            "auto_merge": False,
            "auto_rereview_on_push": False,
            **overrides,
        }
    )


def _trigger(**overrides):
    from github_gateway import SwarmTrigger

    base = dict(
        kind="pr_opened",
        repository="owner/repo",
        number=87,
        title="A pull request",
        body="Closes #80.",
        author="someone",
        html_url="https://github.com/owner/repo/pull/87",
        delivery_id="manual-test",
        action="opened",
        head_sha="a" * 40,
    )
    base.update(overrides)
    return SwarmTrigger(**base)


# ── sign_off_failure_class: public-safe error vocabulary ─────────────────────


class TestSignOffFailureClass:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (SIGN_OFF_NO_SIGNING_KEY, "lens signing key unavailable"),
            (SIGN_OFF_SIGNING_FAILED, "signed write failed"),
            (SIGN_OFF_ENTITY_NOT_FOUND, "no issue entity"),
            (SIGN_OFF_HEAD_MISMATCH, "reviewed head not confirmed"),
            (SIGN_OFF_VERIFY_FAILED, "sign-off did not read back"),
            (SIGN_OFF_GATE_NOT_PENDING, "gate not pending"),
        ],
    )
    def test_known_classes_map_to_stable_public_tokens(self, raw, expected):
        assert sign_off_failure_class(raw) == expected

    def test_unknown_class_has_a_safe_fallback(self):
        assert sign_off_failure_class("something new") == "sign-off failure"

    def test_never_echoes_raw_exception_text(self):
        """The whole point: an unrecognized error string must not be echoed
        verbatim into the public-facing surface, since a raw exception could
        carry a token, a key path, or a signed-request body."""
        secret_bearing = "RuntimeError: Bearer abc123 at /Users/x/keys/waxwing.jwk.json"
        result = sign_off_failure_class(secret_bearing)
        assert result == "sign-off failure"
        assert "abc123" not in result
        assert "jwk.json" not in result


# ── _surface_failed_sign_offs: operator-visible comment + notification ──────


class _RecordingClient:
    """Minimal httpx.AsyncClient stub recording GET (existing comments) and
    POST (new comment) calls, mirroring the pattern already used across
    test_swarm_dispatch.py for comment-posting methods."""

    def __init__(self, existing_comments: list[dict] | None = None):
        self.existing_comments = existing_comments or []
        self.posted: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        class _Resp:
            def __init__(self_inner, rows):
                self_inner._rows = rows

            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                return self_inner._rows

        return _Resp(self.existing_comments)

    async def post(self, url, json=None, headers=None):
        self.posted.append(json)

        class _Resp:
            def raise_for_status(self_inner):
                pass

        return _Resp()


@pytest.mark.asyncio
class TestSurfaceFailedSignOffs:
    async def test_empty_list_is_a_no_op(self, monkeypatch):
        client = _RecordingClient()
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
        d = SwarmDispatcher(_StubNotifier(), _config())

        await d._surface_failed_sign_offs(_trigger(), 80, [])

        assert client.posted == []

    async def test_posts_a_comment_naming_gate_lens_and_failure_class(
        self, monkeypatch
    ):
        client = _RecordingClient(existing_comments=[])
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
        notifier = _StubNotifier()
        d = SwarmDispatcher(notifier, _config())

        await d._surface_failed_sign_offs(
            _trigger(),
            80,
            [("arch", "waxwing", SIGN_OFF_VERIFY_FAILED)],
        )

        assert len(client.posted) == 1
        body = client.posted[0]["body"]
        assert GATE_SIGN_OFF_FAILED_MARKER in body
        assert "arch" in body
        assert "waxwing" in body
        assert "sign-off did not read back" in body
        assert notifier.sent, "must page the operator, not just comment"

    async def test_idempotent_on_its_own_marker(self, monkeypatch):
        """A second call for the same PR must not double-post."""
        client = _RecordingClient(
            existing_comments=[
                {"id": 1, "body": f"{GATE_SIGN_OFF_FAILED_MARKER}\nalready posted"}
            ]
        )
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
        d = SwarmDispatcher(_StubNotifier(), _config())

        await d._surface_failed_sign_offs(
            _trigger(),
            80,
            [("arch", "waxwing", SIGN_OFF_VERIFY_FAILED)],
        )

        assert client.posted == [], "marker already present — must not re-post"

    async def test_never_leaks_secret_bearing_error_text_into_the_comment(
        self, monkeypatch
    ):
        """Only the mapped public-safe class reaches the comment body, never
        an arbitrary error string a caller might pass."""
        client = _RecordingClient(existing_comments=[])
        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
        d = SwarmDispatcher(_StubNotifier(), _config())

        await d._surface_failed_sign_offs(
            _trigger(),
            80,
            [("arch", "waxwing", "raw exception: Bearer sekret at /keys/x.jwk.json")],
        )

        body = client.posted[0]["body"]
        assert "sekret" not in body
        assert "jwk.json" not in body
        assert "Bearer" not in body

"""The dispatcher owns gate-inheritance GitHub effects."""

import asyncio
import json

import httpx

import swarm_dispatch
from swarm_dispatch import SwarmDispatcher, compose_gate_block_comment
from test_swarm_dispatch import _config, _StubNotifier, _trigger
from test_tiering_call_sites import _Recorder, _pr_dispatcher, world


def test_gate_block_comment_names_only_known_pending_gates():
    body = compose_gate_block_comment(80, {"arch", "evil<script>"}, "op")
    assert "`arch`" in body
    assert "waxwing" in body.lower()
    assert "/confirm-gates-clear" in body
    assert "@op" in body
    assert "evil" not in body


def test_lanius_prompt_assigns_github_effects_to_dispatcher():
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)
    assert "Do NOT post GitHub comments or assign reviewers" in prompt
    assert "the dispatcher posts the blocking comment and routes Vanellus" in prompt
    assert "BLOCKED COMMENT REQUIREMENTS" not in prompt
    legacy_rule = prompt.index("LEGACY-ISSUE RULE:")
    authoritative_rule = prompt.index("AUTHORITATIVE FOR THIS RUN:")
    final_line_contract = prompt.index("End your reply with exactly one line:")
    assert legacy_rule < authoritative_rule < final_line_contract
    assert (
        "this instruction overrides the publication steps in your role prompt" in prompt
    )
    assert (
        "do NOT assign or request a reviewer, and make no other GitHub write"
        in prompt
    )


def test_blocked_gate_posts_once_after_lanius_result(monkeypatch, world):
    dispatcher = _pr_dispatcher(monkeypatch, world)
    _Recorder(monkeypatch, stdout={
        "lanius": "GATE_INHERITANCE: blocked\nGATE_PENDING: arch",
    })
    calls = []

    async def record_block(trigger, parent, pending):
        calls.append((trigger.number, parent, pending))

    monkeypatch.setattr(dispatcher, "_post_gate_block", record_block, raising=False)
    asyncio.run(dispatcher._handle_pr(_trigger(body="Closes #80.")))
    assert calls == [(87, 80, {"arch"})]


def test_clear_gate_never_posts_block_and_routes_vanellus(monkeypatch, world):
    dispatcher = _pr_dispatcher(monkeypatch, world)
    assert "Do NOT post GitHub comments or assign reviewers" in (
        dispatcher._lanius_pr_prompt(_trigger(), parent=80)
    )
    recorder = _Recorder(monkeypatch, stdout={
        "lanius": "GATE_INHERITANCE: clear",
        "vanellus": "**APPROVE**\nlgtm",
    })
    calls = []

    async def record_block(trigger, parent, pending):
        calls.append((trigger.number, parent, pending))

    monkeypatch.setattr(dispatcher, "_post_gate_block", record_block, raising=False)
    asyncio.run(dispatcher._handle_pr(_trigger(body="Closes #80.")))
    assert calls == []
    assert len(recorder.of("vanellus")) == 1


def test_gate_block_post_uses_dispatcher_credential_and_issue_comments(monkeypatch):
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    requests = []

    async def has_write_credential(repo, what, *, detail):
        requests.append((repo, what, detail))
        return True

    def receive(request):
        requests.append(request)
        return httpx.Response(201, json={"id": 1})

    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(receive)
    monkeypatch.setattr(dispatcher, "_require_github_write_credential", has_write_credential)
    monkeypatch.setattr(dispatcher, "_github_headers", lambda repo: {"Authorization": "test"})
    monkeypatch.setattr(
        swarm_dispatch.httpx, "AsyncClient",
        lambda **kwargs: client_class(transport=transport),
    )

    asyncio.run(dispatcher._post_gate_block(_trigger(), 80, {"arch"}))

    assert requests[0] == ("owner/repo", "gate-block comment", "owner/repo#87")
    request = requests[1]
    assert request.method == "POST"
    assert str(request.url).endswith("/repos/owner/repo/issues/87/comments")
    assert "`arch`" in json.loads(request.content)["body"]

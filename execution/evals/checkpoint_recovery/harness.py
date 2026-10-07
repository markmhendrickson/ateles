"""Shared harness for the checkpoint recovery scenario evals.

Scenario eval: recovering held tasks into resolvable checkpoints.

Drives the real recovery command (``reissue_held_checkpoints.main``) and the
real signed checkpoint writer against a stateful record stand-in
(``fake_neotoma.py``) that enforces the producer's grant, idempotency-key reuse,
and title-keyed identity.  The scenario file declares the population; every
assertion is about the resulting state of tasks and checkpoints, not about which
functions ran.

Each test names the behavior whose removal turns it red:
* dry run performs zero writes;
* applying leaves each held task held, with exactly one authenticated pending
  checkpoint, and writes no relationship on any identity;
* an exact replay creates nothing;
* a changed task keeps one resolvable current checkpoint;
* a failed persistence retires nothing;
* finished tasks are untouched;
* paging reaches every held task, and read failures are reported distinctly.
"""

from __future__ import annotations

import base64
import dataclasses
import importlib.machinery
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
sys.path.insert(0, str(REPO / "execution" / "daemons" / "apis"))
sys.path.insert(0, str(REPO / "execution" / "mcp" / "ateles"))
sys.path.insert(0, str(HERE))

# The daemon test lane does not install the MCP SDK; the checkpoint authority
# read exercised below is a plain function in server.py.
def _mcp_sdk_missing() -> bool:
    # A module already registered (the SDK, or a stand-in another test module
    # put there, which has no import spec) is not a missing one.
    if "mcp" in sys.modules:
        return False
    return importlib.util.find_spec("mcp") is None


if _mcp_sdk_missing():
    _shapes = {
        "mcp": types.ModuleType("mcp"),
        "mcp.server": types.ModuleType("mcp.server"),
        "mcp.server.stdio": types.ModuleType("mcp.server.stdio"),
        "mcp.types": types.ModuleType("mcp.types"),
    }

    class _Shape:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    _shapes["mcp.server"].Server = _Shape
    _shapes["mcp.server.stdio"].stdio_server = None
    _shapes["mcp.types"].TextContent = _Shape
    _shapes["mcp.types"].Tool = _Shape
    for _name, _module in _shapes.items():
        # A stand-in with a spec, so a later find_spec("mcp") in the same run
        # sees a module rather than raising on a missing spec.
        _module.__spec__ = importlib.machinery.ModuleSpec(_name, None)
    sys.modules.update(_shapes)

import apis  # noqa: E402
import reissue_held_checkpoints as cli  # noqa: E402
import server  # noqa: E402
from fake_neotoma import CHECKPOINT, FakeNeotoma  # noqa: E402, F401
from lib.daemon_runtime import gating  # noqa: E402
from lib.daemon_runtime.aauth_httpsig import HttpSigSigner  # noqa: E402
from lib.daemon_runtime.gating import ExecutionPolicy  # noqa: E402

SCENARIO = json.loads((HERE / "scenario.json").read_text())
HELD = [t["id"] for t in SCENARIO["tasks"] if t["status"] == "awaiting_approval"]
FINISHED = [t["id"] for t in SCENARIO["tasks"] if t["status"] != "awaiting_approval"]


def _signer(
    sub: str = "apis@ateles-swarm", kid: str = "eval-apis-key"
) -> HttpSigSigner:
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1()).private_numbers()

    def b64u(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()

    return HttpSigSigner(
        private_jwk={
            "kty": "EC",
            "crv": "P-256",
            "d": b64u(private.private_value),
            "x": b64u(private.public_numbers.x),
            "y": b64u(private.public_numbers.y),
            "sub": sub,
            "kid": kid,
        },
        sub=sub,
        iss=server.CHECKPOINT_RESOLVER_ISSUER,
        kid=kid,
    )


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append(message)

    def clear_dedupe(self, key):
        pass


class Dispatches:
    """Counts what reaches the dispatch boundary."""

    def __init__(self):
        self.calls: list[tuple[str, str, bool]] = []


@pytest.fixture
def world(monkeypatch, tmp_path):
    signer = _signer()
    resolver = _signer(sub="ateles@ateles-swarm", kid="eval-resolver-key")
    fake = FakeNeotoma(SCENARIO, signer.thumbprint)
    monkeypatch.setattr(gating, "NEOTOMA_BASE_URL", "https://neotoma.test")
    # A small page, so the scenario's population spans several; the tests that
    # matter for production set the real size back.
    monkeypatch.setattr(gating, "QUERY_PAGE_SIZE", SCENARIO["query_page_size"])
    monkeypatch.setattr(gating, "NEOTOMA_BEARER_TOKEN", "eval-token")
    # The resolver's key is pinned in every checkpoint's authority envelope.
    monkeypatch.setattr(gating, "CHECKPOINT_REQUIRED_APPROVER_JKT", resolver.thumbprint)
    monkeypatch.setattr(gating, "CHECKPOINT_PRODUCER_JKT", signer.thumbprint)
    monkeypatch.setattr(
        gating, "_checkpoint_producer_http_signer", lambda handler: signer
    )
    monkeypatch.setattr(gating.httpx, "post", fake.post)
    monkeypatch.setattr(gating.httpx, "get", fake.get)
    monkeypatch.setattr(gating.httpx, "request", fake.request)
    monkeypatch.setattr(server, "NEOTOMA_BASE_URL", "https://neotoma.test")
    monkeypatch.setattr(server, "NEOTOMA_BEARER_TOKEN", "eval-token")
    policy = ExecutionPolicy(
        entity_id="policy",
        low_blast_action_types=frozenset({"local_edit"}),
        high_blast_action_types=frozenset(),
        loaded=True,
    )
    # The live policy the daemon would load.  It is a field of the world so a
    # scenario can edit it mid-flight, the way an operator's policy repair does.
    fake.policy = policy

    def edit_policy(**changes) -> None:
        fake.policy = dataclasses.replace(fake.policy, **changes)

    fake.edit_policy = edit_policy
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _skill: fake.policy)

    # Release side: the real consumer runs; only the lifecycle writer, activity
    # log and notifier (observability, not the mechanism under test) are local.
    monkeypatch.setenv("APIS_CHECKPOINT_DENIAL_DIR", str(tmp_path / "denials"))
    (tmp_path / "denials").mkdir()
    monkeypatch.setattr(apis, "DRY_RUN", True)
    monkeypatch.setattr(apis, "READINESS_GATE", False)

    def set_status(entity_id, status, *, reason=None, **_kw):
        value = status.value if hasattr(status, "value") else str(status)
        fields = {"status": value}
        if reason is not None:
            fields["blocked_reason"] = reason
        fake._put(entity_id, "task", fields, signed=False)
        return True

    monkeypatch.setattr(apis, "set_task_status", set_status)
    monkeypatch.setattr(apis.Notifier, "from_neotoma", lambda: _Notifier())

    class _Job:
        def finished(self, message):
            return None

        def escalated(self, message):
            return None

        def failed(self, message):
            return None

    monkeypatch.setattr(apis._activity, "started", lambda message: _Job())

    dispatches = Dispatches()
    original_dispatch = apis.dispatch_task

    async def spy(entity_id, snapshot, *args, **kwargs):
        dispatches.calls.append(
            (entity_id, kwargs.get("trigger", ""), bool(kwargs.get("gate_override")))
        )
        return await original_dispatch(entity_id, snapshot, *args, **kwargs)

    monkeypatch.setattr(apis, "dispatch_task", spy)
    fake.dispatches = dispatches
    fake.resolver = resolver

    def resolver_headers(checkpoint_id: str, action: str) -> dict[str, str]:
        """Real RFC 9421 headers over the canonical resolution body."""
        from lib.daemon_runtime.checkpoint_protocol import checkpoint_resolution_body

        body = checkpoint_resolution_body(checkpoint_id, action)
        headers = resolver.sign_headers(
            method="POST",
            url=f"{server.NEOTOMA_BASE_URL.rstrip('/')}/correct",
            body=server._canonical_body_bytes(body),
            content_type="application/json",
        )
        return {**headers, "content-type": "application/json"}

    fake.resolver_headers = resolver_headers
    return fake


def run(monkeypatch, capsys, *argv) -> tuple[int, list[dict]]:
    monkeypatch.setattr(sys, "argv", ["reissue_held_checkpoints.py", *argv])
    code = cli.main()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    return code, lines


def outcomes(lines) -> dict[str, str]:
    return {line["task_id"]: line["outcome"] for line in lines if "task_id" in line}


def resolvable(world, task_id) -> list[str]:
    """Pending checkpoints whose authority the resolver surface will accept."""
    found = []
    for checkpoint_id in world.pending_checkpoints(task_id):
        record = world.record(checkpoint_id)
        if gating.read_authenticated_checkpoint_authorization(checkpoint_id, record):
            found.append(checkpoint_id)
    return found

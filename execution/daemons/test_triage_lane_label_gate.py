"""
The swarm label gate binds the two triage-label lanes: Formica and neotoma-agent.

## The failure these cover

ateles#1269 added `ATELES_SWARM_REQUIRE_LABEL` (bootstrap mode / canary lane)
and gated Apis and Anthus through `lib/daemon_runtime/label_gate.py`. Its
security review noted two more daemons that start agent work from Neotoma
`issue` / `pull_request` events on their own, keyed only on their own triage
label:

* **Formica** (`execution/daemons/formica/formica.py`) — `triage:formica`
  -> Cicada (issues) / Vanellus (PRs);
* **neotoma-agent** (`execution/daemons/neotoma-agent/neotoma_agent.py`) —
  `triage:neotoma-agent` -> Cicada / Vanellus on the neotoma repo.

With the gate set, an item carrying only the triage label still launched an
agent in both. Each case below drives the real `handle_event` with the process
spawn replaced by a recorder, and asserts whether an agent was launched:

* gate unset, triage label only -> launches (behaviour unchanged);
* gate set, triage label only -> nothing launched, nobody paged;
* gate set, triage + canary -> launches;
* gate set, canary only -> nothing (the triage label is still required);
* gate set, unreadable labels -> nothing (fails closed).

The structural half — that `_spawn_claude_skill` is each daemon's only process
launch and that every entry reaches it through the gate — lives in
`execution/daemons/apis/test_label_gate_entry_paths.py` beside the Apis and
Anthus scans.

Run: pytest execution/daemons/test_triage_lane_label_gate.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_DAEMONS = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMONS.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import NeotomaEvent  # noqa: E402
from lib.daemon_runtime import label_gate  # noqa: E402

CANARY = "swarm-canary"


def _load(name: str, rel: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _DAEMONS / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


formica = _load("formica", "formica/formica.py")
neotoma_agent = _load("neotoma_agent", "neotoma-agent/neotoma_agent.py")

# (module, its triage label, extra snapshot fields its routing needs)
LANES = [
    pytest.param(formica, "triage:formica", {}, id="formica"),
    pytest.param(
        neotoma_agent,
        "triage:neotoma-agent",
        {"repository": neotoma_agent.NEOTOMA_REPO},
        id="neotoma-agent",
    ),
]


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, msg, priority=None, handler=None, **kwargs):  # noqa: ANN001
        self.sent.append(msg)


class _Grants:
    def is_suspended(self) -> bool:
        return False


class _Job:
    def finished(self, *a, **k):  # noqa: ANN002, ANN003
        return None

    def failed(self, *a, **k):  # noqa: ANN002, ANN003
        return None


class _Proc:
    returncode = 0

    async def communicate(self, input=None):  # noqa: A002, ANN001
        return b"ok", b""

    def kill(self) -> None:
        return None


@pytest.fixture
def launched(monkeypatch, tmp_path):
    """Replace the process launch in both daemons with a recorder.

    Everything up to the launch is real: the triage check, the gate, the
    SKILL.md read, the prompt build. Only `create_subprocess_exec` is faked.
    """
    calls: list[list[str]] = []

    async def fake_exec(*cmd, **kwargs):  # noqa: ANN002, ANN003
        calls.append(list(cmd))
        return _Proc()

    async def no_hydrate(event):  # noqa: ANN001
        return None

    for skill in ("cicada", "vanellus"):
        d = tmp_path / ".claude" / "skills" / skill
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"# {skill}\n")
    monkeypatch.setenv("ATELES_REPO_PATH", str(tmp_path))

    for mod in (formica, neotoma_agent):
        monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(mod, "hydrate_snapshot", no_hydrate)
        monkeypatch.setattr(mod, "CLAUDE_BIN", "/nonexistent/claude")
        monkeypatch.setattr(mod, "DRY_RUN", False)
        monkeypatch.setattr(mod, "_label_gate_warned", set(), raising=False)
    monkeypatch.setattr(
        formica, "_activity", SimpleNamespace(started=lambda *a, **k: _Job())
    )
    return calls


def _set_gate(monkeypatch, value: str) -> None:
    if value:
        monkeypatch.setenv(label_gate.REQUIRE_LABEL_ENV, value)
    else:
        monkeypatch.delenv(label_gate.REQUIRE_LABEL_ENV, raising=False)


def _run(mod, entity_type: str, labels, extra: dict) -> _Notifier:  # noqa: ANN001
    notifier = _Notifier()
    event = NeotomaEvent(
        event_type="entity_created",
        entity_type=entity_type,
        entity_id=f"ent_{entity_type}_1",
        action="created",
        snapshot={"title": "t", "body": "b", "number": 7, "labels": labels, **extra},
    )
    asyncio.run(mod.handle_event(event, notifier, _Grants()))
    return notifier


CASES = [
    # (gate value, labels as a function of the triage label, launches?)
    pytest.param("", lambda t: [t], True, id="gate-unset-triage-only-launches"),
    pytest.param(CANARY, lambda t: [t], False, id="gate-set-triage-only-blocked"),
    pytest.param(CANARY, lambda t: [t, CANARY], True, id="gate-set-both-launches"),
    pytest.param(CANARY, lambda t: [CANARY], False, id="gate-set-canary-only-blocked"),
    pytest.param(
        CANARY, lambda t: [t, CANARY.upper()], False, id="gate-set-wrong-case-blocked"
    ),
    pytest.param(
        CANARY, lambda t: f"{t}, {CANARY}", True, id="gate-set-comma-string-launches"
    ),
]


@pytest.mark.parametrize("entity_type", ["issue", "pull_request"])
@pytest.mark.parametrize("gate,labels,should_launch", CASES)
@pytest.mark.parametrize("mod,triage,extra", LANES)
def test_triage_lane_obeys_label_gate(
    monkeypatch, launched, mod, triage, extra, gate, labels, should_launch, entity_type
):
    _set_gate(monkeypatch, gate)

    notifier = _run(mod, entity_type, labels(triage), extra)

    if should_launch:
        assert len(launched) == 1, "the item should have launched one agent"
    else:
        assert launched == [], (
            f"{mod.__name__}: {entity_type} launched an agent past the label gate"
        )
        assert notifier.sent == [], "a gated item must page nobody"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(lambda t: [t, {"name": None}, 3], id="junk-entries"),
        pytest.param(lambda t: f'["{t}", 7]', id="json-string-no-canary"),
        pytest.param(lambda t: [t, ""], id="empty-name"),
        pytest.param(lambda t: [t, [CANARY]], id="canary-nested-in-list"),
    ],
)
@pytest.mark.parametrize("mod,triage,extra", LANES)
def test_unreadable_labels_fail_closed(monkeypatch, launched, mod, triage, extra, raw):
    """The triage label is readable, the canary label is not: deny."""
    _set_gate(monkeypatch, CANARY)
    notifier = _run(mod, "issue", raw(triage), extra)
    assert launched == [] and notifier.sent == []


@pytest.mark.parametrize("mod,triage,extra", LANES)
def test_spawn_chokepoint_refuses_unlabelled_work(
    monkeypatch, launched, mod, triage, extra
):
    """`_spawn_claude_skill` re-checks the gate, so a future caller that skips
    `handle_event` still cannot launch an agent on unlabelled work."""
    _set_gate(monkeypatch, CANARY)
    snap = {"title": "t", "labels": [triage], **extra}
    asyncio.run(mod._spawn_claude_skill("cicada", "ent_x", snap, _Notifier()))
    assert launched == []

    snap["labels"] = [triage, CANARY]
    asyncio.run(mod._spawn_claude_skill("cicada", "ent_x", snap, _Notifier()))
    assert len(launched) == 1


@pytest.mark.parametrize("mod,triage,extra", LANES)
def test_gated_skip_warns_once_then_debug(
    monkeypatch, launched, caplog, mod, triage, extra
):
    _set_gate(monkeypatch, CANARY)
    with caplog.at_level(logging.DEBUG, logger=mod.log.name):
        _run(mod, "issue", [triage], extra)
        _run(mod, "issue", [triage], extra)
    skips = [r for r in caplog.records if "label gate active" in r.message]
    assert [r.levelno for r in skips] == [logging.WARNING, logging.DEBUG]
    assert launched == []


def test_neotoma_agent_task_hygiene_is_not_gated(monkeypatch, launched):
    """Task due-date hygiene writes a correction and launches no agent, so it
    stays outside the gate (unchanged)."""
    _set_gate(monkeypatch, CANARY)
    seen: list[str] = []

    async def hygiene(entity_id, snapshot):  # noqa: ANN001
        seen.append(entity_id)

    monkeypatch.setattr(neotoma_agent, "apply_due_date_hygiene", hygiene)
    monkeypatch.setattr(neotoma_agent, "DUE_DATE_HYGIENE_ENABLED", True)
    _run(neotoma_agent, "task", [], {})
    assert seen == ["ent_task_1"] and launched == []

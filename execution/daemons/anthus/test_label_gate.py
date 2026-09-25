"""
The label gate binds Anthus's issue/PR orchestration, not only Apis.

## The failure these cover

ateles#1269 added `ATELES_SWARM_REQUIRE_LABEL` so that, in bootstrap mode, the
swarm acts automatically only on labelled (canary) work. Rounds 1-3 gated
every entry inside Apis. Falco's round-3 security review found a second,
independent automatic entry: Anthus subscribes to Neotoma `issue` /
`pull_request` events and, in `_orchestrate_workflow_for`, selects a
workflow (falling back to `feature` when no label matches) and spawns each
ready gate owner via `claude --print`. Nothing there read the gate, so an
unlabelled issue or PR still got automatic gate-lens runs with the gate set.

Each case below drives the real `_orchestrate_workflow_for` with the I/O
stubbed, and records whether workflow selection and agent spawning were
reached:

* gate unset -> unchanged, orchestration proceeds for unlabelled work;
* gate set, unlabelled issue/PR -> nothing selected, nothing spawned;
* gate set, labelled issue / labelled PR / PR whose parent issue is
  labelled -> proceeds;
* unreadable labels, a failed GitHub read, a PR with no number -> denied.

Run: pytest execution/daemons/anthus/test_label_gate.py -v
"""

from __future__ import annotations

import asyncio
import logging

import anthus
import orchestrator
import participation
import pytest

CANARY = "swarm-canary"
REPO = "markmhendrickson/ateles"


def _workflow() -> orchestrator.WorkflowDefinition:
    return orchestrator.WorkflowDefinition(
        entity_id="ent_wf_gate",
        project="ateles",
        workflow_type="feature",
        description="label-gate fixture",
        gates=[
            orchestrator.Gate(
                phase=1,
                gate_name="pm",
                owner_agent="pavo",
                parallel_group=None,
                join_gate=None,
                required=True,
                precondition=None,
            )
        ],
        fast_paths=[],
        legal_required=False,
    )


class _Notifier:
    def send(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


class _AgentDef:
    entity_id = "ent_agentdef"
    last_observation_id = "obs_1"


class _Loader:
    def __init__(self, name):  # noqa: ANN001
        self.name = name

    def load(self):
        return _AgentDef()


@pytest.fixture
def harness(monkeypatch):
    """Stub every I/O edge of `_orchestrate_workflow_for` and record what it
    reached. `github` maps ("pr"|"issue", number) -> {"labels", "body"} for the
    gate's live GitHub read; a missing key makes that read fail (None)."""
    rec = {"workflow_fetch": [], "selected": [], "spawned": [], "gh": []}
    github: dict[tuple[str, int], dict] = {}

    async def fetch_wf(project):  # noqa: ANN001
        rec["workflow_fetch"].append(project)
        return [_workflow()]

    def select(snap, workflows):  # noqa: ANN001
        rec["selected"].append(snap.get("github_number"))
        return workflows[0]

    async def unmet(wf, project):  # noqa: ANN001
        return set()

    async def load_state(work_entity_id):  # noqa: ANN001
        return {}

    async def record_dispatched(**kwargs):  # noqa: ANN003
        return None

    async def spawn(**kwargs):  # noqa: ANN003
        rec["spawned"].append(kwargs["work_entity_id"])

    async def comments(snap):  # noqa: ANN001
        return []

    async def harvest(c):  # noqa: ANN001
        return None

    async def gh(kind, repo, number):  # noqa: ANN001
        rec["gh"].append((kind, number))
        return github.get((kind, number))

    monkeypatch.setattr(orchestrator, "fetch_workflow_definitions", fetch_wf)
    monkeypatch.setattr(orchestrator, "select_workflow", select)
    monkeypatch.setattr(orchestrator, "resolve_unmet_preconditions", unmet)
    monkeypatch.setattr(participation, "load_state_for", load_state)
    monkeypatch.setattr(participation, "record_dispatched", record_dispatched)
    monkeypatch.setattr(anthus, "_spawn_agent", spawn)
    monkeypatch.setattr(anthus, "_fetch_comments", comments)
    monkeypatch.setattr(anthus, "_harvest_drift_signals", harvest)
    monkeypatch.setattr(anthus, "AgentLoader", _Loader)
    monkeypatch.setattr(anthus, "_notifier", _Notifier(), raising=False)
    # Missing on purpose until the gate exists: raising=False lets this
    # fixture install on the pre-fix code, so the red run fails on the
    # assertions below rather than on setup.
    monkeypatch.setattr(anthus, "_fetch_github_labels_and_body", gh, raising=False)
    monkeypatch.setattr(anthus, "_label_gate_warned", set(), raising=False)
    anthus._gate_states.clear()
    rec["github"] = github
    return rec


def _run(entity_type: str, entity_id: str, snapshot: dict) -> None:
    ev = anthus.NeotomaEvent(
        entity_type=entity_type,
        entity_id=entity_id,
        action="updated",
        snapshot=snapshot,
    )
    asyncio.run(anthus._orchestrate_workflow_for(ev))


def _launched(rec) -> bool:
    return bool(rec["selected"] or rec["spawned"])


# ── gate unset: behaviour unchanged ──────────────────────────────────────────


def test_gate_unset_unlabelled_issue_orchestrates(monkeypatch, harness):
    monkeypatch.delenv("ATELES_SWARM_REQUIRE_LABEL", raising=False)
    _run("issue", "ent_i1", {"repository": REPO, "github_number": 11, "labels": []})
    assert harness["spawned"] == ["ent_i1"]
    assert harness["gh"] == [], "gate unset must make no extra GitHub reads"


def test_gate_unset_unlabelled_pr_orchestrates(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", "   ")  # whitespace == unset
    _run("pull_request", "ent_p1", {"repository": REPO, "github_number": 12})
    assert harness["spawned"] == ["ent_p1"]
    assert harness["gh"] == []


# ── gate set: issues ─────────────────────────────────────────────────────────


def test_gate_set_unlabelled_issue_launches_nothing(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    _run("issue", "ent_i2", {"repository": REPO, "github_number": 21, "labels": ["bug"]})
    assert not _launched(harness), harness
    assert harness["workflow_fetch"] == [], "gate must run before workflow selection"


def test_gate_set_labelled_issue_proceeds(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    _run(
        "issue",
        "ent_i3",
        {"repository": REPO, "github_number": 22, "labels": ["bug", CANARY]},
    )
    assert harness["spawned"] == ["ent_i3"]


@pytest.mark.parametrize(
    "labels",
    [
        None,
        "not json [",
        f"[{CANARY!r}",  # truncated JSON array
        42,
        [None, 7, {"nam": CANARY}],
        [CANARY.upper()],
        [f"{CANARY} "],
    ],
    ids=["none", "garbage-string", "broken-json", "number", "junk-entries", "case", "padded"],
)
def test_gate_set_unreadable_or_near_miss_issue_labels_deny(monkeypatch, harness, labels):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    _run("issue", "ent_i4", {"repository": REPO, "github_number": 23, "labels": labels})
    assert not _launched(harness), harness


def test_gate_set_issue_label_encodings_that_are_read(monkeypatch, harness):
    """Neotoma writers have used a JSON string and a comma string; both read."""
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    _run("issue", "ent_i5", {"repository": REPO, "github_number": 24, "labels": f'["{CANARY}"]'})
    _run("issue", "ent_i6", {"repository": REPO, "github_number": 25, "labels": f"bug, {CANARY}"})
    assert harness["spawned"] == ["ent_i5", "ent_i6"]


# ── gate set: pull requests ──────────────────────────────────────────────────


def test_gate_set_unlabelled_pr_launches_nothing(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    harness["github"][("pr", 31)] = {"labels": ["bug"], "body": "no parent here"}
    _run("pull_request", "ent_p2", {"repository": REPO, "github_number": 31})
    assert not _launched(harness), harness


def test_gate_set_pr_labelled_on_github_proceeds(monkeypatch, harness):
    """Neotoma pull_request entities carry no labels; GitHub is read live."""
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    harness["github"][("pr", 32)] = {"labels": [CANARY], "body": ""}
    _run("pull_request", "ent_p3", {"repository": REPO, "github_number": 32})
    assert harness["spawned"] == ["ent_p3"]


def test_gate_set_pr_labelled_on_snapshot_proceeds_without_github(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    _run(
        "pull_request",
        "ent_p4",
        {"repository": REPO, "github_number": 33, "labels": [CANARY]},
    )
    assert harness["spawned"] == ["ent_p4"]
    assert harness["gh"] == []


def test_gate_set_pr_inherits_parent_issue_label(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    harness["github"][("pr", 34)] = {"labels": [], "body": "Part of #900"}
    harness["github"][("issue", 900)] = {"labels": [CANARY], "body": ""}
    _run("pull_request", "ent_p5", {"repository": REPO, "github_number": 34})
    assert harness["spawned"] == ["ent_p5"]
    assert harness["gh"] == [("pr", 34), ("issue", 900)]


def test_gate_set_pr_with_unlabelled_parent_launches_nothing(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    harness["github"][("pr", 35)] = {"labels": [], "body": "Closes #901"}
    harness["github"][("issue", 901)] = {"labels": ["bug"], "body": ""}
    _run("pull_request", "ent_p6", {"repository": REPO, "github_number": 35})
    assert not _launched(harness), harness


def test_gate_set_pr_cross_repo_parent_does_not_vouch(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    harness["github"][("pr", 36)] = {"labels": [], "body": "Closes other/repo#902"}
    harness["github"][("issue", 902)] = {"labels": [CANARY], "body": ""}
    _run("pull_request", "ent_p7", {"repository": REPO, "github_number": 36})
    assert not _launched(harness), harness
    assert ("issue", 902) not in harness["gh"]


def test_gate_set_pr_github_read_failure_denies(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    # No ("pr", 37) entry: the live read returns None.
    _run("pull_request", "ent_p8", {"repository": REPO, "github_number": 37})
    assert not _launched(harness), harness


def test_gate_set_pr_parent_read_failure_denies(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    harness["github"][("pr", 38)] = {"labels": [], "body": "Refs #903"}
    _run("pull_request", "ent_p9", {"repository": REPO, "github_number": 38})
    assert not _launched(harness), harness


def test_gate_set_pr_without_number_denies(monkeypatch, harness):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    _run("pull_request", "ent_p10", {"repository": REPO})
    assert not _launched(harness), harness
    assert harness["gh"] == []


# ── through the real event entry, and logging ────────────────────────────────


def test_handle_event_unlabelled_issue_launches_nothing(monkeypatch, harness):
    """The SSE handler is the daemon's only door to orchestration; drive it."""
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)

    async def hydrate(event):  # noqa: ANN001
        event.snapshot = {"repository": REPO, "github_number": 41, "labels": []}
        event.hydrated = True
        return event

    monkeypatch.setattr(anthus, "hydrate_snapshot", hydrate)
    ev = anthus.NeotomaEvent(
        entity_type="issue", entity_id="ent_i41", action="created", snapshot={}
    )
    asyncio.run(anthus.handle_event(ev))
    assert not _launched(harness), harness


def test_skip_warns_once_per_item_then_debug(monkeypatch, harness, caplog):
    monkeypatch.setenv("ATELES_SWARM_REQUIRE_LABEL", CANARY)
    snap = {"repository": REPO, "github_number": 51, "labels": ["bug"]}
    with caplog.at_level(logging.DEBUG, logger=anthus.log.name):
        _run("issue", "ent_i51", dict(snap))
        _run("issue", "ent_i51", dict(snap))
    skips = [r for r in caplog.records if "label gate active" in r.message]
    assert [r.levelno for r in skips] == [logging.WARNING, logging.DEBUG]
    assert not _launched(harness)

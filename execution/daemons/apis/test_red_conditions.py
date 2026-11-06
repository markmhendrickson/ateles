"""Apis records each red alert once per condition, durably (red_conditions.py).

No real transport and no network: the base ``Notifier.send`` is replaced by a
recorder, and the state file lives under a temp path set through the env.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import red_conditions as rc
from lib.notify import Notifier, Priority

HERE = Path(__file__).resolve().parent
RED_APPROVED_DIRTY = (
    "🔴 acme/widgets#12 was APPROVED {age}d ago and has since rotted to "
    "CONFLICTING — it can no longer be merged without a rebase. "
    "https://example.test/acme/widgets/pull/12"
)
RED_MERGEABLE = (
    "🔴 acme/widgets#12 has been APPROVED and mergeable ({state}) for {age}d "
    "with nothing merging it. Merge is the only remaining step and it needs you."
)
NO_FORMAL_REVIEW = (
    "🔴 acme/widgets#13 has NO FORMAL REVIEW and an empty reviewDecision. "
    "That is an absence of evidence."
)
ESCALATION = (
    "🔴 acme/widgets#14 has no formal review after 3 automatic re-dispatches. "
    "The review agent is failing on this PR."
)
PLAN_NOTICE = "Plan checkpoint ready: please decide."


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "apis-red-conditions.json"
    monkeypatch.setenv(rc.STATE_ENV, str(path))
    return path


@pytest.fixture
def base_sends(monkeypatch):
    calls: list[tuple] = []

    def fake_send(self, *args, **kwargs):
        calls.append((args, kwargs))
        return "base-result"

    monkeypatch.setattr(Notifier, "send", fake_send)
    return calls


def conditions(path: Path) -> dict:
    return json.loads(path.read_text())["conditions"]


def make() -> rc.ApisNotifier:
    return rc.ApisNotifier()


def age_file(path: Path, seconds: int) -> None:
    doc = json.loads(path.read_text())
    doc["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(doc))


# ── the filter ───────────────────────────────────────────────────────────────


def test_no_formal_review_alert_is_not_recorded_but_other_red_is(state, base_sends):
    n = make()
    n.send(NO_FORMAL_REVIEW, priority=Priority.BLOCKER)
    assert conditions(state) == {}
    n.send(ESCALATION, priority=Priority.BLOCKER)
    got = conditions(state)
    assert [v["kind"] for v in got.values()] == ["review-escalation"]
    assert [v["subject"] for v in got.values()] == ["acme/widgets#14"]


def test_non_red_blocker_is_not_recorded(state, base_sends):
    make().send(PLAN_NOTICE, priority=Priority.BLOCKER)
    assert conditions(state) == {}


def test_lowercase_escalation_is_surfaced_and_uppercase_is_not():
    assert rc.is_surfaced_red(ESCALATION)
    assert not rc.is_surfaced_red(NO_FORMAL_REVIEW)
    assert not rc.is_surfaced_red("  " + PLAN_NOTICE)


# ── once per condition, across restarts ──────────────────────────────────────


def test_same_condition_is_recorded_once_across_a_new_instance(state, base_sends):
    make().send(ESCALATION, priority=Priority.BLOCKER)
    first = conditions(state)
    (key,) = first
    first_entry = dict(first[key])
    restarted = make()
    restarted.send(ESCALATION, priority=Priority.BLOCKER)
    assert conditions(state) == {key: first_entry}


def test_age_and_mergeable_state_do_not_change_identity(state, base_sends):
    n = make()
    n.send(RED_APPROVED_DIRTY.format(age=9))
    n.send(RED_APPROVED_DIRTY.format(age=10))
    n.send(RED_MERGEABLE.format(state="unknown", age=3))
    n.send(RED_MERGEABLE.format(state="clean", age=4))
    kinds = sorted(v["kind"] for v in conditions(state).values())
    assert kinds == ["approved-conflicting", "approved-mergeable"]


def test_unrecognised_red_surfaces_and_repeats_collapse(state, base_sends):
    n = make()
    n.send("🔴 something new broke 17 times")
    n.send("🔴 something new broke 18 times")
    n.send("🔴 a different thing broke")
    got = conditions(state)
    assert len(got) == 2
    assert {v["kind"] for v in got.values()} == {"unrecognised"}
    assert {v["subject"] for v in got.values()} == {""}


def test_lens_and_release_subjects():
    assert rc.classify(
        "🔴 acme/widgets#5: the 'security' lens has produced no verdict after 3"
    ) == ("lens-no-verdict", "acme/widgets#5 security")
    assert rc.classify("🔴 Release 1.2.3 publish TIMED OUT after 60s") == (
        "release-failed",
        "1.2.3",
    )
    assert rc.classify(
        "🔴 acme/widgets#6: the author pushed a revision answering"
    ) == ("revision-stuck", "acme/widgets#6")


# ── the send is never disturbed ──────────────────────────────────────────────


def test_super_send_called_with_same_arguments_and_result_returned(state, base_sends):
    result = make().send(
        ESCALATION, Priority.BLOCKER, "apis", True, "dk", False
    )
    assert result == "base-result"
    assert base_sends[-1][0] == (ESCALATION, Priority.BLOCKER, "apis", True, "dk", False)


def test_recording_failure_never_changes_the_send(state, base_sends, monkeypatch):
    n = make()

    def boom(self, message):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(rc.ApisNotifier, "_record_red", boom)
    assert n.send(ESCALATION, priority=Priority.BLOCKER) == "base-result"
    assert len(base_sends) == 1


def test_construction_failure_does_not_stop_apis(tmp_path, monkeypatch, base_sends):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(rc.STATE_ENV, str(blocker / "sub" / "state.json"))
    n = make()  # must not raise
    assert n.send(ESCALATION) == "base-result"


# ── file integrity ───────────────────────────────────────────────────────────


def test_file_created_at_construction_with_created_at(state, base_sends):
    assert not state.exists()
    make()
    doc = json.loads(state.read_text())
    assert doc["schema_version"] == 1 and doc["conditions"] == {}
    assert rc._parse_iso(doc["created_at"]) is not None
    assert oct(state.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    "content", ["not json {", json.dumps({"schema_version": 2, "conditions": {}}),
    json.dumps([1, 2])],
)
def test_unreadable_file_is_not_overwritten(state, base_sends, content):
    state.write_text(content)
    n = make()
    assert n.send(ESCALATION) == "base-result"
    assert state.read_text() == content


def test_missing_file_after_construction_is_not_recreated(state, base_sends):
    n = make()
    state.unlink()
    assert n.send(ESCALATION) == "base-result"
    assert not state.exists()


# ── backlog ──────────────────────────────────────────────────────────────────


def test_backlog_inside_window_for_approved_kinds_only(state, base_sends):
    n = make()
    n.send(RED_APPROVED_DIRTY.format(age=9))
    n.send(ESCALATION)
    got = {v["kind"]: v["backlog"] for v in conditions(state).values()}
    assert got == {"approved-conflicting": True, "review-escalation": False}


def test_approved_after_the_window_is_not_backlog(state, base_sends):
    make()
    age_file(state, rc.BACKLOG_WINDOW_SECONDS + 60)
    make().send(RED_APPROVED_DIRTY.format(age=9))
    (entry,) = conditions(state).values()
    assert entry["backlog"] is False


@pytest.mark.parametrize("created", [None, "garbage", 5])
def test_missing_or_malformed_created_at_closes_the_window(state, base_sends, created):
    make()
    doc = json.loads(state.read_text())
    if created is None:
        doc.pop("created_at")
    else:
        doc["created_at"] = created
    state.write_text(json.dumps(doc))
    make().send(RED_APPROVED_DIRTY.format(age=9))
    (entry,) = conditions(state).values()
    assert entry["backlog"] is False


# ── no free text stored ──────────────────────────────────────────────────────


def test_no_free_text_reaches_the_file(state, base_sends):
    n = make()
    n.send(
        "🔴 Release 1.2.3 email-approved but publish FAILED (rc=1): "
        "Traceback SECRETSTDERRTAIL boom. See phoenicurus-release.log."
    )
    n.send("🔴 Release 1.2.4 email-approved but publish.py is missing at "
           "/Users/someone/private/path/publish.py — publish did NOT run.")
    n.send(RED_APPROVED_DIRTY.format(age=9) + " PR TITLE SHOULD NOT APPEAR")
    text = state.read_text()
    for leak in ("SECRETSTDERRTAIL", "/Users/someone", "Traceback", "TITLE", "http"):
        assert leak not in text
    assert {v["subject"] for v in conditions(state).values()} == {
        "1.2.3",
        "1.2.4",
        "acme/widgets#12",
    }


# ── drift guard ──────────────────────────────────────────────────────────────

_PLACEHOLDERS = {"ref": "acme/widgets#7", "version": "1.2.3", "lens": "qa"}


def _red_literals(source: str) -> list[str]:
    found: list[str] = []
    tree = ast.parse(source)
    inside_fstring = {
        id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values
    }
    for node in ast.walk(tree):
        if id(node) in inside_fstring:
            continue
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    name = v.value.id if isinstance(v.value, ast.Name) else ""
                    parts.append(_PLACEHOLDERS.get(name, "1"))
            text = "".join(parts)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        else:
            continue
        if text.startswith(rc.RED_MARKER):
            found.append(text)
    return found


def test_every_red_alert_in_swarm_dispatch_is_classified_or_excluded():
    literals = _red_literals((HERE / "swarm_dispatch.py").read_text())
    assert len(literals) >= 11, "drift guard found too few red alerts: instrument broken"
    excluded = [t for t in literals if not rc.is_surfaced_red(t)]
    assert excluded, "expected the NO FORMAL REVIEW alert to be found and excluded"
    for text in literals:
        if rc.is_surfaced_red(text):
            kind, _ = rc.classify(text)
            assert kind in rc.KNOWN_KINDS, f"unclassified red alert: {text[:70]!r}"
        else:
            assert rc.EXCLUDED_PHRASE in text
    assert {rc.classify(t)[0] for t in literals if rc.is_surfaced_red(t)} == set(
        rc.KNOWN_KINDS
    )


def test_swarm_dispatch_is_the_only_module_with_red_alerts():
    holders = {
        p.name
        for p in HERE.glob("*.py")
        if not p.name.startswith("test_")
        and p.name != "red_conditions.py"
        and rc.RED_MARKER in p.read_text()
    }
    assert holders == {"swarm_dispatch.py"}, holders


# ── wiring ───────────────────────────────────────────────────────────────────


def test_apis_main_constructs_an_apis_notifier_not_a_plain_one():
    src = (HERE / "apis.py").read_text()
    assert re.search(r"^\s+notifier = ApisNotifier\.from_neotoma\(\)", src, re.M)
    assert not re.search(r"=\s*Notifier\.from_neotoma\(\)", src)
    assert re.search(r"^from red_conditions import ApisNotifier", src, re.M)

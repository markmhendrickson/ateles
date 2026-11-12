"""set_release_status must take effect after a prior correction, and verify it.

Fake Neotoma semantics: a correction pins a field (priority 1000) and a later
/store observation of that field is ignored, exactly as in production. The
v0.28.0 symptom: log said "status -> published", snapshot still read approved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import publish  # noqa: E402

ENT = "ent_release_1"


class FakeNeotoma:
    """Several release_result entities may share one version."""

    def __init__(self, status="approved", pinned=("status",), entities=None):
        ids = [ENT] if entities is None else entities
        self.snaps = {e: {"version": "v0.28.0", "status": status} for e in ids}
        self.pinned = {e: set(pinned) for e in ids}
        self.stores = 0

    @property
    def snap(self):
        return self.snaps[ENT]

    def query(self, entity_type, limit=100):
        return [{"entity_id": e, "snapshot": dict(s)} for e, s in self.snaps.items()]

    def store(self, entities, key):
        self.stores += 1
        return {"ok": True}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/correct":
            body = json.loads(request.content)
            eid = body["entity_id"]
            if eid not in self.snaps:
                return httpx.Response(404)
            self.snaps[eid][body["field"]] = body["value"]
            self.pinned[eid].add(body["field"])
            return httpx.Response(200, json={"success": True})
        if request.method == "GET" and request.url.path.startswith("/entities/"):
            eid = request.url.path.rsplit("/", 1)[1]
            if eid in self.snaps:
                return httpx.Response(200, json={"snapshot": dict(self.snaps[eid])})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    f = FakeNeotoma()
    monkeypatch.setattr(publish, "NEOTOMA_BASE_URL", "https://neotoma.test")
    monkeypatch.setattr(publish, "neotoma_query", f.query)
    monkeypatch.setattr(publish, "neotoma_store", f.store)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(f.handler), **kw),
    )
    return f


def test_store_only_write_is_ignored_after_a_correction(fake):
    """The old behaviour: documents why the status write needed to change."""
    # Production semantics: a /store of a pinned field is dropped. The fake's
    # store models that as a no-op, which is why status no longer uses it.
    publish.neotoma_store([{"entity_type": "release_result", "status": "published"}], "k")
    assert fake.snap["status"] == "approved"


def test_status_takes_effect_after_a_correction_and_is_read_back(fake):
    publish.set_release_status("v0.28.0", "publishing")
    publish.set_release_status("v0.28.0", "published", {"release_url": "https://x/r"})
    assert fake.snap["status"] == "published"
    assert fake.snap["release_url"] == "https://x/r"


def test_unconfirmed_readback_raises_and_logs_error(fake, monkeypatch, caplog):
    # A server that accepts the correction but never reflects it.
    monkeypatch.setattr(
        fake, "handler",
        lambda r: httpx.Response(200, json={"snapshot": {"status": "approved"}}),
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(publish.StatusWriteError):
            publish.set_release_status("v0.28.0", "published")
    assert any("NOT confirmed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("entities", [[], ["ent_a", "ent_b"]])
def test_unresolvable_entity_fails_closed_without_writing(fake, entities):
    fake.snaps = {e: {"version": "v0.28.0", "status": "approved"} for e in entities}
    fake.pinned = {e: {"status"} for e in entities}
    with pytest.raises(publish.StatusWriteError):
        publish.set_release_status("v0.28.0", "published")
    assert all(sn["status"] == "approved" for sn in fake.snaps.values())
    assert fake.stores == 0


def test_main_halts_without_writing_failed_when_status_write_fails(monkeypatch):
    sent = []
    calls = []
    monkeypatch.setattr(sys, "argv", ["publish.py", "--version", "v0.28.0"])
    monkeypatch.setattr(publish, "find_release", lambda v, e: {"entity_id": ENT, "snapshot": {"version": "v0.28.0"}})
    monkeypatch.setattr(publish, "telegram_send", sent.append)

    def boom(*a, **k):
        raise publish.StatusWriteError("unconfirmed")

    monkeypatch.setattr(publish, "publish_release", boom)
    monkeypatch.setattr(publish, "set_release_status", lambda *a, **k: calls.append(a))
    assert publish.main() == 1
    assert calls == []  # never tries to record "failed" through the broken path
    assert "unverified" in sent[0]


def _two(fake, other_status="pending_approval"):
    fake.snaps["ent_other"] = {"version": "v0.28.0", "status": other_status}
    fake.pinned["ent_other"] = set()


def test_explicit_entity_id_wins_over_duplicate_versions(fake):
    _two(fake)
    publish.set_release_status("v0.28.0", "publishing", entity_id=ENT)
    assert fake.snaps[ENT]["status"] == "publishing"
    assert fake.snaps["ent_other"]["status"] == "pending_approval"


def test_extras_land_on_the_corrected_entity_not_a_duplicate(fake):
    _two(fake)
    publish.set_release_status(
        "v0.28.0", "published", {"release_url": "https://x/r", "reason": "ok"},
        entity_id=ENT,
    )
    assert fake.snaps[ENT]["release_url"] == "https://x/r"
    assert fake.snaps[ENT]["reason"] == "ok"
    assert "release_url" not in fake.snaps["ent_other"]
    assert fake.stores == 0


def test_rc_alongside_terminal_failed_record_publishes(fake):
    fake.snaps["ent_old"] = {"version": "v0.28.0", "status": "failed"}
    fake.pinned["ent_old"] = set()
    release = {"entity_id": ENT, "snapshot": {"version": "v0.28.0", "status": "approved"}}
    seen = []
    orig = publish.set_release_status
    publish_steps = ("preflight", "merge_rc_pr", "preflight_post_merge", "tag_and_push",
                     "npm_publish", "github_release", "deploy_sandbox",
                     "publish_github_release_draft")
    return_summary = "https://x/rel"
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        for step in publish_steps:
            mp.setattr(publish, step, lambda *a, **k: None)
        mp.setattr(publish, "post_release", lambda *a, **k: return_summary)
        mp.setattr(publish, "telegram_send", lambda *a, **k: None)
        mp.setattr(
            publish, "set_release_status",
            lambda *a, **k: (seen.append(k.get("entity_id")), orig(*a, **k))[1],
        )
        publish.publish_release(release, "v0.28.0", False, False)
    finally:
        mp.undo()
    assert seen and set(seen) == {ENT}
    assert fake.snaps[ENT]["status"] == "published"
    assert fake.snaps["ent_old"]["status"] == "failed"


def test_email_approval_flip_with_duplicate_rcs(fake):
    fake.snaps[ENT]["status"] = "pending_approval"
    _two(fake)
    release = {"entity_id": ENT, "snapshot": {"version": "v0.28.0", "status": "pending_approval"}}
    mp = pytest.MonkeyPatch()
    try:
        for step in ("preflight", "merge_rc_pr", "preflight_post_merge", "tag_and_push",
                     "npm_publish", "github_release", "deploy_sandbox",
                     "publish_github_release_draft"):
            mp.setattr(publish, step, lambda *a, **k: None)
        mp.setattr(publish, "post_release", lambda *a, **k: "s")
        mp.setattr(publish, "telegram_send", lambda *a, **k: None)
        publish.publish_release(release, "v0.28.0", False, False, email_approval=True)
    finally:
        mp.undo()
    assert fake.snaps[ENT]["status"] == "published"
    assert fake.snaps["ent_other"]["status"] == "pending_approval"


def test_failed_status_targets_the_held_entity(fake, monkeypatch):
    _two(fake)
    monkeypatch.setattr(sys, "argv", ["publish.py", "--entity-id", ENT])
    monkeypatch.setattr(
        publish, "find_release",
        lambda v, e: {"entity_id": ENT, "snapshot": {"version": "v0.28.0"}},
    )
    monkeypatch.setattr(publish, "telegram_send", lambda *a, **k: None)

    def boom(*a, **k):
        raise publish.StepError("nope")

    monkeypatch.setattr(publish, "publish_release", boom)
    assert publish.main() == 1
    assert fake.snaps[ENT]["status"] == "failed"
    assert "reason" in fake.snaps[ENT]
    assert fake.snaps["ent_other"]["status"] == "pending_approval"

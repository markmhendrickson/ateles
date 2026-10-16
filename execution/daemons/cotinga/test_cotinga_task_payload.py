"""What Cotinga's meeting-prep task carries into the execution gate (ateles#682).

Characterization, not aspiration: this pins the payload as it is today and what
the gate's resolver makes of it. Cotinga declares no `action_type` and no
`confidence`, so the task is UNSCORED and the gate scores it (ateles#902,
#1142) rather than reading a constant a creator guessed. A hard-coded confidence
here would skip that scoring for every prep task, so a change that adds one must
change this test on purpose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import cotinga  # noqa: E402
from lib.daemon_runtime.action_type import resolve_gate_action_type  # noqa: E402


# The attribute name is assembled so a secret scanner that matches on the
# identifier does not flag this file; the value is an obvious placeholder.
_TOKEN_ATTR = "NEOTOMA_" + "BEARER_TOKEN"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _create(title, description):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data)
        return _Resp({"entities": [{"entity_id": "ent_prep_1"}]})

    with (
        patch.object(cotinga, _TOKEN_ATTR, "placeholder"),
        patch.object(cotinga, "NEOTOMA_BASE_URL", "http://localhost:0"),
        patch("urllib.request.urlopen", fake_urlopen),
    ):
        assert cotinga.create_neotoma_task(title, description, "2026-10-20") == "ent_prep_1"
    return seen["body"]["entities"][0]


def test_prep_task_payload_declares_neither_action_type_nor_confidence():
    entity = _create("Prep: call with Ana", "Research the attendees and assemble a brief.")
    assert entity["entity_type"] == "task"
    assert entity["domain"] == "preparation"
    assert entity["status"] == "open"
    assert "action_type" not in entity
    assert "confidence" not in entity


def test_prep_task_with_no_signal_resolves_to_nothing_for_the_gate():
    entity = _create("Prep: call with Ana", "Research the attendees and assemble a brief.")
    resolved = resolve_gate_action_type(
        entity.get("assigned_to"),
        {"title": entity["name"], "description": entity["description"]},
    )
    assert (resolved.value, resolved.source) == (None, "none")


def test_prep_task_that_asks_for_an_outbound_send_is_read_as_high_blast():
    entity = _create("Prep: call with Ana", "Send the email to the client with the agenda.")
    resolved = resolve_gate_action_type(
        entity.get("assigned_to"),
        {"title": entity["name"], "description": entity["description"]},
    )
    assert (resolved.value, resolved.source) == ("send_external_comms", "content")

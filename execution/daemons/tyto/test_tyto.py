"""
Unit tests for execution/daemons/tyto/tyto.py — OcrConsumer (pending_ocr consumer).

Run with: python -m pytest execution/daemons/tyto/test_tyto.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path bootstrap — mirror aquila/riparia convention (Tyto is a standalone
# script, not an installed package).
# ---------------------------------------------------------------------------

_TYTO_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TYTO_DIR.parent.parent.parent

for _p in (str(_REPO_ROOT), str(_TYTO_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_tyto():
    spec = importlib.util.spec_from_file_location("tyto", str(_TYTO_DIR / "tyto.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tyto = _load_tyto()


def _run(coro):
    return asyncio.run(coro)


def _query_response(entities):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"entities": entities}
    return resp


def _screenshot_entity(entity_id, status="pending_ocr", **extra):
    snapshot = {
        "filename": "shot.png",
        "source_path": "/tmp/shot.png",
        "status": status,
        **extra,
    }
    return {"entity_id": entity_id, "snapshot": {"snapshot": snapshot}}


def _task_entity(entity_id, title):
    return {"entity_id": entity_id, "snapshot": {"snapshot": {"title": title}}}


def _fake_skill_path(content="skill md"):
    """A stand-in for OCR_SKILL_PATH that behaves like a Path but is mockable."""
    fake = MagicMock()
    fake.exists.return_value = True
    fake.read_text.return_value = content
    return fake


def _ocr_proc(stdout="", returncode=0, stderr=""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def _ok_stdout(ocr_text="saw task Buy milk", extracted=None, matched=None):
    payload = {
        "ok": True,
        "ocr_text": ocr_text,
        "extracted_entities": extracted if extracted is not None else [
            {"type": "task_reference", "value": "Buy milk"}
        ],
        "matched_task_entity_id": matched,
        "error": None,
    }
    return f"{tyto.OCR_RESULT_PREFIX}{json.dumps(payload)}\n"


# ===========================================================================
# Effect-verified end-to-end happy path
# ===========================================================================


class TestEndToEndHappyPath:
    def test_ocr_consumer_end_to_end_happy_path(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"fake-png-bytes")

        screenshot = _screenshot_entity("ent_shot1", source_path=str(img))
        task = _task_entity("ent_task1", "Buy milk")

        store_calls = []
        correct_calls = []
        relationship_calls = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                if json["entity_type"] == "screenshot":
                    return _query_response([screenshot])
                return _query_response([task])
            if url.endswith("/store"):
                store_calls.append(json)
                return _query_response([])
            if url.endswith("/correct"):
                correct_calls.append(json)
                return _query_response([])
            if url.endswith("/create_relationship"):
                relationship_calls.append(json)
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        def fake_get(url, **kwargs):
            if url.endswith("/relationships"):
                return _query_response([])  # no existing edge
            raise AssertionError(f"unexpected GET {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto, "NEOTOMA_BASE_URL", "https://neotoma.test"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto.httpx, "get", side_effect=fake_get), \
             patch.object(tyto, "_find_claude_bin", return_value="/usr/bin/claude"), \
             patch.object(tyto, "OCR_SKILL_PATH", _fake_skill_path()), \
             patch.object(
                 tyto.subprocess,
                 "run",
                 return_value=_ocr_proc(stdout=_ok_stdout(matched="ent_task1")),
             ):
            notifier = MagicMock()
            consumer = tyto.OcrConsumer(notifier)
            _run(consumer.poll_once())

        # (a) OCR text persisted and attributable to the source screenshot entity id
        assert len(store_calls) == 1
        stored_entity = store_calls[0]["entities"][0]
        assert stored_entity["entity_id"] == "ent_shot1"
        assert stored_entity["ocr_text"] == "saw task Buy milk"

        # (b) at least one extracted entity is stored, not merely logged
        assert stored_entity["ocr_extracted_entities"] == [
            {"type": "task_reference", "value": "Buy milk"}
        ]

        # (c) REFERS_TO relationship created screenshot -> matched task
        assert len(relationship_calls) == 1
        assert relationship_calls[0]["relationship_type"] == "REFERS_TO"
        assert relationship_calls[0]["source_entity_id"] == "ent_shot1"
        assert relationship_calls[0]["target_entity_id"] == "ent_task1"

        # (d) status transitions to a terminal, non-pending_ocr value
        assert len(correct_calls) == 1
        assert correct_calls[0]["entity_id"] == "ent_shot1"
        assert correct_calls[0]["field"] == "status"
        assert correct_calls[0]["value"] == "ocr_complete"


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_no_task_match_still_completes(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"bytes")
        screenshot = _screenshot_entity("ent_shot2", source_path=str(img))

        correct_calls = []
        relationship_calls = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                if json["entity_type"] == "screenshot":
                    return _query_response([screenshot])
                return _query_response([])
            if url.endswith("/store"):
                return _query_response([])
            if url.endswith("/correct"):
                correct_calls.append(json)
                return _query_response([])
            if url.endswith("/create_relationship"):
                relationship_calls.append(json)
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto, "_find_claude_bin", return_value="/usr/bin/claude"), \
             patch.object(tyto, "OCR_SKILL_PATH", _fake_skill_path()), \
             patch.object(
                 tyto.subprocess,
                 "run",
                 return_value=_ocr_proc(stdout=_ok_stdout(matched=None)),
             ):
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())

        assert relationship_calls == []
        assert correct_calls[0]["value"] == "ocr_complete"

    def test_ocr_engine_failure_sets_ocr_failed(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"bytes")
        screenshot = _screenshot_entity("ent_shot3", source_path=str(img))
        correct_calls = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                return _query_response([screenshot] if json["entity_type"] == "screenshot" else [])
            if url.endswith("/correct"):
                correct_calls.append(json)
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto, "_find_claude_bin", return_value="/usr/bin/claude"), \
             patch.object(tyto, "OCR_SKILL_PATH", _fake_skill_path()), \
             patch.object(tyto.subprocess, "run", side_effect=RuntimeError("boom")):
            # Exception must not propagate out of the poll loop.
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())

        assert correct_calls[0]["value"] == "ocr_failed"

    def test_failure_does_not_block_subsequent_screenshots(self, tmp_path):
        img1 = tmp_path / "shot1.png"
        img1.write_bytes(b"bytes")
        img2 = tmp_path / "shot2.png"
        img2.write_bytes(b"bytes")
        s1 = _screenshot_entity("ent_shot_a", source_path=str(img1))
        s2 = _screenshot_entity("ent_shot_b", source_path=str(img2))

        correct_calls = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                return _query_response([s1, s2] if json["entity_type"] == "screenshot" else [])
            if url.endswith("/store"):
                return _query_response([])
            if url.endswith("/correct"):
                correct_calls.append(json)
                return _query_response([])
            if url.endswith("/create_relationship"):
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        run_calls = {"n": 0}

        def fake_run(*args, **kwargs):
            run_calls["n"] += 1
            if run_calls["n"] == 1:
                raise RuntimeError("first screenshot OCR blows up")
            return _ocr_proc(stdout=_ok_stdout(matched=None))

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto, "_find_claude_bin", return_value="/usr/bin/claude"), \
             patch.object(tyto, "OCR_SKILL_PATH", _fake_skill_path()), \
             patch.object(tyto.subprocess, "run", side_effect=fake_run):
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())

        assert len(correct_calls) == 2
        statuses = {c["entity_id"]: c["value"] for c in correct_calls}
        assert statuses["ent_shot_a"] == "ocr_failed"
        assert statuses["ent_shot_b"] == "ocr_complete"

    def test_subprocess_dispatch_timeout(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"bytes")
        screenshot = _screenshot_entity("ent_shot4", source_path=str(img))
        correct_calls = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                return _query_response([screenshot] if json["entity_type"] == "screenshot" else [])
            if url.endswith("/correct"):
                correct_calls.append(json)
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto, "_find_claude_bin", return_value="/usr/bin/claude"), \
             patch.object(tyto, "OCR_SKILL_PATH", _fake_skill_path()), \
             patch.object(
                 tyto.subprocess,
                 "run",
                 side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=300),
             ):
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())

        assert correct_calls[0]["value"] == "ocr_failed"

    def test_subprocess_dispatch_nonzero_exit(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"bytes")
        screenshot = _screenshot_entity("ent_shot5", source_path=str(img))
        correct_calls = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                return _query_response([screenshot] if json["entity_type"] == "screenshot" else [])
            if url.endswith("/correct"):
                correct_calls.append(json)
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto, "_find_claude_bin", return_value="/usr/bin/claude"), \
             patch.object(tyto, "OCR_SKILL_PATH", _fake_skill_path()), \
             patch.object(
                 tyto.subprocess,
                 "run",
                 return_value=_ocr_proc(returncode=1, stderr="x" * 800),
             ):
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())

        assert correct_calls[0]["value"] == "ocr_failed"

    def test_missing_claude_bin_or_skill_md(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"bytes")
        screenshot = _screenshot_entity("ent_shot6", source_path=str(img))
        correct_calls = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                return _query_response([screenshot] if json["entity_type"] == "screenshot" else [])
            if url.endswith("/correct"):
                correct_calls.append(json)
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto, "_find_claude_bin", return_value=None):
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())

        assert correct_calls[0]["value"] == "ocr_failed"

    def test_empty_or_corrupt_image_file(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"")  # zero-byte
        screenshot = _screenshot_entity("ent_shot7", source_path=str(img))
        correct_calls = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                return _query_response([screenshot] if json["entity_type"] == "screenshot" else [])
            if url.endswith("/correct"):
                correct_calls.append(json)
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto, "_find_claude_bin", return_value="/usr/bin/claude"), \
             patch.object(tyto, "OCR_SKILL_PATH", _fake_skill_path()):
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())

        assert correct_calls[0]["value"] == "ocr_failed"

    def test_already_processed_screenshot_not_reprocessed(self):
        """The pending_ocr query filter excludes terminal-status screenshots server-side."""
        query_bodies = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                query_bodies.append(json)
                return _query_response([])  # server already filtered; nothing pending
            raise AssertionError(f"unexpected POST {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post):
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())

        assert query_bodies[0]["snapshot_filters"] == {
            "status": {"op": "eq", "value": "pending_ocr"}
        }

    def test_entity_extraction_no_entities_found(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"bytes")
        screenshot = _screenshot_entity("ent_shot8", source_path=str(img))
        store_calls = []
        correct_calls = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                return _query_response([screenshot] if json["entity_type"] == "screenshot" else [])
            if url.endswith("/store"):
                store_calls.append(json)
                return _query_response([])
            if url.endswith("/correct"):
                correct_calls.append(json)
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto, "_find_claude_bin", return_value="/usr/bin/claude"), \
             patch.object(tyto, "OCR_SKILL_PATH", _fake_skill_path()), \
             patch.object(
                 tyto.subprocess,
                 "run",
                 return_value=_ocr_proc(
                     stdout=_ok_stdout(ocr_text="", extracted=[], matched=None)
                 ),
             ):
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())

        assert store_calls[0]["entities"][0]["ocr_extracted_entities"] == []
        assert correct_calls[0]["value"] == "ocr_complete"


# ===========================================================================
# Contract / schema tests
# ===========================================================================


class TestScreenshotEntityContract:
    def test_existing_screenshot_store_payload_unchanged(self, tmp_path):
        """The 7 pre-existing screenshot fields remain unchanged/backward-compatible."""
        img = tmp_path / "shot.png"
        img.write_bytes(b"hello")

        captured = {}

        def fake_post(url, json=None, **kwargs):
            captured["payload"] = json
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"entities": [{"entity_id": "ent_new"}]}
            return resp

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post):
            tyto.ScreenshotWatcher(tmp_path, MagicMock())._store_screenshot_entity(img)

        entity = captured["payload"]["entities"][0]
        assert set(entity.keys()) == {
            "entity_type", "filename", "file_hash", "captured_at",
            "source_path", "daemon", "status",
        }
        assert entity["entity_type"] == "screenshot"
        assert entity["status"] == "pending_ocr"

    def test_ocr_result_store_payload_shape(self, tmp_path):
        """OCR-store payload uses the exact new field names pinned by this test."""
        captured = {}

        def fake_post(url, json=None, **kwargs):
            captured["payload"] = json
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post):
            tyto._store_ocr_results("ent_shot1", "some text", [{"type": "date", "value": "2026-01-01"}])

        entity = captured["payload"]["entities"][0]
        assert entity["entity_id"] == "ent_shot1"
        assert entity["ocr_text"] == "some text"
        assert entity["ocr_extracted_entities"] == [{"type": "date", "value": "2026-01-01"}]
        assert "ocr_completed_at" in entity
        assert captured["payload"]["idempotency_key"] == "tyto-ocr-ent_shot1"

    def test_status_enum_values(self, tmp_path):
        """Only the agreed terminal values are ever written by _set_screenshot_status."""
        captured = []

        def fake_post(url, json=None, **kwargs):
            captured.append(json["value"])
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post):
            tyto._set_screenshot_status("ent_x", "ocr_complete")
            tyto._set_screenshot_status("ent_x", "ocr_failed")

        assert set(captured) <= {"pending_ocr", "ocr_complete", "ocr_failed"}


# ===========================================================================
# Backlog-drain / regression checks
# ===========================================================================


class TestBacklogDrain:
    def test_existing_backlog_entities_get_picked_up(self, tmp_path):
        """Pre-existing pending_ocr screenshots (not just new arrivals) are queried and drained."""
        backlog = [
            _screenshot_entity(f"ent_backlog_{i}", source_path=str(tmp_path / f"s{i}.png"))
            for i in range(3)
        ]
        for i in range(3):
            (tmp_path / f"s{i}.png").write_bytes(b"bytes")

        processed_ids = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                if json["entity_type"] == "screenshot":
                    return _query_response(backlog)
                return _query_response([])
            if url.endswith("/store"):
                processed_ids.append(json["entities"][0]["entity_id"])
                return _query_response([])
            if url.endswith("/correct"):
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto, "_find_claude_bin", return_value="/usr/bin/claude"), \
             patch.object(tyto, "OCR_SKILL_PATH", _fake_skill_path()), \
             patch.object(
                 tyto.subprocess,
                 "run",
                 return_value=_ocr_proc(stdout=_ok_stdout(matched=None)),
             ):
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())

        assert sorted(processed_ids) == ["ent_backlog_0", "ent_backlog_1", "ent_backlog_2"]

    def test_recording_watcher_unaffected(self, tmp_path, monkeypatch):
        """RecordingWatcher.poll_once() runs unchanged when OcrConsumer also runs in the loop."""
        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir()
        notifier = MagicMock()
        rw = tyto.RecordingWatcher(recordings_dir, notifier)

        # No files present — poll_once should simply return without error,
        # exactly as it does with no OCR consumer involved.
        _run(rw.poll_once())
        notifier.send.assert_not_called()


# ===========================================================================
# Security/Arch regression: OCR subprocess env + relationship endpoint
# ===========================================================================


class TestOcrSubprocessEnv:
    def test_env_preserves_claude_auth_and_drops_neotoma_token(self, monkeypatch):
        """
        The OCR subprocess env must NOT be a narrow allowlist that drops the
        `claude` CLI's own auth — every other claude --print dispatch site in
        this repo (formica.py, apis/skill_runner.py, phoenicurus-release/
        prepare.py) inherits the full parent env for exactly this reason.
        NEOTOMA_BEARER_TOKEN is the one credential that should NOT cross into
        this subprocess (the OCR skill never calls Neotoma directly).
        """
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "neotoma-secret")
        monkeypatch.setenv("SOME_OTHER_VAR", "kept")

        env = tyto._ocr_subprocess_env()

        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "oauth-tok"
        assert "ANTHROPIC_API_KEY" not in env, "API key must be dropped when OAuth token is present"
        assert "NEOTOMA_BEARER_TOKEN" not in env, "Neotoma credential must not reach the OCR subprocess"
        assert env.get("SOME_OTHER_VAR") == "kept"

    def test_env_keeps_api_key_when_no_oauth_token(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "neotoma-secret")

        env = tyto._ocr_subprocess_env()

        assert env.get("ANTHROPIC_API_KEY") == "sk-ant-xxx"
        assert "NEOTOMA_BEARER_TOKEN" not in env


class TestRelationshipEndpointContract:
    def test_link_uses_create_relationship_endpoint(self):
        """Matches turdus.py's existing precedent and the create_relationship MCP tool — not a fabricated /relationships path."""
        calls = {"get": [], "post": []}

        def fake_get(url, **kwargs):
            calls["get"].append(url)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"relationships": []}  # no existing edge
            return resp

        def fake_post(url, json=None, **kwargs):
            calls["post"].append((url, json))
            return _query_response([])

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto, "NEOTOMA_BASE_URL", "https://neotoma.test"), \
             patch.object(tyto.httpx, "get", side_effect=fake_get), \
             patch.object(tyto.httpx, "post", side_effect=fake_post):
            result = tyto._link_screenshot_to_task("ent_shot1", "ent_task1")

        assert result is True
        assert calls["get"] == ["https://neotoma.test/entities/ent_shot1/relationships"]
        assert calls["post"] == [
            (
                "https://neotoma.test/create_relationship",
                {
                    "relationship_type": "REFERS_TO",
                    "source_entity_id": "ent_shot1",
                    "target_entity_id": "ent_task1",
                },
            )
        ]

    def test_refers_to_exists_skips_duplicate_create(self):
        """When GET /entities/:id/relationships already shows the edge, no create_relationship call is made."""
        existing_resp = MagicMock()
        existing_resp.raise_for_status = MagicMock()
        existing_resp.json.return_value = {
            "relationships": [
                {"relationship_type": "REFERS_TO", "target_entity_id": "ent_task1"},
            ]
        }

        post_calls = []

        def fake_post(url, json=None, **kwargs):
            post_calls.append(url)
            return _query_response([])

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "get", return_value=existing_resp), \
             patch.object(tyto.httpx, "post", side_effect=fake_post):
            result = tyto._link_screenshot_to_task("ent_shot1", "ent_task1")

        assert result is True
        assert post_calls == []  # no duplicate create_relationship call


class TestOcrPollCadence:
    def test_ocr_poll_gated_by_ocr_poll_interval(self):
        """A second poll_once() within OCR_POLL_INTERVAL is a no-op (independent OCR cadence)."""
        query_calls = []

        def fake_post(url, json=None, **kwargs):
            if url.endswith("/entities/query"):
                query_calls.append(json)
                return _query_response([])
            raise AssertionError(f"unexpected POST {url}")

        with patch.object(tyto, "NEOTOMA_BEARER_TOKEN", "test-token"), \
             patch.object(tyto.httpx, "post", side_effect=fake_post), \
             patch.object(tyto, "OCR_POLL_INTERVAL", 9999):
            consumer = tyto.OcrConsumer(MagicMock())
            _run(consumer.poll_once())
            _run(consumer.poll_once())

        assert len(query_calls) == 1

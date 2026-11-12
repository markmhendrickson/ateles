"""User-facing contract tests for the checkpoint-authority migration CLI.

The migration function has separate effect coverage in
``test_checkpoint_release.py``.  These tests enter through ``main()`` so the
argument parsing, denial-store preflight, exit status, and printed replacement
ID cannot regress while the internal helper tests remain green.
"""

from __future__ import annotations

import sys

import pytest

import apis
import migrate_checkpoint_authority as cli


def _run_cli(monkeypatch: pytest.MonkeyPatch, checkpoint_id: str) -> int:
    monkeypatch.setattr(sys, "argv", [cli.__file__, checkpoint_id])
    return cli.main()


def test_migration_cli_prints_replacement_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[object] = []
    notifier = object()

    monkeypatch.setattr(
        cli,
        "_require_checkpoint_denial_store",
        lambda: events.append("denial-store-ready"),
    )
    monkeypatch.setattr(
        cli.Notifier,
        "from_neotoma",
        lambda: events.append("notifier-ready") or notifier,
    )
    monkeypatch.setattr(
        cli,
        "migrate_checkpoint_authority",
        lambda checkpoint_id, *, notifier: (
            events.append(("migrated", checkpoint_id, notifier)) or "ent_replacement"
        ),
    )
    monkeypatch.setattr(
        apis,
        "dispatch_task",
        lambda *args, **kwargs: pytest.fail("the migration CLI must not dispatch"),
    )

    assert _run_cli(monkeypatch, "ent_legacy") == 0
    captured = capsys.readouterr()
    assert captured.out == "ent_replacement\n"
    assert captured.err == ""
    assert events == [
        "denial-store-ready",
        "notifier-ready",
        ("migrated", "ent_legacy", notifier),
    ]


def test_migration_cli_failure_exits_nonzero_without_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_require_checkpoint_denial_store", lambda: None)
    monkeypatch.setattr(cli.Notifier, "from_neotoma", lambda: object())
    monkeypatch.setattr(
        cli, "migrate_checkpoint_authority", lambda checkpoint_id, *, notifier: None
    )
    monkeypatch.setattr(
        apis,
        "dispatch_task",
        lambda *args, **kwargs: pytest.fail("a failed migration must not dispatch"),
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "ent_legacy")

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "migration failed closed" in captured.err


def test_migration_cli_validates_denial_store_before_migration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    denial_error = RuntimeError("denial store is not durable")
    monkeypatch.setattr(
        cli,
        "_require_checkpoint_denial_store",
        lambda: (_ for _ in ()).throw(denial_error),
    )
    monkeypatch.setattr(
        cli.Notifier,
        "from_neotoma",
        lambda: pytest.fail("notifier construction must follow denial-store proof"),
    )
    monkeypatch.setattr(
        cli,
        "migrate_checkpoint_authority",
        lambda *args, **kwargs: pytest.fail(
            "migration must not run without durable denial state"
        ),
    )

    with pytest.raises(RuntimeError, match="denial store is not durable"):
        _run_cli(monkeypatch, "ent_legacy")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_migration_cli_refuses_a_restricted_checkpoint_without_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through main(): a restrictive original scope is never migrated."""
    record = {
        "entity_id": "ent_restricted",
        "entity_type": "checkpoint_" + "brief",
        "snapshot": {
            "status": "approved",
            "task_entity_id": "ent_t",
            # Claims a restrictive extension, but no authenticated envelope
            # can be read from it: its scope is unknown, never "legacy".
            "body": '{"version": 2, "effect_scope": "review_request_only"}',
        },
    }
    task = {"entity_id": "ent_t", "snapshot": {"status": "awaiting_approval"}}
    writes: list[str] = []
    monkeypatch.setattr(cli, "_require_checkpoint_denial_store", lambda: None)
    monkeypatch.setattr(cli.Notifier, "from_neotoma", lambda: object())
    monkeypatch.setattr(apis, "fetch_checkpoint_record", lambda _id: record)
    monkeypatch.setattr(
        apis,
        "read_authenticated_checkpoint_authorization",
        lambda _id, _rec: None,
    )
    monkeypatch.setattr(apis, "fetch_task_record", lambda _id: task)
    monkeypatch.setattr(apis, "fetch_entity_user_id", lambda _id: "tenant-a")
    monkeypatch.setattr(apis, "checkpoint_already_dispatched", lambda _s: False)
    monkeypatch.setattr(apis, "_checkpoint_denial_persisted", lambda _id: False)
    monkeypatch.setattr(
        apis, "_file_fresh_checkpoint", lambda **kw: writes.append("file") or "x"
    )
    monkeypatch.setattr(
        apis,
        "require_fresh_checkpoint_approval",
        lambda *a, **k: writes.append("retire") or True,
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "ent_restricted")

    assert exc_info.value.code != 0
    assert writes == []

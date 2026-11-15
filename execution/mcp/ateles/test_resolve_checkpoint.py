#!/usr/bin/env python3
"""Tests for resolve_checkpoint.py — operator CLI for checkpoint approve/reject.

Each case is designed to fail before the CLI exists (ImportError / missing
module) and to pass after. Live Neotoma and real resolver keys are never used:
tests generate a throwaway JWK in tmp and monkeypatch ``_get``,
``_checkpoint_resolver_authority``, and ``_resolve_checkpoint``.

What each looked like red (before the change):
  - ImportError: no module named resolve_checkpoint
  - After a stub existed without the effect checks: approve reported done on
    awaiting_operator read-back; wrong-status checkpoints still signed;
    wrong-thumbprint output leaked JWK member values; batch kept going by
    default; dry-run still called the signer; --yes on a non-TTY was accepted.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

# Lives beside the server it drives, so CI collects it in the job that installs
# the server's requirements (mcp); execution/scripts' own job does not.
_MCP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _MCP_DIR.parents[2]
_SCRIPTS = _REPO_ROOT / "execution" / "scripts"
for _path in (_REPO_ROOT, _MCP_DIR, _SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import resolve_checkpoint as rc  # noqa: E402
from lib.daemon_runtime.aauth_httpsig import (  # noqa: E402
    jwk_thumbprint,
    public_part_of,
)

@pytest.fixture(autouse=True)
def _operator_at_terminal():
    """Default every test to an operator at a terminal who answers yes.

    Tests about the non-interactive or declined paths patch these again inside.
    """
    with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch(
        "builtins.input", return_value="y"
    ):
        yield


OWNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SUB = "resolver@example.test"


def _resolver_jwk(*, sub: str = SUB) -> dict:
    private = ec.generate_private_key(ec.SECP256R1()).private_numbers()

    def b64u(value: int) -> str:
        return (
            base64.urlsafe_b64encode(value.to_bytes(32, "big"))
            .rstrip(b"=")
            .decode()
        )

    return {
        "kty": "EC",
        "crv": "P-256",
        "d": b64u(private.private_value),
        "x": b64u(private.public_numbers.x),
        "y": b64u(private.public_numbers.y),
        "sub": sub,
        "kid": "test-resolver-key",
    }


def _write_key(tmp_path: Path, jwk: dict | None = None) -> tuple[Path, dict, str]:
    material = jwk if jwk is not None else _resolver_jwk()
    path = tmp_path / "resolver.jwk.json"
    path.write_text(json.dumps(material))
    return path, material, jwk_thumbprint(public_part_of(material))


def _checkpoint_entity(
    checkpoint_id: str,
    *,
    status: str = "awaiting_operator",
    title: str = "Hold for approval",
    task_entity_id: str = "ent_task_1",
    blast_radius: str = "high",
) -> dict:
    return {
        "entity_id": checkpoint_id,
        "entity_type": "checkpoint_brief",  # vocab-ok: the stored entity type is still checkpoint_brief; the server checks the same literal
        "snapshot": {
            "status": status,
            "title": title,
            "task_entity_id": task_entity_id,
            "blast_radius": blast_radius,
        },
    }


@pytest.fixture(autouse=True)
def _no_batch_delay(monkeypatch):
    monkeypatch.setattr(rc, "BATCH_ITEM_DELAY_SECONDS", 0)


def test_approve_signs_with_checkpoint_authority_and_requires_approved_readback(
    tmp_path, capsys
):
    """Red before: ImportError / no CLI; or done without approved read-back."""
    key_path, _jwk, jkt = _write_key(tmp_path)
    checkpoint_id = "ent_cp_ok"
    entity = _checkpoint_entity(checkpoint_id)
    signed_headers = {
        "signature-key": "sig-key",
        "signature-input": "sig-input",
        "signature": "sig",
        "content-digest": "digest",
        "content-type": "application/json",
    }
    sign_calls: list[dict] = []
    resolve_calls: list[tuple] = []
    reads = {
        f"/entities/{checkpoint_id}": entity,
    }

    def fake_get(path: str, params=None):
        return reads.get(path)

    def fake_sign(checkpoint_id_arg, action, **kwargs):
        sign_calls.append(
            {
                "checkpoint_id": checkpoint_id_arg,
                "action": action,
                **kwargs,
            }
        )
        # Simulate successful write so the post-submit read-back is approved.
        reads[f"/entities/{checkpoint_id}"] = _checkpoint_entity(
            checkpoint_id, status="approved"
        )
        return signed_headers

    async def fake_resolve(checkpoint_id_arg, action, resolver_aauth_headers=None):
        resolve_calls.append(
            (checkpoint_id_arg, action, resolver_aauth_headers)
        )
        return {
            "checkpoint_id": checkpoint_id_arg,
            "new_status": "approved",
            "action_taken": "approved — task release not confirmed by read-back",
        }

    with mock.patch.object(rc.ateles_server, "_get", side_effect=fake_get), mock.patch.object(
        rc.ateles_server,
        "_checkpoint_resolver_authority",
        return_value=(SUB, jkt, OWNER),
    ), mock.patch.object(
        rc, "sign_checkpoint_resolution", side_effect=fake_sign
    ), mock.patch.object(
        rc.ateles_server, "_resolve_checkpoint", side_effect=fake_resolve
    ):
        code = rc.main(
            [checkpoint_id, "approve", "--key", str(key_path)]
        )

    assert code == 0
    assert len(sign_calls) == 1
    assert sign_calls[0]["checkpoint_id"] == checkpoint_id
    assert sign_calls[0]["action"] == "approve"
    assert sign_calls[0]["expected_sub"] == SUB
    assert sign_calls[0]["expected_jkt"] == jkt
    assert sign_calls[0]["private_jwk_path"] == key_path
    assert len(resolve_calls) == 1
    assert resolve_calls[0][0] == checkpoint_id
    assert resolve_calls[0][1] == "approve"
    assert resolve_calls[0][2] == signed_headers
    out = capsys.readouterr().out
    assert f"{checkpoint_id} Hold for approval -> approved" in out


def test_readback_still_awaiting_prints_failed_and_exits_1(tmp_path, capsys):
    """Red before: success reported when status never left awaiting_operator."""
    key_path, _jwk, jkt = _write_key(tmp_path)
    checkpoint_id = "ent_cp_stuck"
    entity = _checkpoint_entity(checkpoint_id)

    async def fake_resolve(checkpoint_id_arg, action, resolver_aauth_headers=None):
        return {
            "checkpoint_id": checkpoint_id_arg,
            "new_status": "approved",
            "action_taken": "approved — task release not confirmed by read-back",
        }

    with mock.patch.object(
        rc.ateles_server, "_get", return_value=entity
    ), mock.patch.object(
        rc.ateles_server,
        "_checkpoint_resolver_authority",
        return_value=(SUB, jkt, OWNER),
    ), mock.patch.object(
        rc,
        "sign_checkpoint_resolution",
        return_value={
            "signature-key": "k",
            "signature-input": "i",
            "signature": "s",
            "content-digest": "d",
            "content-type": "application/json",
        },
    ), mock.patch.object(
        rc.ateles_server, "_resolve_checkpoint", side_effect=fake_resolve
    ):
        code = rc.main(
            [checkpoint_id, "approve", "--key", str(key_path)]
        )

    captured = capsys.readouterr()
    assert code == 1
    assert "FAILED" in captured.out
    assert "awaiting_operator" in captured.out


def test_not_awaiting_operator_refuses_without_signing(tmp_path, capsys):
    """Red before: already-resolved checkpoints were still signed."""
    key_path, _jwk, jkt = _write_key(tmp_path)
    checkpoint_id = "ent_cp_done"
    entity = _checkpoint_entity(checkpoint_id, status="approved")
    sign = mock.Mock()

    with mock.patch.object(
        rc.ateles_server, "_get", return_value=entity
    ), mock.patch.object(
        rc.ateles_server,
        "_checkpoint_resolver_authority",
        return_value=(SUB, jkt, OWNER),
    ), mock.patch.object(rc, "sign_checkpoint_resolution", sign), mock.patch.object(
        rc.ateles_server, "_resolve_checkpoint", mock.AsyncMock()
    ) as resolve:
        code = rc.main(
            [checkpoint_id, "approve", "--key", str(key_path)]
        )

    assert code == 1
    sign.assert_not_called()
    resolve.assert_not_called()
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "awaiting_operator" in out


def test_wrong_thumbprint_refused_without_key_material(tmp_path, capsys):
    """Red before: refusal text included JWK member values from the file."""
    key_path, jwk, _jkt = _write_key(tmp_path)
    wrong_jkt = "B" * 43
    checkpoint_id = "ent_cp_jkt"
    entity = _checkpoint_entity(checkpoint_id)

    with mock.patch.object(
        rc.ateles_server, "_get", return_value=entity
    ), mock.patch.object(
        rc.ateles_server,
        "_checkpoint_resolver_authority",
        return_value=(SUB, wrong_jkt, OWNER),
    ):
        # Real signer: wrong expected_jkt must refuse before headers exist.
        code = rc.main(
            [checkpoint_id, "approve", "--key", str(key_path)]
        )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert "thumbprint" in combined.lower()
    for member in ("d", "x", "y"):
        assert jwk[member] not in combined
    assert json.dumps(jwk) not in combined


def test_batch_stops_at_first_failure_unless_keep_going(tmp_path, capsys):
    """Red before: batch always continued / counts were wrong."""
    key_path, _jwk, jkt = _write_key(tmp_path)
    batch = tmp_path / "batch.txt"
    batch.write_text("# comment\n\nent_a\nent_b\nent_c\n")

    entities = {
        "ent_a": _checkpoint_entity("ent_a", title="First"),
        "ent_b": _checkpoint_entity("ent_b", title="Second"),
        "ent_c": _checkpoint_entity("ent_c", title="Third"),
    }
    store = dict(entities)

    def fake_get(path: str, params=None):
        eid = path.rsplit("/", 1)[-1]
        return store.get(eid)

    def fake_sign(checkpoint_id_arg, action, **kwargs):
        if checkpoint_id_arg == "ent_b":
            # Leave status awaiting so resolve_one fails on read-back.
            return {
                "signature-key": "k",
                "signature-input": "i",
                "signature": "s",
                "content-digest": "d",
                "content-type": "application/json",
            }
        store[checkpoint_id_arg] = _checkpoint_entity(
            checkpoint_id_arg,
            status="approved",
            title=entities[checkpoint_id_arg]["snapshot"]["title"],
        )
        return {
            "signature-key": "k",
            "signature-input": "i",
            "signature": "s",
            "content-digest": "d",
            "content-type": "application/json",
        }

    async def fake_resolve(checkpoint_id_arg, action, resolver_aauth_headers=None):
        return {
            "checkpoint_id": checkpoint_id_arg,
            "new_status": "approved",
            "action_taken": "ok",
        }

    def run_batch(*extra: str) -> int:
        with mock.patch.object(
            rc.ateles_server, "_get", side_effect=fake_get
        ), mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, jkt, OWNER),
        ), mock.patch.object(
            rc, "sign_checkpoint_resolution", side_effect=fake_sign
        ), mock.patch.object(
            rc.ateles_server, "_resolve_checkpoint", side_effect=fake_resolve
        ), mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch.object(
            rc, "_confirm"
        ):
            return rc.main(
                [
                    "--batch",
                    str(batch),
                    "approve",
                    "--key",
                    str(key_path),
                    "--yes",
                    *extra,
                ]
            )

    store.clear()
    store.update(entities)
    code_stop = run_batch()
    out_stop = capsys.readouterr().out
    assert code_stop == 1
    assert "done=1 skipped=0 failed=1" in out_stop
    assert "Third" not in out_stop

    store.clear()
    store.update(entities)
    code_keep = run_batch("--keep-going")
    out_keep = capsys.readouterr().out
    assert code_keep == 1
    assert "done=2 skipped=0 failed=1" in out_keep
    assert "ent_c Third -> approved" in out_keep


def test_dry_run_never_calls_signer(tmp_path, capsys):
    """Red before: dry-run still invoked sign_checkpoint_resolution."""
    checkpoint_id = "ent_cp_dry"
    entity = _checkpoint_entity(checkpoint_id)
    sign = mock.Mock()
    resolve = mock.AsyncMock()

    with mock.patch.object(
        rc.ateles_server, "_get", return_value=entity
    ), mock.patch.object(
        rc.ateles_server,
        "_checkpoint_resolver_authority",
        return_value=(SUB, "A" * 43, OWNER),
    ), mock.patch.object(rc, "sign_checkpoint_resolution", sign), mock.patch.object(
        rc.ateles_server, "_resolve_checkpoint", resolve
    ):
        code = rc.main([checkpoint_id, "approve", "--dry-run"])

    assert code == 0
    sign.assert_not_called()
    resolve.assert_not_called()
    out = capsys.readouterr().out
    assert checkpoint_id in out
    assert "ent_task_1" in out
    assert "high" in out


def test_yes_without_tty_refused_unless_dry_run(tmp_path, capsys):
    """Red before: --yes was accepted on a non-interactive stdin."""
    key_path, _jwk, jkt = _write_key(tmp_path)
    checkpoint_id = "ent_cp_tty"
    entity = _checkpoint_entity(checkpoint_id)

    with mock.patch.object(sys.stdin, "isatty", return_value=False), mock.patch.object(
        rc.ateles_server, "_get", return_value=entity
    ), mock.patch.object(
        rc.ateles_server,
        "_checkpoint_resolver_authority",
        return_value=(SUB, jkt, OWNER),
    ), mock.patch.object(rc, "sign_checkpoint_resolution") as sign:
        code = rc.main(
            [checkpoint_id, "approve", "--key", str(key_path), "--yes"]
        )

    assert code == 1
    sign.assert_not_called()
    err = capsys.readouterr().err
    assert "--yes requires a TTY" in err

    with mock.patch.object(sys.stdin, "isatty", return_value=False), mock.patch.object(
        rc.ateles_server, "_get", return_value=entity
    ), mock.patch.object(
        rc.ateles_server,
        "_checkpoint_resolver_authority",
        return_value=(SUB, jkt, OWNER),
    ), mock.patch.object(rc, "sign_checkpoint_resolution") as sign_dry:
        code_dry = rc.main(
            [checkpoint_id, "approve", "--dry-run", "--yes"]
        )

    assert code_dry == 0
    sign_dry.assert_not_called()


def test_single_id_without_tty_refused_without_signing(tmp_path, capsys):
    """Red before (Pavo, PR #1768): one id skipped both the TTY gate and the
    confirmation, so a non-interactive process holding the key could resolve."""
    key_path, _jwk, jkt = _write_key(tmp_path)
    checkpoint_id = "ent_cp_unattended"
    entity = _checkpoint_entity(checkpoint_id)

    with mock.patch.object(sys.stdin, "isatty", return_value=False), mock.patch.object(
        rc.ateles_server, "_get", return_value=entity
    ), mock.patch.object(
        rc.ateles_server,
        "_checkpoint_resolver_authority",
        return_value=(SUB, jkt, OWNER),
    ), mock.patch.object(rc, "sign_checkpoint_resolution") as sign, mock.patch.object(
        rc.ateles_server, "_resolve_checkpoint"
    ) as resolve:
        code = rc.main([checkpoint_id, "approve", "--key", str(key_path)])

    assert code == 1
    sign.assert_not_called()
    resolve.assert_not_called()
    assert "requires a TTY" in capsys.readouterr().err


def test_single_id_declined_confirmation_never_signs(tmp_path, capsys):
    """Red before: one id resolved with no confirmation prompt at all."""
    key_path, _jwk, jkt = _write_key(tmp_path)
    checkpoint_id = "ent_cp_declined"
    entity = _checkpoint_entity(checkpoint_id)

    with mock.patch("builtins.input", return_value="n") as ask, mock.patch.object(
        rc.ateles_server, "_get", return_value=entity
    ), mock.patch.object(
        rc.ateles_server,
        "_checkpoint_resolver_authority",
        return_value=(SUB, jkt, OWNER),
    ), mock.patch.object(rc, "sign_checkpoint_resolution") as sign, mock.patch.object(
        rc.ateles_server, "_resolve_checkpoint"
    ) as resolve:
        code = rc.main([checkpoint_id, "approve", "--key", str(key_path)])

    assert code == 1
    ask.assert_called_once()
    assert checkpoint_id in ask.call_args.args[0]
    sign.assert_not_called()
    resolve.assert_not_called()
    assert "confirmation declined" in capsys.readouterr().err


#!/usr/bin/env python3
"""Tests for resolve_checkpoint.py — operator CLI for checkpoint approve/reject.

Live Neotoma, 1Password, and real resolver keys are never used: tests generate
a throwaway JWK in memory and monkeypatch ``subprocess.run``, ``_get``,
``_checkpoint_resolver_authority``, and ``_resolve_checkpoint``.

What each looked like red (before the change):
  - Before the 1Password change: ``--key-ref`` was unknown, file paths were
    accepted, and no ``op read`` call existed.
  - Before the effect checks: approve reported done on
    awaiting_operator read-back; wrong-status checkpoints still signed;
    wrong-thumbprint output leaked JWK member values; batch kept going by
    default; dry-run still called the signer; non-TTY input was accepted.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import unicodedata
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
    with (
        mock.patch.object(sys.stdin, "isatty", return_value=True),
        mock.patch("builtins.input", return_value="y"),
    ):
        yield


OWNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SUB = "resolver@example.test"
KEY_REF = "op://Resolver/Checkpoint key/private JWK"


def _resolver_jwk(*, sub: str = SUB) -> dict:
    private = ec.generate_private_key(ec.SECP256R1()).private_numbers()

    def b64u(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()

    return {
        "kty": "EC",
        "crv": "P-256",
        "d": b64u(private.private_value),
        "x": b64u(private.public_numbers.x),
        "y": b64u(private.public_numbers.y),
        "sub": sub,
        "kid": "test-resolver-key",
    }


def _key_material(jwk: dict | None = None) -> tuple[str, dict, str]:
    material = jwk if jwk is not None else _resolver_jwk()
    return json.dumps(material), material, jwk_thumbprint(public_part_of(material))


def _op_result(
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
) -> mock.Mock:
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


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


@pytest.fixture(autouse=True)
def _no_parent_op_auth(monkeypatch):
    """Keep unrelated developer authentication out of ordinary test cases."""
    for name in tuple(os.environ):
        if (
            name == "OP_SERVICE_ACCOUNT_TOKEN"
            or name.startswith("OP_CONNECT_")
            or name.startswith("OP_SESSION_")
        ):
            monkeypatch.delenv(name)


def test_approve_signs_with_checkpoint_authority_and_requires_approved_readback(
    capsys,
):
    """Red before: --key-ref was unknown; or done without approved read-back."""
    key_text, jwk, jkt = _key_material()
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
        resolve_calls.append((checkpoint_id_arg, action, resolver_aauth_headers))
        return {
            "checkpoint_id": checkpoint_id_arg,
            "new_status": "approved",
            "action_taken": "approved — task release not confirmed by read-back",
        }

    with (
        mock.patch.object(
            rc.subprocess,
            "run",
            side_effect=[_op_result(), _op_result(key_text)],
        ) as op_read,
        mock.patch.object(rc.ateles_server, "_get", side_effect=fake_get),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, jkt, OWNER),
        ),
        mock.patch.object(rc, "sign_checkpoint_resolution", side_effect=fake_sign),
        mock.patch.object(
            rc.ateles_server, "_resolve_checkpoint", side_effect=fake_resolve
        ),
    ):
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    assert code == 0
    assert [call.args[0] for call in op_read.call_args_list] == [
        ["op", "signout", "--all"],
        ["op", "read", "--no-newline", KEY_REF],
    ]
    assert len(sign_calls) == 1
    assert sign_calls[0]["checkpoint_id"] == checkpoint_id
    assert sign_calls[0]["action"] == "approve"
    assert sign_calls[0]["expected_sub"] == SUB
    assert sign_calls[0]["expected_jkt"] == jkt
    assert sign_calls[0]["private_jwk"] == jwk
    assert len(resolve_calls) == 1
    assert resolve_calls[0][0] == checkpoint_id
    assert resolve_calls[0][1] == "approve"
    assert resolve_calls[0][2] == signed_headers
    out = capsys.readouterr().out
    assert f"{checkpoint_id} Hold for approval -> approved" in out


def test_readback_still_awaiting_prints_failed_and_exits_1(capsys):
    """Red before: success reported when status never left awaiting_operator."""
    key_text, _jwk, jkt = _key_material()
    checkpoint_id = "ent_cp_stuck"
    entity = _checkpoint_entity(checkpoint_id)

    async def fake_resolve(checkpoint_id_arg, action, resolver_aauth_headers=None):
        return {
            "checkpoint_id": checkpoint_id_arg,
            "new_status": "approved",
            "action_taken": "approved — task release not confirmed by read-back",
        }

    with (
        mock.patch.object(
            rc.subprocess,
            "run",
            side_effect=[_op_result(), _op_result(key_text)],
        ),
        mock.patch.object(rc.ateles_server, "_get", return_value=entity),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, jkt, OWNER),
        ),
        mock.patch.object(
            rc,
            "sign_checkpoint_resolution",
            return_value={
                "signature-key": "k",
                "signature-input": "i",
                "signature": "s",
                "content-digest": "d",
                "content-type": "application/json",
            },
        ),
        mock.patch.object(
            rc.ateles_server, "_resolve_checkpoint", side_effect=fake_resolve
        ),
    ):
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    captured = capsys.readouterr()
    assert code == 1
    assert "FAILED" in captured.out
    assert "awaiting_operator" in captured.out


def test_not_awaiting_operator_refuses_without_signing(capsys):
    """Red before: already-resolved checkpoints were still signed."""
    key_text, _jwk, jkt = _key_material()
    checkpoint_id = "ent_cp_done"
    entity = _checkpoint_entity(checkpoint_id, status="approved")
    sign = mock.Mock()

    with (
        mock.patch.object(
            rc.subprocess,
            "run",
            side_effect=[_op_result(), _op_result(key_text)],
        ),
        mock.patch.object(rc.ateles_server, "_get", return_value=entity),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, jkt, OWNER),
        ),
        mock.patch.object(rc, "sign_checkpoint_resolution", sign),
        mock.patch.object(
            rc.ateles_server, "_resolve_checkpoint", mock.AsyncMock()
        ) as resolve,
    ):
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    assert code == 1
    sign.assert_not_called()
    resolve.assert_not_called()
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "awaiting_operator" in out


def test_wrong_thumbprint_refused_without_key_material(capsys):
    """Red before: refusal text included JWK member values."""
    key_text, jwk, _jkt = _key_material()
    wrong_jkt = "B" * 43
    checkpoint_id = "ent_cp_jkt"
    entity = _checkpoint_entity(checkpoint_id)

    with (
        mock.patch.object(
            rc.subprocess,
            "run",
            side_effect=[_op_result(), _op_result(key_text)],
        ),
        mock.patch.object(rc.ateles_server, "_get", return_value=entity),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, wrong_jkt, OWNER),
        ),
    ):
        # Real signer: wrong expected_jkt must refuse before headers exist.
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert "thumbprint" in combined.lower()
    for member in ("d", "x", "y"):
        assert jwk[member] not in combined
    assert json.dumps(jwk) not in combined


def test_malformed_jwk_is_normalized_without_key_material(capsys):
    """Malformed in-memory key errors expose no JWK member."""
    marker = "PRIVATE_JWK_SENTINEL"
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "d": marker,
        "x": "invalid",
        "y": "invalid",
        "sub": SUB,
    }
    checkpoint_id = "ent_cp_malformed_key"
    entity = _checkpoint_entity(checkpoint_id)
    resolve = mock.AsyncMock()

    with (
        mock.patch.object(
            rc.subprocess,
            "run",
            side_effect=[_op_result(), _op_result(json.dumps(jwk))],
        ),
        mock.patch.object(rc.ateles_server, "_get", return_value=entity),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, "A" * 43, OWNER),
        ),
        mock.patch.object(rc.ateles_server, "_resolve_checkpoint", resolve),
    ):
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == rc.EXIT_FAILURE
    resolve.assert_not_called()
    assert "could not load resolver JWK" in combined
    assert marker not in combined
    assert json.dumps(jwk) not in combined


def test_key_file_option_is_not_supported(capsys):
    """Red before: --key accepted a host-readable resolver key path."""
    with mock.patch.object(rc.subprocess, "run") as op_read:
        code = rc.main(["ent_cp_path", "approve", "--key", "/tmp/resolver.jwk.json"])

    assert code == rc.EXIT_USAGE
    op_read.assert_not_called()
    assert "unrecognized arguments: --key" in capsys.readouterr().err


def test_non_op_reference_is_refused_before_read_or_sign(capsys):
    """Red before: --key-ref did not exist or references were not validated."""
    sign = mock.Mock()
    with (
        mock.patch.object(rc.subprocess, "run") as op_read,
        mock.patch.object(rc, "sign_checkpoint_resolution", sign),
    ):
        code = rc.main(
            ["ent_cp_bad_ref", "approve", "--key-ref", "/tmp/resolver.jwk.json"]
        )

    assert code == rc.EXIT_FAILURE
    op_read.assert_not_called()
    sign.assert_not_called()
    assert "must be an op:// reference" in capsys.readouterr().err


def test_non_op_reference_is_refused_even_in_dry_run(capsys):
    """A dry run skips key access but does not accept a key-file replacement."""
    with mock.patch.object(rc.subprocess, "run") as op_read:
        code = rc.main(
            [
                "ent_cp_bad_ref",
                "approve",
                "--dry-run",
                "--key-ref",
                "/tmp/resolver.jwk.json",
            ]
        )

    assert code == rc.EXIT_FAILURE
    op_read.assert_not_called()
    assert "must be an op:// reference" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (1, "Touch ID prompt cancelled"),
        (7, "1Password account unavailable"),
    ],
)
def test_op_failure_or_cancellation_signs_nothing(returncode, stderr, capsys):
    """Red before: no Touch ID-gated op read existed."""
    checkpoint_id = "ent_cp_op_failed"
    _key_text, _jwk, jkt = _key_material()
    entity = _checkpoint_entity(checkpoint_id)
    sign = mock.Mock()
    resolve = mock.AsyncMock()

    with (
        mock.patch.object(
            rc.subprocess,
            "run",
            return_value=_op_result(
                "PRIVATE_KEY_MUST_NOT_APPEAR",
                returncode=returncode,
                stderr=stderr,
            ),
        ),
        mock.patch.object(rc.ateles_server, "_get", return_value=entity),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, jkt, OWNER),
        ),
        mock.patch.object(rc, "sign_checkpoint_resolution", sign),
        mock.patch.object(rc.ateles_server, "_resolve_checkpoint", resolve),
    ):
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == rc.EXIT_FAILURE
    sign.assert_not_called()
    resolve.assert_not_called()
    assert f"op exit code {returncode}" in combined
    assert "PRIVATE_KEY_MUST_NOT_APPEAR" not in combined
    assert stderr not in combined


def test_op_output_never_appears_in_exception():
    """Red before: the in-memory 1Password reader did not exist."""
    marker = "PRIVATE_JWK_SENTINEL"
    with (
        mock.patch.object(
            rc.subprocess,
            "run",
            return_value=_op_result(marker, returncode=9, stderr=f"failed: {marker}"),
        ),
        pytest.raises(rc.ResolveError) as caught,
    ):
        rc._read_key_from_1password(KEY_REF)

    message = str(caught.value)
    assert marker not in message
    assert message == "could not read resolver key from 1Password (op exit code 9)"


def test_missing_op_refuses_without_leaking_os_error():
    """A missing CLI is a closed failure with no path or OS detail."""
    with (
        mock.patch.object(
            rc.subprocess, "run", side_effect=FileNotFoundError("sensitive path")
        ),
        pytest.raises(rc.ResolveError) as caught,
    ):
        rc._read_key_from_1password(KEY_REF)

    assert str(caught.value) == (
        "could not read resolver key from 1Password (op exit code unavailable)"
    )
    assert caught.value.__cause__ is None


def test_op_read_happens_only_after_confirmation(capsys):
    """Red before: no ordering existed between confirmation and key access."""
    key_text, _jwk, jkt = _key_material()
    checkpoint_id = "ent_cp_order"
    reads = {f"/entities/{checkpoint_id}": _checkpoint_entity(checkpoint_id)}
    confirmed = False

    def fake_input(_prompt: str) -> str:
        nonlocal confirmed
        confirmed = True
        return "y"

    def fake_op(*args, **kwargs):
        assert confirmed
        return _op_result(key_text)

    def fake_sign(checkpoint_id_arg, action, **kwargs):
        reads[f"/entities/{checkpoint_id}"] = _checkpoint_entity(
            checkpoint_id, status="approved"
        )
        return {"signature": "s"}

    async def fake_resolve(checkpoint_id_arg, action, resolver_aauth_headers=None):
        return {"new_status": "approved"}

    with (
        mock.patch("builtins.input", side_effect=fake_input),
        mock.patch.object(rc.subprocess, "run", side_effect=fake_op) as op_read,
        mock.patch.object(
            rc.ateles_server,
            "_get",
            side_effect=lambda path, params=None: reads.get(path),
        ),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, jkt, OWNER),
        ),
        mock.patch.object(rc, "sign_checkpoint_resolution", side_effect=fake_sign),
        mock.patch.object(
            rc.ateles_server, "_resolve_checkpoint", side_effect=fake_resolve
        ),
    ):
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    assert code == rc.EXIT_OK
    assert confirmed
    assert [call.args[0] for call in op_read.call_args_list] == [
        ["op", "signout", "--all"],
        ["op", "read", "--no-newline", KEY_REF],
    ]
    assert "-> approved" in capsys.readouterr().out


def test_batch_stops_at_first_failure_unless_keep_going(tmp_path, capsys):
    """Red before: batch always continued / counts were wrong."""
    key_text, _jwk, jkt = _key_material()
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
        with (
            mock.patch.object(
                rc.subprocess,
                "run",
                side_effect=[_op_result(), _op_result(key_text)],
            ) as op_read,
            mock.patch.object(rc.ateles_server, "_get", side_effect=fake_get),
            mock.patch.object(
                rc.ateles_server,
                "_checkpoint_resolver_authority",
                return_value=(SUB, jkt, OWNER),
            ),
            mock.patch.object(rc, "sign_checkpoint_resolution", side_effect=fake_sign),
            mock.patch.object(
                rc.ateles_server, "_resolve_checkpoint", side_effect=fake_resolve
            ),
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch.object(rc, "_confirm"),
        ):
            code = rc.main(
                [
                    "--batch",
                    str(batch),
                    "approve",
                    "--key-ref",
                    KEY_REF,
                    *extra,
                ]
            )
        assert [call.args[0] for call in op_read.call_args_list] == [
            ["op", "signout", "--all"],
            ["op", "read", "--no-newline", KEY_REF],
        ]
        return code

    store.clear()
    store.update(entities)
    code_stop = run_batch()
    out_stop = capsys.readouterr().out
    assert code_stop == 1
    assert "done=1 skipped=0 failed=1" in out_stop
    assert "ent_c Third -> " not in out_stop  # listed for consent, never resolved

    store.clear()
    store.update(entities)
    code_keep = run_batch("--keep-going")
    out_keep = capsys.readouterr().out
    assert code_keep == 1
    assert "done=2 skipped=0 failed=1" in out_keep
    assert "ent_c Third -> approved" in out_keep


def test_dry_run_never_calls_signer(tmp_path, capsys):
    """Dry-run never invokes 1Password, the signer, or the server mutation."""
    checkpoint_id = "ent_cp_dry"
    entity = _checkpoint_entity(checkpoint_id)
    sign = mock.Mock()
    resolve = mock.AsyncMock()

    with (
        mock.patch.object(rc.subprocess, "run") as op_read,
        mock.patch.object(rc.ateles_server, "_get", return_value=entity),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, "A" * 43, OWNER),
        ),
        mock.patch.object(rc, "sign_checkpoint_resolution", sign),
        mock.patch.object(rc.ateles_server, "_resolve_checkpoint", resolve),
    ):
        code = rc.main([checkpoint_id, "approve", "--dry-run"])

    assert code == 0
    op_read.assert_not_called()
    sign.assert_not_called()
    resolve.assert_not_called()
    out = capsys.readouterr().out
    assert checkpoint_id in out
    assert "ent_task_1" in out
    assert "high" in out


def test_dry_run_is_allowed_without_tty(capsys):
    """A dry run remains non-interactive and never reaches 1Password."""
    checkpoint_id = "ent_cp_tty"
    entity = _checkpoint_entity(checkpoint_id)

    with (
        mock.patch.object(rc.subprocess, "run") as op_dry,
        mock.patch.object(sys.stdin, "isatty", return_value=False),
        mock.patch.object(rc.ateles_server, "_get", return_value=entity),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, "A" * 43, OWNER),
        ),
        mock.patch.object(rc, "sign_checkpoint_resolution") as sign_dry,
    ):
        code_dry = rc.main([checkpoint_id, "approve", "--dry-run"])

    assert code_dry == 0
    op_dry.assert_not_called()
    sign_dry.assert_not_called()
    assert checkpoint_id in capsys.readouterr().out


def test_single_id_without_tty_refused_without_signing(capsys):
    """Red before (Pavo, PR #1768): one id skipped both the TTY gate and the
    confirmation, so a non-interactive process holding the key could resolve."""
    key_text, _jwk, jkt = _key_material()
    checkpoint_id = "ent_cp_unattended"
    entity = _checkpoint_entity(checkpoint_id)

    with (
        mock.patch.object(
            rc.subprocess, "run", return_value=_op_result(key_text)
        ) as op_read,
        mock.patch.object(sys.stdin, "isatty", return_value=False),
        mock.patch.object(rc.ateles_server, "_get", return_value=entity),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, jkt, OWNER),
        ),
        mock.patch.object(rc, "sign_checkpoint_resolution") as sign,
        mock.patch.object(rc.ateles_server, "_resolve_checkpoint") as resolve,
    ):
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    assert code == 1
    op_read.assert_not_called()
    sign.assert_not_called()
    resolve.assert_not_called()
    assert "requires a TTY" in capsys.readouterr().err


def test_single_id_declined_confirmation_never_signs(capsys):
    """Red before: one id resolved with no confirmation prompt at all."""
    key_text, _jwk, jkt = _key_material()
    checkpoint_id = "ent_cp_declined"
    entity = _checkpoint_entity(checkpoint_id)

    with (
        mock.patch.object(
            rc.subprocess,
            "run",
            side_effect=[_op_result(), _op_result(key_text)],
        ) as op_read,
        mock.patch("builtins.input", return_value="n") as ask,
        mock.patch.object(rc.ateles_server, "_get", return_value=entity),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, jkt, OWNER),
        ),
        mock.patch.object(rc, "sign_checkpoint_resolution") as sign,
        mock.patch.object(rc.ateles_server, "_resolve_checkpoint") as resolve,
    ):
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    assert code == 1
    op_read.assert_not_called()
    ask.assert_called_once()
    assert checkpoint_id in ask.call_args.args[0]
    sign.assert_not_called()
    resolve.assert_not_called()
    assert "confirmation declined" in capsys.readouterr().err


def test_selection_listed_before_confirmation_and_release_result_shown(capsys):
    """Red before (Falco, Accipiter advisories): the prompt showed only a count,
    and success output dropped the server's action_taken."""
    key_text, _jwk, jkt = _key_material()
    checkpoint_id = "ent_cp_listed"
    reads = {f"/entities/{checkpoint_id}": _checkpoint_entity(checkpoint_id)}
    seen_before_prompt: list[str] = []

    def fake_input(prompt: str) -> str:
        seen_before_prompt.append(capsys.readouterr().out)
        return "y"

    def fake_sign(checkpoint_id_arg, action, **kwargs):
        reads[f"/entities/{checkpoint_id}"] = _checkpoint_entity(
            checkpoint_id, status="approved"
        )
        return {"signature": "s"}

    async def fake_resolve(checkpoint_id_arg, action, resolver_aauth_headers=None):
        return {"new_status": "approved", "action_taken": "approved and task released"}

    with (
        mock.patch.object(
            rc.subprocess,
            "run",
            side_effect=[_op_result(), _op_result(key_text)],
        ) as op_read,
        mock.patch("builtins.input", side_effect=fake_input),
        mock.patch.object(
            rc.ateles_server,
            "_get",
            side_effect=lambda path, params=None: reads.get(path),
        ),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, jkt, OWNER),
        ),
        mock.patch.object(rc, "sign_checkpoint_resolution", side_effect=fake_sign),
        mock.patch.object(
            rc.ateles_server, "_resolve_checkpoint", side_effect=fake_resolve
        ),
    ):
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    assert code == 0
    assert [call.args[0] for call in op_read.call_args_list] == [
        ["op", "signout", "--all"],
        ["op", "read", "--no-newline", KEY_REF],
    ]
    assert seen_before_prompt and f"{checkpoint_id} title=" in seen_before_prompt[0]
    assert "approved and task released" in capsys.readouterr().out


@pytest.mark.parametrize(
    "variable_name",
    [
        "OP_SERVICE_ACCOUNT_TOKEN",
        "OP_CONNECT_HOST",
        "OP_CONNECT_TOKEN",
        "OP_SESSION_example",
    ],
)
def test_parent_op_auth_environment_refuses_before_reading_or_signing(
    variable_name, monkeypatch, capsys
):
    """Red on 63c282e5: inherited non-biometric op auth reached ``op read``."""
    monkeypatch.setenv(variable_name, "AUTH_VALUE_MUST_NOT_APPEAR")
    get = mock.Mock()
    sign = mock.Mock()

    with (
        mock.patch.object(rc.subprocess, "run") as op_call,
        mock.patch.object(rc.ateles_server, "_get", get),
        mock.patch.object(rc, "sign_checkpoint_resolution", sign),
    ):
        code = rc.main(["ent_cp_env", "approve", "--key-ref", KEY_REF])

    captured = capsys.readouterr()
    assert code == rc.EXIT_FAILURE
    get.assert_not_called()
    op_call.assert_not_called()
    sign.assert_not_called()
    assert variable_name in captured.err
    assert "AUTH_VALUE_MUST_NOT_APPEAR" not in captured.err


def test_op_subprocess_receives_only_allowlisted_environment(monkeypatch):
    """Red on 63c282e5: ``op read`` inherited the entire caller environment."""
    key_text, jwk, _jkt = _key_material()
    monkeypatch.setenv("UNRELATED_PARENT_VALUE", "must-not-be-in-child")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "must-not-be-in-child")
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _op_result(key_text)

    with mock.patch.object(rc.subprocess, "run", side_effect=fake_run):
        assert rc._read_key_from_1password(KEY_REF) == jwk

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["op", "read", "--no-newline", KEY_REF]
    child_env = kwargs["env"]
    assert set(child_env) <= {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "LANG",
        "TERM",
    }
    assert "UNRELATED_PARENT_VALUE" not in child_env
    assert not any(
        name == "OP_SERVICE_ACCOUNT_TOKEN"
        or name.startswith("OP_CONNECT_")
        or name.startswith("OP_SESSION_")
        for name in child_env
    )


def test_real_run_signs_out_before_reading_key(capsys):
    """Red on 63c282e5: a cached CLI session was reused without sign-out."""
    key_text, _jwk, jkt = _key_material()
    checkpoint_id = "ent_cp_fresh_auth"
    reads = {f"/entities/{checkpoint_id}": _checkpoint_entity(checkpoint_id)}
    op_commands: list[list[str]] = []

    def fake_op(command, **kwargs):
        op_commands.append(command)
        child_env = kwargs["env"]
        assert set(child_env) <= set(rc.OP_ENV_ALLOWLIST)
        assert not any(
            name == "OP_SERVICE_ACCOUNT_TOKEN"
            or name.startswith("OP_CONNECT_")
            or name.startswith("OP_SESSION_")
            for name in child_env
        )
        if command[:2] == ["op", "signout"]:
            return _op_result()
        if command[:2] == ["op", "read"]:
            return _op_result(key_text)
        raise AssertionError(command)

    def fake_sign(checkpoint_id_arg, action, **kwargs):
        reads[f"/entities/{checkpoint_id}"] = _checkpoint_entity(
            checkpoint_id, status="approved"
        )
        return {"signature": "s"}

    async def fake_resolve(checkpoint_id_arg, action, resolver_aauth_headers=None):
        return {"new_status": "approved"}

    with (
        mock.patch.object(rc.subprocess, "run", side_effect=fake_op),
        mock.patch.object(
            rc.ateles_server,
            "_get",
            side_effect=lambda path, params=None: reads.get(path),
        ),
        mock.patch.object(
            rc.ateles_server,
            "_checkpoint_resolver_authority",
            return_value=(SUB, jkt, OWNER),
        ),
        mock.patch.object(rc, "sign_checkpoint_resolution", side_effect=fake_sign),
        mock.patch.object(
            rc.ateles_server, "_resolve_checkpoint", side_effect=fake_resolve
        ),
    ):
        code = rc.main([checkpoint_id, "approve", "--key-ref", KEY_REF])

    assert code == rc.EXIT_OK
    assert op_commands == [
        ["op", "signout", "--all"],
        ["op", "read", "--no-newline", KEY_REF],
    ]
    assert "-> approved" in capsys.readouterr().out


def test_yes_option_cannot_skip_typed_confirmation(capsys):
    """Red on 63c282e5: ``--yes`` bypassed typed confirmation on a TTY."""
    with (
        mock.patch.object(rc.subprocess, "run") as op_call,
        mock.patch("builtins.input") as ask,
    ):
        code = rc.main(["ent_cp_yes", "approve", "--key-ref", KEY_REF, "--yes"])

    assert code == rc.EXIT_USAGE
    ask.assert_not_called()
    op_call.assert_not_called()
    assert "unrecognized arguments: --yes" in capsys.readouterr().err


@pytest.mark.parametrize(
    "unsafe",
    [
        "\x1b[31mred\x1b[0m",
        "\x1b[2J\x1b[H",
        "\x1b]8;;https://example.test\x07link\x1b]8;;\x07",
        "\x1b]0;spoofed title\x07",
        "safe\b\bBAD",
        "left\u202eright\u202c",
        "hidden\u200btext\u2066isolated\u2069",
    ],
)
def test_terminal_sanitizer_escapes_control_and_format_characters(unsafe):
    """Red on 63c282e5: record text reached the terminal byte-for-byte."""
    rendered = rc._sanitize_terminal_text(unsafe)

    assert unsafe != rendered
    assert not any(
        unicodedata.category(character) in {"Cc", "Cf"} for character in rendered
    )


def test_dry_run_sanitizes_every_record_derived_display_field(capsys):
    """Red on 63c282e5: title/task/blast fields could inject terminal controls."""
    checkpoint_id = "ent_cp_display"
    snapshot = {
        "title": "\x1b[31mred\x1b[0m",
        "task_entity_id": "\x1b]8;;https://example.test\x07task\x1b]8;;\x07",
        "blast_radius": "h\bhigh\u202e",
    }

    with mock.patch.object(
        rc,
        "load_checkpoint",
        return_value=({}, snapshot, SUB, "A" * 43),
    ):
        rc.dry_run_one(checkpoint_id)

    output = capsys.readouterr().out
    assert "\\u001b" in output
    assert "\\u0007" in output
    assert "\\u0008" in output
    assert "\\u202e" in output
    assert not any(
        unicodedata.category(character) in {"Cc", "Cf"}
        for character in output.rstrip("\n")
    )


def test_failure_reason_is_sanitized_before_display(capsys):
    """Red on 63c282e5: a server reason could forge terminal output."""
    rc._print_result_line(
        "ent_cp_reason",
        "title",
        "",
        failed=True,
        detail="\x1b]0;spoof\x07bad\u200b",
    )

    output = capsys.readouterr().out
    assert "\\u001b" in output
    assert "\\u0007" in output
    assert "\\u200b" in output
    assert "\x1b" not in output
    assert "\x07" not in output
    assert "\u200b" not in output


def test_launcher_help_works_in_clean_shell_without_credentials(tmp_path):
    """Red on 63c282e5: no dependency-aware checkpoint launcher existed."""
    launcher = _SCRIPTS / "resolve_checkpoint.sh"
    clean_home = tmp_path / "home"
    clean_home.mkdir()

    completed = subprocess.run(
        ["/bin/bash", str(launcher), "--help"],
        cwd=_REPO_ROOT,
        env={"HOME": str(clean_home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
    assert "1Password op:// reference" in completed.stdout
    assert "NEOTOMA_BEARER_TOKEN" not in completed.stderr

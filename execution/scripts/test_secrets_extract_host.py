"""Tests for the Design B dual-writer secrets contract (ateles#712 review).

Two scripts write into the SAME `secrets/<block>.sops.enc` snapshot:
`secrets_publish.py` (1Password-backed, manifest-driven) and
`secrets_extract_host.py` (host-only keys, no 1Password reference). Covers:

  * secrets_publish.publish_file no longer wipes host-only keys on a plain
    republish (the arch regression: replace-encrypt from `refs` alone drops
    anything `secrets_extract_host` merge-wrote into the same file).
  * secrets_extract_host's own security-property canaries requested on the
    PR: failures never echo secret-bearing stdout/stderr, --dry-run never
    touches the host, merge preserves existing keys unless --overwrite, and
    --verify prints only names + present/non-empty (no values).

secrets_lib is stubbed throughout so no real `sops`/`op`/`flyctl` binary or
age key is required to run these.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import types
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _stub_secrets_lib(monkeypatch, store):
    """Install a fake secrets_lib backed by an in-memory {block: {k: v}} dict.

    `store` is mutated in place by encrypt/decrypt so tests can assert on it
    directly. sops/op are never actually invoked.

    `merge_preserve_unmanaged` and `resolve_refs` are the REAL implementations
    (imported, not hand-copied) -- those are exactly the functions under test
    for the merge-preserve regression, so a stub copy here would let a future
    change to the real function drift out of sync with what these tests
    assert without ever failing.
    """
    real_secrets_lib = importlib.import_module("secrets_lib")

    stub = types.ModuleType("secrets_lib")
    stub.SECRETS_BASE = Path("/fake/ateles-private")  # type: ignore[attr-defined]
    stub.merge_preserve_unmanaged = real_secrets_lib.merge_preserve_unmanaged  # type: ignore[attr-defined]

    class _EncPath:
        def __init__(self, block):
            self._block = block

        def exists(self):
            return self._block in store

        @property
        def name(self):
            return f"{self._block}.sops.enc"

        @property
        def parent(self):
            return _NullDir()

        def relative_to(self, _base):
            return Path("secrets") / self.name

        def __str__(self):
            return f"/fake/ateles-private/secrets/{self.name}"

    class _NullDir:
        def mkdir(self, parents=True, exist_ok=True):
            pass

    def enc_file(block):
        return _EncPath(block)

    def sops_decrypt_dotenv(src):
        return dict(store.get(src._block, {}))

    def sops_encrypt_dotenv(plaintext, dest):
        pairs = {}
        for line in plaintext.splitlines():
            if not line.strip():
                continue
            k, _, v = line.partition("=")
            pairs[k] = v
        store[dest._block] = pairs

    def to_dotenv(pairs):
        return "".join(f"{k}={v}\n" for k, v in pairs.items())

    stub.resolve_refs = real_secrets_lib.resolve_refs  # type: ignore[attr-defined]
    stub.enc_file = enc_file  # type: ignore[attr-defined]
    stub.sops_decrypt_dotenv = sops_decrypt_dotenv  # type: ignore[attr-defined]
    stub.sops_encrypt_dotenv = sops_encrypt_dotenv  # type: ignore[attr-defined]
    stub.to_dotenv = to_dotenv  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "secrets_lib", stub)
    return stub


def _fresh(monkeypatch, modname, store):
    _stub_secrets_lib(monkeypatch, store)
    sys.modules.pop(modname, None)
    return importlib.import_module(modname)


# ---------------------------------------------------------------------------
# Arch regression: publish must not wipe host-only keys (ateles#712 review)
# ---------------------------------------------------------------------------

def test_publish_preserves_host_only_key_not_in_manifest(monkeypatch):
    """A key secrets_extract_host merge-wrote must survive a plain republish.

    Before the fix, publish_file replace-encrypted from `pairs` (manifest refs
    only), so any key not in the manifest -- e.g. a host-only key -- vanished
    on the next publish. This is the exact failure mode arch flagged.
    """
    store = {"neotoma": {"NEOTOMA_APPROVED_EMAILS": "op@example.com"}}
    mod = _fresh(monkeypatch, "secrets_publish", store)

    manifest = {
        "files": {
            "neotoma": {
                "default": {"NEOTOMA_BEARER_TOKEN": "op://Private/x/token"},
            }
        }
    }
    monkeypatch.setattr(
        mod.sl, "op_read", lambda ref: "fresh-token-value", raising=False
    )

    rc = mod.publish_file(manifest, "neotoma", environment=None)

    assert rc == 0
    # Host-only key, never in the manifest, must still be present.
    assert store["neotoma"]["NEOTOMA_APPROVED_EMAILS"] == "op@example.com"
    # Manifest-managed key was refreshed from 1Password as normal.
    assert store["neotoma"]["NEOTOMA_BEARER_TOKEN"] == "fresh-token-value"


def test_publish_preserves_placeholder_skipped_key_already_in_snapshot(monkeypatch):
    """A placeholder-marked ref must not delete that key's existing value.

    managed_keys must be `set(pairs)` (resolved this run), never `set(refs)`
    (every ref, including ones skipped as placeholders). Passing `set(refs)`
    was a regression caught in self-review: a key skipped as a placeholder
    stays in `refs` but never lands in `pairs`, so treating it as "managed"
    deletes its existing snapshot value instead of leaving it untouched.
    """
    store = {"neotoma": {"STILL_A_PLACEHOLDER_VAR": "previously-published-value"}}
    mod = _fresh(monkeypatch, "secrets_publish", store)

    manifest = {
        "files": {
            "neotoma": {
                "default": {
                    "NEOTOMA_BEARER_TOKEN": "op://Private/x/token",
                    "STILL_A_PLACEHOLDER_VAR": "PLACEHOLDER_not_set_yet",
                },
            }
        }
    }
    monkeypatch.setattr(mod.sl, "op_read", lambda ref: "fresh-token-value", raising=False)

    rc = mod.publish_file(manifest, "neotoma", environment=None)

    assert rc == 0
    # The placeholder-skipped key's existing value must survive untouched.
    assert store["neotoma"]["STILL_A_PLACEHOLDER_VAR"] == "previously-published-value"
    assert store["neotoma"]["NEOTOMA_BEARER_TOKEN"] == "fresh-token-value"


def test_publish_refuses_when_existing_snapshot_cannot_be_decrypted(monkeypatch):
    """A decrypt failure must block publish rather than silently overwrite.

    If we cannot read what's already in the snapshot, a replace-encrypt from
    `pairs` alone would drop anything unreadable -- same failure mode as the
    no-merge bug, just triggered by a decrypt error instead of an empty read.
    """
    store = {"neotoma": {"NEOTOMA_APPROVED_EMAILS": "op@example.com"}}
    mod = _fresh(monkeypatch, "secrets_publish", store)

    def _boom(src):
        raise RuntimeError("sops decrypt failed for neotoma.sops.enc: age: no identity matched")

    monkeypatch.setattr(mod.sl, "sops_decrypt_dotenv", _boom, raising=False)
    monkeypatch.setattr(mod.sl, "op_read", lambda ref: "fresh-token-value", raising=False)

    manifest = {"files": {"neotoma": {"default": {"NEOTOMA_BEARER_TOKEN": "op://Private/x/token"}}}}
    rc = mod.publish_file(manifest, "neotoma", environment=None)

    assert rc == 1
    # Snapshot must be untouched -- host-only key still there, unmodified.
    assert store["neotoma"] == {"NEOTOMA_APPROVED_EMAILS": "op@example.com"}


# ---------------------------------------------------------------------------
# pm canary: --dry-run never calls the host read
# ---------------------------------------------------------------------------

def test_dry_run_never_reads_the_host(monkeypatch):
    store = {}
    mod = _fresh(monkeypatch, "secrets_extract_host", store)

    def _fail_if_called(*a, **k):
        raise AssertionError("read_host_env must not be called in --dry-run")

    monkeypatch.setattr(mod, "read_host_env", _fail_if_called)

    rc = mod.extract(
        app="some-app", block="neotoma", keys=["NEOTOMA_HOST_URL"],
        machine=None, overwrite=False, dry_run=True,
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# pm canary: merge preserves keys unless --overwrite
# ---------------------------------------------------------------------------

def test_extract_merge_preserves_existing_unless_overwrite(monkeypatch, capsys):
    store = {"neotoma": {"NEOTOMA_HOST_URL": "https://old.example.com"}}
    mod = _fresh(monkeypatch, "secrets_extract_host", store)
    monkeypatch.setattr(
        mod, "read_host_env",
        lambda app, keys, machine: {"NEOTOMA_HOST_URL": "https://new.example.com"},
    )

    rc = mod.extract(
        app="some-app", block="neotoma", keys=["NEOTOMA_HOST_URL"],
        machine=None, overwrite=False, dry_run=False,
    )
    assert rc == 0
    assert store["neotoma"]["NEOTOMA_HOST_URL"] == "https://old.example.com"

    out = capsys.readouterr().out
    assert "https://old.example.com" not in out
    assert "https://new.example.com" not in out


def test_extract_overwrite_replaces_existing(monkeypatch):
    store = {"neotoma": {"NEOTOMA_HOST_URL": "https://old.example.com"}}
    mod = _fresh(monkeypatch, "secrets_extract_host", store)
    monkeypatch.setattr(
        mod, "read_host_env",
        lambda app, keys, machine: {"NEOTOMA_HOST_URL": "https://new.example.com"},
    )

    rc = mod.extract(
        app="some-app", block="neotoma", keys=["NEOTOMA_HOST_URL"],
        machine=None, overwrite=True, dry_run=False,
    )
    assert rc == 0
    assert store["neotoma"]["NEOTOMA_HOST_URL"] == "https://new.example.com"


# ---------------------------------------------------------------------------
# pm canary: --verify prints names + present/non-empty only, never values
# ---------------------------------------------------------------------------

def test_verify_prints_no_values(monkeypatch, capsys):
    store = {"neotoma": {"NEOTOMA_APPROVED_EMAILS": "super-secret@example.com"}}
    mod = _fresh(monkeypatch, "secrets_extract_host", store)

    rc = mod.verify("neotoma", ["NEOTOMA_APPROVED_EMAILS"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
    assert "super-secret@example.com" not in out
    assert "NEOTOMA_APPROVED_EMAILS" in out


def test_verify_reports_missing_and_empty_without_values(monkeypatch, capsys):
    store = {"neotoma": {"PRESENT_KEY": "value", "EMPTY_KEY": ""}}
    mod = _fresh(monkeypatch, "secrets_extract_host", store)

    rc = mod.verify("neotoma", ["PRESENT_KEY", "EMPTY_KEY", "MISSING_KEY"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "MISSING_KEY" in out
    assert "EMPTY_KEY" in out
    assert "value" not in out  # the actual value string never appears


# ---------------------------------------------------------------------------
# pm canary: failures never echo secret-bearing stdout/stderr
# ---------------------------------------------------------------------------

def test_host_read_failure_never_echoes_process_output(monkeypatch):
    """flyctl output is withheld wholesale on non-zero exit (may quote env)."""
    store = {}
    mod = _fresh(monkeypatch, "secrets_extract_host", store)

    class _FakeResult:
        returncode = 1
        stdout = "NEOTOMA_APPROVED_EMAILS=leaked@example.com\n"
        stderr = "some secret-bearing stderr"

    monkeypatch.setattr(
        mod.subprocess, "run", lambda *a, **k: _FakeResult()
    )

    try:
        mod.read_host_env("some-app", ["NEOTOMA_APPROVED_EMAILS"])
        raised = False
    except mod.ExtractionError as exc:
        raised = True
        message = str(exc)

    assert raised
    assert "leaked@example.com" not in message
    assert "secret-bearing" not in message


def test_decrypt_failure_reports_type_only_not_sops_message(monkeypatch):
    """secrets_lib embeds sops' stderr in its message; that must never surface."""
    store = {}
    mod = _fresh(monkeypatch, "secrets_extract_host", store)

    def _boom(src):
        raise RuntimeError("sops decrypt failed: dotenv contains SECRET_VALUE=abc123")

    monkeypatch.setattr(mod.sl, "sops_decrypt_dotenv", _boom, raising=False)
    # Make enc_file().exists() True so load_existing attempts the decrypt.
    monkeypatch.setattr(mod.sl, "enc_file", lambda block: types.SimpleNamespace(
        exists=lambda: True, name=f"{block}.sops.enc"
    ), raising=False)

    try:
        mod.load_existing("neotoma")
        raised = False
    except mod.ExtractionError as exc:
        raised = True
        message = str(exc)

    assert raised
    assert "SECRET_VALUE" not in message
    assert "abc123" not in message
    assert "RuntimeError" in message  # type name is fine to surface


def test_main_top_level_unexpected_exception_withholds_message(monkeypatch, capsys):
    """Last-resort guard: an unexpected exception's message could carry payload."""
    store = {}
    mod = _fresh(monkeypatch, "secrets_extract_host", store)

    def _boom(app, block, keys, machine, overwrite, dry_run):
        raise ValueError("oops leaked-secret-value-xyz")

    monkeypatch.setattr(mod, "extract", _boom)
    rc = mod.main(["--app", "a", "--block", "neotoma", "--key", "K"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "leaked-secret-value-xyz" not in err
    assert "ValueError" in err

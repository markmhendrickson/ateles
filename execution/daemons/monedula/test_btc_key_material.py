"""
test_btc_key_material.py — BTC key-material sourcing for Monedula's BTC rail.

The wallet seed is deliberately NOT placed in the daemon's launchd environment
(where it would sit in a long-lived process and be readable via `launchctl
print`). Instead the short-lived send runner loads the wallet checkout's own
.env into itself, just before importing the wallet library.

Covers:
  1. Loader     — reads the wallet .env; real environment always wins.
  2. Containment— parsing is minimal and the secret never leaves the subprocess.
  3. Preflight  — a missing key is reported as a config error BEFORE any send,
                  so an approved payment never fails at the wire for this.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "handlers"))

_spec = importlib.util.spec_from_file_location(
    "btc_send_runner", Path(__file__).parent / "handlers" / "btc_send_runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

# Obviously-fake placeholder: never a real seed, and not BIP-39 wordlist
# entries, so secret scanners do not flag the fixture.
FAKE_KEY = "not-a-real-key-test-fixture-0000"


@pytest.fixture
def wallet_dir(tmp_path, monkeypatch):
    for var in ("BTC_MNEMONIC", "BTC_PRIVATE_KEY", "BTC_NETWORK"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


# --- 1. Loader -------------------------------------------------------------

def test_loads_key_from_wallet_env(wallet_dir):
    (wallet_dir / ".env").write_text(f"BTC_MNEMONIC={FAKE_KEY}\nBTC_NETWORK=mainnet\n")
    runner._load_wallet_env(wallet_dir)
    assert os.environ["BTC_MNEMONIC"] == FAKE_KEY
    assert os.environ["BTC_NETWORK"] == "mainnet"


def test_real_environment_wins_over_dotenv(wallet_dir, monkeypatch):
    """An operator/CI override must not be clobbered by the file."""
    monkeypatch.setenv("BTC_MNEMONIC", "from-environment")
    (wallet_dir / ".env").write_text(f"BTC_MNEMONIC={FAKE_KEY}\n")
    runner._load_wallet_env(wallet_dir)
    assert os.environ["BTC_MNEMONIC"] == "from-environment"


def test_handles_export_comments_and_quotes(wallet_dir):
    (wallet_dir / ".env").write_text(
        "# a comment\n"
        "\n"
        f'export BTC_MNEMONIC="{FAKE_KEY}"\n'
        "  BTC_NETWORK = mainnet \n"
        "MALFORMED_NO_EQUALS\n"
    )
    runner._load_wallet_env(wallet_dir)
    assert os.environ["BTC_MNEMONIC"] == FAKE_KEY
    assert os.environ["BTC_NETWORK"].strip() == "mainnet"


def test_missing_env_file_is_not_fatal(wallet_dir):
    runner._load_wallet_env(wallet_dir / "nonexistent")  # must not raise
    assert "BTC_MNEMONIC" not in os.environ


# --- 3. Preflight ----------------------------------------------------------

def _btc_transfer():
    """Import btc_transfer as a package member (it uses relative imports)."""
    from handlers import btc_transfer
    return btc_transfer


def test_preflight_detects_missing_key(wallet_dir, monkeypatch):
    monkeypatch.setenv("BTC_WALLET_MODULE_PATH", str(wallet_dir))
    mod = _btc_transfer()
    assert "No BTC key material" in mod._key_material_missing()


def test_preflight_satisfied_by_environment(wallet_dir, monkeypatch):
    monkeypatch.setenv("BTC_WALLET_MODULE_PATH", str(wallet_dir))
    monkeypatch.setenv("BTC_MNEMONIC", FAKE_KEY)
    mod = _btc_transfer()
    assert mod._key_material_missing() == ""


def test_preflight_satisfied_by_wallet_dotenv(wallet_dir, monkeypatch):
    monkeypatch.setenv("BTC_WALLET_MODULE_PATH", str(wallet_dir))
    (wallet_dir / ".env").write_text(f"export BTC_MNEMONIC={FAKE_KEY}\n")
    mod = _btc_transfer()
    assert mod._key_material_missing() == ""


def test_preflight_does_not_expose_the_secret(wallet_dir, monkeypatch):
    """The diagnostic names the FILE, never a value."""
    monkeypatch.setenv("BTC_WALLET_MODULE_PATH", str(wallet_dir))
    (wallet_dir / ".env").write_text("BTC_NETWORK=mainnet\n")  # no key
    mod = _btc_transfer()
    msg = mod._key_material_missing()
    assert FAKE_KEY not in msg and "Payment not attempted" in msg

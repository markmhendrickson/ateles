"""
test_btc_send_runner.py — Tests for handlers/btc_send_runner.py::main().

This is the highest-consequence code path in the Monedula BTC rail: it is the
subprocess that actually arms and broadcasts a real send. It had zero test
coverage before PR #249 review (qa lens, finding 2).

Covers main()'s full request→response surface:
  1. Malformed / missing input        — bad JSON, missing address/amount_eur.
  2. Wallet import failure            — BTC_WALLET_MODULE_PATH points nowhere useful.
  3. Config unavailable               — BTCConfig.from_env() raises (no key material).
  4. Price-fetch failure              — _fetch_btc_prices() raises or returns <= 0.
  5. Non-positive computed sats       — a too-small EUR amount rounds to 0 sats.
  6. send_transfer_multi raising      — the wallet call itself fails.
  7. dry_run vs real-send status      — "dry_run" vs "sent" in the response.
  8. The memo invariant               — send_transfer_multi is ALWAYS called with
                                         memo=None, asserted on the actual kwargs
                                         (not just the docstring claim).

No real wallet code runs: bitcoin_wallet is a fake module injected into
sys.modules before main() imports it.
"""

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "handlers"))

_spec = importlib.util.spec_from_file_location(
    "btc_send_runner", Path(__file__).parent / "handlers" / "btc_send_runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


class _FakeConfig:
    network = "mainnet"


def _install_fake_wallet(monkeypatch, *, from_env=None, prices=(Decimal("30000"), Decimal("27000")),
                          send_txid="deadbeefcafe", send_raises=None):
    """Inject a fake bitcoin_wallet module so main() never touches the real one."""
    fake = MagicMock()
    fake.BTCConfig.from_env = MagicMock(
        side_effect=from_env) if callable(from_env) or isinstance(from_env, Exception) \
        else MagicMock(return_value=from_env or _FakeConfig())
    if isinstance(from_env, Exception):
        fake.BTCConfig.from_env.side_effect = from_env

    fake._fetch_btc_prices = MagicMock(return_value=prices)

    if send_raises is not None:
        fake.send_transfer_multi = MagicMock(side_effect=send_raises)
    else:
        fake.send_transfer_multi = MagicMock(return_value=send_txid)

    monkeypatch.setitem(sys.modules, "bitcoin_wallet", fake)
    monkeypatch.setattr(runner, "_load_wallet_env", lambda _p: None)
    return fake


def _run(monkeypatch, req: dict, capsys) -> dict:
    monkeypatch.setattr(sys, "argv", ["btc_send_runner.py", json.dumps(req)])
    rc = runner.main()
    out = json.loads(capsys.readouterr().out)
    return rc, out


# --- 1. Malformed / missing input -------------------------------------------

def test_bad_json_request(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["btc_send_runner.py", "{not json"])
    rc = runner.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["status"] == "failed"
    assert "bad request" in out["error"]


def test_missing_address(monkeypatch, capsys):
    rc, out = _run(monkeypatch, {"amount_eur": 70}, capsys)
    assert rc == 1
    assert out["status"] == "failed"
    assert "missing address or amount_eur" in out["error"]


def test_missing_amount_eur(monkeypatch, capsys):
    rc, out = _run(monkeypatch, {"address": "bc1qexample"}, capsys)
    assert rc == 1
    assert out["status"] == "failed"
    assert "missing address or amount_eur" in out["error"]


# --- 2. Wallet import failure ------------------------------------------------

def test_wallet_import_failure(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "bitcoin_wallet", None)  # forces ImportError
    monkeypatch.setattr(runner, "_load_wallet_env", lambda _p: None)
    rc, out = _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 70}, capsys)
    assert rc == 1
    assert out["status"] == "failed"
    assert "wallet import failed" in out["error"]


# --- 3. Config unavailable ----------------------------------------------------

def test_config_unavailable(monkeypatch, capsys):
    _install_fake_wallet(monkeypatch, from_env=RuntimeError("No key material configured"))
    rc, out = _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 70}, capsys)
    assert rc == 1
    assert out["status"] == "failed"
    assert "wallet config unavailable" in out["error"]


# --- 4. Price-fetch failure ----------------------------------------------------

def test_price_fetch_raises(monkeypatch, capsys):
    fake = _install_fake_wallet(monkeypatch)
    fake._fetch_btc_prices.side_effect = RuntimeError("price API down")
    rc, out = _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 70}, capsys)
    assert rc == 1
    assert out["status"] == "failed"
    assert "eur->sats failed" in out["error"]


def test_price_fetch_returns_nonpositive(monkeypatch, capsys):
    _install_fake_wallet(monkeypatch, prices=(Decimal("30000"), Decimal("0")))
    rc, out = _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 70}, capsys)
    assert rc == 1
    assert out["status"] == "failed"
    assert "eur->sats failed" in out["error"]


# --- 5. Non-positive computed sats --------------------------------------------

def test_nonpositive_amount_sats(monkeypatch, capsys):
    """An EUR amount that rounds to 0 sats must never reach send_transfer_multi."""
    fake = _install_fake_wallet(monkeypatch, prices=(Decimal("30000"), Decimal("1e12")))
    rc, out = _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 0.0000001}, capsys)
    assert rc == 1
    assert out["status"] == "failed"
    assert "non-positive amount_sats" in out["error"]
    fake.send_transfer_multi.assert_not_called()


# --- 6. send_transfer_multi raising -------------------------------------------

def test_send_transfer_raises(monkeypatch, capsys):
    _install_fake_wallet(monkeypatch, send_raises=RuntimeError("broadcast rejected"))
    rc, out = _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 70, "dry_run": False}, capsys)
    assert rc == 1
    assert out["status"] == "failed"
    assert "send failed" in out["error"]
    assert "broadcast rejected" in out["error"]
    assert "amount_sats" in out  # partial diagnostic even on failure


# --- 7. dry_run vs real-send status --------------------------------------------

def test_dry_run_true_reports_dry_run_status(monkeypatch, capsys):
    fake = _install_fake_wallet(monkeypatch, send_txid="dryrun-placeholder")
    rc, out = _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 70, "dry_run": True}, capsys)
    assert rc == 0
    assert out["status"] == "dry_run"
    assert out["txid"] == "dryrun-placeholder"
    assert fake.send_transfer_multi.call_args.kwargs["dry_run"] is True


def test_dry_run_defaults_true_when_absent(monkeypatch, capsys):
    """dry_run must default to True (fail-safe) when the caller omits it."""
    fake = _install_fake_wallet(monkeypatch)
    rc, out = _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 70}, capsys)
    assert rc == 0
    assert out["status"] == "dry_run"
    assert fake.send_transfer_multi.call_args.kwargs["dry_run"] is True


def test_dry_run_false_reports_sent_status(monkeypatch, capsys):
    fake = _install_fake_wallet(monkeypatch, send_txid="e02d0676deadbeef")
    rc, out = _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 70, "dry_run": False}, capsys)
    assert rc == 0
    assert out["status"] == "sent"
    assert out["txid"] == "e02d0676deadbeef"
    assert out["network"] == "mainnet"
    assert fake.send_transfer_multi.call_args.kwargs["dry_run"] is False


# --- 8. The memo invariant -----------------------------------------------------

def test_never_sends_a_memo(monkeypatch, capsys):
    """The module docstring claims 'never attaches a memo / OP_RETURN' — assert
    it directly on the kwargs passed to send_transfer_multi, not just the text."""
    fake = _install_fake_wallet(monkeypatch)
    _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 70, "dry_run": False}, capsys)
    assert fake.send_transfer_multi.call_args.kwargs["memo"] is None


def test_recipients_carry_address_and_computed_sats(monkeypatch, capsys):
    fake = _install_fake_wallet(monkeypatch, prices=(Decimal("30000"), Decimal("30000")))
    _run(monkeypatch, {"address": "bc1qexample", "amount_eur": 30, "dry_run": False}, capsys)
    recipients = fake.send_transfer_multi.call_args.kwargs["recipients"]
    assert recipients == [{"address": "bc1qexample", "amount_sats": 100_000}]

"""Read-only BTC chain reconciliation for the payment retry command.

Before a failed BTC payment may be retried, the chain itself must show that
the first attempt paid nothing. The operator's words cannot prove that: a
Claude child that timed out may have broadcast a transaction after its parent
stopped listening.

WHAT IS QUERIED
---------------
The payee address's transaction history, mempool and confirmed, through a
public Esplora block-explorer API (mempool.space by default) that needs no
credentials. The sending wallet's address is not configured anywhere the
handler reads: the handler drives the wallet through the btc-wallet MCP,
which derives its own addresses. Every transaction that pays the payee
address appears in the payee address's history whichever wallet sent it, so
this query covers every transaction a sending-wallet query would, and more.
Any output that pays the payee address inside the window refuses the retry,
whatever its amount and whoever sent it.

THE WINDOW
----------
From the original attempt's ``intent_at`` minus :data:`BLOCK_TIME_SLACK_S`
onward, through the time of the query. Block timestamps are not monotonic and
may differ from wall time by about two hours under the consensus rules, so the
window starts six hours early, and confirmed history is paged until a whole
page is older than the window start minus a further
:data:`PAGE_STOP_MARGIN_S`. Every mempool transaction is inside the window.

Addresses are compared case-insensitively. Bech32 addresses are
case-insensitive; for base58 addresses this can only add matches, which
refuses more, never less.

FAIL CLOSED
-----------
A lookup that fails, times out, returns something unparseable, or cannot be
shown to be complete (a full mempool page, too many confirmed pages) raises
:class:`ReconciliationError`. The caller refuses the retry.

The evidence returned never carries the payee address itself, only its
SHA-256 digest, so it may be stored on the journal and the payment_approval
record, both of which never carry account numbers or addresses.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

EXPLORER_URL_ENV = "MONEDULA_BTC_EXPLORER_URL"
DEFAULT_EXPLORER_URL = "https://mempool.space/api"
USER_AGENT = "ateles-monedula-reconcile/1.0"
REQUEST_TIMEOUT_S = 15

BLOCK_TIME_SLACK_S = 6 * 3600
PAGE_STOP_MARGIN_S = 6 * 3600
# Esplora page sizes: up to 50 mempool and 25 confirmed transactions per call.
MEMPOOL_PAGE_CAP = 50
CHAIN_PAGE_SIZE = 25
MAX_CHAIN_PAGES = 20

RESULT_CLEAR = "no_payment_to_payee_found"
RESULT_PAID = "payment_to_payee_found"
EVIDENCE_VERSION = 1

_ADDRESS_RE = re.compile(r"[A-Za-z0-9]{14,90}")
_TXID_RE = re.compile(r"[0-9a-f]{64}")

Fetch = Callable[[str], Any]


class ReconciliationError(Exception):
    """The chain could not be read completely. Refuse the retry."""


def _default_fetch(url: str) -> Any:
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ReconciliationError(
            f"block explorer lookup failed: {type(exc).__name__}"
        ) from exc


def explorer_url() -> str:
    return (os.environ.get(EXPLORER_URL_ENV, "") or DEFAULT_EXPLORER_URL).rstrip("/")


class EsploraExplorer:
    """Esplora REST client. ``fetch`` is injectable: tests never use the network."""

    def __init__(self, base_url: str | None = None, fetch: Fetch | None = None) -> None:
        self.base_url = (base_url or explorer_url()).rstrip("/")
        self._fetch = fetch or _default_fetch

    def get(self, path: str) -> Any:
        if not self.base_url.startswith("https://"):
            raise ReconciliationError("block explorer URL must use https")
        try:
            return self._fetch(f"{self.base_url}{path}")
        except ReconciliationError:
            raise
        except Exception as exc:  # noqa: BLE001 - any client failure fails closed
            raise ReconciliationError(
                f"block explorer lookup failed: {type(exc).__name__}"
            ) from exc


def _query_form(address: str) -> str:
    """Bech32 addresses are case-insensitive; explorers index them lowercase."""
    lower = address.lower()
    return lower if lower.startswith(("bc1", "tb1", "bcrt1")) else address


def address_digest(address: str) -> str:
    return hashlib.sha256(_query_form(address.strip()).encode()).hexdigest()


def window_start(intent_at: float) -> float:
    return float(intent_at) - BLOCK_TIME_SLACK_S


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _parse_tx(raw: Any) -> tuple[str, bool, int | None, list[str | None]]:
    """``(txid, confirmed, block_time, output addresses)`` or raise."""
    if not isinstance(raw, dict):
        raise ReconciliationError("block explorer returned an unparseable transaction")
    txid = raw.get("txid")
    status = raw.get("status")
    vout = raw.get("vout")
    if not isinstance(txid, str) or not _TXID_RE.fullmatch(txid):
        raise ReconciliationError("block explorer returned an unparseable txid")
    if not isinstance(status, dict) or not isinstance(status.get("confirmed"), bool):
        raise ReconciliationError("block explorer returned an unparseable status")
    confirmed = status["confirmed"]
    block_time = status.get("block_time")
    if confirmed and (not isinstance(block_time, int) or isinstance(block_time, bool)):
        raise ReconciliationError("block explorer returned an unparseable block time")
    if not isinstance(vout, list) or not vout:
        raise ReconciliationError("block explorer returned unparseable outputs")
    outputs: list[str | None] = []
    for out in vout:
        if not isinstance(out, dict):
            raise ReconciliationError("block explorer returned unparseable outputs")
        addr = out.get("scriptpubkey_address")
        if addr is not None and not isinstance(addr, str):
            raise ReconciliationError("block explorer returned unparseable outputs")
        outputs.append(addr)
    return txid, confirmed, block_time if confirmed else None, outputs


def _page(explorer: EsploraExplorer, path: str) -> list[Any]:
    data = explorer.get(path)
    if not isinstance(data, list):
        raise ReconciliationError("block explorer returned an unparseable page")
    return data


def reconcile(
    explorer: EsploraExplorer,
    payee_address: str,
    intent_at: float,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Query the chain and return the evidence. Raises on any doubt.

    The caller must refuse unless ``evidence["result"] == RESULT_CLEAR``.
    """
    address = (payee_address or "").strip()
    if not _ADDRESS_RE.fullmatch(address):
        raise ReconciliationError("payee BTC address is missing or malformed")
    start = window_start(intent_at)
    base = f"/address/{_query_form(address)}/txs"
    target = address.lower()

    seen: dict[str, None] = {}
    paying: list[str] = []

    def consider(raw: Any) -> int | None:
        txid, confirmed, block_time, outputs = _parse_tx(raw)
        if confirmed and block_time is not None and block_time < start:
            return block_time
        seen.setdefault(txid, None)
        if any(out is not None and out.lower() == target for out in outputs) and (
            txid not in paying
        ):
            paying.append(txid)
        return block_time

    mempool = _page(explorer, f"{base}/mempool")
    if len(mempool) >= MEMPOOL_PAGE_CAP:
        raise ReconciliationError(
            "block explorer mempool page is full; cannot prove it is complete"
        )
    for raw in mempool:
        consider(raw)

    path = f"{base}/chain"
    for _ in range(MAX_CHAIN_PAGES):
        page = _page(explorer, path)
        newest: int | None = None
        for raw in page:
            block_time = consider(raw)
            if block_time is not None:
                newest = block_time if newest is None else max(newest, block_time)
        # Block times are not monotonic: stop only once the WHOLE page is
        # well before the window, never on one old-looking block.
        if len(page) < CHAIN_PAGE_SIZE or (
            newest is not None and newest < start - PAGE_STOP_MARGIN_S
        ):
            break
        last = page[-1]
        path = f"{base}/chain/{last['txid']}"
    else:
        raise ReconciliationError(
            "block explorer history is longer than the reconciliation bound"
        )

    checked_at = float(now())
    return {
        "version": EVIDENCE_VERSION,
        "explorer": explorer.base_url,
        "payee_address_sha256": address_digest(address),
        "window_start": _iso(start),
        "window_start_epoch": start,
        "window_end": _iso(checked_at),
        "checked_at": _iso(checked_at),
        "txids_seen": sorted(seen),
        "paying_txids": sorted(paying),
        "result": RESULT_PAID if paying else RESULT_CLEAR,
    }


def canonical(evidence: dict[str, Any]) -> str:
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"))


def validate_approved(
    raw: Any,
    *,
    explorer: EsploraExplorer,
    payee_address: str,
    intent_at: float,
) -> dict[str, Any]:
    """Parse evidence stored on an approval and check it still describes this
    payment: same explorer, payee, window, and a clear result. Raises."""
    if not isinstance(raw, str) or not raw:
        raise ReconciliationError("approval carries no chain reconciliation evidence")
    try:
        evidence = json.loads(raw)
    except ValueError as exc:
        raise ReconciliationError("approval chain evidence is unparseable") from exc
    if not isinstance(evidence, dict) or canonical(evidence) != raw:
        raise ReconciliationError("approval chain evidence is not canonical")
    expected = {
        "version": EVIDENCE_VERSION,
        "explorer": explorer.base_url,
        "payee_address_sha256": address_digest(payee_address),
        "window_start_epoch": window_start(intent_at),
        "result": RESULT_CLEAR,
        "paying_txids": [],
    }
    for name, want in expected.items():
        if evidence.get(name) != want:
            raise ReconciliationError(
                f"approval chain evidence does not match this payment ({name})"
            )
    if not isinstance(evidence.get("txids_seen"), list):
        raise ReconciliationError("approval chain evidence is malformed (txids_seen)")
    return evidence

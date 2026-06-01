"""
ECB-based foreign exchange rates via Frankfurter (https://www.frankfurter.app/).

No API key. Suitable for historical dates (YYYY-MM-DD) and latest rates.
Use for imports and scripts; tax filing still defers to gestor / official rules when required.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

FRANKFURTER_BASE = "https://api.frankfurter.app"
DEFAULT_TIMEOUT = 10.0

logger = logging.getLogger(__name__)


def _normalize(ccy: str) -> str:
    return (ccy or "").strip().upper() or "USD"


def fetch_frankfurter_rate(
    from_currency: str,
    to_currency: str,
    *,
    date: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Optional[float]:
    """
    Return units of `to_currency` per one unit of `from_currency`.

    :param date: ISO date YYYY-MM-DD for ECB fixing; if None, uses /latest.
    """
    a = _normalize(from_currency)
    b = _normalize(to_currency)
    if a == b:
        return 1.0

    sess = session or requests
    raw = (date or "").strip()
    path = raw if raw else "latest"
    url = f"{FRANKFURTER_BASE}/{path}"
    try:
        resp = sess.get(
            url,
            params={"from": a, "to": b},
            timeout=DEFAULT_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            logger.warning(
                "Frankfurter HTTP %s for %s %s→%s: %s",
                resp.status_code,
                path,
                a,
                b,
                resp.text[:200],
            )
            return None
        data = resp.json()
        rates = data.get("rates") or {}
        rate = rates.get(b)
        if rate is None:
            logger.warning("Frankfurter missing rate %s→%s on %s", a, b, path)
            return None
        return float(rate)
    except (requests.RequestException, TypeError, ValueError) as e:
        logger.warning("Frankfurter request failed %s→%s %s: %s", a, b, path, e)
        return None


class CurrencyConverter:
    """Historical and latest FX using Frankfurter (ECB); last-resort static fallbacks."""

    def __init__(self) -> None:
        self.rate_cache: dict[str, float] = {}
        self._session = requests.Session()

    def get_exchange_rate(
        self, from_currency: str, to_currency: str, date: str
    ) -> float:
        """Rate to multiply `from_currency` amounts to get `to_currency` (for date ISO or latest if empty)."""
        if from_currency == to_currency:
            return 1.0

        cache_key = (
            f"{_normalize(from_currency)}_{_normalize(to_currency)}_{date or 'latest'}"
        )
        if cache_key in self.rate_cache:
            return self.rate_cache[cache_key]

        d = (date or "").strip() or None
        rate = fetch_frankfurter_rate(
            from_currency,
            to_currency,
            date=d,
            session=self._session,
        )
        if rate is not None:
            self.rate_cache[cache_key] = rate
            return rate

        # Latest fallback if historical failed (e.g. bad date string)
        if d:
            rate = fetch_frankfurter_rate(
                from_currency,
                to_currency,
                date=None,
                session=self._session,
            )
            if rate is not None:
                self.rate_cache[cache_key] = rate
                return rate

        # Static fallbacks (legacy importer behavior)
        fb = {"EUR": 1.08, "GBP": 1.27, "USD": 1.0}
        out = fb.get(_normalize(from_currency), 1.0)
        logger.warning(
            "Frankfurter unavailable; using static fallback for %s→%s on %s",
            from_currency,
            to_currency,
            date,
        )
        self.rate_cache[cache_key] = out
        return out

    def convert_to_usd(self, amount: float, currency: str, date: str) -> float:
        """Convert amount to USD using historical (or latest) rate."""
        rate = self.get_exchange_rate(currency, "USD", date)
        return amount * rate

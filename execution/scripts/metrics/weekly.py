"""Weekly windows, robust statistics, and metric_reading construction.

Weeks are Monday 00:00 UTC to the next Monday 00:00 UTC. The window end is the
exclusive boundary, rendered ``YYYY-MM-DDT00:00:00Z``; it is the reading's
identity along with the metric and artifact.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from execution.scripts.metrics import schemas

UTC = timezone.utc


def parse_ts(value: str) -> datetime:
    """ISO-8601 (with Z or offset, or a bare date) to an aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def week_start(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def last_complete_week_end(now: datetime, lag_days: int = 0) -> datetime:
    """Latest Monday 00:00Z that is at or before ``now - lag_days``."""
    return week_start(now - timedelta(days=lag_days))


def weeks_back(now: datetime, count: int, lag_days: int = 0) -> list[tuple[datetime, datetime]]:
    """``count`` complete weeks ending at the latest complete boundary, oldest first."""
    end = last_complete_week_end(now, lag_days)
    return [(end - timedelta(weeks=i + 1), end - timedelta(weeks=i)) for i in reversed(range(count))]


def weeks_between(first: datetime, now: datetime, lag_days: int = 0) -> list[tuple[datetime, datetime]]:
    """Complete weeks from the week containing ``first`` up to the latest boundary."""
    end = last_complete_week_end(now, lag_days)
    out = []
    cursor = week_start(first)
    while cursor + timedelta(weeks=1) <= end:
        out.append((cursor, cursor + timedelta(weeks=1)))
        cursor += timedelta(weeks=1)
    return out


def in_window(dt: datetime, start: datetime, end: datetime) -> bool:
    return start <= dt < end


def median(values: Iterable[float]) -> float | None:
    vals = list(values)
    return statistics.median(vals) if vals else None


def mad(values: Iterable[float]) -> float | None:
    """Median absolute deviation (unscaled, as the noise-floor rule uses it)."""
    vals = list(values)
    if not vals:
        return None
    m = statistics.median(vals)
    return statistics.median(abs(v - m) for v in vals)


def summarize(values: list[float]) -> dict[str, Any]:
    """Median and the noise band F = 2 x MAD, with F as a share of the median."""
    if not values:
        return {"n": 0, "median": None, "mad": None, "noise_floor": None, "noise_floor_pct": None, "total": 0}
    m = median(values)
    d = mad(values)
    f = 2 * d
    pct = (f / m * 100) if m else None
    return {"n": len(values), "median": m, "mad": d, "noise_floor": f, "noise_floor_pct": pct, "total": sum(values)}


def make_reading(*, metric_id: str, artifact: str, start: datetime, end: datetime, value: float,
                 unit: str, collector: str, sourced_at: datetime, coverage_ratio: float = 1.0,
                 coverage_note: str = "", probe_ok: bool | None = None,
                 detail: dict[str, Any] | None = None) -> dict[str, Any]:
    window_end = iso(end)
    return schemas.build_record(
        "metric_reading",
        reading_key=schemas.reading_key(metric_id, artifact, window_end),
        metric_ref=metric_id,
        artifact_ref=artifact,
        value=float(value),
        unit=unit,
        window_start=iso(start),
        window_end=window_end,
        sourced_at=iso(sourced_at),
        coverage_ratio=float(coverage_ratio),
        # Always written, "" when nothing is missing: an absent field is not written, so a clean rerun
        # would leave an earlier run's note (e.g. from --allow-stale) on the stored reading.
        coverage_note=coverage_note or "",
        probe_ok=probe_ok,
        collector=collector,
        status="ok" if coverage_ratio >= 0.9 else "partial",
        detail=detail or None,
    )


def summarize_readings(readings: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Per (metric, artifact): summary over readings with coverage of at least 0.9."""
    grouped: dict[tuple[str, str], list[float]] = {}
    for r in readings:
        if (r.get("coverage_ratio") or 0) < 0.9:
            continue
        grouped.setdefault((r["metric_ref"], r["artifact_ref"]), []).append(r["value"])
    return {k: summarize(v) for k, v in sorted(grouped.items())}


def format_summary(summary: dict[tuple[str, str], dict[str, Any]], units: dict[str, str] | None = None) -> str:
    lines = []
    for (metric, artifact), s in summary.items():
        if s["n"] == 0:
            lines.append(f"{metric} {artifact}: no readings")
            continue
        unit = (units or {}).get(metric, "")
        pct = f", {s['noise_floor_pct']:.0f}% of median" if s["noise_floor_pct"] is not None else ""
        lines.append(f"{metric} {artifact}: median {s['median']:.4g} {unit} +/- {s['noise_floor']:.4g} "
                     f"(2xMAD{pct}; n={s['n']} weeks, sum {s['total']:.6g})")
    return "\n".join(lines)

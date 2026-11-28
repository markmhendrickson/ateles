#!/usr/bin/env python3
"""One bounded filesystem-usage poll; no model calls or scheduling side effects.

Runtime contract and activation checks: docs/store_disk_watch.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.capabilities.neotoma_http import request_json  # noqa: E402
from lib.daemon_runtime.neotoma_correct import unwrap_snapshot  # noqa: E402
from lib.notify import held_view  # noqa: E402

TOKEN_ENV = "STORE_WATCH_FLY_METRICS_TOKEN"
STALE_SECONDS = 3600
SAMPLE_MAX_AGE = 300
CONFIG_PREFIX = "store-watch:"
LEVELS = ("healthy", "warning", "critical")
MAX_TARGETS = 100
# Provider contract: https://docs.fly.io/monitoring/metrics (Instance Filesystem).
# Block size cancels out of the df ratio, so only these three gauges are read.
FILESYSTEM_METRICS = (
    "fly_instance_filesystem_blocks",
    "fly_instance_filesystem_blocks_free",
    "fly_instance_filesystem_blocks_avail",
)
APP_LABEL, MOUNT_LABEL, INSTANCE_LABEL = "app", "mount", "instance"
FAILURE_RECEIPT = "store-watch:state-failure"
DELIVERY_PREFIX = "store-watch-delivery: "
RUNBOOK = "See docs/store_disk_watch.md#failure-and-recovery-behavior."
FAILURES = frozenset(
    (
        "invalid_number",
        "metrics_endpoint_invalid",
        "metrics_token_missing_or_invalid",
        "metrics_auth_failure",
        "metrics_http_failure",
        "metrics_request_failure",
        "metrics_partial_or_invalid",
        "metrics_not_vector",
        "metrics_empty_or_invalid",
        "metrics_labels_mismatch",
        "metrics_duplicate_series",
        "metrics_invalid_sample",
        "metrics_stale",
        "metrics_clock_skew",
        "metrics_invalid_capacity",
        "metrics_gauge_missing",
        "metrics_instance_missing",
        "instance_inventory_unknown",
        "configuration_unknown",
        "metrics_read_failure",
    )
)


def diagnostic(error, fallback):
    return (
        str(error)
        if isinstance(error, Unknown) and str(error) in FAILURES
        else fallback
    )


def valid_instances(value):
    return (
        isinstance(value, list)
        and all(
            isinstance(i, str)
            and i
            and len(i) <= 200
            and not any(ord(c) < 32 for c in i)
            for i in value
        )
        and len(value) == len(set(value))
    )


class Unknown(RuntimeError):
    """A fixed diagnostic category; never carries provider text or credentials."""


def finite(value):
    if isinstance(value, bool):
        raise Unknown("invalid_number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise Unknown("invalid_number") from None
    if not math.isfinite(number):
        raise Unknown("invalid_number")
    return number


def level(percent):
    return "critical" if percent >= 85 else "warning" if percent >= 70 else "healthy"


def target_key(target):
    return hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()[:24]


def load_targets(request, environ):
    """Read exactly the selected records; never infer a list from a partial query."""
    ids = json.loads(environ.get("STORE_WATCH_CONFIG_IDS", "null"))
    if (
        not isinstance(ids, list)
        or not 0 < len(ids) <= MAX_TARGETS
        or any(
            not isinstance(i, str) or not re.fullmatch(r"ent_[a-zA-Z0-9]+", i)
            for i in ids
        )
        or len(set(ids)) != len(ids)
    ):
        raise Unknown("configuration_list_missing_or_invalid")
    targets = []
    for identifier in ids:
        row = request("GET", f"/entities/{quote(identifier, safe='')}")
        snap = unwrap_snapshot(row)
        if (
            not snap
            or row.get("entity_type", snap.get("entity_type"))
            != "deployment_configuration"
            or not isinstance(snap.get("fly_app"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", snap["fly_app"])
        ):
            raise Unknown("configuration_record_invalid")
        gotchas = snap.get("gotchas")
        lines = gotchas.splitlines() if isinstance(gotchas, str) else gotchas
        if not isinstance(lines, list) or any(not isinstance(x, str) for x in lines):
            raise Unknown("configuration_mounts_missing")
        entries = [
            x[len(CONFIG_PREFIX) :] for x in lines if x.startswith(CONFIG_PREFIX)
        ]
        if len(entries) != 1:
            raise Unknown("configuration_mounts_missing")
        mounts = json.loads(entries[0])
        if not isinstance(mounts, list) or not mounts:
            raise Unknown("configuration_mounts_invalid")
        for mount in mounts:
            if (
                not isinstance(mount, str)
                or not mount
                or len(mount) > 200
                or any(ord(c) < 32 for c in mount)
            ):
                raise Unknown("configuration_mounts_invalid")
            # Use the observed label literally: Fly describes a destination
            # name, which must be confirmed before activation, not normalized.
            target = {"app": snap["fly_app"], "mount": mount}
            if target in targets:
                raise Unknown("configuration_duplicate_mount")
            targets.append(target)
    if len(targets) > MAX_TARGETS:
        raise Unknown("configuration_too_large")
    return targets


class Metrics:
    """Read every matching filesystem, retaining valid siblings of bad series."""

    def __init__(self, environ, client):
        self.client = client
        self.url = environ.get("STORE_WATCH_METRICS_URL", "")
        url = urlsplit(self.url)
        if (
            url.scheme != "https"
            or url.netloc != "api.fly.io"
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not re.fullmatch(
                r"/prometheus/[a-z0-9][a-z0-9-]*/api/v1/query", url.path
            )
        ):
            raise Unknown("metrics_endpoint_invalid")
        self.token = environ.get(TOKEN_ENV, "").strip()
        # Require the operator-created credential; never use an ambient Fly token.
        if not self.token.startswith("FlyV1 ") or any(c in self.token for c in "\r\n"):
            raise Unknown("metrics_token_missing_or_invalid")

    def sample(self, query, labels, now):
        response = self.client.get(
            self.url,
            params={"query": query, "time": now},
            headers={"Authorization": self.token},
            timeout=15,
            follow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise Unknown("metrics_auth_failure")
        if response.status_code != 200:
            raise Unknown("metrics_http_failure")
        data = response.json()
        if (
            not isinstance(data, dict)
            or data.get("status") != "success"
            or data.get("warnings")
            or data.get("infos")
            or data.get("isPartial")
        ):
            raise Unknown("metrics_partial_or_invalid")
        body = data.get("data")
        if (
            not isinstance(body, dict)
            or body.get("resultType") != "vector"
            or body.get("isPartial")
        ):
            raise Unknown("metrics_not_vector")
        rows = body.get("result")
        if not isinstance(rows, list) or not rows:
            raise Unknown("metrics_empty_or_invalid")
        samples, failure = {}, None
        for row in rows:
            metric = row.get("metric") if isinstance(row, dict) else None
            if (
                not isinstance(metric, dict)
                or any(metric.get(k) != v for k, v in labels.items())
                or not valid_instances([metric.get(INSTANCE_LABEL)])
                or any(not isinstance(v, str) for v in metric.values())
            ):
                failure = failure or "metrics_labels_mismatch"
                continue
            # timestamp() removes the metric name; all other labels must match
            # exactly across gauges to avoid mixing two different filesystems.
            key = tuple(sorted((k, v) for k, v in metric.items() if k != "__name__"))
            if key in samples:
                samples[key] = None
                failure = failure or "metrics_duplicate_series"
                continue
            try:
                pair = row.get("value")
                if not isinstance(pair, list) or len(pair) != 2:
                    raise Unknown("metrics_invalid_sample")
                stamp, value = map(finite, pair)
                if stamp > now + 30:
                    raise Unknown("metrics_clock_skew")
                if now - stamp > SAMPLE_MAX_AGE:
                    raise Unknown("metrics_stale")
                samples[key] = value
            except Unknown as error:
                samples[key] = None
                failure = failure or diagnostic(error, "metrics_invalid_sample")
        return samples, failure

    def read(self, target, now):
        labels = {APP_LABEL: target["app"], MOUNT_LABEL: target["mount"]}
        selector = ",".join(k + "=" + json.dumps(v) for k, v in sorted(labels.items()))
        vectors, failure = [], None
        for name in FILESYSTEM_METRICS:
            query = name + "{" + selector + "}"
            # Both arguments are plain instant-vector selectors, valid inputs
            # to timestamp(v instant-vector); no aggregation/range selectors.
            for expression in (query, f"timestamp({query})"):
                try:
                    samples, problem = self.sample(expression, labels, now)
                except Exception as error:
                    samples, problem = {}, diagnostic(error, "metrics_request_failure")
                vectors.append(samples)
                failure = failure or problem
        instances = []
        keys = sorted(set().union(*(v.keys() for v in vectors)))
        for key in keys:
            try:
                if any(key not in v for v in vectors):
                    raise Unknown("metrics_gauge_missing")
                blocks, t_blocks, free, t_free, avail, t_avail = (
                    finite(v.get(key)) for v in vectors
                )
                stamps = (t_blocks, t_free, t_avail)
                if any(stamp > now + 30 for stamp in stamps):
                    raise Unknown("metrics_clock_skew")
                if any(now - stamp > SAMPLE_MAX_AGE for stamp in stamps):
                    raise Unknown("metrics_stale")
                used = blocks - free
                if not (
                    blocks > 0 and 0 <= avail <= free <= blocks and used + avail > 0
                ):
                    raise Unknown("metrics_invalid_capacity")
                instances.append(
                    {
                        "instance": dict(key)[INSTANCE_LABEL],
                        "percent": 100 * (used / (used + avail)),
                        "stamp": min(stamps),
                    }
                )
            except Unknown as error:
                failure = failure or diagnostic(error, "metrics_invalid_sample")
        return {
            "instances": instances,
            "seen_instances": sorted({dict(key)[INSTANCE_LABEL] for key in keys}),
            "complete": failure is None and bool(instances),
            "failure": failure,
        }


class Notifications:
    """Use the existing transport and its durable, retryable send receipts."""

    def __init__(self):
        from lib.notify import Notifier

        self.notifier = Notifier()

    def __call__(self, event):
        from lib.notify import Priority

        runtime_paths()
        result = self.notifier.send(
            event["message"],
            Priority.CRITICAL if event["severity"] == "critical" else Priority.WARN,
            handler="store-watch",
            bypass_silence=True,
            dedupe_key=event["id"],
            retryable=True,
            email_eligible=False,
        )
        if result is True:
            return True
        # A crash after transport acceptance but before the watch checkpoint
        # must reuse the receipt, rather than send again or claim False is sent.
        with held_view.file_lock(
            held_view.journal_path().with_name(held_view.journal_path().name + ".lock"),
            fail_open=False,
        ) as locked:
            if not locked:
                return False
            try:
                journal = json.loads(
                    held_view.read_text_nofollow(held_view.journal_path())
                )
                if not isinstance(journal, dict) or any(
                    held_view.receipt_problem(value)
                    for key, value in journal.items()
                    if key != held_view.RECOVERED_AT
                ):
                    return False
                entry = journal.get(event["id"])
                return (
                    not held_view.receipt_problem(entry)
                    and entry["state"] == "delivered"
                )
            except (OSError, ValueError, AttributeError):
                return False


def new_state():
    return {"version": 2, "volumes": {}, "configuration_failed": False, "events": []}


def require(condition):
    if not condition:
        raise Unknown("state_unreadable")


def read_state(path, now):
    try:
        state = json.loads(held_view.read_text_nofollow(path))
    except FileNotFoundError:
        return new_state()
    try:
        require(
            isinstance(state, dict)
            and set(state) == {"version", "volumes", "configuration_failed", "events"}
        )
        require(
            type(state["version"]) is int
            and state["version"] in (1, 2)
            and type(state["configuration_failed"]) is bool
        )
        require(
            isinstance(state["volumes"], dict) and isinstance(state["events"], list)
        )
        for key, value in state["volumes"].items():
            require(key == target_key(value["target"]))
            require(set(value["target"]) == {"app", "mount"})
            require(all(isinstance(v, str) and v for v in value["target"].values()))
            require(value["level"] in LEVELS and type(value["stale"]) is bool)
            if state["version"] == 2:
                require(
                    value["seen_instances"] is None
                    or valid_instances(value["seen_instances"])
                )
            for name in ("first_seen", "last_success"):
                require(
                    (value[name] is None and name == "last_success")
                    or 0 <= finite(value[name]) <= now + 30
                )
        keys = set()
        for event in state["events"]:
            require(
                isinstance(event["id"], str) and event["id"] and event["id"] not in keys
            )
            keys.add(event["id"])
            require(isinstance(event["message"], str) and event["message"])
            require(event["severity"] in ("warning", "critical"))
            require(event["delivery"] in ("delivered", "undelivered"))
            require(type(event["recorded"]) is bool)
            require(
                event["task_id"] is None
                or isinstance(event["task_id"], str)
                and event["task_id"]
            )
            require(not event["recorded"] or event["task_id"] is not None)
    except (KeyError, TypeError, Unknown):
        raise Unknown("state_unreadable") from None
    if state["version"] == 1:
        # Old state cannot prove which machines vanished before migration.
        # Preserve its alarms/clock and require an explicit human baseline.
        for value in state["volumes"].values():
            value["seen_instances"] = None
        state["version"] = 2
    return state


def add_event(state, message, severity="critical", identifier=None):
    state["events"].append(
        {
            "id": identifier or "store-watch:" + uuid.uuid4().hex,
            "message": message + " " + RUNBOOK,
            "severity": severity,
            "delivery": "undelivered",
            "task_id": None,
            "recorded": False,
        }
    )


def record_event(event, request):
    """Verify task identity/routing, then merge and read back delivery notes."""
    line = DELIVERY_PREFIX + json.dumps(
        {"delivery": event["delivery"], "event": event["id"]}, sort_keys=True
    )
    if not event["task_id"]:
        # The create payload is immutable, including across a lost response.
        # Delivery is updated separately, without resetting human-owned status.
        wanted = {
            "title": event["message"].split(". ", 1)[0] + " [" + event["id"] + "]",
            "description": event["message"],
            "status": "blocked",
            "assigned_to": "operator",
            "blocked_reason": "Human assessment required; observation only, no automatic repair.",
            "priority": "p0" if event["severity"] == "critical" else "p1",
            "tags": ["store-watch"],
            "notes": DELIVERY_PREFIX
            + json.dumps(
                {"delivery": "undelivered", "event": event["id"]}, sort_keys=True
            ),
        }
        result = request(
            "POST",
            "/store",
            {
                "entities": [{"entity_type": "task", **wanted}],
                "observation_source": "workflow_state",
                "idempotency_key": event["id"] + ":create",
            },
        )
        if result.get("success") is False or result.get("unknown_fields_count"):
            raise Unknown("task_write_failed")
        rows = result.get("entities", [])
        if not rows:
            result = request(
                "POST",
                "/entities/query",
                {
                    "entity_type": "task",
                    "include_snapshots": True,
                    "snapshot_filters": {
                        "title": {"op": "eq", "value": wanted["title"]}
                    },
                    "limit": 2,
                },
            )
            rows = result.get("entities", [])
            if result.get("next_cursor") or result.get("total") != 1:
                raise Unknown("task_readback_ambiguous")
        if len(rows) != 1 or not isinstance(rows[0].get("entity_id"), str):
            raise Unknown("task_readback_missing")
        identifier = rows[0]["entity_id"]
        actual = unwrap_snapshot(
            request("GET", "/entities/" + quote(identifier, safe=""))
        )
        if (
            not actual
            or not all(k in actual for k in wanted)
            or any(
                actual.get(k) != wanted[k]
                for k in ("title", "description", "status", "assigned_to", "priority")
            )
        ):
            raise Unknown("task_readback_mismatch")
        event["task_id"] = identifier
    actual = unwrap_snapshot(
        request("GET", "/entities/" + quote(event["task_id"], safe=""))
    )
    if not actual or not isinstance(actual.get("notes"), str):
        raise Unknown("task_notes_unreadable")
    # Replace only this event's machine-owned line, preserving human text and
    # other events verbatim, including line endings and human-owned status.
    previous = actual["notes"]
    lines, replaced = [], False
    for existing in previous.splitlines(keepends=True):
        owned = False
        if existing.startswith(DELIVERY_PREFIX):
            try:
                owned = (
                    json.loads(existing[len(DELIVERY_PREFIX) :]).get("event")
                    == event["id"]
                )
            except (ValueError, AttributeError):
                pass
        if owned:
            if not replaced:
                ending = (
                    "\r\n"
                    if existing.endswith("\r\n")
                    else "\n"
                    if existing.endswith("\n")
                    else ""
                )
                lines.append(line + ending)
                replaced = True
        else:
            lines.append(existing)
    notes = "".join(lines)
    if not replaced:
        notes += ("\n" if notes and not notes.endswith("\n") else "") + line
    result = request(
        "POST",
        "/correct",
        {
            "entity_id": event["task_id"],
            "entity_type": "task",
            "field": "notes",
            "value": notes,
            "idempotency_key": event["id"]
            + ":notes:"
            + hashlib.sha256(notes.encode()).hexdigest(),
        },
    )
    if result.get("success") is False:
        raise Unknown("task_write_failed")
    actual = unwrap_snapshot(
        request("GET", "/entities/" + quote(event["task_id"], safe=""))
    )
    if not actual or actual.get("notes") != notes:
        raise Unknown("task_readback_mismatch")
    event["recorded"] = True


def drain(state, save, request, notify):
    for event in state["events"]:
        if not event["recorded"]:
            try:
                record_event(event, request)
            except Exception:
                event["recorded"] = False
            save()
        if event["delivery"] != "delivered":
            try:
                delivered = notify(event) is True
            except Exception:
                delivered = False
            if delivered:
                event["delivery"], event["recorded"] = "delivered", False
                save()
                try:
                    record_event(event, request)
                except Exception:
                    event["recorded"] = False
                save()


def run_once(path, request, read_usage, notify, environ=None, now=None):
    environ = os.environ if environ is None else environ
    now = time.time() if now is None else finite(now)
    state = read_state(path, now)
    if not path.parent.is_dir():
        raise Unknown("state_directory_missing")

    def save():
        held_view.atomic_write(path, json.dumps(state, sort_keys=True, allow_nan=False))

    try:
        targets = load_targets(request, environ)
    except Exception:
        targets = [v["target"] for v in state["volumes"].values()]
        configured = False
        if not state["configuration_failed"]:
            add_event(
                state,
                "Store disk watch configuration is UNKNOWN. Check the selected deployment records and mounts; monitoring is incomplete.",
            )
        state["configuration_failed"] = True
    else:
        configured = True
        if state["configuration_failed"]:
            add_event(
                state,
                "Store disk watch configuration recovered; filesystem usage still requires a complete read.",
                "warning",
            )
        state["configuration_failed"] = False
    results = []
    selected = {target_key(t) for t in targets}
    # A removed target is not silently healthy; its retirement is recorded.
    if configured:
        for key in set(state["volumes"]) - selected:
            add_event(
                state,
                "Store disk watch target removed from configuration: "
                + json.dumps(state["volumes"][key]["target"], sort_keys=True),
                "warning",
            )
            del state["volumes"][key]
    for target in targets:
        value = state["volumes"].setdefault(
            target_key(target),
            {
                "target": target,
                "first_seen": now,
                "last_success": None,
                "level": "healthy",
                "stale": False,
                "seen_instances": [],
            },
        )
        label = json.dumps(target, sort_keys=True)
        failure = None
        try:
            if not configured:
                raise Unknown("configuration_unknown")
            reading = read_usage(target, now)
            complete = reading["complete"]
            require(type(complete) is bool)
            instances = reading["instances"]
            require(isinstance(instances, list))
            seen = reading.get("seen_instances", [s["instance"] for s in instances])
            require(valid_instances(seen))
            failure = reading.get("failure")
            if failure not in FAILURES:
                failure = None if complete else "metrics_partial_or_invalid"
            if value["seen_instances"] is None:
                complete, failure = False, failure or "instance_inventory_unknown"
            else:
                missing = set(value["seen_instances"]) - set(seen)
                value["seen_instances"] = sorted(
                    set(value["seen_instances"]) | set(seen)
                )
                if missing:
                    complete, failure = False, failure or "metrics_instance_missing"
            if not instances:
                raise Unknown(failure or "metrics_empty_or_invalid")
            for sample in instances:
                percent, stamp = finite(sample["percent"]), finite(sample["stamp"])
                require(0 <= percent <= 100 and -30 <= now - stamp <= SAMPLE_MAX_AGE)
                require(
                    isinstance(sample["instance"], str) and bool(sample["instance"])
                )
            worst = max(instances, key=lambda sample: sample["percent"])
            percent = finite(worst["percent"])
        except Exception as error:
            failure = diagnostic(error, "metrics_read_failure")
            results.append(
                {
                    "target": target,
                    "status": "unknown",
                    "complete": False,
                    "failure": failure,
                }
            )
        else:
            current = level(percent)
            if complete:
                stamp = min(finite(sample["stamp"]) for sample in instances)
                value["last_success"] = max(value["last_success"] or 0, stamp)
                if value["stale"]:
                    add_event(
                        state, "Store disk watch reads recovered: " + label, "warning"
                    )
                    value["stale"] = False
            # Partial reads can escalate, but never clear a known worse level.
            if current != value["level"] and (
                complete or LEVELS.index(current) > LEVELS.index(value["level"])
            ):
                add_event(
                    state,
                    f"Filesystem {current}: {target['app']} {target['mount']} "
                    f"instance {worst['instance']}: {percent:.1f}% used. "
                    + (
                        "Complete known instance set valid. "
                        if complete
                        else "Some series UNKNOWN. "
                    )
                    + "Assess capacity; this job changes no volume.",
                    "critical" if current == "critical" else "warning",
                )
                value["level"] = current
            results.append(
                {
                    "target": target,
                    "status": current
                    if complete or current != "healthy"
                    else "unknown",
                    "percent": percent,
                    "complete": complete,
                    "failure": failure,
                    "instances": instances,
                }
            )
        since = (
            value["last_success"]
            if value["last_success"] is not None
            else value["first_seen"]
        )
        if now - since >= STALE_SECONDS and not value["stale"]:
            add_event(
                state,
                "Store disk watch critical: no successful filesystem read for one hour; usage UNKNOWN. "
                + label
                + "; failure="
                + (failure or "metrics_stale"),
            )
            value["stale"] = True
    # This checkpoint is mandatory before any external write/send. Replay uses
    # the same event id for the task and the notifier's existing receipt.
    save()
    drain(state, save, request, notify)
    pending = [
        e for e in state["events"] if not e["recorded"] or e["delivery"] != "delivered"
    ]
    completed = [e for e in state["events"] if e not in pending]
    state["events"] = completed[-100:] + pending
    save()
    return {
        "configuration": "ok" if configured else "unknown",
        "volumes": results,
        "undelivered": sum(e["delivery"] != "delivered" for e in pending),
        "unrecorded": sum(not e["recorded"] for e in pending),
    }


@contextmanager
def failure_journal():
    path = runtime_paths()
    with held_view.file_lock(
        path.with_name(path.name + ".lock"), fail_open=False
    ) as locked:
        require(locked)
        try:
            journal = json.loads(held_view.read_text_nofollow(path))
        except FileNotFoundError:
            journal = {}
        require(isinstance(journal, dict))
        require(
            not any(
                held_view.receipt_problem(v)
                for k, v in journal.items()
                if k != held_view.RECOVERED_AT
            )
        )
        yield journal
        held_view.atomic_write(
            path, json.dumps(journal, sort_keys=True, allow_nan=False)
        )


def failure_episode(request, notify, recovered=False):
    """Reuse a receipt's optional event until recovery and effects are confirmed."""
    with failure_journal() as journal:
        receipt = journal.get(FAILURE_RECEIPT)
        if receipt is None:
            if recovered:
                return None
            emergency = new_state()
            add_event(
                emergency,
                "Store disk watch state is UNKNOWN; monitoring failed. Preserve the state and inspect the job before trusting any healthy result.",
            )
            receipt = {
                "state": "pending",
                "pending_at": datetime.now(timezone.utc).isoformat(),
                "handler": "store-watch",
                "watch_event": emergency["events"][0],
            }
            journal[FAILURE_RECEIPT] = receipt
        event = receipt.get("watch_event")
        require(
            isinstance(event, dict)
            and set(event)
            == {"id", "message", "severity", "delivery", "task_id", "recorded"}
        )
        # Optional receipt metadata is also fail-closed, never discarded.
        require(
            isinstance(event.get("id"), str) and event["id"].startswith("store-watch:")
        )
        require(event.get("delivery") in ("delivered", "undelivered"))
        require(type(event.get("recorded")) is bool)
        require(isinstance(event.get("message"), str) and bool(event["message"]))
        require(event.get("severity") in ("critical", "warning"))
        require(
            event.get("task_id") is None
            or isinstance(event["task_id"], str)
            and bool(event["task_id"])
        )
        require(not event["recorded"] or event["task_id"] is not None)

    def save():
        with failure_journal() as journal:
            require(
                journal.get(FAILURE_RECEIPT, {}).get("watch_event", {}).get("id")
                == event["id"]
            )
            journal[FAILURE_RECEIPT]["watch_event"] = event

    # The existing state lock excludes concurrent episode owners. A failed
    # state lock is a competing poll, not permission to mutate this episode.
    drain({"events": [event]}, save, request, notify)
    if recovered and event["recorded"] and event["delivery"] == "delivered":
        with failure_journal() as journal:
            require(
                journal.get(FAILURE_RECEIPT, {}).get("watch_event", {}).get("id")
                == event["id"]
            )
            del journal[FAILURE_RECEIPT]
    return event


def runtime_paths(state_path=None):
    configured = os.environ.get("ATELES_DEDUPE_JOURNAL_PATH", "").strip()
    if not configured:
        raise Unknown("journal_path_missing")
    path = Path(configured)
    if not path.is_absolute() or not path.parent.is_dir():
        raise Unknown("journal_directory_missing")
    if state_path is not None and not state_path.parent.is_dir():
        raise Unknown("state_directory_missing")
    return path


def change_inventory(path, action, selection, now):
    """Checkpoint an explicit human inventory edit without asserting recovery."""
    app, mount, *instances = selection
    if not valid_instances(instances) or not instances:
        raise Unknown("inventory_action_invalid")
    state = read_state(path, now)
    value = state["volumes"].get(target_key({"app": app, "mount": mount}))
    if value is None:
        raise Unknown("inventory_target_unknown")
    if action == "retire":
        if (
            value["seen_instances"] is None
            or instances[0] not in value["seen_instances"]
        ):
            raise Unknown("inventory_instance_unknown")
        value["seen_instances"].remove(instances[0])
    else:
        if value["seen_instances"] is not None:
            raise Unknown("inventory_already_known")
        value["seen_instances"] = sorted(instances)
    add_event(
        state,
        "Store disk watch inventory "
        + action
        + ": "
        + json.dumps(
            {"target": value["target"], "instances": instances}, sort_keys=True
        )
        + ". Human assessment recorded; usage remains UNKNOWN until a complete read.",
        "warning",
    )
    held_view.atomic_write(path, json.dumps(state, sort_keys=True, allow_nan=False))
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        type=Path,
        required=True,
        help="Private state file on persistent storage",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--retire-instance", nargs=3, metavar=("APP", "MOUNT", "INSTANCE")
    )
    actions.add_argument(
        "--confirm-instances", nargs="+", metavar="APP_MOUNT_INSTANCES"
    )
    args = parser.parse_args(argv)
    try:
        runtime_paths(args.state)
        notifications = Notifications()
        with held_view.file_lock(
            args.state.with_suffix(".lock"), blocking=False, fail_open=False
        ) as locked:
            if not locked:
                raise Unknown("state_lock_unavailable")
            import httpx

            if args.retire_instance or args.confirm_instances:
                selection = args.retire_instance or args.confirm_instances
                if len(selection) < 3:
                    raise Unknown("inventory_action_invalid")
                state = change_inventory(
                    args.state,
                    "retire" if args.retire_instance else "confirm",
                    selection,
                    time.time(),
                )
                drain(
                    state,
                    lambda: held_view.atomic_write(
                        args.state, json.dumps(state, sort_keys=True, allow_nan=False)
                    ),
                    request_json,
                    notifications,
                )
                print(
                    json.dumps(
                        {
                            "status": "unknown",
                            "inventory": "updated",
                            "event": state["events"][-1],
                        }
                    )
                )
                return 1

            with httpx.Client(trust_env=False) as client:

                def read_usage(target, now):
                    return Metrics(os.environ, client).read(target, now)

                report = run_once(args.state, request_json, read_usage, notifications)
                episode = failure_episode(request_json, notifications, recovered=True)
                if episode:
                    report["undelivered"] += episode["delivery"] != "delivered"
                    report["unrecorded"] += not episode["recorded"]
        print(json.dumps(report, sort_keys=True))
        return int(
            report["configuration"] != "ok"
            or any(
                v["status"] != "healthy" or not v["complete"] for v in report["volumes"]
            )
            or report["undelivered"] > 0
            or report["unrecorded"] > 0
        )
    except Exception:
        # Never replace damaged watch state. Persist episode identity before
        # external effects in the existing receipt journal, or report locally.
        try:
            runtime_paths(args.state)
            notifications = Notifications()
            with held_view.file_lock(
                args.state.with_suffix(".lock"), blocking=False, fail_open=False
            ) as locked:
                require(locked)
                event = failure_episode(request_json, notifications)
        except Exception:
            event = {"delivery": "unknown", "recorded": False}
        print(
            json.dumps(
                {
                    "status": "unknown",
                    "failure": "state_or_job_failure",
                    "delivery": event["delivery"],
                    "recorded": event["recorded"],
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

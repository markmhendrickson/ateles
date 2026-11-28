#!/usr/bin/env python3
"""One bounded volume-usage poll; no model calls or scheduling side effects.

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
                not isinstance(mount, dict)
                or set(mount) != {"volume", "mount"}
                or any(
                    not isinstance(v, str)
                    or not v
                    or len(v) > 200
                    or any(ord(c) < 32 for c in v)
                    for v in mount.values()
                )
                or not mount["mount"].startswith("/")
            ):
                raise Unknown("configuration_mounts_invalid")
            target = {"app": snap["fly_app"], **mount}
            if target in targets:
                raise Unknown("configuration_duplicate_volume")
            targets.append(target)
    if len(targets) > MAX_TARGETS:
        raise Unknown("configuration_too_large")
    return targets


class Metrics:
    """All provider metric/label assumptions are supplied in one mapping."""

    def __init__(self, environ, client):
        self.client = client
        self.url = environ.get("STORE_WATCH_METRICS_URL", "")
        url = urlsplit(self.url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not url.path.endswith("/api/v1/query")
        ):
            raise Unknown("metrics_endpoint_invalid")
        self.token = environ.get(TOKEN_ENV, "").strip()
        # Require the operator-created credential; never use an ambient Fly token.
        if not self.token.startswith("FlyV1 ") or any(c in self.token for c in "\r\n"):
            raise Unknown("metrics_token_missing_or_invalid")
        self.names = json.loads(environ.get("STORE_WATCH_METRICS", "null"))
        if not isinstance(self.names, dict) or set(self.names) != {
            "used",
            "size",
            "app_label",
            "volume_label",
            "mount_label",
        }:
            raise Unknown("metrics_mapping_missing")
        for key, value in self.names.items():
            pattern = (
                r"[a-zA-Z_:][a-zA-Z0-9_:]*"
                if key in ("used", "size")
                else r"[a-zA-Z_][a-zA-Z0-9_]*"
            )
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise Unknown("metrics_mapping_invalid")
        if (
            self.names["used"] == self.names["size"]
            or len(
                {self.names[k] for k in ("app_label", "volume_label", "mount_label")}
            )
            != 3
        ):
            raise Unknown("metrics_mapping_ambiguous")

    def sample(self, query, labels, now):
        response = self.client.get(
            self.url,
            params={"query": query, "time": now},
            headers={"Authorization": self.token},
            timeout=15,
            follow_redirects=False,
        )
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
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
        ):
            raise Unknown("metrics_empty_or_ambiguous")
        row = rows[0]
        if not isinstance(row.get("metric"), dict) or any(
            row["metric"].get(k) != v for k, v in labels.items()
        ):
            raise Unknown("metrics_wrong_volume")
        pair = row.get("value")
        if not isinstance(pair, list) or len(pair) != 2:
            raise Unknown("metrics_invalid_sample")
        stamp, value = map(finite, pair)
        if not -30 <= now - stamp <= SAMPLE_MAX_AGE:
            raise Unknown("metrics_stale")
        return value

    def read(self, target, now):
        labels = {
            self.names[k + "_label"]: target[k] for k in ("app", "volume", "mount")
        }
        selector = ",".join(k + "=" + json.dumps(v) for k, v in sorted(labels.items()))
        values, stamps = {}, []
        for kind in ("used", "size"):
            query = self.names[kind] + "{" + selector + "}"
            values[kind] = self.sample(query, labels, now)
            # Instant-query timestamps can be evaluation time. Check the source
            # sample's timestamp explicitly, rather than renewing stale gauges.
            stamp = self.sample(f"timestamp({query})", labels, now)
            if not -30 <= now - stamp <= SAMPLE_MAX_AGE:
                raise Unknown("metrics_stale")
            stamps.append(stamp)
        if not 0 <= values["used"] <= values["size"] or values["size"] <= 0:
            raise Unknown("metrics_invalid_capacity")
        return 100 * values["used"] / values["size"], min(stamps)


class Notifications:
    """Use the existing transport and its durable, retryable send receipts."""

    def __init__(self):
        from lib.notify import Notifier

        self.notifier = Notifier()

    def clear_failure(self):
        """Re-arm the existing receipt after state access has recovered."""
        self.notifier.clear_dedupe("store-watch:state-failure")

    def __call__(self, event):
        from lib.notify import Priority

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
    return {"version": 1, "volumes": {}, "configuration_failed": False, "events": []}


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
            and state["version"] == 1
            and type(state["configuration_failed"]) is bool
        )
        require(
            isinstance(state["volumes"], dict) and isinstance(state["events"], list)
        )
        for key, value in state["volumes"].items():
            require(key == target_key(value["target"]))
            require(set(value["target"]) == {"app", "volume", "mount"})
            require(all(isinstance(v, str) and v for v in value["target"].values()))
            require(value["level"] in LEVELS and type(value["stale"]) is bool)
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
    return state


def add_event(state, message, severity="critical", identifier=None):
    state["events"].append(
        {
            "id": identifier or "store-watch:" + uuid.uuid4().hex,
            "message": message,
            "severity": severity,
            "delivery": "undelivered",
            "task_id": None,
            "recorded": False,
        }
    )


def record_event(event, request):
    """Read back every field; a 2xx or a silently dropped field is not success."""
    notes = json.dumps(
        {"delivery": event["delivery"], "event": event["id"]}, sort_keys=True
    )
    if not event["task_id"]:
        # The create payload is immutable, including across a lost response.
        # Delivery is updated separately, without resetting human-owned status.
        wanted = {
            "title": "Store disk watch " + event["id"],
            "description": event["message"],
            "status": "blocked",
            "assigned_to": "operator",
            "blocked_reason": "Human assessment required; observation only, no automatic repair.",
            "priority": "p0" if event["severity"] == "critical" else "p1",
            "tags": ["store-watch"],
            "notes": json.dumps(
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
        if not actual or any(
            actual.get(k) != v for k, v in wanted.items() if k != "notes"
        ):
            raise Unknown("task_readback_mismatch")
        event["task_id"] = identifier
    result = request(
        "POST",
        "/correct",
        {
            "entity_id": event["task_id"],
            "entity_type": "task",
            "field": "notes",
            "value": notes,
            "idempotency_key": event["id"] + ":" + event["delivery"],
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
            add_event(state, "Store disk watch configuration recovered.", "warning")
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
            },
        )
        label = json.dumps(target, sort_keys=True)
        try:
            if not configured:
                raise Unknown("configuration_unknown")
            percent, stamp = read_usage(target, now)
            percent, stamp = finite(percent), finite(stamp)
            if not 0 <= percent <= 100 or not -30 <= now - stamp <= SAMPLE_MAX_AGE:
                raise Unknown("usage_unknown")
        except Exception:
            results.append({"target": target, "status": "unknown"})
        else:
            value["last_success"] = max(value["last_success"] or 0, stamp)
            current = level(percent)
            if value["stale"]:
                add_event(
                    state, "Store disk watch reads recovered: " + label, "warning"
                )
                value["stale"] = False
            if current != value["level"]:
                add_event(
                    state,
                    f"Volume {current}: {percent:.1f}% used. "
                    + label
                    + ". Assess capacity; this job changes no volume.",
                    "critical" if current == "critical" else "warning",
                )
                value["level"] = current
            results.append({"target": target, "status": current, "percent": percent})
        since = (
            value["last_success"]
            if value["last_success"] is not None
            else value["first_seen"]
        )
        if now - since >= STALE_SECONDS and not value["stale"]:
            add_event(
                state,
                "Store disk watch critical: no successful volume read for one hour; usage UNKNOWN. "
                + label,
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        type=Path,
        required=True,
        help="Private state file on persistent storage",
    )
    args = parser.parse_args(argv)
    notifications = Notifications()
    try:
        with held_view.file_lock(
            args.state.with_suffix(".lock"), blocking=False, fail_open=False
        ) as locked:
            if not locked:
                raise Unknown("state_lock_unavailable")
            import httpx

            with httpx.Client() as client:

                def read_usage(target, now):
                    return Metrics(os.environ, client).read(target, now)

                report = run_once(args.state, request_json, read_usage, notifications)
                clear_failure = getattr(notifications, "clear_failure", None)
                if clear_failure:
                    clear_failure()
        print(json.dumps(report, sort_keys=True))
        return int(
            report["configuration"] != "ok"
            or any(v["status"] != "healthy" for v in report["volumes"])
            or report["undelivered"] > 0
            or report["unrecorded"] > 0
        )
    except Exception:
        # Preserve damaged state. The existing notification receipt owns dedupe
        # for this emergency; never replace an unreadable file with empty state.
        emergency = new_state()
        add_event(
            emergency,
            "Store disk watch state is UNKNOWN; monitoring failed. Preserve the state and inspect the job before trusting any healthy result.",
            identifier="store-watch:state-failure",
        )
        drain(emergency, lambda: None, request_json, notifications)
        print(
            json.dumps(
                {
                    "status": "unknown",
                    "failure": "state_or_job_failure",
                    "delivery": emergency["events"][0]["delivery"],
                    "recorded": emergency["events"][0]["recorded"],
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Synthetic volume polls and real notifier receipts; no external services."""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from execution.scripts import store_disk_watch as watch

NOW = 1_800_000_000
TARGET = {"app": "synthetic", "volume": "synthetic", "mount": "/data"}
ENV = {"STORE_WATCH_CONFIG_IDS": '["ent_synthetic"]'}
NAMES = {
    "used": "synthetic_used",
    "size": "synthetic_size",
    "app_label": "app",
    "volume_label": "volume",
    "mount_label": "mount",
}


@pytest.fixture(autouse=True)
def isolated_notifications(tmp_path, monkeypatch):
    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "receipts.json"))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "held.json"))
    monkeypatch.setenv("ATELES_NOTIFY_SINK_DIR", str(tmp_path / "sink"))
    monkeypatch.setenv("ATELES_NOTIFY_NEOTOMA_SINK", "off")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")


class Records:
    def __init__(self):
        self.config = {
            "entity_type": "deployment_configuration",
            "snapshot": {
                "fly_app": TARGET["app"],
                "gotchas": [
                    watch.CONFIG_PREFIX
                    + json.dumps([{k: TARGET[k] for k in ("volume", "mount")}])
                ],
            },
        }
        self.tasks = {}
        self.calls = []
        self.fail_config = False
        self.fail_store = False
        self.drop_field = None
        self.replays = set()

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path == "/entities/ent_synthetic":
            if self.fail_config:
                raise OSError("synthetic failure")
            return copy.deepcopy(self.config)
        if self.fail_store:
            raise OSError("synthetic store failure")
        if path == "/store":
            if body["idempotency_key"] in self.replays:
                return {"success": True}
            self.replays.add(body["idempotency_key"])
            entity = copy.deepcopy(body["entities"][0])
            identifier = "ent_task" + str(len(self.tasks))
            if self.drop_field:
                entity.pop(self.drop_field, None)
            self.tasks[identifier] = entity
            return {"entities": [{"entity_id": identifier}]}
        if path == "/entities/query":
            title = body["snapshot_filters"]["title"]["value"]
            rows = [
                {"entity_id": key}
                for key, row in self.tasks.items()
                if row["title"] == title
            ]
            return {"entities": rows, "total": len(rows)}
        if path == "/correct":
            self.tasks[body["entity_id"]][body["field"]] = body["value"]
            return {"success": True}
        if method == "GET":
            return {"snapshot": copy.deepcopy(self.tasks[path.rsplit("/", 1)[1]])}
        raise AssertionError((method, path, body))


@pytest.fixture
def rig(tmp_path):
    records = Records()
    sent = []
    path = tmp_path / "state.json"

    def poll(percent=60, now=NOW, notify=None, read=None, environ=None):
        def send(event):
            sent.append(copy.deepcopy(event))
            return True

        return watch.run_once(
            path,
            records,
            read or (lambda target, clock: (percent, clock)),
            notify or send,
            ENV if environ is None else environ,
            now,
        )

    return SimpleNamespace(records=records, path=path, sent=sent, poll=poll)


@pytest.mark.parametrize(
    "percent,expected",
    [
        (0, "healthy"),
        (69.999, "healthy"),
        (70, "warning"),
        (84.999, "warning"),
        (85, "critical"),
        (100, "critical"),
    ],
)
def test_threshold_effect_creates_human_task_and_notice(rig, percent, expected):
    result = rig.poll(percent)
    assert result["volumes"][0]["status"] == expected
    assert len(rig.sent) == len(rig.records.tasks) == (expected != "healthy")
    for task in rig.records.tasks.values():
        assert task["status"] == "blocked" and task["assigned_to"] == "operator"
        assert json.loads(task["notes"])["delivery"] == "delivered"


def test_dedup_escalation_recovery_and_restart(rig):
    rig.poll(70)
    rig.poll(80, NOW + 60)
    assert len(rig.sent) == 1
    # Every poll re-reads state, as a fresh scheduled process does.
    rig.poll(85, NOW + 120)
    rig.poll(90, NOW + 180)
    assert len(rig.sent) == 2
    rig.poll(30, NOW + 240)
    assert len(rig.sent) == 3
    assert "healthy" in rig.sent[-1]["message"]
    rig.poll(60, NOW + 300)
    assert len(rig.sent) == 3
    rig.poll(70, NOW + 360)
    assert len(rig.sent) == 4
    assert len({e["id"] for e in rig.sent}) == 4


def no_read(*args):
    raise watch.Unknown("synthetic_unknown")


def test_unknown_for_one_hour_alerts_once_and_read_recovery_is_recorded(rig):
    assert rig.poll(read=no_read)["volumes"][0]["status"] == "unknown"
    rig.poll(now=NOW + 3599, read=no_read)
    assert not rig.sent
    report = rig.poll(now=NOW + 3600, read=no_read)
    assert report["volumes"][0]["status"] == "unknown"
    assert len(rig.sent) == 1 and rig.sent[0]["severity"] == "critical"
    rig.poll(now=NOW + 7200, read=no_read)
    assert len(rig.sent) == 1
    rig.poll(now=NOW + 7201)
    assert "reads recovered" in rig.sent[-1]["message"]


def test_last_success_survives_restart_and_unknown_never_clears_a_disk_alert(rig):
    rig.poll(90)
    rig.poll(now=NOW + 3000, read=no_read)
    assert len(rig.sent) == 1
    rig.poll(now=NOW + 3600, read=no_read)
    state = watch.read_state(rig.path, NOW + 3600)
    assert next(iter(state["volumes"].values()))["level"] == "critical"
    assert len(rig.sent) == 2


def test_unconfirmed_send_is_undelivered_and_retried_as_same_task(rig):
    report = rig.poll(90, notify=lambda event: False)
    assert report["undelivered"] == 1
    task = next(iter(rig.records.tasks.values()))
    assert json.loads(task["notes"])["delivery"] == "undelivered"
    rig.poll(90, NOW + 60)
    assert len(rig.sent) == 1 and len(rig.records.tasks) == 1
    assert json.loads(task["notes"])["delivery"] == "delivered"


def test_truthy_uncertain_send_result_never_means_delivered(rig):
    report = rig.poll(90, notify=lambda event: "delivered_receipt_unknown")
    assert report["undelivered"] == 1


def test_task_failure_does_not_prevent_person_alert_and_retries_recording(rig):
    rig.records.fail_store = True
    report = rig.poll(90)
    assert report["unrecorded"] == 1 and len(rig.sent) == 1
    rig.records.fail_store = False
    report = rig.poll(90, NOW + 60)
    assert report["unrecorded"] == 0 and len(rig.sent) == 1
    assert len(rig.records.tasks) == 1


def test_silent_task_field_drop_is_not_success(rig):
    rig.records.drop_field = "status"
    report = rig.poll(90)
    assert report["unrecorded"] == 1


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"STORE_WATCH_CONFIG_IDS": "[]"},
        {"STORE_WATCH_CONFIG_IDS": "["},
        {"STORE_WATCH_CONFIG_IDS": '["ent_synthetic", "ent_synthetic"]'},
    ],
)
def test_missing_or_malformed_configuration_is_critical_not_empty_healthy(
    rig, environment
):
    report = rig.poll(environ=environment)
    assert report["configuration"] == "unknown" and len(rig.sent) == 1
    assert rig.sent[0]["severity"] == "critical"
    rig.poll(now=NOW + 60, environ=environment)
    assert len(rig.sent) == 1


def test_unreadable_configuration_uses_known_targets_for_stale_watch(rig):
    rig.poll()
    rig.records.fail_config = True
    report = rig.poll(now=NOW + 3600)
    assert report["configuration"] == "unknown"
    assert report["volumes"][0]["status"] == "unknown"
    assert len(rig.sent) == 2
    rig.records.fail_config = False
    rig.poll(now=NOW + 3660)
    assert any("configuration recovered" in e["message"] for e in rig.sent)


def test_partial_configuration_does_not_poll_only_the_readable_subset(rig):
    calls = []
    env = {"STORE_WATCH_CONFIG_IDS": '["ent_synthetic", "ent_unreadable"]'}
    result = rig.poll(environ=env, read=lambda *args: calls.append(args))
    assert result["configuration"] == "unknown" and calls == []


@pytest.mark.parametrize(
    "gotchas",
    [
        None,
        [],
        ["store-watch:[]"],
        ["store-watch:broken"],
        ["store-watch:[{}]"],
        ["store-watch:[]", "store-watch:[]"],
    ],
)
def test_bad_mount_list_fails_configuration(rig, gotchas):
    rig.records.config["snapshot"]["gotchas"] = gotchas
    assert rig.poll()["configuration"] == "unknown"


def test_state_write_failure_happens_before_task_or_transport(rig, monkeypatch):
    monkeypatch.setattr(
        watch.held_view, "atomic_write", lambda *args: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(OSError):
        rig.poll(90)
    assert not rig.sent and not rig.records.tasks


def test_crash_after_durable_event_replays_same_event(rig, monkeypatch):
    original = watch.drain
    monkeypatch.setattr(
        watch, "drain", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        rig.poll(90)
    event_id = json.loads(rig.path.read_text())["events"][0]["id"]
    monkeypatch.setattr(watch, "drain", original)
    rig.poll(90, NOW + 60)
    assert len(rig.sent) == 1 and rig.sent[0]["id"] == event_id


@pytest.mark.parametrize("payload", ["{", "null", "{}", '{"version": 1}'])
def test_malformed_state_is_preserved_not_reset(rig, payload):
    rig.path.write_text(payload)
    with pytest.raises((watch.Unknown, ValueError)):
        rig.poll(90)
    assert rig.path.read_text() == payload and not rig.sent


def test_unreadable_state_read_itself_fails_closed(rig, monkeypatch):
    rig.poll()
    monkeypatch.setattr(
        watch.held_view,
        "read_text_nofollow",
        lambda *args: (_ for _ in ()).throw(PermissionError()),
    )
    with pytest.raises(PermissionError):
        rig.poll(90)
    assert not rig.sent


def metric_reply(query, value="80", timestamp=NOW, labels=None):
    if query.startswith("timestamp("):
        value = str(timestamp)
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": labels or TARGET, "value": [NOW, value]}],
        },
    }


def metrics(handler):
    env = {
        "STORE_WATCH_METRICS_URL": "https://metrics.invalid/api/v1/query",
        watch.TOKEN_ENV: "FlyV1 synthetic",
        "STORE_WATCH_METRICS": json.dumps(NAMES),
    }
    return watch.Metrics(env, httpx.Client(transport=httpx.MockTransport(handler)))


def test_metrics_get_uses_named_token_exact_selectors_and_source_freshness():
    seen = []

    def handler(request):
        seen.append(request)
        query = request.url.params["query"]
        return httpx.Response(
            200,
            json=metric_reply(
                query, "100" if query.startswith("synthetic_size") else "85"
            ),
        )

    percent, stamp = metrics(handler).read(TARGET, NOW)
    assert percent == 85 and stamp == NOW and len(seen) == 4
    assert all(
        r.method == "GET" and r.headers["Authorization"] == "FlyV1 synthetic"
        for r in seen
    )
    assert (
        'app="synthetic",mount="/data",volume="synthetic"'
        in seen[0].url.params["query"]
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(status="error"),
        lambda p: p.update(warnings=["partial"]),
        lambda p: p.update(isPartial=True),
        lambda p: p["data"].update(isPartial=True),
        lambda p: p["data"].update(result=[]),
        lambda p: p["data"].update(resultType="matrix"),
        lambda p: p["data"]["result"].append(copy.deepcopy(p["data"]["result"][0])),
        lambda p: p["data"]["result"][0]["metric"].update(volume="wrong"),
        lambda p: p["data"]["result"][0].update(value=[NOW - 301, "80"]),
        lambda p: p["data"]["result"][0].update(value=[NOW + 31, "80"]),
        lambda p: p["data"]["result"][0].update(value=[NOW, "NaN"]),
        lambda p: p["data"]["result"][0].update(value=[NOW, "Infinity"]),
        lambda p: p["data"]["result"][0].update(value=[]),
    ],
)
def test_empty_partial_wrong_and_unparsable_metrics_are_unknown(change):
    def handler(request):
        payload = metric_reply(request.url.params["query"])
        change(payload)
        return httpx.Response(200, json=payload)

    with pytest.raises(watch.Unknown):
        metrics(handler).read(TARGET, NOW)


def test_fresh_query_timestamp_does_not_hide_old_source_sample():
    with pytest.raises(watch.Unknown, match="metrics_stale"):
        metrics(
            lambda request: httpx.Response(
                200,
                json=metric_reply(request.url.params["query"], timestamp=NOW - 3600),
            )
        ).read(TARGET, NOW)


@pytest.mark.parametrize("used,size", [(101, 100), (-1, 100), (0, 0)])
def test_invalid_capacity_is_unknown(used, size):
    def handler(request):
        query = request.url.params["query"]
        return httpx.Response(
            200,
            json=metric_reply(
                query, str(size if query.startswith("synthetic_size") else used)
            ),
        )

    with pytest.raises(watch.Unknown):
        metrics(handler).read(TARGET, NOW)


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_http_failures_and_redirects_are_unknown(status):
    with pytest.raises(watch.Unknown):
        metrics(
            lambda request: httpx.Response(
                status, headers={"Location": "https://other.invalid/"}
            )
        ).read(TARGET, NOW)


def test_real_notifier_receipt_survives_crash_without_resending(tmp_path, monkeypatch):
    from lib.notify import Notifier

    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "receipts.json"))
    monkeypatch.setenv("ATELES_DIGEST_QUEUE_PATH", str(tmp_path / "held.json"))
    monkeypatch.setenv("ATELES_NOTIFY_NEOTOMA_SINK", "off")
    calls = []
    adapter = watch.Notifications()
    adapter.notifier = Notifier()
    adapter.notifier._deliver = lambda *args, **kwargs: calls.append(args) or True
    event = {
        "id": "store-watch:synthetic",
        "message": "Synthetic capacity needs assessment",
        "severity": "critical",
    }
    assert adapter(event) is True
    assert adapter(event) is True
    assert len(calls) == 1


def test_notification_adapter_forbids_email_and_uncertain_success(
    monkeypatch, tmp_path
):
    from lib.notify import Notifier

    monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(tmp_path / "receipts.json"))
    calls = []
    monkeypatch.setattr(
        Notifier,
        "send",
        lambda self, *args, **kw: calls.append(kw) or "delivered_receipt_unknown",
    )
    assert (
        watch.Notifications()(
            {"id": "synthetic", "message": "synthetic", "severity": "critical"}
        )
        is False
    )
    assert calls[0]["email_eligible"] is False and calls[0]["retryable"] is True


def test_tasks_are_not_routable_or_retried_by_dispatcher(rig):
    directory = Path(watch.ROOT) / "execution/daemons/apis"
    sys.path.insert(0, str(directory))
    import routing
    from lib.daemon_runtime.task_lifecycle import TaskStatus

    spec = importlib.util.spec_from_file_location(
        "store_watch_watchdog", directory / "task_watchdog.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    rig.poll(90)
    task = next(iter(rig.records.tasks.values()))
    assert (
        routing.resolve_skill(task["tags"], task["assigned_to"]) == routing.HUMAN_OWNED
    )
    assert task["status"] == TaskStatus.BLOCKED.value
    assert (
        module.TaskWatchdog().classify("synthetic", task["status"], 100000)
        == module.WatchdogAction.NONE
    )


def test_lost_task_create_response_is_reconciled_without_duplicate(rig):
    original = rig.records.__call__
    lost = [False]

    def request(method, path, body=None):
        result = original(method, path, body)
        if path == "/store" and not lost[0]:
            lost[0] = True
            raise OSError("synthetic response lost after write")
        return result

    watch.run_once(
        rig.path, request, lambda *args: (90, NOW), lambda event: True, ENV, NOW
    )
    report = watch.run_once(
        rig.path, request, lambda *args: (90, NOW), lambda event: True, ENV, NOW + 60
    )
    assert report["unrecorded"] == 0 and len(rig.records.tasks) == 1


def test_delivery_notes_do_not_reopen_a_human_closed_task(rig):
    rig.poll(90, notify=lambda event: False)
    task = next(iter(rig.records.tasks.values()))
    task["status"] = "done"
    rig.poll(90, NOW + 60)
    assert task["status"] == "done"


def test_ignored_delivery_note_correction_remains_unrecorded(rig):
    original = rig.records.__call__

    def request(method, path, body=None):
        if path == "/correct" and '"delivered"' in body["value"]:
            return {"success": True}
        return original(method, path, body)

    result = watch.run_once(
        rig.path, request, lambda *args: (90, NOW), lambda event: True, ENV, NOW
    )
    assert result["unrecorded"] == 1


def test_reconciler_skips_blocked_alert_task(rig):
    sys.path.insert(0, str(Path(watch.ROOT) / "execution/daemons/apis"))
    import task_reconciler

    rig.poll(90)
    task = next(iter(rig.records.tasks.values()))
    assert (
        task_reconciler.TaskReconciler().should_dispatch(
            "synthetic", task["status"], 100000
        )
        == task_reconciler.SkipReason.NOT_PENDING
    )


def test_main_exercises_configuration_metrics_task_and_notice_path(
    rig, monkeypatch, capsys
):
    client_class = httpx.Client

    def handler(request):
        query = request.url.params["query"]
        return httpx.Response(
            200,
            json=metric_reply(
                query, "100" if query.startswith("synthetic_size") else "90"
            ),
        )

    monkeypatch.setattr(
        httpx, "Client", lambda: client_class(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(watch.time, "time", lambda: NOW)
    monkeypatch.setattr(
        watch,
        "Notifications",
        lambda: lambda event: rig.sent.append(copy.deepcopy(event)) or True,
    )
    for name, value in {
        **ENV,
        "STORE_WATCH_METRICS_URL": "https://metrics.invalid/api/v1/query",
        watch.TOKEN_ENV: "FlyV1 synthetic",
        "STORE_WATCH_METRICS": json.dumps(NAMES),
    }.items():
        monkeypatch.setenv(name, value)
    assert watch.main(["--state", str(rig.path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["volumes"][0]["status"] == "critical"
    assert result["undelivered"] == 0 and result["unrecorded"] == 0
    assert len(rig.sent) == len(rig.records.tasks) == 1
    assert watch.main(["--state", str(rig.path)]) == 1
    assert len(rig.sent) == len(rig.records.tasks) == 1


def test_main_reports_state_read_failure_through_existing_route_and_task(
    rig, monkeypatch, capsys
):
    rig.path.write_text("damaged")
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(
        watch,
        "Notifications",
        lambda: lambda event: rig.sent.append(copy.deepcopy(event)) or False,
    )
    assert watch.main(["--state", str(rig.path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unknown" and result["delivery"] == "undelivered"
    assert result["recorded"] is True
    assert len(rig.sent) == len(rig.records.tasks) == 1
    assert rig.path.read_text() == "damaged"


def test_lock_failure_is_reported_without_polling_or_reset(rig, monkeypatch, capsys):
    from contextlib import contextmanager

    @contextmanager
    def unavailable(*args, **kwargs):
        yield False

    monkeypatch.setattr(watch.held_view, "file_lock", unavailable)
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(watch, "Notifications", lambda: lambda event: False)
    assert watch.main(["--state", str(rig.path)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
    assert not rig.path.exists()
    assert not any(
        path == "/entities/ent_synthetic" for _, path, _ in rig.records.calls
    )


def test_separate_state_failures_rearm_existing_notification_receipt(
    rig, monkeypatch, capsys
):
    from lib.notify import Notifier

    calls = []
    monkeypatch.setattr(
        Notifier, "_deliver", lambda self, text, **kwargs: calls.append(text) or True
    )
    adapter = watch.Notifications()
    monkeypatch.setattr(watch, "Notifications", lambda: adapter)
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setenv("STORE_WATCH_CONFIG_IDS", "[]")
    rig.path.write_text("damaged")
    assert watch.main(["--state", str(rig.path)]) == 1
    rig.path.write_text(json.dumps(watch.new_state()))
    assert watch.main(["--state", str(rig.path)]) == 1
    rig.path.write_text("damaged again")
    assert watch.main(["--state", str(rig.path)]) == 1
    assert sum("state is UNKNOWN" in message for message in calls) == 2
    capsys.readouterr()


def test_malformed_delivery_journal_is_not_treated_as_sent(tmp_path, monkeypatch):
    from lib.notify import Notifier

    adapter = watch.Notifications()
    monkeypatch.setattr(Notifier, "send", lambda *args, **kwargs: False)
    watch.held_view.journal_path().write_text(
        json.dumps(
            {
                "store-watch:synthetic": {
                    "state": "delivered",
                    "handler": "store-watch",
                    "notified_at": "2026-01-01T00:00:00Z",
                },
                "malformed": {},
            }
        )
    )
    assert (
        adapter(
            {
                "id": "store-watch:synthetic",
                "message": "synthetic",
                "severity": "critical",
            }
        )
        is False
    )

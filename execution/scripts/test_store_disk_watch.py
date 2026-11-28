"""Synthetic volume polls and real notifier receipts; no external services."""

import copy
import importlib.util
import json
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from execution.scripts import store_disk_watch as watch

NOW = 1_800_000_000
TARGET = {"app": "synthetic", "mount": "/data"}
ENV = {"STORE_WATCH_CONFIG_IDS": '["ent_synthetic"]'}
GAUGES = {
    "fly_instance_filesystem_blocks": 110,
    "fly_instance_filesystem_blocks_free": 20,
    "fly_instance_filesystem_blocks_avail": 10,
}


def reading(percent, stamp):
    return {
        "complete": True,
        "failure": None,
        "seen_instances": ["synthetic-a"],
        "instances": [{"instance": "synthetic-a", "percent": percent, "stamp": stamp}],
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
                "gotchas": [watch.CONFIG_PREFIX + json.dumps([TARGET["mount"]])],
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
            if body["idempotency_key"] not in self.replays:
                self.tasks[body["entity_id"]][body["field"]] = body["value"]
                self.replays.add(body["idempotency_key"])
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
            read or (lambda target, clock: reading(percent, clock)),
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
        assert (
            json.loads(
                task["notes"].split("store-watch-delivery: ")[1].splitlines()[0]
            )["delivery"]
            == "delivered"
        )


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
    assert (
        json.loads(task["notes"].split("store-watch-delivery: ")[1].splitlines()[0])[
            "delivery"
        ]
        == "undelivered"
    )
    rig.poll(90, NOW + 60)
    assert len(rig.sent) == 1 and len(rig.records.tasks) == 1
    assert (
        json.loads(task["notes"].split("store-watch-delivery: ")[1].splitlines()[0])[
            "delivery"
        ]
        == "delivered"
    )


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
    "field,replacement",
    [("status", "pending"), ("assigned_to", "synthetic"), ("priority", "p3")],
)
def test_initial_task_readback_verifies_human_routing_fields(rig, field, replacement):
    original = rig.records.__call__

    def request(method, path, body=None):
        result = original(method, path, body)
        if path == "/store":
            next(iter(rig.records.tasks.values()))[field] = replacement
        return result

    report = watch.run_once(
        rig.path, request, lambda *a: reading(90, NOW), lambda e: True, ENV, NOW
    )
    assert report["unrecorded"] == 1
    event = watch.read_state(rig.path, NOW)["events"][0]
    assert event["task_id"] is None and event["recorded"] is False


def test_identified_task_retry_preserves_human_routing_and_priority(rig):
    rig.poll(90, notify=lambda e: False)
    task = next(iter(rig.records.tasks.values()))
    task.update(status="done", assigned_to="synthetic", priority="p3")
    assert rig.poll(90, NOW + 60)["unrecorded"] == 0
    assert (task["status"], task["assigned_to"], task["priority"]) == (
        "done",
        "synthetic",
        "p3",
    )


@pytest.mark.parametrize(
    "problem,category",
    [
        ("auth", "metrics_auth_failure"),
        ("mount", "metrics_labels_mismatch"),
        ("clock", "metrics_clock_skew"),
    ],
)
def test_fixed_failure_category_reaches_result_and_one_hour_notice(
    rig, problem, category
):
    rig.poll(40)

    def handler(request):
        payload = metric_reply(request.url.params["query"])
        if problem == "auth":
            return httpx.Response(401, text="untrusted provider text")
        for row in payload["data"]["result"]:
            if problem == "mount":
                row["metric"]["mount"] = "wrong"
            else:
                row["value"][0] = NOW + 3631
        return httpx.Response(200, json=payload)

    for now in (NOW + 60, NOW + 3600):
        result = rig.poll(now=now, read=metrics(handler).read)["volumes"][0]
        assert result["status"] == "unknown" and result["failure"] == category
    assert "failure=" + category in rig.sent[-1]["message"]
    assert watch.RUNBOOK in rig.sent[-1]["message"]
    assert watch.RUNBOOK in next(iter(rig.records.tasks.values()))["description"]
    assert "untrusted provider text" not in json.dumps(rig.sent)


def test_unrecognized_exception_text_is_never_exported(rig):
    def fail(*args):
        raise watch.Unknown("untrusted provider text")

    result = rig.poll(read=fail)["volumes"][0]
    assert result["failure"] == "metrics_read_failure"


@pytest.mark.parametrize(
    "url",
    [
        "https://metrics.invalid/prometheus/synthetic/api/v1/query",
        "https://api.fly.io.attacker.invalid/prometheus/synthetic/api/v1/query",
        "https://api.fly.io:443/prometheus/synthetic/api/v1/query",
        "https://api.fly.io/wrong/api/v1/query",
        "http://api.fly.io/prometheus/synthetic/api/v1/query",
        "https://user@api.fly.io/prometheus/synthetic/api/v1/query",
        "https://api.fly.io/prometheus/synthetic/api/v1/query?query=x",
        "https://api.fly.io/prometheus/synthetic/api/v1/query#fragment",
    ],
)
def test_metrics_endpoint_rejects_unapproved_host_or_shape(url):
    with pytest.raises(watch.Unknown, match="metrics_endpoint_invalid"):
        watch.Metrics(
            {"STORE_WATCH_METRICS_URL": url, watch.TOKEN_ENV: "FlyV1 synthetic"}, None
        )


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "   ",
        "synthetic",
        "Bearer synthetic",
        "FlyV1 ",
        "FlyV1 synthetic\nother",
        "FlyV1 synthetic\n",
        "\nFlyV1 synthetic",
        "FlyV1 synthetic\r",
    ],
)
def test_invalid_dedicated_metrics_token_never_uses_ambient_credentials(
    rig, monkeypatch, token
):
    env = {
        **ENV,
        "STORE_WATCH_METRICS_URL": "https://api.fly.io/prometheus/synthetic/api/v1/query",
        **dict.fromkeys(
            (
                "FLY_API_TOKEN",
                "FLY_ACCESS_TOKEN",
                "FLY_TOKEN",
                "GH_TOKEN",
                "GITHUB_TOKEN",
            ),
            "FlyV1 synthetic-ambient",
        ),
    }
    if token is not None:
        env[watch.TOKEN_ENV] = token
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if token is None:
        monkeypatch.delenv(watch.TOKEN_ENV, raising=False)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=metric_reply(request.url.params["query"]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(watch.Unknown, match="^metrics_token_missing_or_invalid$"):
            watch.Metrics(env, client)

        def read_usage(target, now):
            return watch.Metrics(env, client).read(target, now)

        result = rig.poll(read=read_usage, environ=env)["volumes"][0]
    assert result["failure"] == "metrics_token_missing_or_invalid"
    assert result["status"] == "unknown" and not result["complete"]
    assert calls == []


@pytest.mark.parametrize("problem", ["auth", "transport", "redirect"])
@pytest.mark.parametrize("state_write_fails", [False, True])
def test_metrics_canary_never_escapes_to_state_effects_journal_or_stdout(
    rig, monkeypatch, capsys, problem, state_write_fails
):
    from lib.notify import Notifier

    rig.poll(40)
    canary = "FlyV1 synthetic-canary-" + uuid.uuid4().hex
    for name, value in {
        **ENV,
        "STORE_WATCH_METRICS_URL": "https://api.fly.io/prometheus/synthetic/api/v1/query",
        watch.TOKEN_ENV: canary,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(watch.time, "time", lambda: NOW + 3600)
    monkeypatch.setattr(
        Notifier,
        "_deliver",
        lambda self, message, **kwargs: rig.sent.append(message) or True,
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == canary
        if problem == "transport":
            raise httpx.RequestError(canary, request=request)
        return httpx.Response(
            401 if problem == "auth" else 307,
            text=canary,
            headers={"Location": "/unapproved"},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    atomic_write = watch.held_view.atomic_write

    def save(path, text):
        if state_write_fails and path == rig.path:
            raise OSError(canary)
        return atomic_write(path, text)

    monkeypatch.setattr(watch.held_view, "atomic_write", save)
    assert watch.main(["--state", str(rig.path)]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert len(calls) == 6
    assert rig.sent and rig.records.tasks
    assert any(path == "/store" for _, path, _ in rig.records.calls)
    assert any(path == "/correct" for _, path, _ in rig.records.calls)
    journal = watch.held_view.journal_path().read_text()
    if state_write_fails:
        assert report["failure"] == "state_or_job_failure"
        assert json.loads(journal)[watch.FAILURE_RECEIPT]["watch_event"]["recorded"]
    else:
        category = {
            "auth": "metrics_auth_failure",
            "transport": "metrics_request_failure",
            "redirect": "metrics_http_failure",
        }[problem]
        assert report["volumes"][0]["failure"] == category
        assert any("failure=" + category in message for message in rig.sent)
    for surface in (
        rig.path.read_text(),
        json.dumps(rig.records.calls),
        json.dumps(rig.records.tasks),
        json.dumps(rig.sent),
        journal,
        captured.out,
        captured.err,
    ):
        assert canary not in surface


@pytest.mark.parametrize(
    "destination",
    ["https://redirect.invalid/metrics", "https://api.fly.io/unapproved"],
)
def test_main_metrics_client_ignores_environment_and_refuses_redirects(
    rig, monkeypatch, capsys, destination
):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", str(rig.path.parent / "absent-cert"))
    monkeypatch.setenv(
        "STORE_WATCH_METRICS_URL",
        "https://api.fly.io/prometheus/synthetic/api/v1/query",
    )
    monkeypatch.setenv(watch.TOKEN_ENV, "FlyV1 synthetic")
    monkeypatch.setenv("STORE_WATCH_CONFIG_IDS", ENV["STORE_WATCH_CONFIG_IDS"])
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(watch, "Notifications", lambda: lambda e: True)
    monkeypatch.setattr(watch.time, "time", lambda: NOW)
    real = httpx.Client
    clients, requests = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": destination})

    def client(**kwargs):
        client = real(
            transport=httpx.MockTransport(handler), follow_redirects=True, **kwargs
        )
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", client)
    assert watch.main(["--state", str(rig.path)]) == 1
    assert len(clients) == 1 and clients[0].trust_env is False
    assert clients[0].follow_redirects is True
    assert len(requests) == 6
    assert all(
        str(request.url.copy_with(query=None))
        == "https://api.fly.io/prometheus/synthetic/api/v1/query"
        and request.headers["Authorization"] == "FlyV1 synthetic"
        for request in requests
    )
    assert (
        json.loads(capsys.readouterr().out)["volumes"][0]["failure"]
        == "metrics_http_failure"
    )


@pytest.mark.parametrize(
    "problem", ["unset", "blank", "journal_parent", "state_parent"]
)
def test_main_missing_runtime_paths_never_create_directories_or_effects(
    rig, monkeypatch, capsys, problem
):
    state_path = rig.path
    missing = rig.path.parent / "absent"
    if problem == "unset":
        monkeypatch.delenv("ATELES_DEDUPE_JOURNAL_PATH")
    elif problem == "blank":
        monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", "  ")
    elif problem == "journal_parent":
        monkeypatch.setenv("ATELES_DEDUPE_JOURNAL_PATH", str(missing / "receipts.json"))
    else:
        state_path = missing / "state.json"
    monkeypatch.setattr(
        watch, "request_json", lambda *a: pytest.fail("no external effects")
    )
    monkeypatch.setattr(
        watch, "Notifications", lambda: pytest.fail("no notifier fallback")
    )
    assert watch.main(["--state", str(state_path)]) == 1
    assert not missing.exists() and not state_path.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"


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


def metric_reply(query, values=None, timestamps=None):
    source_time = query.startswith("timestamp(")
    name = query.removeprefix("timestamp(").split("{")[0]
    values = values or {"synthetic-a": GAUGES}
    rows = []
    for instance, gauges in values.items():
        if name not in gauges:
            continue
        labels = {**TARGET, "region": "synthetic", "host": "abcd", "instance": instance}
        # timestamp() drops __name__; raw gauges include it.
        if not source_time:
            labels["__name__"] = name
        value = (
            (timestamps or {}).get((instance, name), NOW)
            if source_time
            else gauges[name]
        )
        rows.append({"metric": labels, "value": [NOW, str(value)]})
    return {"status": "success", "data": {"resultType": "vector", "result": rows}}


def metrics(handler):
    env = {
        "STORE_WATCH_METRICS_URL": "https://api.fly.io/prometheus/synthetic/api/v1/query",
        watch.TOKEN_ENV: "FlyV1 synthetic",
    }
    return watch.Metrics(env, httpx.Client(transport=httpx.MockTransport(handler)))


def test_filesystem_df_ratio_and_valid_promql_selectors_without_mapping():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=metric_reply(request.url.params["query"]))

    result = metrics(handler).read(TARGET, NOW)
    assert result == reading(90, NOW)  # 90 / (90 + 10), not 90 / 110
    assert len(seen) == 6
    expected = {name + '{app="synthetic",mount="/data"}' for name in GAUGES}
    assert {r.url.params["query"] for r in seen} == expected | {
        "timestamp(" + query + ")" for query in expected
    }
    assert all(
        r.method == "GET" and r.headers["Authorization"] == "FlyV1 synthetic"
        for r in seen
    )


@pytest.mark.parametrize(
    "damage", ["stale", "nan", "missing", "duplicate", "negative", "zero", "bad_sample"]
)
@pytest.mark.parametrize("bad_first", [False, True])
def test_valid_critical_sibling_alerts_but_partial_read_never_refreshes_success(
    rig, damage, bad_first
):
    rig.poll()
    values = {"synthetic-a": dict(GAUGES), "synthetic-b": dict(GAUGES)}
    timestamps = {}
    if damage == "stale":
        timestamps[("synthetic-b", "fly_instance_filesystem_blocks_avail")] = NOW - 301
    elif damage == "nan":
        values["synthetic-b"]["fly_instance_filesystem_blocks_free"] = "NaN"
    elif damage == "missing":
        del values["synthetic-b"]["fly_instance_filesystem_blocks_avail"]
    elif damage == "negative":
        values["synthetic-b"]["fly_instance_filesystem_blocks_avail"] = -1
    elif damage == "zero":
        values["synthetic-b"] = dict.fromkeys(GAUGES, 0)
    if bad_first:
        values = dict(reversed(list(values.items())))

    def handler(request):
        payload = metric_reply(request.url.params["query"], values, timestamps)
        rows = payload["data"]["result"]
        bad = next((r for r in rows if r["metric"]["instance"] == "synthetic-b"), None)
        if bad and damage == "duplicate":
            rows.append(copy.deepcopy(bad))
        if bad and damage == "bad_sample":
            bad["value"] = []
        return httpx.Response(200, json=payload)

    result = rig.poll(now=NOW + 60, read=metrics(handler).read)
    target = result["volumes"][0]
    assert target["status"] == "critical" and target["complete"] is False
    assert len(rig.sent) == 1 and "synthetic-a" in rig.sent[0]["message"]
    state = watch.read_state(rig.path, NOW + 60)
    assert next(iter(state["volumes"].values()))["last_success"] == NOW
    rig.poll(now=NOW + 3600, read=metrics(handler).read)
    assert any("one hour" in e["message"] for e in rig.sent)


def test_all_instances_valid_uses_worst_level(rig):
    values = {
        "synthetic-a": dict(GAUGES),
        "synthetic-b": {
            "fly_instance_filesystem_blocks": 100,
            "fly_instance_filesystem_blocks_free": 50,
            "fly_instance_filesystem_blocks_avail": 50,
        },
    }
    source = metrics(
        lambda r: httpx.Response(200, json=metric_reply(r.url.params["query"], values))
    )
    report = rig.poll(read=source.read)
    assert report["volumes"][0]["percent"] == 90
    assert report["volumes"][0]["complete"] is True
    assert "synthetic-a" in rig.sent[0]["message"]


def test_vanished_instance_across_restart_never_recovers_and_still_alarms(rig):
    values = {
        "synthetic-a": dict(GAUGES),
        "synthetic-b": dict.fromkeys(GAUGES, 100),
    }
    values["synthetic-b"]["fly_instance_filesystem_blocks_free"] = 60
    values["synthetic-b"]["fly_instance_filesystem_blocks_avail"] = 60

    def poll(now, worker=watch):
        def handler(request):
            payload = metric_reply(request.url.params["query"], values)
            for row in payload["data"]["result"]:
                row["value"][0] = now
                if request.url.params["query"].startswith("timestamp("):
                    row["value"][1] = str(now)
            return httpx.Response(200, json=payload)

        return worker.run_once(
            rig.path,
            rig.records,
            metrics(handler).read,
            lambda event: rig.sent.append(copy.deepcopy(event)) or True,
            ENV,
            now,
        )

    assert poll(NOW)["volumes"][0]["complete"] is True
    first = rig.path.read_text()
    # Import a fresh module, in addition to re-reading the persisted checkpoint.
    spec = importlib.util.spec_from_file_location("watch_after_restart", watch.__file__)
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    assert restarted.read_state(rig.path, NOW) == json.loads(first)
    del values["synthetic-a"]
    report = poll(NOW + 60, restarted)
    result = report["volumes"][0]
    assert result["status"] == "unknown" and result["complete"] is False
    assert result["failure"] == "metrics_instance_missing"
    state = restarted.read_state(rig.path, NOW + 60)["volumes"][
        watch.target_key(TARGET)
    ]
    assert state["level"] == "critical" and state["last_success"] == NOW
    assert len(rig.sent) == 1
    poll(NOW + 3600, restarted)
    assert len(rig.sent) == 2
    assert "one hour" in rig.sent[-1]["message"]
    assert "failure=metrics_instance_missing" in rig.sent[-1]["message"]
    assert not any(
        "healthy" in e["message"] or "reads recovered" in e["message"] for e in rig.sent
    )


def test_explicit_retirement_checkpoints_notice_without_recovering(
    rig, monkeypatch, capsys
):
    rig.poll(90)
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(watch.time, "time", lambda: NOW + 60)
    monkeypatch.setattr(
        watch,
        "Notifications",
        lambda: lambda e: rig.sent.append(copy.deepcopy(e)) or True,
    )
    monkeypatch.setattr(
        watch, "Metrics", lambda *a: pytest.fail("retirement must not read metrics")
    )
    assert (
        watch.main(
            [
                "--state",
                str(rig.path),
                "--retire-instance",
                "synthetic",
                "/data",
                "synthetic-a",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
    value = watch.read_state(rig.path, NOW + 60)["volumes"][watch.target_key(TARGET)]
    assert value["seen_instances"] == []
    assert value["level"] == "critical" and value["last_success"] == NOW
    assert "inventory retire" in rig.sent[-1]["message"]
    replacement = reading(40, NOW + 120)
    replacement["instances"][0]["instance"] = "synthetic-b"
    replacement["seen_instances"] = ["synthetic-b"]
    assert (
        rig.poll(now=NOW + 120, read=lambda *a: replacement)["volumes"][0]["status"]
        == "healthy"
    )
    assert "healthy" in rig.sent[-1]["message"]


@pytest.mark.parametrize(
    "option,selection,category",
    [
        (
            "--retire-instance",
            ["absent", "/data", "synthetic-a"],
            "inventory_target_unknown",
        ),
        (
            "--confirm-instances",
            ["absent", "/data", "synthetic-a"],
            "inventory_target_unknown",
        ),
        (
            "--retire-instance",
            ["synthetic", "/data", "absent"],
            "inventory_instance_unknown",
        ),
        (
            "--confirm-instances",
            ["synthetic", "/data", "synthetic-a"],
            "inventory_already_known",
        ),
        ("--confirm-instances", ["synthetic", "/data"], "inventory_action_invalid"),
        (
            "--confirm-instances",
            ["synthetic", "/data", "a", "a"],
            "inventory_action_invalid",
        ),
        (
            "--retire-instance",
            ["synthetic", "/data", "a\n"],
            "inventory_action_invalid",
        ),
        (
            "--retire-instance",
            ["synthetic", "/data", "synthetic-a"],
            "state_lock_unavailable",
        ),
        (
            "--confirm-instances",
            ["synthetic", "/data", "synthetic-a"],
            "state_lock_unavailable",
        ),
    ],
)
def test_manual_input_or_busy_lock_is_local_error_without_failure_episode(
    rig, monkeypatch, capsys, option, selection, category
):
    rig.poll(40)
    before = rig.path.read_bytes()
    rig.records.calls.clear()
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(watch.time, "time", lambda: NOW + 60)
    monkeypatch.setattr(
        watch,
        "Notifications",
        lambda: lambda e: rig.sent.append(copy.deepcopy(e)) or True,
    )
    monkeypatch.setattr(
        watch, "Metrics", lambda *a: pytest.fail("manual command must not poll")
    )
    lock = (
        watch.held_view.file_lock(rig.path.with_suffix(".lock"), fail_open=False)
        if category == "state_lock_unavailable"
        else nullcontext(True)
    )
    with lock as locked:
        assert locked
        assert watch.main(["--state", str(rig.path), option, *selection]) == 2
    assert json.loads(capsys.readouterr().out) == {"failure": category}
    assert rig.path.read_bytes() == before
    assert rig.records.calls == [] and rig.records.tasks == {} and rig.sent == []
    assert not watch.held_view.journal_path().exists()


@pytest.mark.parametrize("option", ["--retire-instance", "--confirm-instances"])
@pytest.mark.parametrize("pending", ["none", "recording", "delivery", "both"])
@pytest.mark.parametrize("older_pending", [False, True])
def test_manual_action_exit_code_tracks_recording_and_delivery(
    rig, monkeypatch, capsys, option, pending, older_pending
):
    rig.poll(40)
    state = watch.read_state(rig.path, NOW)
    if option == "--confirm-instances":
        state["volumes"][watch.target_key(TARGET)]["seen_instances"] = None
    old_id = "store-watch:synthetic-pending"
    if older_pending:
        watch.add_event(state, "Synthetic older pending event", "warning", old_id)
    if option == "--confirm-instances" or older_pending:
        rig.path.write_text(json.dumps(state))
    rig.records.fail_store = pending in ("recording", "both")

    def request(method, path, body=None):
        if body and body.get("idempotency_key", "").startswith(old_id):
            raise OSError("synthetic older recording failure")
        return rig.records(method, path, body)

    def send(event):
        rig.sent.append(copy.deepcopy(event))
        return event["id"] != old_id and pending not in ("delivery", "both")

    monkeypatch.setattr(watch, "request_json", request)
    monkeypatch.setattr(watch.time, "time", lambda: NOW + 60)
    monkeypatch.setattr(watch, "Notifications", lambda: send)
    assert watch.main(
        ["--state", str(rig.path), option, "synthetic", "/data", "synthetic-a"]
    ) == (0 if pending == "none" else 1)
    report = json.loads(capsys.readouterr().out)
    assert report["inventory"] == "updated" and report["status"] == "unknown"
    state = watch.read_state(rig.path, NOW + 60)
    value = state["volumes"][watch.target_key(TARGET)]
    assert value["last_success"] == NOW
    assert value["seen_instances"] == (
        [] if option == "--retire-instance" else ["synthetic-a"]
    )
    event = state["events"][-1]
    assert report["event"] == event
    assert event["recorded"] == (pending not in ("recording", "both"))
    assert event["delivery"] == (
        "undelivered" if pending in ("delivery", "both") else "delivered"
    )
    assert len(rig.sent) == (2 if older_pending else 1)
    assert rig.sent[-1]["severity"] == "warning"
    if older_pending:
        assert state["events"][0]["recorded"] is False
        assert state["events"][0]["delivery"] == "undelivered"
    assert not watch.held_view.journal_path().exists()


@pytest.mark.parametrize("option", ["--retire-instance", "--confirm-instances"])
@pytest.mark.parametrize("damage", ["unreadable", "write_failure"])
def test_manual_state_damage_keeps_existing_critical_failure_path(
    rig, monkeypatch, capsys, option, damage
):
    rig.poll(40)
    if damage == "unreadable":
        rig.path.write_text("damaged")
    elif option == "--confirm-instances":
        state = watch.read_state(rig.path, NOW)
        state["volumes"][watch.target_key(TARGET)]["seen_instances"] = None
        rig.path.write_text(json.dumps(state))
    before = rig.path.read_bytes()
    atomic_write = watch.held_view.atomic_write

    def save(path, text):
        if damage == "write_failure" and path == rig.path:
            raise OSError("synthetic write failure")
        return atomic_write(path, text)

    monkeypatch.setattr(watch.held_view, "atomic_write", save)
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(watch.time, "time", lambda: NOW + 60)
    monkeypatch.setattr(
        watch,
        "Notifications",
        lambda: lambda e: rig.sent.append(copy.deepcopy(e)) or True,
    )
    assert (
        watch.main(
            ["--state", str(rig.path), option, "synthetic", "/data", "synthetic-a"]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["failure"] == "state_or_job_failure"
    assert rig.path.read_bytes() == before
    assert len(rig.sent) == len(rig.records.tasks) == 1
    assert rig.sent[0]["severity"] == "critical"
    assert "state is UNKNOWN" in rig.sent[0]["message"]
    assert json.loads(watch.held_view.journal_path().read_text())[watch.FAILURE_RECEIPT]


def test_retirement_write_failure_precedes_external_effects(rig, monkeypatch):
    rig.poll(90)
    before = rig.path.read_text()
    monkeypatch.setattr(
        watch.held_view, "atomic_write", lambda *a: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(OSError):
        watch.change_inventory(
            rig.path, "retire", ["synthetic", "/data", "synthetic-a"], NOW + 60
        )
    assert rig.path.read_text() == before and len(rig.sent) == 1


@pytest.mark.parametrize(
    "inventory", ["synthetic-a", {}, [None], [""], ["a", "a"], ["a\n"]]
)
def test_invalid_persisted_instance_inventory_fails_closed(rig, inventory):
    rig.poll()
    state = json.loads(rig.path.read_text())
    state["volumes"][watch.target_key(TARGET)]["seen_instances"] = inventory
    rig.path.write_text(json.dumps(state))
    before = rig.path.read_text()
    with pytest.raises(watch.Unknown):
        rig.poll()
    assert rig.path.read_text() == before


def test_legacy_migration_requires_explicit_baseline_and_preserves_alarm(
    rig, monkeypatch, capsys
):
    rig.poll(90)
    legacy = json.loads(rig.path.read_text())
    legacy["version"] = 1
    del legacy["volumes"][watch.target_key(TARGET)]["seen_instances"]
    rig.path.write_text(json.dumps(legacy))
    result = rig.poll(40, NOW + 60)["volumes"][0]
    assert (
        result["status"] == "unknown"
        and result["failure"] == "instance_inventory_unknown"
    )
    value = watch.read_state(rig.path, NOW + 60)["volumes"][watch.target_key(TARGET)]
    assert value["level"] == "critical" and value["last_success"] == NOW
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(watch.time, "time", lambda: NOW + 120)
    monkeypatch.setattr(watch, "Notifications", lambda: lambda e: True)
    assert (
        watch.main(
            [
                "--state",
                str(rig.path),
                "--confirm-instances",
                "synthetic",
                "/data",
                "synthetic-a",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
    value = watch.read_state(rig.path, NOW + 120)["volumes"][watch.target_key(TARGET)]
    assert value["level"] == "critical" and value["last_success"] == NOW
    assert rig.poll(40, NOW + 180)["volumes"][0]["status"] == "healthy"


def test_instance_seen_only_in_bad_gauge_is_retained(rig):
    values = {
        "synthetic-a": dict(GAUGES),
        "synthetic-b": {"fly_instance_filesystem_blocks": 100},
    }
    source = metrics(
        lambda r: httpx.Response(200, json=metric_reply(r.url.params["query"], values))
    )
    rig.poll(read=source.read)
    assert watch.read_state(rig.path, NOW)["volumes"][watch.target_key(TARGET)][
        "seen_instances"
    ] == ["synthetic-a", "synthetic-b"]
    assert rig.poll(now=NOW + 60)["volumes"][0]["failure"] == "metrics_instance_missing"


def test_missing_instance_still_allows_escalation_but_never_downgrade(rig):
    rig.poll(75)
    replacement = reading(90, NOW + 60)
    replacement["seen_instances"] = ["synthetic-b"]
    replacement["instances"][0]["instance"] = "synthetic-b"
    assert (
        rig.poll(now=NOW + 60, read=lambda *a: replacement)["volumes"][0]["status"]
        == "critical"
    )
    assert "Filesystem critical" in rig.sent[-1]["message"]
    replacement["instances"][0].update(percent=75, stamp=NOW + 120)
    report = rig.poll(now=NOW + 120, read=lambda *a: replacement)
    assert report["volumes"][0]["complete"] is False
    value = watch.read_state(rig.path, NOW + 120)["volumes"][watch.target_key(TARGET)]
    assert value["level"] == "critical" and value["last_success"] == NOW
    assert len(rig.sent) == 2


def test_retirement_event_survives_interruption_and_does_not_recover_stale_reads(
    rig, monkeypatch
):
    rig.poll(90)
    rig.poll(now=NOW + 3600, read=no_read)
    original = watch.drain
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(watch.time, "time", lambda: NOW + 3660)
    monkeypatch.setattr(watch, "Notifications", lambda: lambda e: True)
    monkeypatch.setattr(
        watch, "drain", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        watch.main(
            [
                "--state",
                str(rig.path),
                "--retire-instance",
                "synthetic",
                "/data",
                "synthetic-a",
            ]
        )
    state = watch.read_state(rig.path, NOW + 3660)
    value = state["volumes"][watch.target_key(TARGET)]
    event = state["events"][-1]
    assert (
        value["stale"] is True
        and value["level"] == "critical"
        and value["last_success"] == NOW
    )
    assert event["recorded"] is False and "inventory retire" in event["message"]
    monkeypatch.setattr(watch, "drain", original)
    rig.poll(now=NOW + 3720, read=no_read)
    assert rig.sent[-1]["id"] == event["id"]
    assert not any("reads recovered" in e["message"] for e in rig.sent)


def test_configuration_recovery_never_claims_filesystem_recovery(rig):
    rig.poll(90)
    rig.records.fail_config = True
    rig.poll(now=NOW + 3600)
    rig.records.fail_config = False
    result = rig.poll(now=NOW + 3660, read=no_read)
    value = watch.read_state(rig.path, NOW + 3660)["volumes"][watch.target_key(TARGET)]
    assert (
        result["configuration"] == "ok" and result["volumes"][0]["status"] == "unknown"
    )
    assert (
        value["level"] == "critical"
        and value["stale"] is True
        and value["last_success"] == NOW
    )
    assert "configuration recovered" in rig.sent[-1]["message"]
    assert not any(
        "reads recovered" in e["message"] or "healthy" in e["message"] for e in rig.sent
    )


def test_explicit_configuration_target_removal_records_a_person_event(rig):
    rig.records.config["snapshot"]["gotchas"] = ['store-watch:["/data", "/other"]']
    rig.poll(90)
    rig.records.config["snapshot"]["gotchas"] = ['store-watch:["/data"]']
    rig.poll(90, NOW + 60)
    assert "target removed from configuration" in rig.sent[-1]["message"]
    assert '"mount": "/other"' in rig.sent[-1]["message"]
    assert not any("healthy" in e["message"] for e in rig.sent)


@pytest.mark.parametrize("name", list(GAUGES))
def test_each_source_gauge_detects_clock_skew(name):
    source = metrics(
        lambda r: httpx.Response(
            200,
            json=metric_reply(
                r.url.params["query"], timestamps={("synthetic-a", name): NOW + 31}
            ),
        )
    )
    result = source.read(TARGET, NOW)
    assert result["complete"] is False and result["failure"] == "metrics_clock_skew"


def test_partial_healthy_read_cannot_clear_critical_or_stale_alarm(rig):
    rig.poll(90)
    rig.poll(now=NOW + 3600, read=no_read)
    partial = reading(20, NOW + 3660)
    partial["complete"] = False
    rig.poll(now=NOW + 3660, read=lambda *args: partial)
    state = next(iter(watch.read_state(rig.path, NOW + 3660)["volumes"].values()))
    assert state["level"] == "critical" and state["stale"] is True
    assert len(rig.sent) == 2


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(status="error"),
        lambda p: p.update(warnings=["partial"]),
        lambda p: p.update(isPartial=True),
        lambda p: p["data"].update(isPartial=True),
        lambda p: p["data"].update(result=[]),
        lambda p: p["data"].update(resultType="matrix"),
        lambda p: p["data"]["result"][0]["metric"].update(mount="wrong"),
        lambda p: p["data"]["result"][0]["metric"].pop("instance"),
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

    result = metrics(handler).read(TARGET, NOW)
    assert result["instances"] == [] and result["complete"] is False
    assert result["failure"] in watch.FAILURES


@pytest.mark.parametrize("name", list(GAUGES))
def test_source_freshness_checked_for_every_gauge(name):
    source = metrics(
        lambda r: httpx.Response(
            200,
            json=metric_reply(
                r.url.params["query"], timestamps={("synthetic-a", name): NOW - 3600}
            ),
        )
    )
    result = source.read(TARGET, NOW)
    assert result["instances"] == [] and result["complete"] is False
    assert result["failure"] in watch.FAILURES


def test_no_application_space_is_100_percent():
    values = {"synthetic-a": {**GAUGES, "fly_instance_filesystem_blocks_avail": 0}}
    source = metrics(
        lambda r: httpx.Response(200, json=metric_reply(r.url.params["query"], values))
    )
    assert source.read(TARGET, NOW) == reading(100, NOW)


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_http_failures_and_redirects_are_unknown(status):
    source = metrics(
        lambda request: httpx.Response(
            status, headers={"Location": "https://other.invalid/"}
        )
    )
    result = source.read(TARGET, NOW)
    assert result["instances"] == [] and result["complete"] is False
    assert result["failure"] in watch.FAILURES


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
        rig.path, request, lambda *args: reading(90, NOW), lambda event: True, ENV, NOW
    )
    report = watch.run_once(
        rig.path,
        request,
        lambda *args: reading(90, NOW),
        lambda event: True,
        ENV,
        NOW + 60,
    )
    assert report["unrecorded"] == 0 and len(rig.records.tasks) == 1


def test_delivery_notes_do_not_reopen_a_human_closed_task(rig):
    rig.poll(90, notify=lambda event: False)
    task = next(iter(rig.records.tasks.values()))
    task["status"] = "done"
    task["notes"] += (
        "\nHuman assessment: capacity review in progress.\nKeep this second line.\n"
    )
    rig.poll(90, NOW + 60)
    assert task["status"] == "done"
    assert task["notes"].endswith(
        "Human assessment: capacity review in progress.\nKeep this second line.\n"
    )
    assert task["notes"].count("store-watch-delivery: ") == 1


def test_ignored_delivery_note_correction_remains_unrecorded(rig):
    original = rig.records.__call__

    def request(method, path, body=None):
        if path == "/correct" and '"delivered"' in body["value"]:
            return {"success": True}
        return original(method, path, body)

    result = watch.run_once(
        rig.path, request, lambda *args: reading(90, NOW), lambda event: True, ENV, NOW
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
            json=metric_reply(query),
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
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
        "STORE_WATCH_METRICS_URL": "https://api.fly.io/prometheus/synthetic/api/v1/query",
        watch.TOKEN_ENV: "FlyV1 synthetic",
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
    first_task = next(iter(rig.records.tasks.values()))
    first_task["status"] = "done"
    first_task["notes"] += "\nHuman handled first failure."
    rig.path.write_text(json.dumps(watch.new_state()))
    assert watch.main(["--state", str(rig.path)]) == 1
    rig.path.write_text("damaged again")
    assert watch.main(["--state", str(rig.path)]) == 1
    assert watch.main(["--state", str(rig.path)]) == 1
    assert sum("state is UNKNOWN" in message for message in calls) == 2
    failures = [
        t for t in rig.records.tasks.values() if "state is UNKNOWN" in t["description"]
    ]
    assert len(failures) == 2
    assert first_task["status"] == "done"
    assert first_task["notes"].endswith("Human handled first failure.")
    assert failures[0]["title"] != failures[1]["title"]
    assert all("state is UNKNOWN" in t["title"] for t in failures)
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


def test_task_title_is_readable_and_stable_for_event(rig):
    rig.poll(90, notify=lambda event: False)
    task = next(iter(rig.records.tasks.values()))
    title = task["title"]
    assert "critical" in title and "synthetic" in title and "/data" in title
    rig.poll(90, NOW + 60)
    assert task["title"] == title


@pytest.mark.parametrize("mounts", [["/data"], ["data"], ["/", "/data"]])
def test_mount_list_uses_observed_label_without_volume_identity(mounts):
    records = Records()
    records.config["snapshot"]["gotchas"] = "Other gotcha\nstore-watch:" + json.dumps(
        mounts
    )
    assert watch.load_targets(records, ENV) == [
        {"app": "synthetic", "mount": m} for m in mounts
    ]


@pytest.mark.parametrize(
    "mounts",
    [["/data", "/data"], [""], [None], [{"volume": "synthetic", "mount": "/data"}]],
)
def test_duplicate_or_obsolete_mount_shape_fails_closed(mounts):
    records = Records()
    records.config["snapshot"]["gotchas"] = ["store-watch:" + json.dumps(mounts)]
    with pytest.raises(watch.Unknown):
        watch.load_targets(records, ENV)


def test_retry_merges_notes_again_after_lost_correction_response(rig):
    rig.poll(90, notify=lambda event: False)
    task = next(iter(rig.records.tasks.values()))
    original = rig.records.__call__
    lost = [False]

    def request(method, path, body=None):
        result = original(method, path, body)
        if path == "/correct" and not lost[0]:
            lost[0] = True
            task["notes"] += "\nHuman added after correction reached server."
            task["status"] = "done"
            raise OSError("synthetic lost response")
        return result

    for now in (NOW + 60, NOW + 120):
        watch.run_once(
            rig.path, request, lambda *args: reading(90, now), lambda e: True, ENV, now
        )
    assert task["notes"].endswith("Human added after correction reached server.")
    assert task["status"] == "done"
    line = next(
        line
        for line in task["notes"].splitlines()
        if line.startswith(watch.DELIVERY_PREFIX)
    )
    assert json.loads(line[len(watch.DELIVERY_PREFIX) :])["delivery"] == "delivered"


def test_lost_create_response_without_identified_task_cannot_trust_changed_status(rig):
    original = rig.records.__call__
    lost = [False]

    def request(method, path, body=None):
        result = original(method, path, body)
        if path == "/store" and not lost[0]:
            lost[0] = True
            task = next(iter(rig.records.tasks.values()))
            task["status"] = "done"
            task["notes"] += "\nHuman acted before replay."
            raise OSError("synthetic lost response")
        return result

    report = watch.run_once(
        rig.path, request, lambda *args: reading(90, NOW), lambda e: True, ENV, NOW
    )
    assert report["unrecorded"] == 1 and len(rig.records.tasks) == 1
    task = next(iter(rig.records.tasks.values()))
    assert task["status"] == "done" and task["notes"].endswith(
        "Human acted before replay."
    )


def test_failure_episode_checkpoint_failure_precedes_all_external_effects(
    rig, monkeypatch
):
    monkeypatch.setattr(
        watch.held_view, "atomic_write", lambda *args: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(OSError):
        watch.failure_episode(rig.records, lambda e: rig.sent.append(e))
    assert not rig.sent and not rig.records.tasks


@pytest.mark.parametrize("payload", ["damaged", "{}", '{"state":"pending"}'])
def test_unreadable_failure_metadata_cannot_send_or_create_task(rig, payload):
    journal = watch.held_view.journal_path()
    journal.write_text(
        payload
        if payload == "damaged"
        else json.dumps({watch.FAILURE_RECEIPT: json.loads(payload)})
    )
    before = journal.read_text()
    with pytest.raises((watch.Unknown, ValueError)):
        watch.failure_episode(rig.records, lambda e: rig.sent.append(e))
    assert not rig.sent and not rig.records.tasks and journal.read_text() == before


def test_failure_journal_read_itself_fails_closed(rig, monkeypatch):
    monkeypatch.setattr(
        watch.held_view,
        "read_text_nofollow",
        lambda *args: (_ for _ in ()).throw(PermissionError()),
    )
    with pytest.raises(PermissionError):
        watch.failure_episode(rig.records, lambda e: rig.sent.append(e))
    assert not rig.sent and not rig.records.tasks


def test_interrupted_failure_after_checkpoint_reuses_episode(rig, monkeypatch):
    original = watch.drain
    monkeypatch.setattr(
        watch, "drain", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        watch.failure_episode(rig.records, lambda e: True)
    episode_id = json.loads(watch.held_view.journal_path().read_text())[
        watch.FAILURE_RECEIPT
    ]["watch_event"]["id"]
    monkeypatch.setattr(watch, "drain", original)
    for _ in range(2):
        event = watch.failure_episode(
            rig.records, lambda e: rig.sent.append(copy.deepcopy(e)) or True
        )
        assert event["id"] == episode_id and event["recorded"] is True
    assert len(rig.records.tasks) == len(rig.sent) == 1


def test_recovery_keeps_unrecorded_episode_for_retry(rig):
    rig.records.fail_store = True
    event = watch.failure_episode(rig.records, lambda e: True)
    assert event["recorded"] is False
    recovered = watch.failure_episode(rig.records, lambda e: True, recovered=True)
    assert recovered["id"] == event["id"]
    assert watch.FAILURE_RECEIPT in json.loads(
        watch.held_view.journal_path().read_text()
    )
    rig.records.fail_store = False
    watch.failure_episode(rig.records, lambda e: True, recovered=True)
    assert watch.FAILURE_RECEIPT not in json.loads(
        watch.held_view.journal_path().read_text()
    )
    next_event = watch.failure_episode(rig.records, lambda e: True)
    assert next_event["id"] != event["id"] and len(rig.records.tasks) == 2


def test_malformed_sibling_receipt_makes_whole_journal_unknown(rig):
    watch.failure_episode(rig.records, lambda e: True)
    path = watch.held_view.journal_path()
    journal = json.loads(path.read_text())
    journal["synthetic-malformed"] = {}
    path.write_text(json.dumps(journal))
    with pytest.raises(watch.Unknown):
        watch.failure_episode(rig.records, lambda e: rig.sent.append(e))
    assert not rig.sent and len(rig.records.tasks) == 1


def test_main_keeps_pending_failure_effect_visible_after_state_recovers(
    rig, monkeypatch, capsys
):
    rig.records.fail_store = True
    watch.failure_episode(rig.records, lambda e: True)
    monkeypatch.setattr(watch, "request_json", rig.records)
    monkeypatch.setattr(
        watch,
        "run_once",
        lambda *args: {
            "configuration": "ok",
            "volumes": [{"status": "healthy", "complete": True}],
            "undelivered": 0,
            "unrecorded": 0,
        },
    )
    monkeypatch.setattr(watch, "Notifications", lambda: lambda e: True)
    assert watch.main(["--state", str(rig.path)]) == 1
    assert json.loads(capsys.readouterr().out)["unrecorded"] == 1

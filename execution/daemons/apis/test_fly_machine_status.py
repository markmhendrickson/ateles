"""The bounded Fly login-machine status command (fly_machine_status.py).

Every test drives the real ``main`` (parser, config load, the existing
``FlyMachinesClient`` and the projector) against an ``httpx.MockTransport``
with synthetic data, and asserts on what actually reached stdout, stderr and
logging. No network, no Fly CLI, no Neotoma, no real credentials.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import fly_machine_status
import fly_runner
import httpx
import pytest

TOKEN = "synthetic-deploy-token-canary-7731"
APP = "synthetic-runner-app"
IDS = {"claude": "aaaa1111", "codex": "bbbb2222", "cursor": "cccc3333"}
ROW_KEYS = ["provider", "machine_state", "run_marker_present", "job_config_present"]
ERROR_KEYS = ["provider", "code", "http_status"]

SENTINEL = "ZQX-sentinel-9f3e1c-plaintext"
SENTINEL_UNICODE = "ZQXé☃sentinel"
SENTINEL_LONG = "ZQX-long-" + "q7" * 200


def _encodings(text: str) -> list[str]:
    raw = text.encode("utf-8")
    return [
        text,
        base64.b64encode(raw).decode()[:-4] or base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode()[:-4],
        json.dumps(text)[1:-1],
        json.dumps(text, ensure_ascii=True)[1:-1],
        raw.hex(),
    ]


ALL_LEAKS = [enc for s in (SENTINEL, SENTINEL_UNICODE, SENTINEL_LONG, TOKEN, APP,
                           *IDS.values(), "synthetic-run-77") for enc in _encodings(s)]  # fmt: skip


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("APIS_FLY_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("APIS_FLY_APP", APP)
    monkeypatch.setenv("APIS_FLY_API_TOKEN", TOKEN)
    for provider, mid in IDS.items():
        monkeypatch.setenv(fly_runner.login_machine_setting(provider), mid)
    # A fresh working directory of its own: shared autouse fixtures write
    # into tmp_path, and the "no file output" checks must see only this run.
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def _machine(mid: str, state: str = "stopped", config: object = None, **extra) -> dict:
    return {"id": mid, "state": state, "instance_id": "01SYNTHETICINSTANCE",
            "config": {} if config is None else config, **extra}  # fmt: skip


def _hostile(mid: str, state: str = "started") -> dict:
    big = SENTINEL + "x" * (1024 * 1024)
    encoded = base64.b64encode(SENTINEL.encode()).decode()
    long_encoded = base64.b64encode(SENTINEL_LONG.encode()).decode()
    return _machine(
        mid, state,
        config={
            "files": [{"guest_path": f"/run/job/{SENTINEL}", "raw_value": encoded},
                      {"guest_path": "/run/job/b", "raw_value": long_encoded}],
            "env": {"ATELES_RUN_ID": "synthetic-run-77", SENTINEL: SENTINEL_UNICODE},
            "metadata": {"ateles_run_id": "synthetic-run-77", "label": json.dumps(SENTINEL),
                         "nested": {"deep": [SENTINEL_UNICODE, {"x": SENTINEL_LONG}]}},
            "image": f"registry/{SENTINEL}:v1",
            "huge": big,
        },
        name=SENTINEL, region=SENTINEL_UNICODE, version=SENTINEL_LONG,
        extra={"nested": [{"value": big}]},
    )  # fmt: skip


class Fake:
    """A transport answering per machine id; records every request."""

    def __init__(self, answers: dict[str, object]):
        self.answers = answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        mid = request.url.path.rsplit("/", 1)[-1]
        answer = self.answers.get(mid, httpx.Response(404, json={"error": SENTINEL}))
        if isinstance(answer, httpx.Response):
            return answer
        if isinstance(answer, BaseException):
            raise answer
        return httpx.Response(200, json=answer)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def run(capsys, caplog, argv, fake: Fake | None = None):
    caplog.set_level(logging.DEBUG)
    code = fly_machine_status.main(argv, transport=fake.transport if fake else None)
    out, err = capsys.readouterr()
    logs = caplog.text + "".join(repr(r.args) + str(r.exc_info) for r in caplog.records)
    return code, out, err, logs


def envelope(out: str) -> dict:
    assert out.endswith("\n") and out.count("\n") == 1
    assert len(out.encode()) <= fly_machine_status.MAX_OUTPUT_BYTES
    doc = json.loads(out)
    assert list(doc) == ["schema_version", "complete", "machines", "errors"]
    assert doc["schema_version"] == 1 and isinstance(doc["complete"], bool)
    assert len(doc["machines"]) <= 3 and len(doc["errors"]) <= 3
    for row in doc["machines"]:
        assert list(row) == ROW_KEYS
        assert row["provider"] in IDS
        assert row["machine_state"] in fly_runner.MACHINE_STATES | {"absent", "unknown"}
        assert row["run_marker_present"] in (True, False, None)
        assert row["job_config_present"] in (True, False, None)
    for error in doc["errors"]:
        assert list(error) == ERROR_KEYS
        assert error["provider"] in (None, *IDS)
        assert error["code"] in fly_machine_status.ERROR_CODES
        assert error["http_status"] is None or 100 <= error["http_status"] <= 599
    return doc


def assert_no_leak(*texts: str) -> None:
    joined = "\n".join(texts)
    for leak in ALL_LEAKS:
        assert leak not in joined, "a forbidden value reached the output"


# 1. The closed rows.


def test_stopped_empty_and_started_with_job_project_exact_rows(env, capsys, caplog):
    fake = Fake({
        IDS["claude"]: _machine(IDS["claude"], "stopped", {}),
        IDS["codex"]: _hostile(IDS["codex"], "started"),
        IDS["cursor"]: _machine(IDS["cursor"], "stopped",
                                {"files": [{"guest_path": "/run/job/x", "raw_value": "eA=="}]}),
    })  # fmt: skip
    code, out, err, logs = run(capsys, caplog, ["--provider", "all"], fake)
    doc = envelope(out)
    assert code == 0 and err == ""
    assert doc == {
        "schema_version": 1, "complete": True, "errors": [],
        "machines": [
            {"provider": "claude", "machine_state": "stopped",
             "run_marker_present": False, "job_config_present": False},
            {"provider": "codex", "machine_state": "started",
             "run_marker_present": True, "job_config_present": True},
            {"provider": "cursor", "machine_state": "stopped",
             "run_marker_present": False, "job_config_present": True},
        ],
    }  # fmt: skip
    for word in ("available", "clean", "released", "free"):
        assert word not in out
    assert_no_leak(out, err, logs)


def test_single_provider_reads_only_that_machine(env, capsys, caplog):
    fake = Fake({IDS["cursor"]: _machine(IDS["cursor"], "stopped", {})})
    code, out, _, _ = run(capsys, caplog, ["--provider=cursor"], fake)
    assert code == 0
    assert [r["provider"] for r in envelope(out)["machines"]] == ["cursor"]
    assert [r.url.path for r in fake.requests] == [
        f"/v1/apps/{APP}/machines/{IDS['cursor']}"
    ]


# 2. Sentinels everywhere reach nothing.


def test_hostile_payloads_and_error_bodies_never_reach_any_output(env, capsys, caplog):
    fake = Fake({
        IDS["claude"]: _hostile(IDS["claude"], "stopped"),
        IDS["codex"]: _hostile(IDS["codex"], state=SENTINEL),  # unknown state
        IDS["cursor"]: httpx.Response(500, json={"error": SENTINEL, "detail": SENTINEL_LONG}),
    })  # fmt: skip
    code, out, err, logs = run(capsys, caplog, [], fake)
    doc = envelope(out)
    assert code == 2 and doc["complete"] is False
    assert doc["machines"][1] == {
        "provider": "codex",
        "machine_state": "unknown",
        "run_marker_present": None,
        "job_config_present": None,
    }
    assert doc["errors"] == [
        {"provider": "codex", "code": "unrecognized_state", "http_status": None},
        {"provider": "cursor", "code": "request_failed", "http_status": 500},
    ]
    assert_no_leak(out, err, logs)
    assert list(env.iterdir()) == []  # no file output


def test_megabyte_fields_give_a_tiny_bounded_output(env, capsys, caplog):
    fake = Fake({mid: _hostile(mid) for mid in IDS.values()})
    code, out, _, _ = run(capsys, caplog, [], fake)
    assert code == 0 and len(out.encode()) < 600


# 3. Distinct failure meanings; unknown is never empty or false-clean.


@pytest.mark.parametrize("answer,state,code,status", [
    (httpx.Response(404, json={"error": SENTINEL}), "absent", None, None),
    (httpx.Response(200, content=b"{not json " + SENTINEL.encode()), "unknown",
     "malformed_response", None),
    (httpx.ReadTimeout(SENTINEL), "unknown", "timed_out", None),
    (httpx.ConnectError(SENTINEL), "unknown", "request_failed", None),
    (httpx.Response(403, text=SENTINEL), "unknown", "request_failed", 403),
    (httpx.Response(503, json={"error": SENTINEL}), "unknown", "request_failed", 503),
    ({"id": "aaaa1111", "state": "zzz-" + SENTINEL, "instance_id": "i", "config": {}},
     "unknown", "unrecognized_state", None),
    # A body cannot fake the command's own verdicts.
    ({"id": "aaaa1111", "state": "absent", "instance_id": "i", "config": {}},
     "unknown", "unrecognized_state", None),
    ({"id": "aaaa1111", "state": "unknown", "instance_id": "i", "config": {}},
     "unknown", "unrecognized_state", None),
    ({"id": "aaaa1111", "state": "stopped", "instance_id": "i", "config": [SENTINEL]},
     "unknown", "malformed_response", None),
    ({"id": "aaaa1111", "state": "stopped", "instance_id": "i"}, "unknown",
     "malformed_response", None),
    ({"id": "other999", "state": "stopped", "instance_id": "i", "config": {}}, "unknown",
     "malformed_response", None),
    ([SENTINEL], "unknown", "malformed_response", None),
])  # fmt: skip
def test_each_failure_has_its_own_fixed_meaning(
    env, capsys, caplog, answer, state, code, status
):
    fake = Fake({IDS["claude"]: answer})
    exit_code, out, err, logs = run(capsys, caplog, ["--provider", "claude"], fake)
    doc = envelope(out)
    row = doc["machines"][0]
    assert row["machine_state"] == state
    if state == "absent":
        assert exit_code == 0 and doc["complete"] is True and doc["errors"] == []
        assert row["run_marker_present"] is None and row["job_config_present"] is None
    else:
        assert exit_code == 2 and doc["complete"] is False
        assert row["run_marker_present"] is None and row["job_config_present"] is None
        assert doc["errors"] == [
            {"provider": "claude", "code": code, "http_status": status}
        ]
    assert_no_leak(out, err, logs)


def test_aggregate_deadline_bounds_all_reads(env, capsys, caplog, monkeypatch):
    monkeypatch.setattr(fly_machine_status, "DEADLINE_SECONDS", 0.2)

    class Slow(httpx.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request):
            Slow.calls += 1
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    caplog.set_level(logging.DEBUG)
    code = fly_machine_status.main([], transport=Slow())
    out, err = capsys.readouterr()
    doc = envelope(out)
    assert code == 2 and Slow.calls == 3
    assert [e["code"] for e in doc["errors"]] == ["timed_out"] * 3
    assert all(r["machine_state"] == "unknown" for r in doc["machines"])
    assert_no_leak(out, err, caplog.text)


@pytest.mark.parametrize("bindings,code", [
    ({"claude": ""}, "missing_binding"),
    ({"claude": f"../{SENTINEL}"}, "invalid_binding"),
    ({"claude": "aaaa1111?x=" + SENTINEL}, "invalid_binding"),
    ({"claude": "bbbb2222"}, "duplicate_binding"),
])  # fmt: skip
def test_bad_bindings_are_errors_not_empty_success(env, capsys, caplog, monkeypatch,
                                                   bindings, code):  # fmt: skip
    for provider, value in bindings.items():
        monkeypatch.setenv(fly_runner.login_machine_setting(provider), value)
    fake = Fake({})
    exit_code, out, err, logs = run(capsys, caplog, ["--provider", "claude"], fake)
    doc = envelope(out)
    assert exit_code == 2 and doc["complete"] is False and fake.requests == []
    assert doc["machines"][0]["machine_state"] == "unknown"
    assert doc["errors"] == [{"provider": "claude", "code": code, "http_status": None}]
    assert_no_leak(out, err, logs)


@pytest.mark.parametrize("bindings,faults", [
    ({"cursor": ""}, {"cursor": "missing_binding"}),
    ({"cursor": f"../{SENTINEL}"}, {"cursor": "invalid_binding"}),
    ({"codex": "aaaa1111"}, {"claude": "duplicate_binding", "codex": "duplicate_binding"}),
    ({"claude": "", "cursor": "x.y"}, {"claude": "missing_binding", "cursor": "invalid_binding"}),
])  # fmt: skip
@pytest.mark.parametrize("argv", [["--provider", "all"], []])
def test_any_bad_binding_stops_a_multi_provider_run_before_any_request(
    env, capsys, caplog, monkeypatch, bindings, faults, argv
):
    # Every requested binding is checked before the first read: one bad
    # binding means no machine is read, not a partial read of the others.
    for provider, value in bindings.items():
        monkeypatch.setenv(fly_runner.login_machine_setting(provider), value)
    fake = Fake({mid: _machine(mid) for mid in IDS.values()})
    exit_code, out, err, logs = run(capsys, caplog, argv, fake)
    doc = envelope(out)
    assert exit_code == 2 and doc["complete"] is False
    assert fake.requests == []
    assert doc["machines"] == [
        {"provider": p, "machine_state": "unknown",
         "run_marker_present": None, "job_config_present": None}
        for p in IDS
    ]  # fmt: skip
    assert doc["errors"] == [
        {"provider": p, "code": code, "http_status": None} for p, code in faults.items()
    ]
    assert_no_leak(out, err, logs)


# 4. Configuration and argument refusals before any call, with no reflection.


@pytest.mark.parametrize("name,value,code", [
    ("APIS_FLY_APP", "", "missing_configuration"),
    ("APIS_FLY_API_TOKEN", "", "missing_configuration"),
    ("APIS_FLY_API_BASE", "http://127.0.0.1:9/v1", "invalid_configuration"),
    ("APIS_FLY_API_BASE", f"https://{SENTINEL}.example/v1", "invalid_configuration"),
    ("APIS_FLY_APP", f"app/../{SENTINEL}", "invalid_configuration"),
    ("APIS_FLY_API_TOKEN", f"tok {SENTINEL}\n", "invalid_configuration"),
])  # fmt: skip
def test_configuration_refusals_make_no_call(
    env, capsys, caplog, monkeypatch, name, value, code
):
    monkeypatch.setenv(name, value)
    fake = Fake({})
    exit_code, out, err, logs = run(capsys, caplog, [], fake)
    doc = envelope(out)
    assert exit_code == 2 and fake.requests == []
    assert doc == {"schema_version": 1, "complete": False, "machines": [],
                   "errors": [{"provider": None, "code": code, "http_status": None}]}  # fmt: skip
    assert_no_leak(out, err, logs)


@pytest.mark.parametrize("argv", [
    ["--provider", SENTINEL], [f"--provider={SENTINEL}"], [SENTINEL], ["--prov", "claude"],
    ["--provider"], ["--provider", "claude", "--provider", "codex"], ["--raw"], ["--json"],
    ["--debug"], ["--verbose"], ["--query", SENTINEL], ["--command", SENTINEL],
    ["--env-file", SENTINEL], ["--token", TOKEN], [TOKEN], ["--help", SENTINEL],
    ["--provider", "Claude"], ["--provider", "claude "],
])  # fmt: skip
def test_hostile_arguments_get_fixed_text_and_no_call(env, capsys, caplog, argv):
    fake = Fake({})
    code, out, err, logs = run(capsys, caplog, argv, fake)
    assert code == 2 and fake.requests == []
    assert envelope(out)["errors"] == [
        {"provider": None, "code": "invalid_arguments", "http_status": None}
    ]
    assert err == fly_machine_status.INVALID_ARGUMENTS_TEXT
    assert_no_leak(out, err, logs)


def test_help_is_static_and_reads_nothing(capsys, caplog, monkeypatch):
    monkeypatch.setattr(fly_runner.FlyConfig, "from_env",
                        classmethod(lambda cls: pytest.fail("help read config")))  # fmt: skip
    for flag in ("--help", "-h"):
        code, out, err, _ = run(capsys, caplog, [flag])
        assert code == 0 and out == fly_machine_status.HELP and err == ""
    # It names the default and the settings it reads (names, never values).
    assert "defaults to all" in fly_machine_status.HELP
    for setting in (
        "APIS_FLY_APP",
        "APIS_FLY_API_TOKEN",
        "APIS_FLY_<PROVIDER>_MACHINE_ID",
    ):
        assert setting in fly_machine_status.HELP


def test_unexpected_failure_prints_only_the_fixed_envelope(
    env, capsys, caplog, monkeypatch
):
    def boom(*a, **k):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(fly_runner, "project_machine_status", boom)
    fake = Fake({mid: _machine(mid) for mid in IDS.values()})
    code, out, err, logs = run(capsys, caplog, [], fake)
    assert code == 2 and envelope(out)["errors"][0]["code"] == "unexpected_error"
    assert "Traceback" not in out + err
    assert_no_leak(out, err, logs)


@pytest.mark.parametrize("bad_row", [
    {"provider": "claude", "machine_state": "stopped", "run_marker_present": False,
     "job_config_present": False, "leak": SENTINEL},
    {"provider": "claude", "machine_state": SENTINEL, "run_marker_present": None,
     "job_config_present": None},
    {"provider": "claude", "machine_state": "stopped", "run_marker_present": SENTINEL,
     "job_config_present": False},
])  # fmt: skip
def test_output_shape_check_refuses_a_row_outside_the_closed_envelope(
    env, capsys, caplog, monkeypatch, bad_row
):
    # The last line of defence: should an upstream regression build a row
    # with an extra key or an out-of-domain value, the encoder refuses it and
    # only the fixed fallback is printed.
    monkeypatch.setattr(fly_runner, "project_machine_status",
                        lambda provider, machine, absent=False: dict(bad_row))  # fmt: skip
    fake = Fake({mid: _machine(mid) for mid in IDS.values()})
    code, out, err, logs = run(capsys, caplog, ["--provider", "claude"], fake)
    assert code == 2 and out == fly_machine_status.FALLBACK_OUTPUT + "\n" and err == ""
    assert_no_leak(out, err, logs)


def test_failure_outside_the_reads_prints_only_the_fixed_envelope(
    env, capsys, caplog, monkeypatch
):
    # Escapes collect() itself, so only main's last-resort handler stands
    # between the exception text (carrying a credential) and the output.
    def boom(cls):
        raise RuntimeError(f"{SENTINEL} {TOKEN}")

    monkeypatch.setattr(fly_runner.FlyConfig, "from_env", classmethod(boom))
    code, out, err, logs = run(capsys, caplog, [], Fake({}))
    assert code == 2 and out == fly_machine_status.FALLBACK_OUTPUT + "\n" and err == ""
    assert "Traceback" not in out + err
    assert_no_leak(out, err, logs)


def test_interruption_prints_a_fixed_envelope(env, capsys, caplog, monkeypatch):
    def interrupt(*a, **k):
        raise KeyboardInterrupt(SENTINEL)

    monkeypatch.setattr(fly_machine_status, "collect", interrupt)
    code, out, err, logs = run(capsys, caplog, [])
    assert code == 2 and envelope(out)["errors"][0]["code"] == "interrupted"
    assert_no_leak(out, err, logs)


# 4b. Low-level HTTP loggers, children included, are silent during the run.

CHILD_LOGGERS = ("httpcore.http11", "httpcore.connection", "httpcore.http2",
                 "httpcore.proxy", "httpcore.socks", "hpack.hpack", "h2.connection")  # fmt: skip


def test_child_transport_loggers_never_reach_root_handlers(env, capsys, caplog):
    # MockTransport never goes through httpcore, so this transport emits
    # through the real child logger names itself, as httpcore's trace does,
    # plus a child first created during the run. caplog's handler sits on the
    # root logger at DEBUG: anything propagating there is captured.
    fresh_child = "httpcore.created_during_run_" + str(time.monotonic_ns())

    class Chatty(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            for name in (*CHILD_LOGGERS, fresh_child, "httpx", "httpcore"):
                child = logging.getLogger(name)
                child.debug("trace %s %s", SENTINEL, request.url)
                child.warning("trace %s", SENTINEL)
            mid = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=_machine(mid))

    caplog.set_level(logging.DEBUG)
    code = fly_machine_status.main([], transport=Chatty())
    out, err = capsys.readouterr()
    assert code == 0 and envelope(out)["complete"] is True
    assert SENTINEL not in caplog.text and err == ""  # not root, not last resort
    assert_no_leak(out, err, caplog.text)
    # Restored afterwards, and the instrument itself is live: the same child
    # logger now reaches the root handler.
    for name in (*CHILD_LOGGERS, fresh_child, "httpx", "httpcore"):
        restored = logging.getLogger(name)
        assert restored.disabled is False and restored.propagate is True
        assert not any(isinstance(h, logging.NullHandler) for h in restored.handlers)
    logging.getLogger("httpcore.http11").debug("after-the-run-marker")
    assert "after-the-run-marker" in caplog.text


# 4c. The deadline bounds the whole run, including cancelled work and close.


async def _ignore_cancellation_forever():
    while True:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            continue


class IgnoresCancellation(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        await _ignore_cancellation_forever()


class HangsOnClose(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        mid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_machine(mid))

    async def aclose(self):
        await _ignore_cancellation_forever()


@pytest.mark.parametrize("transport_cls", [IgnoresCancellation, HangsOnClose])
def test_deadline_bounds_cancelled_reads_and_a_hung_close(
    env, capsys, caplog, monkeypatch, transport_cls
):
    deadline = 0.4
    monkeypatch.setattr(fly_machine_status, "DEADLINE_SECONDS", deadline)
    caplog.set_level(logging.DEBUG)
    result: dict = {}

    def target():
        result["code"] = fly_machine_status.main([], transport=transport_cls())

    # A thread, so a regression fails this test instead of hanging the suite.
    worker = threading.Thread(target=target, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=10)
    elapsed = time.monotonic() - started
    assert not worker.is_alive(), "the run outlived its deadline"
    assert elapsed < deadline + 1.0
    out, err = capsys.readouterr()
    doc = envelope(out)
    assert result["code"] == 2 and doc["complete"] is False and err == ""
    assert [e["code"] for e in doc["errors"]] == ["timed_out"] * 3
    assert all(r["machine_state"] == "unknown" for r in doc["machines"])
    assert "Traceback" not in out + err + caplog.text
    assert_no_leak(out, err, caplog.text)


def test_a_read_that_finished_keeps_its_row_when_another_ignores_cancellation(
    env, capsys, caplog, monkeypatch
):
    # The drain after cancelling is itself bounded inside collect, so a stuck
    # sibling costs only its own row, not the rows already read.
    monkeypatch.setattr(fly_machine_status, "DEADLINE_SECONDS", 0.4)

    class OneStuck(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            mid = request.url.path.rsplit("/", 1)[-1]
            if mid == IDS["codex"]:
                await _ignore_cancellation_forever()
            return httpx.Response(200, json=_machine(mid))

    caplog.set_level(logging.DEBUG)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.update(
            code=fly_machine_status.main([], transport=OneStuck())
        ),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "the run outlived its deadline"
    out, err = capsys.readouterr()
    doc = envelope(out)
    assert result["code"] == 2 and err == ""
    assert [r["machine_state"] for r in doc["machines"]] == [
        "stopped",
        "unknown",
        "stopped",
    ]
    assert doc["errors"] == [
        {"provider": "codex", "code": "timed_out", "http_status": None}
    ]


# 5. The transport oracle: GET of the exact known paths only, nothing else.


def test_only_gets_of_known_machine_paths_and_no_other_effect(
    env, capsys, caplog, monkeypatch
):
    def forbidden(*a, **k):
        raise AssertionError("forbidden side effect")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(fly_runner, "get_executor", forbidden)
    monkeypatch.setattr(fly_runner.FlyLedger, "__init__", forbidden)
    for name in (
        "create",
        "update",
        "start",
        "stop",
        "destroy",
        "acquire_lease",
        "release_lease",
        "get_lease",
        "list",
        "wait",
    ):
        monkeypatch.setattr(fly_runner.FlyMachinesClient, name, forbidden)
    monkeypatch.setattr(fly_runner, "read_result_record", forbidden)
    fake = Fake({mid: _machine(mid) for mid in IDS.values()})
    code, out, _, _ = run(capsys, caplog, ["--provider", "all"], fake)
    assert code == 0 and envelope(out)["complete"] is True
    assert len(fake.requests) == 3
    assert {(r.method, str(r.url)) for r in fake.requests} == {
        ("GET", f"{fly_runner.FLY_API_BASE_DEFAULT}/apps/{APP}/machines/{mid}")
        for mid in IDS.values()
    }
    assert all(r.content == b"" for r in fake.requests)
    assert list(env.iterdir()) == []


# 6. Lifecycle consumers keep the rich objects.


def test_client_get_still_returns_the_full_machine(env):
    machine = _hostile(IDS["claude"])
    client = fly_runner.FlyMachinesClient(
        fly_runner.FlyConfig.from_env(),
        transport=Fake({IDS["claude"]: machine}).transport,
    )
    got = asyncio.run(client.get(IDS["claude"]))
    assert got["config"]["files"] == machine["config"]["files"]
    assert got["config"]["metadata"]["ateles_run_id"] == "synthetic-run-77"


def test_projector_builds_fresh_records(env):
    machine = _machine(IDS["claude"], "stopped", {"env": {"A": "1"}})
    row = fly_runner.project_machine_status("claude", machine)
    assert row == {
        "provider": "claude",
        "machine_state": "stopped",
        "run_marker_present": False,
        "job_config_present": True,
    }
    assert row is not machine and not set(row) & set(machine)
    assert (
        fly_runner.project_machine_status("claude", None, absent=True)["machine_state"]
        == "absent"
    )
    # Malformed input is unknown with null predicates: null, never false.
    unknown = {"provider": "claude", "machine_state": "unknown",
               "run_marker_present": None, "job_config_present": None}  # fmt: skip
    assert fly_runner.project_machine_status("claude", None) == unknown
    list_config = _machine(IDS["claude"], "stopped", [{"env": {"A": "1"}}])
    assert fly_runner.project_machine_status("claude", list_config) == unknown
    assert fly_runner.project_machine_status("claude", [SENTINEL]) == unknown
    unknown_state = _machine(IDS["claude"], "zzz-unrecognized", {"env": {"A": "1"}})
    assert fly_runner.project_machine_status("claude", unknown_state) == unknown


def test_script_entrypoint_as_a_file(tmp_path):
    # The documented invocation runs the file by path, with no binding: help is
    # static, and a missing configuration is the fixed refusal, not a traceback.
    import sys

    script = Path(fly_machine_status.__file__)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("APIS_FLY_")}
    child_env["ATELES_SKIP_DOTENV"] = "1"

    def invoke(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(script), *args],
            check=False,
            capture_output=True,
            text=True,
            env=child_env,
            cwd=workdir,
            timeout=60,
        )

    run_help = invoke("--help")
    assert run_help.returncode == 0 and run_help.stdout == fly_machine_status.HELP
    assert run_help.stderr == ""
    run_bare = invoke("--provider", "codex")
    assert run_bare.returncode == 2 and run_bare.stderr == ""
    assert json.loads(run_bare.stdout)["errors"] == [
        {"provider": None, "code": "missing_configuration", "http_status": None}
    ]
    assert list(workdir.iterdir()) == []

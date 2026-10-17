"""In-process error classification; all external collaborators are inert."""

import asyncio
import subprocess
from unittest.mock import Mock

import pytest

import gate_waive as gate


def _prepared_error(monkeypatch, failure):
    ns = gate._ns
    sender = Mock()
    sender.poll.return_value = True
    sender.recv.return_value = ("run", 110.0)
    resolver = Mock(return_value={"key": "unused", "sub": "synthetic", "kid": "unused"})
    proc = Mock()
    proc.communicate.side_effect = failure
    popen = Mock()
    popen.return_value.__enter__ = Mock(return_value=proc)
    popen.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(ns.os, "setsid", Mock())
    monkeypatch.setattr(ns.time, "monotonic", lambda: 100.0)

    ns._prepare_signed_request(
        sender, resolver, "synthetic", None, None, "POST", "http://unused.invalid",
        {"idempotency_key": "same-key"}, 110.0, "unused", "unused", "unused", {}, popen,
    )

    proc.communicate.assert_called_once()
    sender.close.assert_called_once()
    messages = [call.args[0] for call in sender.send.call_args_list]
    assert messages[:2] == [("ready",), ("prepared",)]
    assert "private detail" not in repr(messages)
    return messages[-1][1]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (subprocess.TimeoutExpired("private detail", 1), "helper_timeout"),
        (ValueError("private detail"), "helper_error"),
        (OSError("private detail"), "helper_error"),
    ],
)
def test_preparation_failure_preserves_fixed_class(monkeypatch, failure, code):
    assert _prepared_error(monkeypatch, failure) == code


@pytest.mark.parametrize("code", ["helper_timeout", "helper_error", "no_key", "expired"])
def test_coded_failure_survives_gate_classifier(code):
    assert gate.signed_failure_code(gate._ns.SignedRequestError("private detail", code)) == code


@pytest.mark.parametrize(
    ("failure", "attempts", "succeeds"),
    [
        (subprocess.TimeoutExpired("private detail", 1), 2, True),
        (ValueError("private detail"), 1, False),
        (OSError("private detail"), 1, False),
    ],
)
def test_preparation_code_uses_existing_same_key_retry(monkeypatch, failure, attempts, succeeds):
    code = _prepared_error(monkeypatch, failure)
    calls, waits = [], []

    async def correct(entity_id, field, value, key, **kwargs):
        calls.append((entity_id, field, value, key, kwargs))
        if len(calls) == 1:
            raise gate._ns.SignedRequestError("private detail", code)
        return 200, {}

    async def sleep(delay):
        waits.append(delay)

    store = gate.IssueGateStore("http://unused.invalid", "unused")
    monkeypatch.setattr(store, "_signed_correct", correct)
    monkeypatch.setattr(gate, "_monotonic", lambda: 100.0)
    monkeypatch.setattr(gate, "_retry_sleep", sleep)
    result = asyncio.run(store._sign_off_write(
        "synthetic", gate.GATE_STATUS_FIELD, {"pm": "signed_off"}, "same-key",
        lens_agent="pavo", lens_sub="synthetic", context=("synthetic/repo", 1, "pm"),
    ))

    assert (result == "") is succeeds
    assert len(calls) == attempts
    assert all(call == calls[0] for call in calls)
    assert waits == ([gate.SIGNED_WRITE_BACKOFFS_S[0]] if succeeds else [])

"""Bounded retry of a signed Neotoma request the server briefly refused.

What is pinned:

- a 503 ADMISSION_UNAVAILABLE then 200 succeeds in two attempts carrying the
  same idempotency key and body under two distinct signatures;
- a stated Retry-After is honoured, up to the policy's cap;
- a 401 AUTH_REQUIRED is repeated exactly once (a transition measure until the
  server answers 503 for a failed admission lookup), and a second 401 goes back
  to the caller;
- a write without an idempotency key, and a 400 or 403, are never repeated;
- no attempt starts at or after the caller's deadline;
- a principal that changes between attempts still fails with identity_changed.

No network: NeotomaWriter runs over an httpx MockTransport, and
signed_request_with_retry over a faked ``signed_request``. Synthetic keys only.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

import neotoma_signed as ns

BASE = "https://neotoma.example"


# ── fixtures ────────────────────────────────────────────────────────────────


def _b64u(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()


def _write_jwk(keys_dir: Path, agent: str) -> None:
    nums = ec.generate_private_key(ec.SECP256R1()).private_numbers()
    jwk = {
        "kty": "EC", "crv": "P-256",
        "d": _b64u(nums.private_value),
        "x": _b64u(nums.public_numbers.x),
        "y": _b64u(nums.public_numbers.y),
        "sub": f"{agent}@ateles-swarm", "kid": f"{agent}-test-kid",
    }
    keys_dir.mkdir(parents=True, exist_ok=True)
    (keys_dir / f"{agent}.jwk.json").write_text(json.dumps(jwk))


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"entities": [{"entity_id": "ent_1", "observation_id": "obs_1"}]})


class _Scripted:
    """Answers each request with the next scripted response, then 200."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.responses.pop(0) if self.responses else _ok(request)
        return answer(request) if callable(answer) else answer


@pytest.fixture
def http(monkeypatch):
    real_async, real_sync = httpx.AsyncClient, httpx.Client

    def install(*responses) -> _Scripted:
        rec = _Scripted(*responses)
        transport = httpx.MockTransport(rec.handler)
        strip = lambda kw: {k: v for k, v in kw.items() if k != "transport"}  # noqa: E731
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_async(transport=transport, **strip(kw)))
        monkeypatch.setattr(httpx, "Client", lambda **kw: real_sync(transport=transport, **strip(kw)))
        return rec

    return install


def _writer(tmp_path: Path, policy: "ns.SignedRetryPolicy | None" = None,
            mode: ns.SigningMode = ns.SigningMode.ON):
    keys = tmp_path / "keys"
    _write_jwk(keys, "anthus")
    w = ns.NeotomaWriter("anthus", mode=mode, base_url=BASE, bearer="tok",
                         keys_dir=keys, retry_policy=policy)
    slept: list[float] = []
    w._sleep = slept.append

    async def asleep(delay):
        slept.append(delay)

    w._asleep = asleep
    return w, slept


def _store_body(key: "str | None" = "idem-1") -> dict:
    body = {"entities": [{"entity_type": "report", "title": "synthetic"}]}
    if key is not None:
        body["idempotency_key"] = key
    return body


def _admission(retry_after: "str | None" = None) -> httpx.Response:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return httpx.Response(503, headers=headers,
                          json={"error": {"error_code": "ADMISSION_UNAVAILABLE", "message": "busy"}})


def _auth_required() -> httpx.Response:
    return httpx.Response(401, json={"error": {"error_code": "AUTH_REQUIRED", "message": "Missing Bearer token"}})


# ── NeotomaWriter (the shared write client) ─────────────────────────────────


def test_admission_unavailable_then_ok_retries_once_with_same_key_and_fresh_signature(tmp_path, http):
    rec = http(_admission())
    w, slept = _writer(tmp_path)
    result = w.post("store", _store_body())
    assert result.status == 200 and result.signed
    assert len(rec.requests) == 2 and len(slept) == 1
    first, second = rec.requests
    assert first.content == second.content  # same body, so the same idempotency key
    assert json.loads(second.content)["idempotency_key"] == "idem-1"
    assert first.headers["signature"] != second.headers["signature"]
    assert all("authorization" not in r.headers for r in rec.requests)


def test_async_path_retries_the_same_way(tmp_path, http):
    rec = http(_admission())
    w, slept = _writer(tmp_path)
    result = asyncio.run(w.apost("store", _store_body()))
    assert result.status == 200
    assert len(rec.requests) == 2 and len(slept) == 1
    assert rec.requests[0].headers["signature"] != rec.requests[1].headers["signature"]


def test_retry_after_is_honoured_up_to_the_cap(tmp_path, http):
    policy = ns.SignedRetryPolicy(retry_after_cap_s=3.0, total_bound_s=60.0)
    rec = http(_admission("2"), _admission("30"))
    w, slept = _writer(tmp_path, policy)
    assert w.post("store", _store_body()).status == 200
    assert len(rec.requests) == 3
    assert slept == [2.0, 3.0]


def test_gateway_502_and_503_are_retried(tmp_path, http):
    rec = http(httpx.Response(502, text="<html>bad gateway</html>"),
               httpx.Response(503, text="<html>unavailable</html>"))
    w, slept = _writer(tmp_path)
    assert w.post("store", _store_body()).status == 200
    assert len(rec.requests) == 3 and len(slept) == 2
    assert all(0 < d <= ns.DEFAULT_SIGNED_RETRY_POLICY.max_delay_s for d in slept)


def test_attempts_are_bounded_to_three(tmp_path, http):
    rec = http(_admission(), _admission(), _admission(), _admission())
    w, _ = _writer(tmp_path)
    with pytest.raises(ns.NeotomaWriteError) as caught:
        w.post("store", _store_body())
    assert caught.value.status == 503
    assert len(rec.requests) == 3


def test_auth_required_is_retried_exactly_once(tmp_path, http):
    rec = http(_auth_required(), _auth_required())
    w, slept = _writer(tmp_path)
    with pytest.raises(ns.SignedWriteError):
        w.post("store", _store_body())
    assert len(rec.requests) == 2 and len(slept) == 1
    assert all("authorization" not in r.headers for r in rec.requests)


def test_auth_required_once_then_ok_succeeds(tmp_path, http):
    rec = http(_auth_required())
    w, _ = _writer(tmp_path)
    assert w.post("store", _store_body()).status == 200
    assert len(rec.requests) == 2


@pytest.mark.parametrize("async_mode", [False, True])
def test_shadow_auth_required_retry_then_bearer_fallback_preserves_keyed_body(tmp_path, http, async_mode):
    rec = http(_auth_required(), _auth_required())
    w, slept = _writer(tmp_path, mode=ns.SigningMode.SHADOW)
    result = (asyncio.run(w.apost("store", _store_body())) if async_mode
              else w.post("store", _store_body()))
    assert result.signed is False
    assert len(rec.requests) == 3
    assert [("signature" in r.headers, "authorization" in r.headers) for r in rec.requests] == [
        (True, False), (True, False), (False, True),
    ]
    assert rec.requests[0].content == rec.requests[1].content == rec.requests[2].content
    assert json.loads(rec.requests[2].content)["idempotency_key"] == "idem-1"
    assert len(slept) == 1


@pytest.mark.parametrize("async_mode", [False, True])
def test_shadow_fallback_spends_the_third_attempt(tmp_path, http, async_mode):
    rec = http(_admission(), _admission(), _auth_required())
    w, _ = _writer(tmp_path, mode=ns.SigningMode.SHADOW)
    with pytest.raises(ns.SignedWriteError) as caught:
        asyncio.run(w.apost("store", _store_body())) if async_mode else w.post("store", _store_body())
    assert caught.value.status == 401
    assert len(rec.requests) == 3
    assert all("signature" in r.headers and "authorization" not in r.headers for r in rec.requests)


@pytest.mark.parametrize("async_mode", [False, True])
def test_shadow_fallback_refused_after_total_deadline(tmp_path, http, monkeypatch, async_mode):
    clock = _Clock()

    def late_401(_request):
        clock.now += 21
        return _auth_required()

    rec = http(late_401)
    policy = ns.SignedRetryPolicy(total_bound_s=20)
    w, _ = _writer(tmp_path, policy, mode=ns.SigningMode.SHADOW)
    monkeypatch.setattr(w, "_begin_retry", lambda path, body: policy.begin("POST", body, clock=clock))
    with pytest.raises(ns.SignedWriteError) as caught:
        asyncio.run(w.apost("store", _store_body())) if async_mode else w.post("store", _store_body())
    assert caught.value.status == 401
    assert len(rec.requests) == 1


def test_write_without_idempotency_key_is_never_retried(tmp_path, http):
    rec = http(_admission())
    w, slept = _writer(tmp_path)
    with pytest.raises(ns.NeotomaWriteError):
        w.post("store", _store_body(key=None))
    assert len(rec.requests) == 1 and slept == []


@pytest.mark.parametrize("async_mode", [False, True])
def test_unkeyed_shadow_refusal_does_not_send_bearer(tmp_path, http, async_mode):
    rec = http(_auth_required())
    w, _ = _writer(tmp_path, mode=ns.SigningMode.SHADOW)
    with pytest.raises(ns.SignedWriteError) as caught:
        asyncio.run(w.apost("store", _store_body(None))) if async_mode else w.post("store", _store_body(None))
    assert caught.value.status == 401
    assert len(rec.requests) == 1 and "authorization" not in rec.requests[0].headers


@pytest.mark.parametrize("status", [400, 403])
def test_other_4xx_are_not_retried(tmp_path, http, status):
    rec = http(httpx.Response(status, json={"error": {"error_code": "AUTH_REQUIRED"}}))
    w, slept = _writer(tmp_path)
    with pytest.raises(ns.NeotomaWriteError):
        w.post("store", _store_body())
    assert len(rec.requests) == 1 and slept == []


def test_writer_retry_log_names_no_body_or_signature(tmp_path, http, caplog):
    rec = http(_admission())
    w, _ = _writer(tmp_path)
    with caplog.at_level("WARNING", logger="daemon_runtime.neotoma_signed"):
        w.post("store", _store_body())
    lines = [r.getMessage() for r in caplog.records if "retrying" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "503" in line and "ADMISSION_UNAVAILABLE" in line and "attempt 2/3" in line
    assert "idem-1" not in line and "synthetic" not in line
    assert rec.requests[0].headers["signature"] not in line


# ── signed_request_with_retry (callers of signed_request) ───────────────────


class _FakeSigned:
    """Stands in for signed_request: admits each attempt's principal on the pin
    the way the real preparer does, and records what each attempt sent."""

    def __init__(self, monkeypatch, answers, principals=None):
        self.answers = list(answers)
        self.principals = list(principals or [])
        self.calls: list[dict] = []
        monkeypatch.setattr(ns, "signed_request", self)

    def __call__(self, method, url, body=None, agent_name="", timeout=20, **kwargs):
        pin = kwargs.get("principal_pin")
        principal = self.principals.pop(0) if self.principals else ("pavo@ateles-swarm", "tp-a")
        self.calls.append({"method": method, "body": json.dumps(body, sort_keys=True),
                           "signature": f"sig-{len(self.calls) + 1}", "pin": pin, **kwargs})
        if pin is not None and not pin.admit(principal):
            raise ns.SignedRequestError("signing principal changed", "identity_changed")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.slept.append(delay)
        self.now += delay


_BODY = {"field": "f", "value": "v", "idempotency_key": "idem-2"}
_ADMISSION_BODY = {"error": {"error_code": "ADMISSION_UNAVAILABLE",
                             "details": {"retry_after_seconds": 1}}}


def _call(clock, **kwargs):
    return ns.signed_request_with_retry("POST", f"{BASE}/correct", kwargs.pop("body", _BODY),
                                        agent_name="pavo", clock=clock, sleep=clock.sleep, **kwargs)


def test_wrapper_retries_admission_unavailable_with_same_key_and_one_pin(monkeypatch):
    fake = _FakeSigned(monkeypatch, [(503, _ADMISSION_BODY), (200, {"ok": True})])
    clock = _Clock()
    assert _call(clock) == (200, {"ok": True})
    assert len(fake.calls) == 2
    assert fake.calls[0]["body"] == fake.calls[1]["body"]
    assert fake.calls[0]["signature"] != fake.calls[1]["signature"]  # each attempt re-signs
    assert fake.calls[0]["pin"] is fake.calls[1]["pin"] is not None
    assert clock.slept == [1.0]  # the body's stated retry time


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), 3601, True, [], "later"])
def test_wrapper_ignores_invalid_stated_delay(monkeypatch, bad):
    body = {"error": {"error_code": "ADMISSION_UNAVAILABLE",
                      "details": {"retry_after_seconds": bad}}}
    _FakeSigned(monkeypatch, [(503, body), (200, {})])
    clock = _Clock()
    assert _call(clock, rng=lambda: 1.0) == (200, {})
    assert clock.slept == [0.5]


def test_wrapper_ignores_legacy_delay_outside_envelope(monkeypatch):
    body = {"error": {"error_code": "ADMISSION_UNAVAILABLE"}, "retry_after": 1}
    _FakeSigned(monkeypatch, [(503, body), (200, {})])
    clock = _Clock()
    assert _call(clock, rng=lambda: 1.0) == (200, {})
    assert clock.slept == [0.5]


def test_wrapper_caller_mutation_cannot_change_retry_body(monkeypatch):
    fake = _FakeSigned(monkeypatch, [(503, _ADMISSION_BODY), (200, {})])
    body = dict(_BODY)
    clock = _Clock()

    def mutate_after_first_attempt(delay):
        body["idempotency_key"] = "changed"
        clock.sleep(delay)

    assert ns.signed_request_with_retry(
        "POST", f"{BASE}/correct", body, agent_name="pavo", clock=clock,
        sleep=mutate_after_first_attempt,
    ) == (200, {})
    assert fake.calls[0]["body"] == fake.calls[1]["body"]
    assert json.loads(fake.calls[1]["body"])["idempotency_key"] == "idem-2"


def test_wrapper_retries_a_proxy_html_503(monkeypatch):
    html = ns.SignedRequestError("signing request failed", "response_not_json",
                                 response_received=True, http_status=503)
    fake = _FakeSigned(monkeypatch, [html, (200, {})])
    assert _call(_Clock()) == (200, {})
    assert len(fake.calls) == 2


def test_wrapper_auth_required_once_then_returns_second_401(monkeypatch):
    refusal = {"error": {"error_code": "AUTH_REQUIRED", "message": "Missing Bearer token"}}
    fake = _FakeSigned(monkeypatch, [(401, refusal), (401, refusal), (200, {})])
    assert _call(_Clock()) == (401, refusal)
    assert len(fake.calls) == 2


def test_wrapper_never_retries_a_write_without_idempotency_key(monkeypatch):
    fake = _FakeSigned(monkeypatch, [(503, _ADMISSION_BODY), (200, {})])
    status, _ = _call(_Clock(), body={"field": "f", "value": "v"})
    assert status == 503 and len(fake.calls) == 1


def test_wrapper_retries_a_get_without_a_body(monkeypatch):
    fake = _FakeSigned(monkeypatch, [(502, {}), (200, {"x": 1})])
    clock = _Clock()
    result = ns.signed_request_with_retry("GET", f"{BASE}/entities/ent_1", agent_name="pavo",
                                          clock=clock, sleep=clock.sleep)
    assert result == (200, {"x": 1}) and len(fake.calls) == 2


def test_get_retry_log_does_not_claim_an_idempotency_key(monkeypatch, caplog):
    _FakeSigned(monkeypatch, [(502, {}), (200, {})])
    clock = _Clock()
    with caplog.at_level("WARNING", logger="daemon_runtime.neotoma_signed"):
        ns.signed_request_with_retry("GET", f"{BASE}/entities/ent_1", agent_name="pavo",
                                     clock=clock, sleep=clock.sleep)
    assert any("retrying" in r.message for r in caplog.records)
    assert all("idempotency key" not in r.message for r in caplog.records)


@pytest.mark.parametrize("status", [400, 403])
def test_wrapper_does_not_retry_other_4xx(monkeypatch, status):
    fake = _FakeSigned(monkeypatch, [(status, {"error": {"error_code": "AUTH_REQUIRED"}}), (200, {})])
    assert _call(_Clock())[0] == status and len(fake.calls) == 1


def test_wrapper_starts_no_retry_after_the_deadline(monkeypatch):
    fake = _FakeSigned(monkeypatch, [(503, {"error": {"error_code": "ADMISSION_UNAVAILABLE",
                                              "details": {"retry_after_seconds": 5}}}),
                                     (200, {})])
    clock = _Clock()
    assert _call(clock, deadline=clock.now + 2)[0] == 503
    assert len(fake.calls) == 1 and clock.slept == []


def test_wrapper_expired_initial_deadline_sends_nothing(monkeypatch):
    fake = _FakeSigned(monkeypatch, [(200, {})])
    clock = _Clock()
    with pytest.raises(ns.SignedRequestError) as caught:
        _call(clock, deadline=clock.now)
    assert caught.value.code == "expired"
    assert fake.calls == []


def test_wrapper_sleep_overshoot_keeps_the_original_503(monkeypatch):
    fake = _FakeSigned(monkeypatch, [(503, _ADMISSION_BODY), (200, {})])
    clock = _Clock()
    status, body = ns.signed_request_with_retry(
        "POST", f"{BASE}/correct", _BODY, agent_name="pavo", clock=clock,
        sleep=lambda delay: setattr(clock, "now", clock.now + 30),
    )
    assert (status, body) == (503, _ADMISSION_BODY)
    assert len(fake.calls) == 1


def test_wrapper_principal_change_between_attempts_fails_identity_changed(monkeypatch):
    fake = _FakeSigned(monkeypatch, [(503, _ADMISSION_BODY), (200, {})],
                       principals=[("pavo@ateles-swarm", "tp-a"), ("pavo@ateles-swarm", "tp-b")])
    with pytest.raises(ns.SignedRequestError) as caught:
        _call(_Clock())
    assert caught.value.code == "identity_changed"
    assert len(fake.calls) == 2


def test_wrapper_uses_the_callers_pin(monkeypatch):
    pin = ns.PrincipalPin()
    pin.admit(("pavo@ateles-swarm", "tp-other"))
    _FakeSigned(monkeypatch, [(200, {})])
    with pytest.raises(ns.SignedRequestError) as caught:
        _call(_Clock(), principal_pin=pin)
    assert caught.value.code == "identity_changed"


def test_response_error_code_reads_only_documented_envelopes():
    assert ns.response_error_code({"error": {"error_code": "A"}}) == "A"
    assert ns.response_error_code({"error": {"code": "B", "issues": []}}) == "B"
    assert ns.response_error_code({"error_code": "C"}) is None
    assert ns.response_error_code({"code": "D"}) is None
    assert ns.response_error_code({"error": "E"}) is None
    assert ns.response_error_code('{"error": {"error_code": "F"}}') == "F"
    assert ns.response_error_code("<html>") is None
    assert ns.response_error_code({}) is None


@pytest.mark.parametrize("async_mode", [False, True])
def test_writer_total_bound_refuses_stated_delay_without_sleep(tmp_path, http, monkeypatch, async_mode):
    refusal = _admission("5")
    rec = http(refusal)
    policy = ns.SignedRetryPolicy(total_bound_s=1.0)
    w, slept = _writer(tmp_path, policy)
    clock = _Clock()
    monkeypatch.setattr(w, "_begin_retry", lambda path, body: policy.begin("POST", body, clock=clock))
    with pytest.raises(ns.NeotomaWriteError) as caught:
        if async_mode:
            asyncio.run(w.apost("store", _store_body()))
        else:
            w.post("store", _store_body())
    assert caught.value.status == 503
    assert "busy" in str(caught.value)
    assert len(rec.requests) == 1 and slept == []


def test_writer_sleep_overshoot_does_not_start_another_post(tmp_path, http, monkeypatch):
    rec = http(_admission("1"))
    policy = ns.SignedRetryPolicy(total_bound_s=2.0)
    w, _ = _writer(tmp_path, policy)
    clock = _Clock()
    monkeypatch.setattr(w, "_begin_retry", lambda path, body: policy.begin("POST", body, clock=clock))
    w._sleep = lambda delay: setattr(clock, "now", clock.now + 3)
    with pytest.raises(ns.NeotomaWriteError) as caught:
        w.post("store", _store_body())
    assert caught.value.status == 503
    assert len(rec.requests) == 1


def test_writer_auth_refusal_overshoot_does_not_fall_back(tmp_path, http, monkeypatch):
    rec = http(_auth_required())
    policy = ns.SignedRetryPolicy(total_bound_s=1.0)
    w, _ = _writer(tmp_path, policy, mode=ns.SigningMode.SHADOW)
    clock = _Clock()
    monkeypatch.setattr(w, "_begin_retry", lambda path, body: policy.begin("POST", body, clock=clock))
    w._sleep = lambda delay: setattr(clock, "now", clock.now + 2)
    with pytest.raises(ns.SignedWriteError) as caught:
        w.post("store", _store_body())
    assert caught.value.status == 401
    assert len(rec.requests) == 1 and "authorization" not in rec.requests[0].headers


def test_writer_caller_mutation_cannot_change_retry_body(tmp_path, http):
    rec = http(_admission())
    w, _ = _writer(tmp_path)
    body = _store_body()
    w._sleep = lambda delay: body.update(idempotency_key="changed")
    assert w.post("store", body).status == 200
    assert rec.requests[0].content == rec.requests[1].content
    assert json.loads(rec.requests[1].content)["idempotency_key"] == "idem-1"

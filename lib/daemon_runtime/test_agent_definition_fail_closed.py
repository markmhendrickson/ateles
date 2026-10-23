"""An agent definition that cannot be loaded now fails closed.

The loader retries a transient failure briefly and then refuses with a controlled
error that carries only a fixed reason code. It never returns a stand-in for a
failed load. A caller that runs a role with no definition on purpose must opt in,
and the opt-in covers only a definite "Neotoma has none".

Run: pytest lib/daemon_runtime/test_agent_definition_fail_closed.py -v
"""

from __future__ import annotations

import logging
import json as _json

import httpx
import pytest

import agent_loader as al
from agent_loader import AgentLoader

# Resolved by name so that, run against code without the change, each test FAILS
# on its assertion rather than the whole file failing to import.
AgentDefinitionUnavailable = getattr(al, "AgentDefinitionUnavailable", RuntimeError)

REAL = {
    "entities": [
        {
            "entity_id": "ent_real",
            "snapshot": {
                "name": "pavo",
                "prompt_markdown": "# the real role prompt",
                "tool_allowlist": ["Read", "Bash(gh pr:*)"],
                "status": "active",
            },
        }
    ]
}


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "http error", request=httpx.Request("POST", "http://x"), response=self
            )

    def json(self):
        return self._payload


@pytest.fixture
def neotoma(monkeypatch):
    """A scripted Neotoma: each call pops one outcome (the last repeats)."""
    state = {"script": [], "calls": 0, "sleeps": []}
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(al, "_sleep", lambda s: state["sleeps"].append(s), raising=False)

    def post(url, **kwargs):
        state["calls"] += 1
        outcome = state["script"].pop(0) if len(state["script"]) > 1 else state["script"][0]
        if outcome == "timeout":
            raise httpx.ReadTimeout("timed out")
        if outcome == "connect":
            raise httpx.ConnectError("refused")
        if outcome == "real":
            return _Resp(REAL)
        if outcome == "none":
            return _Resp({"entities": []})
        if outcome == "garbage":
            resp = _Resp()
            resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
            return resp
        return _Resp(status=int(outcome))

    monkeypatch.setattr(al.httpx, "post", post)
    return state


# ── the observed failure ─────────────────────────────────────────────────────


def test_a_timeout_then_success_loads_the_real_definition(neotoma):
    neotoma["script"] = ["timeout", "real"]

    d = AgentLoader("pavo").load()

    assert d.is_stub is False
    assert d.prompt_markdown == "# the real role prompt"
    assert d.tools == ["Read", "Bash(gh pr:*)"]
    assert neotoma["calls"] == 2
    assert neotoma["sleeps"] == [0.5]


def test_a_persistent_timeout_is_refused_with_no_stand_in(neotoma):
    neotoma["script"] = ["timeout"]

    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()

    assert raised.value.code == "timeout"
    assert raised.value.attempts == 3
    assert raised.value.transient is True
    assert neotoma["calls"] == 3
    assert neotoma["sleeps"] == [0.5, 1.5]


def test_the_refusal_carries_only_a_name_a_code_and_a_count(neotoma, caplog):
    neotoma["script"] = ["timeout"]

    with caplog.at_level(logging.INFO), pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()

    texts = [str(raised.value)] + [r.getMessage() for r in caplog.records]
    for text in texts:
        assert "timed out" not in text and "http" not in text.lower()
    assert "pavo" in str(raised.value) and "timeout" in str(raised.value)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
def test_a_server_side_failure_is_retried(neotoma, status):
    neotoma["script"] = [str(status), "real"]
    assert AgentLoader("pavo").load().prompt_markdown
    assert neotoma["calls"] == 2


def test_a_connection_failure_is_retried(neotoma):
    neotoma["script"] = ["connect", "connect", "real"]
    assert AgentLoader("pavo").load().prompt_markdown
    assert neotoma["sleeps"] == [0.5, 1.5]


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_another_4xx_is_final_at_once(neotoma, status):
    neotoma["script"] = [str(status), "real"]
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()
    assert raised.value.code == "rejected"
    assert neotoma["calls"] == 1 and neotoma["sleeps"] == []


def test_a_response_that_is_not_json_is_final_at_once(neotoma):
    neotoma["script"] = ["garbage", "real"]
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()
    assert raised.value.code == "bad_response" and neotoma["calls"] == 1


# ── "there is no such definition" is a different fact ─────────────────────────


def test_no_such_definition_is_refused_by_default(neotoma):
    neotoma["script"] = ["none"]
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("ghost").load()
    assert raised.value.code == "not_found"
    assert raised.value.transient is False
    assert neotoma["calls"] == 1, "a definite answer is not retried"


def test_the_opt_in_returns_the_undefined_definition_only_for_not_found(neotoma):
    neotoma["script"] = ["none"]
    d = AgentLoader("ghost").load(allow_undefined=True)
    assert d.is_stub is True and d.load_error == "not_found"
    assert d.status == al.UNDEFINED_STATUS


@pytest.mark.parametrize("failure", ["timeout", "connect", "503", "403", "garbage"])
def test_the_opt_in_never_turns_a_failed_load_into_a_stand_in(neotoma, failure):
    neotoma["script"] = [failure]
    with pytest.raises(AgentDefinitionUnavailable):
        AgentLoader("pavo").load(allow_undefined=True)


def test_the_explicit_id_path_fails_closed_too(neotoma, monkeypatch):
    monkeypatch.setenv("PAVO_AGENT_DEFINITION_ID", "ent_x")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(al.httpx, "get", get)
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load(allow_undefined=True)
    assert raised.value.code == "timeout" and len(calls) == 3


def test_a_404_on_the_explicit_id_is_a_route_answer_not_absence(neotoma, monkeypatch):
    monkeypatch.setenv("PAVO_AGENT_DEFINITION_ID", "ent_x")
    monkeypatch.setattr(al.httpx, "get", lambda url, **kw: _Resp(status=404))
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load(allow_undefined=True)
    assert raised.value.code == "rejected"


# ── a daemon's own startup ───────────────────────────────────────────────────


def test_a_daemon_that_cannot_read_its_definition_exits_non_zero(neotoma):
    """The status check is what stops a retired agent; it must not be skipped."""
    neotoma["script"] = ["timeout"]
    with pytest.raises(SystemExit) as raised:
        al.load_agent_definition_or_exit("pavo")
    assert raised.value.code == 1


def test_a_daemon_survives_one_timeout_at_startup(neotoma):
    neotoma["script"] = ["timeout", "real"]
    assert al.load_agent_definition_or_exit("pavo").entity_id == "ent_real"


def test_an_unregistered_daemon_refuses_startup(neotoma):
    neotoma["script"] = ["none"]
    with pytest.raises(SystemExit) as raised:
        al.load_agent_definition_or_exit("ghost")
    assert raised.value.code == 1


# ── planted red ──────────────────────────────────────────────────────────────


def test_even_a_reinstated_absence_marker_has_no_authority(neotoma, monkeypatch):
    """The library marker has no tools even if a caller wrongly returns it on failure."""
    real_load = AgentLoader.load
    if not hasattr(AgentLoader, "_undefined_definition"):
        pytest.skip("nothing to reinstate")

    def reinstated(self, *, allow_undefined=False):
        try:
            return real_load(self, allow_undefined=allow_undefined)
        except AgentDefinitionUnavailable as exc:
            return self._undefined_definition(getattr(exc, "code", "unexpected"))

    monkeypatch.setattr(AgentLoader, "load", reinstated)
    neotoma["script"] = ["timeout"]

    d = AgentLoader("pavo").load()

    assert d.is_stub and not d.prompt_markdown and not d.tools
    assert al.evaluate_status(d.status)[0] is al.StatusAction.REFUSE


# ── authoritative absence is narrow: everything else is unavailable ──────────



def _response(status=200, body=None, text=None):
    request = httpx.Request("POST", "http://neotoma.invalid/entities/query")
    if text is not None:
        return httpx.Response(status, request=request, text=text)
    return httpx.Response(status, request=request, content=_json.dumps(body).encode())


def _entity(name="pavo", **snap):
    snapshot = {"name": name, "prompt_markdown": "# the real role prompt",
                "tool_allowlist": ["Read"], "status": "active"}
    snapshot.update(snap)
    return {"entity_id": f"ent_{name}", "snapshot": snapshot}


NEGATIVE_CONTROLS = {
    # the route or transport answered, not the lookup
    "route 404": lambda: _response(404, {"error": "not found"}),
    "route 404 empty": lambda: _response(404, {"entities": []}),
    "400": lambda: _response(400, {"entities": []}),
    "401": lambda: _response(401, {}),
    "403": lambda: _response(403, {"entities": []}),
    "301 redirect": lambda: _response(301, {"entities": []}),
    "302 redirect": lambda: _response(302, {}),
    "204 no body": lambda: _response(204, text=""),
    # wrong envelope shape or types
    "list body": lambda: _response(200, []),
    "string body": lambda: _response(200, "ok"),
    "null body": lambda: _response(200, None),
    "not json": lambda: _response(200, text="<html>gateway</html>"),
    "missing entities": lambda: _response(200, {}),
    "renamed field": lambda: _response(200, {"results": []}),
    "entities null": lambda: _response(200, {"entities": None}),
    "entities object": lambda: _response(200, {"entities": {}}),
    "entities string": lambda: _response(200, {"entities": "[]"}),
    # error-shaped envelopes
    "error with empty list": lambda: _response(200, {"entities": [], "error": "boom"}),
    "error code only": lambda: _response(200, {"error_code": "ERR_X", "message": "no"}),
    "errors list": lambda: _response(200, {"entities": [], "errors": ["x"]}),
    # a partial page cannot claim absence
    "partial: total says 3": lambda: _response(200, {"entities": [], "total": 3}),
    "partial: has_more": lambda: _response(200, {"entities": [], "has_more": True}),
    "partial: next cursor": lambda: _response(200, {"entities": [], "next_cursor": "abc"}),
    "total mistyped": lambda: _response(200, {"entities": [], "total": "0"}),
    # results that are not this agent's definition
    "different name": lambda: _response(200, {"entities": [_entity("someone-else")]}),
    "several different names": lambda: _response(200, {"entities": [_entity("a"), _entity("b")]}),
    "conflicting duplicates": lambda: _response(
        200, {"entities": [_entity("pavo"), {**_entity("pavo"), "entity_id": "ent_other"}]}),
    "same entity twice": lambda: _response(200, {"entities": [_entity("pavo"), _entity("pavo")]}),
    # a malformed "found"
    "entry not an object": lambda: _response(200, {"entities": ["pavo"]}),
    "entry without id": lambda: _response(200, {"entities": [{"snapshot": {"name": "pavo"}}]}),
    "entry id mistyped": lambda: _response(200, {"entities": [{"entity_id": 7, "snapshot": {"name": "pavo"}}]}),
    "snapshot not an object": lambda: _response(200, {"entities": [{"entity_id": "e", "snapshot": "x"}]}),
    "snapshot missing": lambda: _response(200, {"entities": [{"entity_id": "e"}]}),
    "name missing": lambda: _response(200, {"entities": [{"entity_id": "e", "snapshot": {"prompt_markdown": "p"}}]}),
    "name mistyped": lambda: _response(200, {"entities": [_entity(name=5)]}),
    "prompt mistyped": lambda: _response(200, {"entities": [_entity(prompt_markdown=5)]}),
    "allowlist object": lambda: _response(200, {"entities": [_entity(tool_allowlist={"a": 1})]}),
    "allowlist item mistyped": lambda: _response(200, {"entities": [_entity(tool_allowlist=["Read", 3])]}),
    "status mistyped": lambda: _response(200, {"entities": [_entity(status=["active"])]}),
    "provenance mistyped": lambda: _response(200, {"entities": [_entity(provenance="x")]}),
    "good entry beside a malformed one": lambda: _response(200, {"entities": [_entity("pavo"), "junk"]}),
}


@pytest.fixture
def lookup(monkeypatch):
    state = {"make": None, "calls": 0}
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(al, "_sleep", lambda s: None, raising=False)

    def post(url, **kwargs):
        state["calls"] += 1
        return state["make"]()

    monkeypatch.setattr(al.httpx, "post", post)
    return state


@pytest.mark.parametrize("name", sorted(NEGATIVE_CONTROLS))
def test_a_negative_control_is_unavailable_and_never_a_stand_in(lookup, name):
    lookup["make"] = NEGATIVE_CONTROLS[name]
    for opt_in in (False, True):
        with pytest.raises(AgentDefinitionUnavailable) as raised:
            AgentLoader("pavo").load(allow_undefined=opt_in)
        assert raised.value.code != "not_found", f"{name} was read as authoritative absence"


@pytest.mark.parametrize("name", ["", "   "])
def test_an_empty_name_is_refused_before_any_lookup(lookup, name):
    lookup["make"] = lambda: _response(200, {"entities": []})
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader(name).load(allow_undefined=True)
    assert raised.value.code == "invalid_name" and lookup["calls"] == 0


def test_positive_control_a_true_empty_200_is_absence_only_with_the_opt_in(lookup):
    lookup["make"] = lambda: _response(200, {"entities": []})
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()
    assert raised.value.code == "not_found"
    assert AgentLoader("pavo").load(allow_undefined=True).is_stub is True


def test_positive_control_an_empty_200_with_zero_total_is_absence(lookup):
    lookup["make"] = lambda: _response(200, {"entities": [], "total": 0, "has_more": False})
    assert AgentLoader("pavo").load(allow_undefined=True).load_error == "not_found"


def test_positive_control_a_well_formed_definition_loads(lookup):
    lookup["make"] = lambda: _response(200, {"entities": [_entity("pavo")]})
    d = AgentLoader("pavo").load()
    assert d.is_stub is False and d.entity_id == "ent_pavo"
    assert d.prompt_markdown == "# the real role prompt" and d.tools == ["Read"]


def test_positive_control_the_doubly_nested_snapshot_loads(lookup):
    nested = {"entity_id": "ent_pavo", "snapshot": {"snapshot": _entity("pavo")["snapshot"]}}
    lookup["make"] = lambda: _response(200, {"entities": [nested]})
    assert AgentLoader("pavo").load().entity_id == "ent_pavo"


def test_positive_control_the_search_may_return_other_agents_beside_it(lookup):
    lookup["make"] = lambda: _response(200, {"entities": [_entity("pavo-legacy"), _entity("pavo")]})
    assert AgentLoader("pavo").load().entity_id == "ent_pavo"


def test_positive_control_a_partial_page_that_contains_the_definition_loads(lookup):
    lookup["make"] = lambda: _response(200, {"entities": [_entity("pavo")], "total": 9})
    assert AgentLoader("pavo").load().entity_id == "ent_pavo"


def test_positive_control_null_optional_fields_take_their_defaults(lookup):
    lookup["make"] = lambda: _response(
        200, {"entities": [_entity("pavo", description=None)]})
    d = AgentLoader("pavo").load()
    assert d.description == "" and d.status == "active" and d.tools == ["Read"]


def test_planted_red_loosened_classification_accepts_the_negative_controls(lookup, monkeypatch):
    """Loosen it the way the defect was: treat any 404, any non-list `entities` and any
    unmatched result as absence. The table would then read these as absence."""
    real = AgentLoader._load_by_name

    def loose(self, deadline):
        try:
            return real(self, deadline)
        except Exception as exc:  # noqa: BLE001
            code = exc.code if isinstance(exc, AgentDefinitionUnavailable) else al.load_failure_code(exc)
            if code in ("bad_response", "rejected"):
                raise self._unavailable(al.NOT_FOUND) from None
            raise

    monkeypatch.setattr(AgentLoader, "_load_by_name", loose)
    reached_the_stand_in = []
    for name in ("route 404", "list body", "missing entities", "error with empty list",
                 "different name", "partial: total says 3"):
        lookup["make"] = NEGATIVE_CONTROLS[name]
        try:
            d = AgentLoader("pavo").load(allow_undefined=True)
        except AgentDefinitionUnavailable:
            continue
        if d.is_stub:
            reached_the_stand_in.append(name)
    assert reached_the_stand_in, "the loosened rule must let some control reach the stand-in"


# ── the deadline is real: requests are bounded, not only the waits ───────────
#
# The clock advances on REQUEST DURATION as well as on sleep. A request runs for the
# pattern's duration or for the timeout it was given, whichever is shorter.

CAP = 25.0
HANG = 10_000.0


class _VClock:
    def __init__(self):
        self.now = 500.0
        self.start = self.now
        self.launched = []  # (timeout, time left at launch)

    @property
    def elapsed(self):
        return self.now - self.start


def _virtual(monkeypatch, clock, durations, *, signed=False):
    calls = {"n": 0}

    def take(timeout):
        i = min(calls["n"], len(durations) - 1)
        calls["n"] += 1
        clock.launched.append((timeout, CAP - clock.elapsed))
        clock.now += min(durations[i], timeout)
        return durations[i] >= timeout

    def post(url, json=None, headers=None, timeout=None):
        timeout = timeout.read if isinstance(timeout, httpx.Timeout) else timeout
        if take(timeout):
            raise httpx.ReadTimeout("timed out")
        return _response(200, {"entities": []})

    def signed_request(method, url, body=None, agent_name="", timeout=20, *, sub=None, **kwargs):
        if take(timeout):
            raise RuntimeError("helper timed out")
        return 200, {"entities": []}

    monkeypatch.setattr(al, "_monotonic", lambda: clock.now)
    monkeypatch.setattr(al, "_sleep", lambda s: setattr(clock, "now", clock.now + s), raising=False)
    monkeypatch.setattr(al.httpx, "post", post)
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: signed)
    monkeypatch.setattr(al.ns, "agent_identity", lambda name, sub=None: {"key": "k"} if signed else None)
    monkeypatch.setattr(al.ns, "signed_request", signed_request)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")


LOAD_PATTERNS = {
    "first hangs": [HANG, 0.1],
    "second hangs": [0.1, HANG, 0.1],
    "all hang": [HANG],
    "slow then hang": [9.0, HANG],
    "third hangs": [HANG, HANG, 0.1],
    "fast server errors": [0.1],
}


@pytest.mark.parametrize("signed", [False, True], ids=["bearer", "signed+fallback"])
@pytest.mark.parametrize("name", sorted(LOAD_PATTERNS))
def test_a_load_never_outlasts_the_cap(monkeypatch, name, signed):
    clock = _VClock()
    _virtual(monkeypatch, clock, LOAD_PATTERNS[name], signed=signed)

    try:
        AgentLoader("pavo").load()
    except AgentDefinitionUnavailable:
        pass

    assert clock.elapsed <= CAP + 1e-9, f"{name}: {clock.elapsed}s"
    for timeout, left in clock.launched:
        assert timeout <= left + 1e-9, "a request was launched with more time than was left"


def test_all_hang_uses_the_whole_budget_and_not_a_second_more(monkeypatch):
    clock = _VClock()
    _virtual(monkeypatch, clock, [HANG])

    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()

    assert raised.value.code == "timeout"
    assert [t for t, _ in clock.launched] == [10.0, 10.0, 3.0]
    assert clock.elapsed == pytest.approx(25.0)


def test_a_retry_that_cannot_fit_is_not_started(monkeypatch):
    clock = _VClock()
    _virtual(monkeypatch, clock, [HANG])
    monkeypatch.setattr(al, "LOAD_TOTAL_CAP_S", 12.0)

    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()

    # 10 s used; a retry needs 0.5 s of backoff plus the 2 s minimum: 12.5 > 12
    assert raised.value.attempts == 1 and len(clock.launched) == 1


def test_the_signed_request_timeout_refuses_without_bearer_fallback(monkeypatch):
    clock = _VClock()
    _virtual(monkeypatch, clock, [HANG], signed=True)

    with pytest.raises(AgentDefinitionUnavailable):
        AgentLoader("pavo").load()

    # A timed-out helper may have received a response, so no bearer request starts.
    assert clock.launched[0] == (20.0, 25.0)
    assert len(clock.launched) == 1
    assert clock.elapsed <= CAP + 1e-9


def test_a_request_that_trickles_is_cut_at_its_budget_in_real_time(monkeypatch):
    import time as _time

    monkeypatch.setattr(al, "BEARER_REQUEST_TIMEOUT_S", 0.2)
    monkeypatch.setattr(al, "LOAD_TOTAL_CAP_S", 1.0)
    monkeypatch.setattr(al, "MIN_ATTEMPT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(al, "LOAD_BACKOFFS_S", (0.02, 0.02))
    monkeypatch.setattr(al, "_monotonic", _time.monotonic)
    monkeypatch.setattr(al, "_sleep", _time.sleep, raising=False)
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(al.httpx, "post", lambda *a, **k: _time.sleep(0.6))
    t0 = _time.monotonic()

    with pytest.raises(AgentDefinitionUnavailable):
        AgentLoader("pavo").load()

    assert _time.monotonic() - t0 < 1.6


def test_planted_red_the_old_check_overruns_the_cap(monkeypatch):
    """The previous rule: full default timeouts, and a retry check of elapsed + wait only."""
    clock = _VClock()
    _virtual(monkeypatch, clock, [HANG])
    monkeypatch.setattr(al, "_on_worker", lambda fn, deadline: fn(10_000.0))
    monkeypatch.setattr(al, "MIN_ATTEMPT_TIMEOUT_S", 0.0)

    with pytest.raises(AgentDefinitionUnavailable):
        AgentLoader("pavo").load()

    assert clock.elapsed > CAP, "the old rule is the defect: three 10 s failures took 32 s"


# ── the TRANSPORT RESULT contract: both request paths, before any body is read ─

SUCCESS = {"entities": [_entity("pavo")]}  # a body that WOULD load if it were accepted


class _Obj:
    """A response-like object with whatever fields a test sets."""

    def __init__(self, status_code=200, body=None, content_type=None):
        self.status_code = status_code
        self._body = SUCCESS if body is None else body
        self.headers = {"content-type": content_type} if content_type else {}

    def raise_for_status(self):
        return None  # a lenient transport that never raises: the status must still be checked

    def json(self):
        return self._body


BEARER_RESULT_CONTROLS = {
    "201": lambda: _Obj(201),
    "202": lambda: _Obj(202),
    "204": lambda: _Obj(204),
    "206": lambda: _Obj(206),
    "301": lambda: _Obj(301),
    "302": lambda: _Obj(302),
    "304": lambda: _Obj(304),
    "400": lambda: _Obj(400),
    "401": lambda: _Obj(401),
    "403": lambda: _Obj(403),
    "404": lambda: _Obj(404),
    "410": lambda: _Obj(410),
    "429": lambda: _Obj(429),
    "500": lambda: _Obj(500),
    "503": lambda: _Obj(503),
    "status is a string": lambda: _Obj("200"),
    "status is a float": lambda: _Obj(200.0),
    "status is a bool": lambda: _Obj(True),
    "status is None": lambda: _Obj(None),
    "no status at all": lambda: object(),
    "html content type": lambda: _Obj(200, content_type="text/html"),
    "plain content type": lambda: _Obj(200, content_type="text/plain; charset=utf-8"),
    "an error object": lambda: {"error": "boom"},
    "a tuple": lambda: (200, SUCCESS),
    "None": lambda: None,
}

SIGNED_RESULT_CONTROLS = {
    "201": lambda: (201, SUCCESS),
    "202": lambda: (202, SUCCESS),
    "204": lambda: (204, {}),
    "302": lambda: (302, SUCCESS),
    "400": lambda: (400, SUCCESS),
    "401": lambda: (401, SUCCESS),
    "404": lambda: (404, SUCCESS),
    "429": lambda: (429, SUCCESS),
    "500": lambda: (500, SUCCESS),
    "status is a string": lambda: ("200", SUCCESS),
    "status is a float": lambda: (200.0, SUCCESS),
    "status is a bool": lambda: (True, SUCCESS),
    "status is None": lambda: (None, SUCCESS),
    "status 0": lambda: (0, SUCCESS),
    "one element": lambda: (200,),
    "three elements": lambda: (200, SUCCESS, "x"),
    "a dict": lambda: {"status": 200, "body": SUCCESS},
    "an error object": lambda: {"error": "boom"},
    "a string": lambda: "ok",
    "None": lambda: None,
    "a list body": lambda: (200, []),
    "a string body": lambda: (200, "ok"),
    "a null body": lambda: (200, None),
}


@pytest.fixture
def transport(monkeypatch):
    state = {"bearer": None, "signed": None, "bearer_calls": 0, "signed_calls": 0}
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(al, "_sleep", lambda s: None, raising=False)

    def post(url, **kwargs):
        state["bearer_calls"] += 1
        return state["bearer"]()

    def signed_request(method, url, body=None, agent_name="", timeout=20, *, sub=None, **kwargs):
        state["signed_calls"] += 1
        return state["signed"]()

    monkeypatch.setattr(al.httpx, "post", post)
    monkeypatch.setattr(al.ns, "signed_request", signed_request)
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(al.ns, "agent_identity", lambda name, sub=None: {"key": "k"})
    return state


@pytest.mark.parametrize("name", sorted(BEARER_RESULT_CONTROLS))
def test_a_bad_bearer_result_is_unavailable_even_when_its_body_would_load(transport, name):
    transport["bearer"] = BEARER_RESULT_CONTROLS[name]
    for opt_in in (False, True):
        with pytest.raises(AgentDefinitionUnavailable) as raised:
            AgentLoader("pavo").load(allow_undefined=opt_in)
        assert raised.value.code != "not_found"


@pytest.mark.parametrize("name", sorted(SIGNED_RESULT_CONTROLS))
def test_a_bad_signed_result_is_unavailable_and_does_not_fall_back_to_bearer(
    transport, monkeypatch, name
):
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: True)
    transport["signed"] = SIGNED_RESULT_CONTROLS[name]
    transport["bearer"] = lambda: _Obj(200)  # would load, if it were ever asked
    for opt_in in (False, True):
        with pytest.raises(AgentDefinitionUnavailable) as raised:
            AgentLoader("pavo").load(allow_undefined=opt_in)
        assert raised.value.code != "not_found"
    assert transport["bearer_calls"] == 0, "a returned signed result is final: no bearer fallback"


def test_an_unclassified_signed_helper_error_refuses_without_bearer(transport, monkeypatch):
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: True)

    def broken():
        raise RuntimeError("helper crashed")

    transport["signed"] = broken
    transport["bearer"] = lambda: _Obj(200)
    with pytest.raises(AgentDefinitionUnavailable):
        AgentLoader("pavo").load()
    assert transport["signed_calls"] == 1 and transport["bearer_calls"] == 0


def test_positive_controls_exactly_200_with_a_json_body_loads_on_both_paths(transport, monkeypatch):
    transport["bearer"] = lambda: _Obj(200, content_type="application/json")
    assert AgentLoader("pavo").load().entity_id == "ent_pavo"
    transport["bearer"] = lambda: _Obj(200)  # no content-type header: no assumption is made
    assert AgentLoader("pavo").load().entity_id == "ent_pavo"

    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: True)
    transport["signed"] = lambda: (200, SUCCESS)
    assert AgentLoader("pavo").load().entity_id == "ent_pavo"
    transport["signed"] = lambda: [200, SUCCESS]
    assert AgentLoader("pavo").load().entity_id == "ent_pavo"


def test_a_transient_returned_status_is_still_retried(transport, monkeypatch):
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: True)
    answers = [(503, {}), (200, SUCCESS)]
    transport["signed"] = lambda: answers.pop(0)
    assert AgentLoader("pavo").load().entity_id == "ent_pavo"
    assert transport["signed_calls"] == 2


def test_planted_red_accepting_any_2xx_lets_an_unvalidated_result_load(transport, monkeypatch):
    """Not checking the status exactly: a 201 or 204 carrying a definition-shaped body
    would be accepted as found."""
    monkeypatch.setattr(al, "SUCCESS_STATUS", 200)
    lenient_signed = lambda self, result: result[1] if 200 <= result[0] < 300 else self._raise(result[0])  # noqa: E731
    monkeypatch.setattr(AgentLoader, "_raise", lambda self, s: (_ for _ in ()).throw(al._TransportStatus(s)), raising=False)
    monkeypatch.setattr(AgentLoader, "_accept_signed", lenient_signed)
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: True)
    transport["signed"] = lambda: (201, SUCCESS)

    assert AgentLoader("pavo").load().entity_id == "ent_pavo", "the lenient rule accepts a 201"


# ── request workers: fixed in number, reclaimed, and idle before the deadline ─


def _slow_post(seconds):
    def post(url, **kwargs):
        _time_mod.sleep(seconds)
        return _response(200, {"entities": []})

    return post


import threading as _threading  # noqa: E402
import time as _time_mod  # noqa: E402
import concurrent.futures as _cf  # noqa: E402


def test_repeated_expirations_do_not_grow_the_workers_and_they_are_reclaimed(monkeypatch):
    """50 loads that each outlast their deadline: live threads stay inside the fixed pool
    and return to the baseline once the stuck requests finish."""
    monkeypatch.setattr(al, "LOAD_TOTAL_CAP_S", 0.12)
    monkeypatch.setattr(al, "MIN_ATTEMPT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(al, "LOAD_BACKOFFS_S", (0.0, 0.0))
    monkeypatch.setattr(al, "_monotonic", _time_mod.monotonic)
    monkeypatch.setattr(al, "_sleep", lambda s: None, raising=False)
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    pool = _cf.ThreadPoolExecutor(max_workers=3)
    monkeypatch.setattr(al, "_load_pool", pool)
    permits = _threading.BoundedSemaphore(3)
    monkeypatch.setattr(al, "_load_permits", permits)
    monkeypatch.setattr(al.httpx, "post", _slow_post(0.3))  # a request that outlasts the deadline
    before = _threading.active_count()
    peak = 0

    for _ in range(50):
        with pytest.raises(AgentDefinitionUnavailable):
            AgentLoader("pavo").load()
        peak = max(peak, _threading.active_count())

    assert peak <= before + 3, (before, peak)
    _time_mod.sleep(0.6)  # the stuck requests finish
    free = [permits.acquire(blocking=False) for _ in range(3)]
    assert all(free), "a permit was not returned when its worker finished"
    pool.shutdown(wait=True)
    assert _threading.active_count() <= before, "worker threads were not reclaimed"


def test_with_every_worker_busy_a_load_fails_at_once_without_a_request(monkeypatch):
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(al, "_sleep", lambda s: None, raising=False)
    permits = _threading.BoundedSemaphore(1)
    assert permits.acquire(blocking=False)
    monkeypatch.setattr(al, "_load_permits", permits)
    sent = []
    monkeypatch.setattr(al.httpx, "post", lambda *a, **k: sent.append(1))

    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()

    assert raised.value.code == "busy" and sent == []


def test_a_request_that_would_begin_after_the_deadline_is_never_sent(monkeypatch):
    class _LatePool:
        def __init__(self):
            self.real = _cf.ThreadPoolExecutor(max_workers=2)

        def submit(self, fn):
            def late():
                _time_mod.sleep(0.2)
                return fn()

            return self.real.submit(late)

    monkeypatch.setattr(al, "LOAD_TOTAL_CAP_S", 0.1)
    monkeypatch.setattr(al, "MIN_ATTEMPT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(al, "LOAD_BACKOFFS_S", (0.0, 0.0))
    monkeypatch.setattr(al, "_monotonic", _time_mod.monotonic)
    monkeypatch.setattr(al, "_sleep", lambda s: None, raising=False)
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(al, "_load_pool", _LatePool())
    monkeypatch.setattr(al, "_load_permits", _threading.BoundedSemaphore(2))
    sent = []
    monkeypatch.setattr(al.httpx, "post", lambda *a, **k: sent.append(1) or _response(200, {"entities": []}))
    t0 = _time_mod.monotonic()

    with pytest.raises(AgentDefinitionUnavailable):
        AgentLoader("pavo").load()

    held = _time_mod.monotonic() - t0
    _time_mod.sleep(0.5)
    assert sent == [], "a request that began after the deadline was sent"
    assert held < 0.6


def test_signed_identity_preparation_cannot_hold_the_loader_past_its_deadline(monkeypatch):
    """The signed preparation boundary owns key I/O; no synchronous preflight."""
    import time

    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: True)
    monkeypatch.setattr(al, "LOAD_TOTAL_CAP_S", 0.08)
    monkeypatch.setattr(al, "MIN_ATTEMPT_TIMEOUT_S", 0.01)
    monkeypatch.setattr(al, "LOAD_BACKOFFS_S", ())
    monkeypatch.setattr(al, "_monotonic", time.monotonic)
    identity_calls = []

    def stalled_identity(*args, **kwargs):
        identity_calls.append(True)
        time.sleep(0.35)
        return {"key": "synthetic"}

    monkeypatch.setattr(al.ns, "agent_identity", stalled_identity)
    monkeypatch.setattr(al.ns, "signed_request", lambda *a, **k: (200, REAL))
    started = time.monotonic()
    definition = AgentLoader("pavo").load()
    assert time.monotonic() - started < 0.25
    assert definition.entity_id == "ent_real"
    assert identity_calls == [], "identity preparation must be inside signed_request's bounded mechanism"


def _stalled_loader_preparation(sender, *args):
    # Synthetic key-read stall in the actual spawned preparation process.
    import time
    time.sleep(2)


def test_actual_signed_preparation_is_bounded_and_reaped_for_loader(monkeypatch):
    import multiprocessing
    import time

    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: True)
    monkeypatch.setattr(al.ns, "_prepare_signed_request", _stalled_loader_preparation)
    monkeypatch.setattr(al, "LOAD_TOTAL_CAP_S", 0.3)
    monkeypatch.setattr(al, "MIN_ATTEMPT_TIMEOUT_S", 0.01)
    monkeypatch.setattr(al, "LOAD_BACKOFFS_S", ())
    monkeypatch.setattr(al, "_monotonic", time.monotonic)
    monkeypatch.setattr(al.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic")))
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(AgentDefinitionUnavailable):
        AgentLoader("pavo").load()
    assert time.monotonic() - started < 1.0
    # The outer timeout and worker's child cleanup can race by a few scheduler ticks.
    limit = time.monotonic() + 1.0
    while time.monotonic() < limit and {c.pid for c in multiprocessing.active_children()} - before:
        time.sleep(0.01)
    assert {child.pid for child in multiprocessing.active_children()} <= before

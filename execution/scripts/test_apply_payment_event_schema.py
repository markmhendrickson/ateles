"""apply_payment_event_schema: dry-run diff, apply read-back, identity of the file.

All HTTP is a fake requester; no network.
"""

import copy
import json

import pytest

import apply_payment_event_schema as tool

DEF = tool.load_definition()


def _live_v1():
    """A synthetic stand-in for today's live schema: no identity, heuristic opt-out."""
    fields = {n: dict(s) for n, s in DEF["schema_definition"]["fields"].items()
              if n not in ("approval_id", "session_keys", "primary_session_key")}
    return {
        "entity_type": "payment_event",
        "schema_version": "1.0",
        "active": True,
        "schema_definition": {"fields": fields, "identity_opt_out": "heuristic_canonical_name"},
    }


def _live_matching():
    return {"entity_type": "payment_event", "schema_version": DEF["schema_version"],
            "active": True, "schema_definition": copy.deepcopy(DEF["schema_definition"])}


class Fake:
    def __init__(self, gets):
        self.gets = list(gets)
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "GET":
            response = self.gets.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {}


# --- the definition file -------------------------------------------------

def _identity_fields(rules):
    out = set()
    for r in rules:
        out |= set(r["composite"]) if isinstance(r, dict) else {r}
    return out


def test_definition_never_uses_btc_address_as_identity():
    sd = DEF["schema_definition"]
    rules = sd["canonical_name_fields"]
    assert "btc_address" not in _identity_fields(rules)
    for rule in rules:
        assert isinstance(rule, dict) and "payment_date" in rule["composite"]
        assert rule["composite"] != ["btc_address"]
    assert "identity_opt_out" not in sd
    assert sd["canonical_name_strict"] is True
    assert rules[0] == {"composite": ["approval_id", "payment_date"]}


def test_definition_keeps_existing_fields_and_adds_the_new_ones():
    fields = DEF["schema_definition"]["fields"]
    for name in ("label", "payment_type", "amount_eur", "amount_btc", "btc_address", "txid",
                 "status", "payment_date", "memo_policy", "network", "fee_sats", "notes"):
        assert name in fields
    assert fields["approval_id"]["type"] == "string"
    assert fields["session_keys"]["type"] == "array"
    assert set(DEF["reducer_config"]["merge_policies"]) == set(fields)


# --- dry run ---------------------------------------------------------------

def test_dry_run_prints_a_field_level_diff_and_posts_nothing(capsys):
    live = _live_v1()
    live["schema_definition"]["fields"]["notes"]["example_value"] = "SYNTHETIC-SECRET-VALUE"
    fake = Fake([live])
    assert tool.run(False, fake, DEF) == 0
    out = capsys.readouterr().out
    assert [c[0] for c in fake.calls] == ["GET"]
    for added in ("approval_id", "session_keys", "primary_session_key"):
        assert f"  + {added}" in out
    assert "heuristic_canonical_name" in out and "approval_id" in out
    assert "DRY RUN" in out
    assert "SYNTHETIC-SECRET-VALUE" not in out


def test_diff_reports_type_changes_and_removed_fields():
    live = _live_v1()
    live["schema_definition"]["fields"]["amount_eur"]["type"] = "string"
    live["schema_definition"]["fields"]["legacy_only"] = {"type": "string"}
    d = tool.diff(live, DEF)
    assert d["type_changed"] == {"amount_eur": {"live": "string", "file": "number"}}
    assert d["removed"] == ["legacy_only"]


def test_real_404_means_absent_and_dry_run_still_works(capsys):
    fake = Fake([tool.neotoma_http.NeotomaRequestError(404, "not found")])
    assert tool.run(False, fake, DEF) == 0
    assert [method for method, _, _ in fake.calls] == ["GET"]
    assert "DRY RUN" in capsys.readouterr().out


@pytest.mark.parametrize("response,problem", [
    (["unexpected"], "response body is not an object"),
    ("unexpected", "response body is not an object"),
    ({"schema": "x"}, "response schema is not an object"),
])
def test_apply_refuses_unreadable_initial_read(response, problem, capsys):
    fake = Fake([response])
    assert tool.run(True, fake, DEF) == 1
    assert [method for method, _, _ in fake.calls] == ["GET"]
    error = capsys.readouterr().err
    assert "REFUSED" in error and problem in error
    assert "unexpected" not in error and "'x'" not in error


# --- apply -----------------------------------------------------------------

def test_apply_posts_then_reads_back_and_passes_when_it_matches():
    fake = Fake([_live_v1(), _live_matching()])
    assert tool.run(True, fake, DEF) == 0
    assert [(m, p) for m, p, _ in fake.calls] == [
        ("GET", "/schemas/payment_event"), ("POST", "/register_schema"),
        ("GET", "/schemas/payment_event")]
    body = fake.calls[1][2]
    assert body["activate"] is True and body["schema_definition"] == DEF["schema_definition"]


def test_apply_read_back_mismatch_exits_non_zero(capsys):
    # Server accepted the POST but the live schema still carries the old identity.
    stale = _live_matching()
    stale["schema_definition"].pop("canonical_name_fields")
    stale["schema_definition"]["identity_opt_out"] = "heuristic_canonical_name"
    assert tool.run(True, Fake([_live_v1(), stale]), DEF) == 1
    assert "identity rule differs" in capsys.readouterr().err

    missing = _live_matching()
    missing["schema_definition"]["fields"].pop("session_keys")
    assert tool.run(True, Fake([_live_v1(), missing]), DEF) == 1

    inactive = {**_live_matching(), "active": False}
    assert tool.run(True, Fake([_live_v1(), inactive]), DEF) == 1


@pytest.mark.parametrize("response,problem", [
    (["unexpected"], "response body is not an object"),
    ("unexpected", "response body is not an object"),
    ({"schema": "x"}, "response schema is not an object"),
])
def test_apply_refuses_unreadable_read_back(response, problem, capsys):
    fake = Fake([_live_v1(), response])
    assert tool.run(True, fake, DEF) == 1
    assert [method for method, _, _ in fake.calls] == ["GET", "POST", "GET"]
    error = capsys.readouterr().err
    assert "READ-BACK FAILED" in error and problem in error
    assert "unexpected" not in error and "'x'" not in error


def test_verify_reports_every_extra_live_field_and_version_mismatch():
    live = _live_matching()
    live["schema_definition"]["fields"]["extra_one"] = {"type": "string"}
    live["schema_definition"]["fields"]["extra_two"] = {"type": "number"}
    live["schema_version"] = "different-version"
    problems = tool.verify(live, DEF)
    assert any("extra_one" in problem for problem in problems)
    assert any("extra_two" in problem for problem in problems)
    assert any("version" in problem and "different-version" in problem for problem in problems)
    assert tool.verify(_live_matching(), DEF) == []


def test_apply_is_a_no_op_when_live_already_matches():
    fake = Fake([_live_matching()])
    assert tool.run(True, fake, DEF) == 0
    assert [c[0] for c in fake.calls] == ["GET"]


def test_apply_refuses_to_change_an_existing_type_or_reuse_a_version(capsys):
    live = _live_v1()
    live["schema_definition"]["fields"]["fee_sats"]["type"] = "string"
    fake = Fake([live])
    assert tool.run(True, fake, DEF) == 1
    assert [c[0] for c in fake.calls] == ["GET"]

    same_version = {**_live_v1(), "schema_version": DEF["schema_version"]}
    fake = Fake([same_version])
    assert tool.run(True, fake, DEF) == 1
    assert [c[0] for c in fake.calls] == ["GET"]


def test_main_refuses_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    assert tool.main([]) == 2


def test_main_never_prints_the_token(monkeypatch, capsys):
    token = "tok" + "-synthetic-0000"
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.net")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", token)
    monkeypatch.setattr(tool, "run", lambda apply_change: 0)
    assert tool.main(["--apply"]) == 0
    out = capsys.readouterr()
    assert "https://neotoma.example.net" in out.out and token not in out.out + out.err


def test_apply_refuses_a_matching_but_inactive_schema(capsys):
    fake = Fake([{**_live_matching(), "active": False}])
    assert tool.run(True, fake, DEF) == 1
    assert [c[0] for c in fake.calls] == ["GET"]
    assert "not active" in capsys.readouterr().err

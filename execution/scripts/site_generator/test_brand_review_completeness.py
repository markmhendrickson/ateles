"""Effect tests for schema-owned brand-review completeness."""

from __future__ import annotations

import copy
import json
import sys
from datetime import date
from pathlib import Path

import pytest

GEN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(GEN_DIR))

from brand_review_completeness import (  # noqa: E402
    COMPLETE,
    INCOMPLETE,
    VALIDATION_UNAVAILABLE,
    required_deliverable_ids,
    review_contract,
    review_paths,
)

TODAY = date(2026, 9, 23)


@pytest.fixture()
def schema() -> dict:
    return json.loads((GEN_DIR / "brand_systems/schema.v1.json").read_text())


@pytest.fixture()
def valid() -> dict:
    return json.loads(
        (GEN_DIR / "fixtures/brand_review/valid-provisional.json").read_text()
    )


def _item(contract: dict, deliverable_id: str = "cinematic.inputs") -> dict:
    return next(
        item
        for item in contract["completeness"]["deliverables"]
        if item["id"] == deliverable_id
    )


def test_valid_provisional_contract_passes_four_proof_rule(schema, valid):
    report = review_contract(schema, valid, today=TODAY)
    assert report.status == COMPLETE
    assert report.findings == ()


def test_awaiting_approval_with_exactly_three_proofs_fails(schema):
    contract = json.loads(
        (
            GEN_DIR
            / "fixtures/brand_review/awaiting-approval-missing-evidence.json"
        ).read_text()
    )
    report = review_contract(schema, contract, today=TODAY)
    assert report.status == INCOMPLETE
    assert [(item.deliverable_id, item.reason_code) for item in report.findings] == [
        ("cinematic.inputs", "MISSING_VALIDATION_EVIDENCE")
    ]


@pytest.mark.parametrize(
    ("reason", "mutate"),
    [
        (
            "MISSING_ARTIFACT",
            lambda contract, item: item.update(artifact={"kind": "structured_value"}),
        ),
        (
            "STALE_VALIDATION",
            lambda contract, item: item["validation"].update(checked_at="2020-01-01"),
        ),
        (
            "PLACEHOLDER_ONLY",
            lambda contract, item: (
                contract.update(_test_placeholder=None),
                item["artifact"].update(locators=["/_test_placeholder"]),
            ),
        ),
        (
            "OBSOLETE_SOURCE",
            lambda contract, item: (
                contract.update(_test_obsolete={"status": "retired"}),
                item["source"].update(locator="/_test_obsolete"),
            ),
        ),
        (
            "DESCRIBED_NOT_RENDERED",
            lambda contract, item: item["render"].update(proofs=[]),
        ),
        (
            "STATUS_ONLY",
            lambda contract, item: (
                contract.update(_test_status="provisional"),
                item["artifact"].update(locators=["/_test_status"]),
            ),
        ),
        ("MISSING_SOURCE", lambda contract, item: item.update(source={})),
        ("MISSING_RENDER", lambda contract, item: item.update(render={})),
        (
            "MISSING_VALIDATION_EVIDENCE",
            lambda contract, item: item.update(validation={}),
        ),
        (
            "APPROVAL_BLOCK_NOT_RENDERED",
            lambda contract, item: item["render"].update(
                proofs=[
                    proof
                    for proof in item["render"]["proofs"]
                    if proof != "approval-blocked"
                ]
            ),
        ),
    ],
)
def test_each_reason_code_is_reachable(schema, valid, reason, mutate):
    contract = copy.deepcopy(valid)
    mutate(contract, _item(contract))
    report = review_contract(schema, contract, today=TODAY)
    assert reason in {item.reason_code for item in report.findings}


def test_findings_are_fully_aggregated_and_ordered(schema, valid):
    contract = copy.deepcopy(valid)
    for deliverable_id in ("visual.motion", "rationale.category"):
        contract["completeness"]["deliverables"] = [
            item
            for item in contract["completeness"]["deliverables"]
            if item["id"] != deliverable_id
        ]
    report = review_contract(schema, contract, today=TODAY)
    assert [
        (item.product, item.deliverable_id, item.reason_code)
        for item in report.findings
    ] == sorted(
        (item.product, item.deliverable_id, item.reason_code)
        for item in report.findings
    )


def test_schema_and_both_products_have_identical_deliverable_ids(schema):
    expected = set(required_deliverable_ids(schema))
    for product in ("ateles", "neotoma"):
        contract = json.loads(
            (GEN_DIR / f"brand_systems/{product}.json").read_text()
        )
        assert {
            item["id"] for item in contract["completeness"]["deliverables"]
        } == expected


def test_removing_one_required_deliverable_changes_complete_to_incomplete(schema, valid):
    assert review_contract(schema, valid, today=TODAY).status == COMPLETE
    contract = copy.deepcopy(valid)
    contract["completeness"]["deliverables"].pop()
    report = review_contract(schema, contract, today=TODAY)
    assert report.status == INCOMPLETE
    assert report.findings[0].reason_code == "MISSING_ARTIFACT"


def test_unreadable_schema_or_source_is_validation_unavailable(tmp_path, valid):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text("{")
    contract_path = tmp_path / "ateles.json"
    contract_path.write_text(json.dumps(valid))
    report = review_paths(schema_path, [contract_path], today=TODAY)[0]
    assert report.status == VALIDATION_UNAVAILABLE

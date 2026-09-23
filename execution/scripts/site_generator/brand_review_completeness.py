#!/usr/bin/env python3
"""Derive brand-review completeness from schema-owned deliverable requirements."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"
VALIDATION_UNAVAILABLE = "VALIDATION_UNAVAILABLE"
FOUR_PROOFS = (
    "concrete artifact",
    "traceable source",
    "rendered specimen",
    "current validation evidence",
)
PLACEHOLDERS = {"", "none", "null", "missing", "not produced", "placeholder", "tbd"}


@dataclass(frozen=True)
class ReviewFinding:
    product: str
    deliverable_id: str
    reason_code: str
    observed: str
    fix_owner: str
    fix: str

    @property
    def review_path(self) -> str:
        anchor = self.deliverable_id.replace(".", "-").replace("_", "-")
        return f"/brand/#review-{anchor}"

    def format(self) -> str:
        return "\n".join(
            (
                f"[{self.reason_code}] deliverable={self.deliverable_id}",
                f"observed={self.observed}",
                "required=concrete artifact + traceable source + rendered specimen + current validation evidence",
                f"fix_owner={self.fix_owner}",
                f"fix={self.fix}",
                "verify=python3 execution/scripts/site_generator/render_brand_systems.py --check",
                f"review={self.review_path}",
            )
        )


@dataclass(frozen=True)
class ReviewReport:
    product: str
    status: str
    findings: tuple[ReviewFinding, ...] = ()
    error: str | None = None

    def format(self) -> str:
        if self.status == VALIDATION_UNAVAILABLE:
            return (
                f"BRAND_REVIEW_VALIDATION_UNAVAILABLE product={self.product} "
                f"error={self.error or 'unknown validation error'}"
            )
        header = (
            f"BRAND_REVIEW_{self.status} product={self.product} "
            f"failures={len(self.findings)}"
        )
        return "\n".join((header, *(finding.format() for finding in self.findings)))


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def required_deliverable_ids(schema: dict) -> tuple[str, ...]:
    requirements = schema.get("x-review-deliverables")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("schema has no x-review-deliverables requirements")
    ids = tuple(item.get("id") for item in requirements if isinstance(item, dict))
    if len(ids) != len(requirements) or any(not item for item in ids):
        raise ValueError("schema x-review-deliverables contains an invalid id")
    if len(ids) != len(set(ids)):
        raise ValueError("schema x-review-deliverables contains duplicate ids")
    return ids


def _resolve(document: object, locator: object) -> object:
    if not isinstance(locator, str) or not locator.startswith("/"):
        raise KeyError(str(locator))
    value = document
    for token in locator.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value[token]
        elif isinstance(value, list):
            value = value[int(token)]
        else:
            raise KeyError(locator)
    return value


def _concrete(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in PLACEHOLDERS
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _finding(
    product: str,
    deliverable_id: str,
    reason: str,
    observed: str,
    requirement: dict,
) -> ReviewFinding:
    return ReviewFinding(
        product=product,
        deliverable_id=deliverable_id,
        reason_code=reason,
        observed=observed,
        fix_owner=str(requirement.get("fix_owner") or "brand owner"),
        fix=str(
            requirement.get("fix")
            or "add the missing proof to the canonical brand contract and rerender"
        ),
    )


def _artifact_findings(
    product: str, contract: dict, item: dict, requirement: dict
) -> Iterable[ReviewFinding]:
    deliverable_id = requirement["id"]
    artifact = item.get("artifact")
    if not isinstance(artifact, dict) or not artifact.get("locators"):
        yield _finding(
            product, deliverable_id, "MISSING_ARTIFACT", "no artifact locators", requirement
        )
        return
    locators = artifact["locators"]
    values: list[object] = []
    missing: list[str] = []
    for locator in locators:
        try:
            values.append(_resolve(contract, locator))
        except (KeyError, IndexError, TypeError, ValueError):
            missing.append(str(locator))
    if missing:
        yield _finding(
            product,
            deliverable_id,
            "MISSING_ARTIFACT",
            f"artifact locators do not resolve: {', '.join(missing)}",
            requirement,
        )
        return
    if all(
        isinstance(value, str)
        and value.strip().casefold() in {"approved", "provisional", "awaiting_approval"}
        for value in values
    ):
        yield _finding(
            product,
            deliverable_id,
            "STATUS_ONLY",
            "artifact resolves only to lifecycle/status prose",
            requirement,
        )
    elif not any(_concrete(value) for value in values):
        yield _finding(
            product,
            deliverable_id,
            "PLACEHOLDER_ONLY",
            "artifact values are empty, missing, or placeholder-only",
            requirement,
        )


def _proof_findings(
    product: str,
    contract: dict,
    item: dict,
    requirement: dict,
    today: date,
) -> Iterable[ReviewFinding]:
    deliverable_id = requirement["id"]
    source = item.get("source")
    if not isinstance(source, dict) or not all(
        source.get(field) for field in ("owner", "kind", "locator", "revision", "label")
    ):
        yield _finding(
            product,
            deliverable_id,
            "MISSING_SOURCE",
            "source owner/kind/locator/revision/label is incomplete",
            requirement,
        )
    else:
        try:
            source_value = _resolve(contract, source["locator"])
        except (KeyError, IndexError, TypeError, ValueError):
            source_value = None
        if not _concrete(source_value):
            yield _finding(
                product,
                deliverable_id,
                "MISSING_SOURCE",
                f"source locator does not resolve: {source.get('locator')}",
                requirement,
            )
        elif (
            isinstance(source_value, dict)
            and str(source_value.get("status", "")).casefold() == "retired"
        ):
            yield _finding(
                product,
                deliverable_id,
                "OBSOLETE_SOURCE",
                "source resolves to a retired artifact",
                requirement,
            )

    render = item.get("render")
    required_proofs = set(requirement.get("render_proofs") or ())
    if not isinstance(render, dict) or not render.get("route") or not render.get("anchor"):
        yield _finding(
            product,
            deliverable_id,
            "MISSING_RENDER",
            "render route or review anchor is missing",
            requirement,
        )
    elif not render.get("proofs"):
        yield _finding(
            product,
            deliverable_id,
            "DESCRIBED_NOT_RENDERED",
            "deliverable is described but has no rendered proof marker",
            requirement,
        )
    elif not required_proofs.issubset(set(render.get("proofs") or ())):
        yield _finding(
            product,
            deliverable_id,
            "MISSING_RENDER",
            "required render proof markers are absent",
            requirement,
        )

    validation = item.get("validation")
    required_checks = set(requirement.get("required_checks") or ())
    if not isinstance(validation, dict) or not validation.get("checked_at"):
        yield _finding(
            product,
            deliverable_id,
            "MISSING_VALIDATION_EVIDENCE",
            "validation date or checks are absent",
            requirement,
        )
    else:
        checks = set(validation.get("checks") or ())
        if not required_checks.issubset(checks):
            yield _finding(
                product,
                deliverable_id,
                "MISSING_VALIDATION_EVIDENCE",
                "validation checks do not cover the schema requirement",
                requirement,
            )
        try:
            checked_at = datetime.strptime(validation["checked_at"], "%Y-%m-%d").date()
            max_age = int(requirement.get("max_age_days") or validation["max_age_days"])
        except (KeyError, TypeError, ValueError):
            yield _finding(
                product,
                deliverable_id,
                "MISSING_VALIDATION_EVIDENCE",
                "validation date or max age is invalid",
                requirement,
            )
        else:
            if (today - checked_at).days > max_age:
                yield _finding(
                    product,
                    deliverable_id,
                    "STALE_VALIDATION",
                    f"checked_at={checked_at.isoformat()} max_age_days={max_age}",
                    requirement,
                )

    if item.get("lifecycle_state") == "awaiting_approval":
        block = item.get("approval_block")
        if not isinstance(block, dict) or not all(
            block.get(field) for field in ("required", "reason", "owner", "next_action")
        ):
            yield _finding(
                product,
                deliverable_id,
                "APPROVAL_BLOCK_NOT_RENDERED",
                "awaiting-approval deliverable lacks an explicit approval block",
                requirement,
            )
        elif "approval-blocked" not in set((render or {}).get("proofs") or ()):
            yield _finding(
                product,
                deliverable_id,
                "APPROVAL_BLOCK_NOT_RENDERED",
                "approval block is not represented by a render proof",
                requirement,
            )


def review_contract(
    schema: dict,
    contract: dict,
    *,
    today: date | None = None,
    product: str | None = None,
) -> ReviewReport:
    product = product or str(contract.get("slug") or contract.get("product") or "unknown")
    try:
        requirements = schema["x-review-deliverables"]
        required_deliverable_ids(schema)
        deliverables = (contract.get("completeness") or {}).get("deliverables")
        if not isinstance(deliverables, list):
            deliverables = []
        by_id = {
            item.get("id"): item
            for item in deliverables
            if isinstance(item, dict) and item.get("id")
        }
        findings: list[ReviewFinding] = []
        for requirement in requirements:
            deliverable_id = requirement["id"]
            item = by_id.get(deliverable_id)
            if item is None:
                findings.append(
                    _finding(
                        product,
                        deliverable_id,
                        "MISSING_ARTIFACT",
                        "no completeness deliverable entry",
                        requirement,
                    )
                )
                continue
            findings.extend(_artifact_findings(product, contract, item, requirement))
            findings.extend(
                _proof_findings(
                    product, contract, item, requirement, today or date.today()
                )
            )
        ordered = tuple(
            sorted(
                findings,
                key=lambda item: (item.product, item.deliverable_id, item.reason_code),
            )
        )
        return ReviewReport(
            product=product,
            status=INCOMPLETE if ordered else COMPLETE,
            findings=ordered,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return ReviewReport(product=product, status=VALIDATION_UNAVAILABLE, error=str(exc))


def review_paths(
    schema_path: Path, contract_paths: Iterable[Path], *, today: date | None = None
) -> tuple[ReviewReport, ...]:
    try:
        schema = load_json(schema_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return tuple(
            ReviewReport(
                product=path.stem, status=VALIDATION_UNAVAILABLE, error=str(exc)
            )
            for path in contract_paths
        )
    reports = []
    for path in contract_paths:
        try:
            contract = load_json(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            reports.append(
                ReviewReport(
                    product=path.stem, status=VALIDATION_UNAVAILABLE, error=str(exc)
                )
            )
        else:
            reports.append(
                review_contract(schema, contract, today=today, product=path.stem)
            )
    return tuple(reports)

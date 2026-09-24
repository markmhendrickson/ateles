"""Broken AD-21..26<->five-rules mapping -> AD-31 red; sixth doc missing a required part -> AD-32 red."""

from __future__ import annotations

from execution.conformance.adapter_admission import instruments
from execution.conformance.adapter_admission.instruments import (
    OBLIGATION_TO_FIVE_RULES_ROW,
    REQUIRED_DOCUMENT_PARTS,
    check_ad31,
    check_ad32,
)


def test_ad31_green_on_intact_mapping():
    assert check_ad31() is False


def test_ad31_red_on_broken_mapping(monkeypatch):
    broken = dict(OBLIGATION_TO_FIVE_RULES_ROW)
    del broken["AD-24"]  # simulate a gap: obligation 4 has no five-rules row behind it
    monkeypatch.setattr(instruments, "OBLIGATION_TO_FIVE_RULES_ROW", broken)
    assert check_ad31() is True


def test_ad32_green_on_intact_document():
    document_text = instruments.default_sixth_document_path().read_text()
    assert check_ad32(document_text) is False


def test_ad32_red_when_required_part_missing():
    document_text = instruments.default_sixth_document_path().read_text()
    broken = document_text.replace("## Recoveries", "## Not A Required Part")
    assert "Recoveries" not in broken
    assert check_ad32(broken) is True


def test_ad32_names_all_eight_parts():
    assert len(REQUIRED_DOCUMENT_PARTS) == 8

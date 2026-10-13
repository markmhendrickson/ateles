"""Turn the labelled set into replayable run units.

``dataset.json`` holds one record per finding or per head. A *run unit* is one
lens run on one head (with its patches): several finding records on the same
head and lens share one run, and a head labelled for "all" lenses fans out to
each lens. The unit keeps every record it covers so the scorer can match the
model's findings against all of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

LENSES = ("pm", "qa", "ux", "arch", "security")
FINDING_KINDS = ("confirmed_blocker", "false_blocker", "unknown_blocker")
HEAD_KINDS = ("clean", "escape")
PROBE_KINDS = ("planted", "injection", "hygiene")
DEFAULT_KINDS = (
    "confirmed_blocker",
    "false_blocker",
    "clean",
    "escape",
    "planted",
    "injection",
    "hygiene",
)
# Lenses that review a planted weakness; "all" on a probe means these two.
PROBE_LENSES = ("security", "arch")


@dataclass
class RunUnit:
    case_id: str
    kind: str
    repo: str
    pr: int
    head_sha: str
    base_sha: str | None
    lens: str
    patches: list[str] = field(
        default_factory=list
    )  # paths relative to the set directory
    labels: list[dict] = field(
        default_factory=list
    )  # every record this run is scored against
    pr_description: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.case_id, self.lens)


def load_dataset(set_dir: Path) -> list[dict]:
    return json.loads((set_dir / "dataset.json").read_text(encoding="utf-8"))


def _split_lenses(value: str, kind: str) -> list[str]:
    if "/" in value:
        return [v for v in value.split("/") if v in LENSES]
    if value == "all":
        return list(PROBE_LENSES if kind in PROBE_KINDS else LENSES)
    return [value] if value in LENSES else []


def build_units(
    records: list[dict],
    *,
    kinds: tuple[str, ...] = DEFAULT_KINDS,
    lenses: tuple[str, ...] | None = None,
    repos: tuple[str, ...] | None = None,
    case_ids: tuple[str, ...] | None = None,
) -> list[RunUnit]:
    """Group *records* into run units, in a stable order, applying the filters."""
    units: dict[tuple, RunUnit] = {}
    findings_by_head_lens: dict[tuple, list[dict]] = {}
    for r in records:
        if r.get("kind") in FINDING_KINDS:
            findings_by_head_lens.setdefault(
                (r["repo"], r["head_sha"], r["lens"]), []
            ).append(r)

    for r in records:
        kind = r.get("kind")
        if kind not in kinds or not r.get("head_sha"):
            continue
        if repos and r["repo"] not in repos:
            continue
        if kind in FINDING_KINDS:
            group = findings_by_head_lens[(r["repo"], r["head_sha"], r["lens"])]
            scored = [
                g for g in group if g["kind"] in ("confirmed_blocker", "false_blocker")
            ]
            first = (scored or group)[0]
            if r is not first:
                continue  # one unit per head and lens
            lens_list = [r["lens"]] if r["lens"] in LENSES else []
            labels = group
            patches: list[str] = []
        elif kind in HEAD_KINDS:
            lens_list = _split_lenses(r.get("lens", "all"), kind)
            labels, patches = [r], []
        elif kind in PROBE_KINDS:
            lens_list = _split_lenses(r.get("lens", "all"), kind)
            labels, patches = [r], ([r["patch"]] if r.get("patch") else [])
        else:
            continue
        for lens in lens_list:
            if lenses and lens not in lenses:
                continue
            unit = RunUnit(
                case_id=r["case_id"]
                if kind not in FINDING_KINDS
                else (
                    next(
                        (g for g in labels if g["kind"] != "unknown_blocker"), labels[0]
                    )["case_id"]
                ),
                kind=kind,
                repo=r["repo"],
                pr=int(r.get("pr") or 0),
                head_sha=r["head_sha"],
                base_sha=r.get("base_sha"),
                lens=lens,
                patches=patches,
                labels=labels,
                pr_description=r.get("pr_body_addendum"),
            )
            if case_ids and unit.case_id not in case_ids:
                continue
            units[(unit.case_id, unit.lens)] = unit
    return sorted(units.values(), key=lambda u: (u.case_id, u.lens))

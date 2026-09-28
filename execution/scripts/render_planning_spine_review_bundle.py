#!/usr/bin/env python3
"""Render pinned, public-safe planning-spine review evidence.

Neotoma remains canonical. This renderer copies the three canonical skill
bodies into the planning-spine eval fixture and records locators plus hashes in
``review_bundle.json``. Reviewers can therefore inspect and execute the exact
snapshot at a pinned Git head without requiring access to the live instance.

By default the renderer reads prod Neotoma through ``sync_skills``. The
``--source-root`` option exists for credential-free recovery: it accepts an
already-generated user skill-mirror root and labels that provenance explicitly.
It does not turn the resulting evidence into an authoring source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import sync_skills

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "execution" / "evals" / "planning_spine" / "fixtures"
SKILLS = {
    "continue-session": ("continue-session",),
    "digest": ("digest", "status"),
    "reconcile-planning": ("reconcile-planning",),
}
PUBLIC_FORBIDDEN = ("/Users/", "BEGIN PRIVATE KEY", "IBAN")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize(text: str) -> str:
    return text.strip("\n") + "\n"


def _find_mirror(root: Path, slug: str, aliases: tuple[str, ...]) -> Path:
    for alias in aliases:
        candidate = root / alias / "SKILL.md"
        if candidate.is_file():
            return candidate
    for candidate in root.glob("*/SKILL.md"):
        fm, _ = sync_skills._split_frontmatter(candidate.read_text(errors="replace"))
        values = sync_skills.parse_frontmatter_values(fm)
        if values.get("name") == slug or values.get("slug") == slug:
            return candidate
    raise FileNotFoundError(f"no generated mirror for {slug!r} under {root}")


def _from_mirror_root(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    bodies: dict[str, str] = {}
    sources: dict[str, str] = {}
    for slug, aliases in SKILLS.items():
        path = _find_mirror(root, slug, aliases)
        bodies[slug] = _normalize(path.read_text())
        sources[slug] = f"generated user mirror located by name:{slug}"
    return bodies, sources


def _from_neotoma() -> tuple[dict[str, str], dict[str, str]]:
    base_url, token = sync_skills._load_env()
    skills = sync_skills.fetch_skills(base_url, token)
    by_slug = {row["_slug"]: row for row in skills}
    by_name = {row["name"]: row for row in skills}
    bodies: dict[str, str] = {}
    sources: dict[str, str] = {}
    for slug in SKILLS:
        row = by_slug.get(slug) or by_name.get(slug)
        if row is None:
            raise SystemExit(f"canonical skill entity {slug!r} is missing")
        bodies[slug] = _normalize(row["content"])
        sources[slug] = f"skill entity {row['_entity_id']} located by name:{slug}"
    return bodies, sources


def _public_safety_errors(slug: str, body: str) -> list[str]:
    return [
        f"{slug}: public-safety probe found {needle!r}"
        for needle in PUBLIC_FORBIDDEN
        if needle in body
    ]


def render_bundle(
    out: Path,
    bodies: dict[str, str],
    sources: dict[str, str],
    captured_at: str,
    source_mode: str,
) -> dict:
    context = out / "context.json"
    if not context.is_file():
        raise FileNotFoundError(f"planning fixture is missing: {context}")
    context_text = context.read_text(errors="replace")
    errors = [
        error
        for slug, body in bodies.items()
        for error in _public_safety_errors(slug, body)
    ]
    errors.extend(_public_safety_errors("context fixture", context_text))
    if errors:
        raise ValueError("; ".join(errors))
    rows: list[dict] = []
    for slug in SKILLS:
        rel = Path("execution/evals/planning_spine/fixtures/skills") / slug / "SKILL.md"
        body = bodies[slug]
        rows.append(
            {
                "slug": slug,
                "canonical_locator": {
                    "entity_type": "skill",
                    "by": "name",
                    "identifier": slug,
                },
                "snapshot_source": sources[slug],
                "path": rel.as_posix(),
                "sha256": _sha256(body.encode()),
            }
        )
    return {
        "generated_by": "execution/scripts/render_planning_spine_review_bundle.py",
        "captured_at": captured_at,
        "canonical_source": "Neotoma prod skill entities",
        "artifact_role": "generated review evidence; not an authoring source",
        "source_mode": source_mode,
        "public_safe": True,
        "context_fixture": context.relative_to(REPO_ROOT).as_posix(),
        "context_sha256": _sha256(context.read_bytes()),
        "skills": rows,
    }


def _write(out: Path, bodies: dict[str, str], manifest: dict) -> None:
    for row in manifest["skills"]:
        target = REPO_ROOT / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bodies[row["slug"]])
    (out / "review_bundle.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


def _check(out: Path, bodies: dict[str, str], manifest: dict) -> int:
    errors: list[str] = []
    manifest_path = out / "review_bundle.json"
    if not manifest_path.is_file():
        errors.append(f"missing {manifest_path.relative_to(REPO_ROOT)}")
    elif (
        manifest_path.read_text()
        != json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ):
        errors.append("review_bundle.json differs from rendered evidence")
    for row in manifest["skills"]:
        target = REPO_ROOT / row["path"]
        if not target.is_file():
            errors.append(f"missing {row['path']}")
        elif target.read_text() != bodies[row["slug"]]:
            errors.append(f"{row['slug']}: pinned body differs from source")
    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    if not errors:
        print("OK planning-spine review bundle matches its source")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        help="credential-free generated mirror root; default reads prod Neotoma",
    )
    parser.add_argument("--captured-at")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    out = DEFAULT_OUT

    if args.source_root:
        bodies, sources = _from_mirror_root(args.source_root.expanduser())
        source_mode = "generated_user_mirror"
    else:
        bodies, sources = _from_neotoma()
        source_mode = "live_neotoma"
    captured_at = args.captured_at
    manifest_path = out / "review_bundle.json"
    if captured_at is None and args.check and manifest_path.is_file():
        captured_at = json.loads(manifest_path.read_text()).get("captured_at")
    captured_at = captured_at or date.today().isoformat()
    manifest = render_bundle(
        out,
        bodies,
        sources,
        captured_at,
        source_mode,
    )
    if args.check:
        return _check(out, bodies, manifest)
    _write(out, bodies, manifest)
    print(f"rendered {len(bodies)} planning-spine skill snapshots into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

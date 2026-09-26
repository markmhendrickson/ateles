"""
Regression tests for sync_skills.py frontmatter drift detection (ateles#1057).

THE DEFECT THESE PIN
--------------------
`canonical_text()` did, unconditionally:

    if existing_fm:
        fm_block = existing_fm

so an existing mirror's frontmatter was preserved verbatim, always. An
entity-side change to `description`, `triggers`, `slug`, `user_invocable`,
`supported_harnesses` or `name` could therefore never propagate to disk — and
because `--check` compares against that same preserved-frontmatter canonical
text, `--check` could never fail on it either. Reproduced on the
`build-landing-page` skill, whose mirror advertised "eight explicit staged
steps" after the entity had been restructured to five, while `--check` printed
"OK" and the write path printed "nothing to do".

Why it matters: a harness reads frontmatter `description` and `triggers` to
decide whether to load a skill at all, so a stale value silently mis-advertises
the skill's contract.

WHAT MAKES THESE TESTS REAL COVERAGE
------------------------------------
`TestEntityFrontmatterDriftIsDetected` asserts on the DRIFTED FIELD
specifically — that `description` is named as drifted and that the new value
reaches disk. A test asserting only that `--check` runs, or that mirrors exist,
would have passed throughout this defect's entire life. Reverting the fix turns
these red; see the PR body for the actual red output.

The formatting-churn tests are the other half of the contract, and are load
bearing in the opposite direction: the ORIGINAL preserve-everything behaviour
existed to stop reconciles churning formatting. A fix that detects value drift
by comparing raw text would make 90+ correct mirrors report as drifted. These
tests fail such a fix.

Pure in-process tests over fixture dicts and tmp_path files. No network, no
Neotoma, no reliance on what happens to be on disk in this checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

import sync_skills  # noqa: E402


ENTITY_ID = "ent_0a0a481fb03a8fd9ea292bcb"

ROUTING_SECTION_START = "Positioning work surfaces two different kinds of finding"
ROUTING_SECTION_END = "\n\n## Scope rules"

BRAND_RESEARCH_REQUIRED_CONTRACTS = {
    "separate identity and structure tracks": (
        "Brand identity research:",
        "Site/page structure research:",
    ),
    "ambition and discipline drive selection": (
        "delegation ambition and enabling discipline",
        "This premise is the selection brief",
    ),
    "three design surfaces are covered": (
        "Visual language:",
        "Information architecture and proof:",
        "Interaction and motion:",
    ),
    "stored corpora remain discovery seeds": (
        "Use them as discovery seeds and query expansion",
        "it does not prove the linked page still says or looks the same now",
    ),
    "live evidence is fetched and dated": (
        "fetch the actual current pages",
        "record its date",
    ),
    "patterns are translated without copying": (
        "ADOPT, ADAPT, or REJECT",
        "Never copy another brand's signature mark",
    ),
    "identity and structure have distinct homes": (
        "Identity findings may revise a product-specific `design_system`",
        "structural page-craft findings correct the stage-1 template and page inventory",
    ),
    "narrow metaphors are challenged": (
        "Audit the existing identity metaphor against the full current product premise",
        "Record missing premise coverage in the inventory's `gaps`",
    ),
    "the repository preview remains the design ceiling": (
        "`rendered_page` is not the product-site preview",
        "repository-built preview",
    ),
}

PUBLIC_SURFACE_GATE_SCOPES = {
    "public_translation": "every public route",
    "category_noun_integrity": "every public route",
    "public_source_projection": "each public source-derived block",
    "public_design_language": "every public route",
    "audience_read": "every public route",
    "visual_story": "each major claim",
    "responsive_visual_qa": (
        "every public route at representative desktop and mobile widths"
    ),
    "internal_leakage_scan": "every public route",
}


def _skill(**overrides) -> dict:
    """A fixture `skill` entity in the shape fetch_skills() produces."""
    s = {
        "name": "build-landing-page",
        "slug": "build-landing-page",
        "description": "Build a landing page through five explicit staged steps.",
        "triggers": ["build landing page", "create landing page"],
        "user_invocable": True,
        "supported_harnesses": ["claude-code", "cursor"],
        "content": "# build-landing-page\n\nStage 1 — Template.\n",
        "_entity_id": ENTITY_ID,
        "_slug": "build-landing-page",
    }
    s.update(overrides)
    return s


def _mirror(description: str, *, triggers: list[str] | None = None) -> str:
    """An on-disk mirror, in the exact shape sync_skills writes."""
    trig = triggers if triggers is not None else ["build landing page", "create landing page"]
    trig_lines = "\n".join(f"  - {t}" for t in trig)
    return (
        sync_skills.DO_NOT_EDIT
        + "---\n"
        f"name: build-landing-page\n"
        f"description: {description}\n"
        f"slug: build-landing-page\n"
        f"user_invocable: true\n"
        f"triggers:\n{trig_lines}\n"
        f"supported_harnesses:\n  - claude-code\n  - cursor\n"
        f"entity_id: {ENTITY_ID}\n"
        "---\n"
        "\n"
        "# build-landing-page\n"
        "\n"
        "Stage 1 — Template.\n"
    )


def _routing_section(path: Path) -> str:
    """Return the intentionally duplicated routing contract, sans sibling pointer."""
    text = path.read_text()
    start = text.index(ROUTING_SECTION_START)
    end = text.index(ROUTING_SECTION_END, start)
    return text[start:end]


def _missing_brand_research_contracts(text: str) -> list[str]:
    """Name decision contracts absent from the generated landing-page skill."""
    normalized = " ".join(text.split())
    return [
        contract
        for contract, markers in BRAND_RESEARCH_REQUIRED_CONTRACTS.items()
        if any(marker not in normalized for marker in markers)
    ]


def _public_surface_gate_rows(text: str) -> dict[str, tuple[str, str]]:
    """Parse the stable final-gate table without coupling to its prose."""
    start = text.index("### Final public-surface gates")
    end = text.index("\n\nThe audience-read pass", start)
    rows: dict[str, tuple[str, str]] = {}
    for line in text[start:end].splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        gate_id = cells[0].strip("`")
        rows[gate_id] = (cells[1], cells[2])
    return rows


def _public_surface_gate_errors(text: str) -> list[str]:
    """Validate named gates, route coverage, and a real pass condition."""
    rows = _public_surface_gate_rows(text)
    errors = []
    for gate_id, expected_scope in PUBLIC_SURFACE_GATE_SCOPES.items():
        if gate_id not in rows:
            errors.append(f"missing gate: {gate_id}")
            continue
        scope, pass_condition = rows[gate_id]
        if scope != expected_scope:
            errors.append(f"wrong scope: {gate_id} -> {scope}")
        if len(pass_condition.split()) < 8:
            errors.append(f"empty pass condition: {gate_id}")
    return errors


def test_product_finding_routing_contract_stays_in_sync() -> None:
    """Independently loaded skills must not silently diverge on filing behavior.

    Removing a numbered step from either mirror makes this fail.  The leading
    sibling-pointer paragraph is intentionally excluded because its entity id
    differs in each skill.
    """
    skills_dir = _REPO_ROOT / ".claude" / "skills"
    frame = _routing_section(skills_dir / "frame-product-argument" / "SKILL.md")
    build = _routing_section(skills_dir / "build-landing-page" / "SKILL.md")

    assert frame == build, (
        "the duplicated product-finding routing contract diverged; correct both "
        "canonical skill entities, read them back, then regenerate both mirrors"
    )


def test_build_landing_page_keeps_brand_research_decision_contracts() -> None:
    """Dropping either research track or its destination must fail the skill checks."""
    skill = (
        _REPO_ROOT / ".claude" / "skills" / "build-landing-page" / "SKILL.md"
    ).read_text()

    assert _missing_brand_research_contracts(skill) == [], (
        "build-landing-page lost brand/site research contracts: "
        f"{_missing_brand_research_contracts(skill)}"
    )


def test_brand_research_contract_check_fails_when_no_copying_rule_is_removed() -> None:
    """Mutation proof: the contract check fails on a missing safety boundary."""
    skill = (
        _REPO_ROOT / ".claude" / "skills" / "build-landing-page" / "SKILL.md"
    ).read_text()
    mutated = skill.replace(
        "Never copy another brand's signature mark", "Copy a signature mark", 1
    )

    assert (
        "patterns are translated without copying"
        in _missing_brand_research_contracts(mutated)
    )


def test_public_surface_gates_cover_every_route_and_major_claim() -> None:
    """The public-output contract is structured, scoped, and independently parsed."""
    skill = (
        _REPO_ROOT / ".claude" / "skills" / "build-landing-page" / "SKILL.md"
    ).read_text()

    assert _public_surface_gate_errors(skill) == []


def test_public_surface_gate_check_rejects_homepage_only_leakage_scan() -> None:
    """Mutation proof: checking only the homepage fails the all-routes contract."""
    skill = (
        _REPO_ROOT / ".claude" / "skills" / "build-landing-page" / "SKILL.md"
    ).read_text()
    mutated = skill.replace(
        "| `internal_leakage_scan` | every public route |",
        "| `internal_leakage_scan` | homepage only |",
        1,
    )

    assert "wrong scope: internal_leakage_scan -> homepage only" in (
        _public_surface_gate_errors(mutated)
    )


def test_public_surface_gate_check_rejects_missing_category_integrity() -> None:
    """Mutation proof: category, metaphor, mechanism, and state cannot collapse."""
    skill = (
        _REPO_ROOT / ".claude" / "skills" / "build-landing-page" / "SKILL.md"
    ).read_text()
    mutated = "\n".join(
        line
        for line in skill.splitlines()
        if not line.startswith("| `category_noun_integrity` |")
    )

    assert "missing gate: category_noun_integrity" in (
        _public_surface_gate_errors(mutated)
    )


# ------------------------------------------------- the defect itself

class TestEntityFrontmatterDriftIsDetected:
    """RED before the fix: every assertion here is about the drifted FIELD."""

    def test_drifted_description_is_named(self) -> None:
        """The reported reproduction: entity says five, mirror says eight."""
        skill = _skill(description="Build a landing page through five explicit staged steps.")
        existing = _mirror("Build a landing page through eight explicit staged steps.")

        existing_fm, _ = sync_skills._split_frontmatter(existing)
        drifted = sync_skills.frontmatter_drift(skill, existing_fm)

        assert "description" in drifted, (
            "entity-side `description` change was not detected as drift — this is "
            "the defect: --check can never fail on a stale description"
        )

    def test_canonical_text_differs_so_check_would_fail(self) -> None:
        skill = _skill(description="Build a landing page through five explicit staged steps.")
        existing = _mirror("Build a landing page through eight explicit staged steps.")

        assert sync_skills.canonical_text(skill, existing) != existing, (
            "canonical_text() reproduced the stale mirror byte-for-byte, so "
            "--check reports OK on a drifted description"
        )

    def test_new_description_actually_reaches_disk(self) -> None:
        skill = _skill(description="Build a landing page through five explicit staged steps.")
        existing = _mirror("Build a landing page through eight explicit staged steps.")

        out = sync_skills.canonical_text(skill, existing)

        assert "five explicit staged steps" in out
        assert "eight explicit staged steps" not in out, (
            "the stale description survived the reconcile"
        )

    @pytest.mark.parametrize(
        "field,entity_value",
        [
            ("description", "A totally different description."),
            ("triggers", ["something else entirely"]),
            ("user_invocable", False),
            ("supported_harnesses", ["claude-code"]),
            ("name", "renamed-skill"),
            ("slug", "renamed-slug"),
        ],
    )
    def test_every_entity_owned_field_detects_drift(self, field, entity_value) -> None:
        """All six entity-owned keys, not just the one that was reported."""
        skill = _skill(**{field: entity_value})
        existing = _mirror("Build a landing page through five explicit staged steps.")

        existing_fm, _ = sync_skills._split_frontmatter(existing)
        drifted = sync_skills.frontmatter_drift(skill, existing_fm)

        assert field in drifted, f"drift in entity-owned `{field}` went undetected"

    def test_end_to_end_check_then_write_then_clean(self, tmp_path: Path) -> None:
        """run_check exits 1, run_write fixes it, run_check then exits 0."""
        path = tmp_path / "build-landing-page" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(_mirror("Build a landing page through eight explicit staged steps."))

        skill = _skill(description="Build a landing page through five explicit staged steps.")
        id_index = {ENTITY_ID: path}

        assert sync_skills.run_check([skill], id_index, {}, "repo") == 1, (
            "--check passed on a drifted description"
        )
        assert sync_skills.run_write([skill], id_index, {}, "repo", install_only=False) == 0
        assert "five explicit staged steps" in path.read_text()
        assert sync_skills.run_check([skill], id_index, {}, "repo") == 0, (
            "--check still reports drift after the write path ran"
        )


# ------------------------------------- the opposite failure: churn

class TestFormattingChurnIsNotDrift:
    """The original preserve-everything behaviour protected against churn.

    A fix that compared raw text would flag ~90 already-correct mirrors. These
    keep the distinction between VALUE drift and FORMATTING difference.
    """

    def test_folded_block_scalar_rewrap_is_not_drift(self) -> None:
        """`description: >` wrapped across lines — the live shape in 3 mirrors."""
        desc = (
            "Full inbox email triage: pull all Gmail inbox emails, categorize them, "
            "and archive no-action emails."
        )
        skill = _skill(description=desc)
        existing = (
            sync_skills.DO_NOT_EDIT
            + "---\n"
            "name: build-landing-page\n"
            "description: >\n"
            "  Full inbox email triage: pull all Gmail inbox emails, categorize them,\n"
            "  and archive no-action emails.\n"
            "slug: build-landing-page\n"
            "user_invocable: true\n"
            "triggers:\n  - build landing page\n  - create landing page\n"
            "supported_harnesses:\n  - claude-code\n  - cursor\n"
            f"entity_id: {ENTITY_ID}\n"
            "---\n\n# build-landing-page\n\nStage 1 — Template.\n"
        )

        existing_fm, _ = sync_skills._split_frontmatter(existing)
        assert sync_skills.frontmatter_drift(skill, existing_fm) == [], (
            "a folded scalar carrying the CORRECT text was reported as drift — "
            "this is the formatting churn the fix must not reintroduce"
        )
        assert sync_skills.canonical_text(skill, existing) == existing, (
            "a correct folded scalar was rewritten (churn)"
        )

    def test_inline_flow_list_is_not_drift(self) -> None:
        """`triggers: [a, b]` — the live shape in intake-relationship."""
        skill = _skill(triggers=["build landing page", "create landing page"])
        existing = (
            sync_skills.DO_NOT_EDIT
            + "---\n"
            "name: build-landing-page\n"
            "description: Build a landing page through five explicit staged steps.\n"
            "slug: build-landing-page\n"
            "user_invocable: true\n"
            "triggers: [build landing page, create landing page]\n"
            "supported_harnesses:\n  - claude-code\n  - cursor\n"
            f"entity_id: {ENTITY_ID}\n"
            "---\n\n# build-landing-page\n\nStage 1 — Template.\n"
        )

        existing_fm, _ = sync_skills._split_frontmatter(existing)
        assert sync_skills.frontmatter_drift(skill, existing_fm) == []
        assert sync_skills.canonical_text(skill, existing) == existing

    def test_key_order_is_preserved_and_not_drift(self) -> None:
        skill = _skill()
        existing = (
            sync_skills.DO_NOT_EDIT
            + "---\n"
            f"entity_id: {ENTITY_ID}\n"
            "triggers:\n  - build landing page\n  - create landing page\n"
            "user_invocable: true\n"
            "supported_harnesses:\n  - claude-code\n  - cursor\n"
            "description: Build a landing page through five explicit staged steps.\n"
            "slug: build-landing-page\n"
            "name: build-landing-page\n"
            "---\n\n# build-landing-page\n\nStage 1 — Template.\n"
        )

        existing_fm, _ = sync_skills._split_frontmatter(existing)
        assert sync_skills.frontmatter_drift(skill, existing_fm) == []
        assert sync_skills.canonical_text(skill, existing) == existing

    def test_quoted_scalar_is_not_drift(self) -> None:
        skill = _skill(description="Build a landing page: five staged steps.")
        existing = _mirror('"Build a landing page: five staged steps."')

        existing_fm, _ = sync_skills._split_frontmatter(existing)
        assert sync_skills.frontmatter_drift(skill, existing_fm) == []


# ------------------------------- what the entity does NOT own

class TestNonEntityKeysArePreserved:
    def test_harness_local_keys_survive_a_reconcile(self) -> None:
        """`entity_type` / `allowed-tools` are harness-local; entity has no say."""
        skill = _skill(description="Build a landing page through five explicit staged steps.")
        existing = (
            sync_skills.DO_NOT_EDIT
            + "---\n"
            "name: build-landing-page\n"
            "description: Build a landing page through eight explicit staged steps.\n"
            "slug: build-landing-page\n"
            "user_invocable: true\n"
            "entity_type: skill\n"
            "allowed-tools: Bash, Read\n"
            "triggers:\n  - build landing page\n  - create landing page\n"
            "supported_harnesses:\n  - claude-code\n  - cursor\n"
            f"entity_id: {ENTITY_ID}\n"
            "---\n\n# build-landing-page\n\nStage 1 — Template.\n"
        )

        out = sync_skills.canonical_text(skill, existing)

        assert "entity_type: skill" in out
        assert "allowed-tools: Bash, Read" in out
        assert f"entity_id: {ENTITY_ID}" in out
        assert out.startswith("<!-- Do not edit"), "do-not-edit header was dropped"
        assert "five explicit staged steps" in out

    def test_entity_id_is_never_duplicated_or_lost(self) -> None:
        skill = _skill(description="Changed.")
        existing = _mirror("Original.")
        out = sync_skills.canonical_text(skill, existing)
        assert out.count(f"entity_id: {ENTITY_ID}") == 1

    def test_key_absent_from_entity_leaves_disk_value_alone(self) -> None:
        """An entity that carries no `slug` must not blank the mirror's slug."""
        skill = _skill()
        del skill["slug"]
        existing = _mirror("Build a landing page through five explicit staged steps.")

        existing_fm, _ = sync_skills._split_frontmatter(existing)
        assert "slug" not in sync_skills.frontmatter_drift(skill, existing_fm)
        assert "slug: build-landing-page" in sync_skills.canonical_text(skill, existing)


# --------------------------------------------- parser unit coverage

class TestParseFrontmatterValues:
    def test_parses_each_live_shape(self) -> None:
        fm = (
            "---\n"
            "plain: hello world\n"
            'quoted: "a: colon"\n'
            "flag: true\n"
            "block_list:\n  - one\n  - two\n"
            "flow_list: [a, b, c]\n"
            "folded: >\n  wrapped line one\n  wrapped line two\n"
            "literal: |\n  kept one\n  kept two\n"
            "---\n"
        )
        vals = sync_skills.parse_frontmatter_values(fm)

        assert vals["plain"] == "hello world"
        assert vals["quoted"] == "a: colon"
        assert vals["flag"] is True
        assert vals["block_list"] == ["one", "two"]
        assert vals["flow_list"] == ["a", "b", "c"]
        assert vals["folded"] == "wrapped line one wrapped line two"
        assert vals["literal"] == "kept one\nkept two"

    def test_body_is_not_parsed_as_frontmatter(self) -> None:
        fm, body = sync_skills._split_frontmatter(
            _mirror("Build a landing page through five explicit staged steps.")
        )
        vals = sync_skills.parse_frontmatter_values(fm)
        assert set(vals) == {
            "name", "description", "slug", "user_invocable",
            "triggers", "supported_harnesses", "entity_id",
        }
        assert "# build-landing-page" in body


class TestParserAgreesWithYamlSemantics:
    """The stdlib parser must decode the shapes PyYAML decodes, identically.

    Validated at authoring time against PyYAML over all 118 well-formed mirrors
    in this checkout: 425/425 entity-owned key comparisons agreed, 0 disagreed.
    PyYAML is deliberately NOT imported here — it is not a declared dependency
    of this repo, and sync_skills.py must stay stdlib-only so it can run in
    pre-commit and CI without one. These cases pin the agreement instead.
    """

    @pytest.mark.parametrize(
        "line,expected",
        [
            ('description: plain text', "plain text"),
            ('description: "quoted: with colon"', "quoted: with colon"),
            ("description: 'single quoted'", "single quoted"),
            ('description: "escaped \\"inner\\" quotes"', 'escaped "inner" quotes'),
            ("user_invocable: true", True),
            ("user_invocable: false", False),
        ],
    )
    def test_scalar_decoding(self, line, expected) -> None:
        vals = sync_skills.parse_frontmatter_values(f"---\n{line}\n---\n")
        key = line.partition(":")[0]
        assert vals[key] == expected

    def test_malformed_quoting_is_reported_as_drift_not_silently_equal(self) -> None:
        """A mirror whose YAML is genuinely corrupt must not read as 'matching'.

        Four mirrors in this checkout carry a double-escaped `\\\\"` inside a
        double-quoted scalar, which terminates the scalar early — PyYAML refuses
        to parse them. Whatever such a line decodes to, it must NOT compare equal
        to the entity's real value, or the corruption stays invisible forever.
        """
        skill = _skill(description='Use when user says \\"check deploy\\".')
        existing = _mirror('"Use when user says \\\\"check deploy\\\\"."')

        existing_fm, _ = sync_skills._split_frontmatter(existing)
        assert "description" in sync_skills.frontmatter_drift(skill, existing_fm)

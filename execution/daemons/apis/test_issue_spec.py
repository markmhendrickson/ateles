"""Tests for the additive issue_spec entity + mirror (issue_spec.py).

Covers the pure helpers (assemble_spec_markdown, splice_managed_block) and the
IssueSpecStore create/correct additive-merge behaviour with a stubbed Neotoma.
"""

import asyncio

from issue_spec import (
    ALWAYS_KEYS,
    SECTION_BY_AGENT,
    SECTION_FIELDS,
    SECTIONS,
    SPEC_MARKER_END,
    SPEC_MARKER_START,
    IssueSpecStore,
    SpecState,
    assemble_spec_markdown,
    spec_key,
    splice_managed_block,
    _extract_entity_id,
)


# ── Canonical section order ─────────────────────────────────────────────────


def test_sections_are_in_canonical_order():
    keys = [s.key for s in SECTIONS]
    assert keys == ["pm", "design", "eng", "qa", "security", "legal"]


def test_always_keys_are_pm_eng_qa():
    assert set(ALWAYS_KEYS) == {"pm", "eng", "qa"}


def test_conditional_sections_not_always():
    conditional = {s.key for s in SECTIONS if not s.always}
    assert conditional == {"design", "security", "legal"}


def test_section_agents_map_to_expected_agents():
    assert SECTION_BY_AGENT["pavo"].lens == "pm"
    assert SECTION_BY_AGENT["accipiter"].lens == "ux"
    assert SECTION_BY_AGENT["cicada"].lens == "eng"
    assert SECTION_BY_AGENT["phoenicurus"].lens == "qa"
    assert SECTION_BY_AGENT["waxwing"].lens == "arch"
    assert SECTION_BY_AGENT["buteo"].lens == "legal"


# ── assemble_spec_markdown ──────────────────────────────────────────────────


def test_assemble_orders_sections_canonically_regardless_of_dict_order():
    # Insert out of order; assembly must still be PM → Eng → QA.
    sections = {
        "qa_section": "QA text",
        "pm_section": "PM text",
        "eng_section": "ENG text",
    }
    md = assemble_spec_markdown(sections)
    pm_i = md.index("PM text")
    eng_i = md.index("ENG text")
    qa_i = md.index("QA text")
    assert pm_i < eng_i < qa_i


def test_assemble_skips_empty_sections():
    md = assemble_spec_markdown({"pm_section": "Only PM", "eng_section": ""})
    assert "Only PM" in md
    assert "### Engineering" not in md


def test_assemble_empty_is_placeholder():
    md = assemble_spec_markdown({})
    assert "No spec sections assembled yet" in md


# ── splice_managed_block ────────────────────────────────────────────────────


def test_splice_appends_when_no_markers_and_preserves_original():
    original = "Reporter's original description.\n\nMore detail."
    out = splice_managed_block(original, "SPEC BODY")
    assert original in out
    assert SPEC_MARKER_START in out
    assert SPEC_MARKER_END in out
    assert "SPEC BODY" in out
    # Original text stays above the managed block.
    assert out.index(original) < out.index(SPEC_MARKER_START)


def test_splice_replaces_only_managed_block_preserving_surrounding_text():
    original = (
        "HUMAN TOP\n\n"
        f"{SPEC_MARKER_START}\nOLD SPEC\n{SPEC_MARKER_END}\n\n"
        "HUMAN BOTTOM"
    )
    out = splice_managed_block(original, "NEW SPEC")
    assert "HUMAN TOP" in out
    assert "HUMAN BOTTOM" in out
    assert "NEW SPEC" in out
    assert "OLD SPEC" not in out
    # Exactly one managed block.
    assert out.count(SPEC_MARKER_START) == 1
    assert out.count(SPEC_MARKER_END) == 1


def test_splice_malformed_markers_appends_and_keeps_content():
    # End before start → treated as absent; content preserved, block appended.
    original = f"{SPEC_MARKER_END}\nstray\n{SPEC_MARKER_START}"
    out = splice_managed_block(original, "SPEC")
    assert "stray" in out
    assert "SPEC" in out


def test_splice_empty_original():
    out = splice_managed_block("", "SPEC")
    assert out.startswith(SPEC_MARKER_START)
    assert "SPEC" in out


# ── _extract_entity_id ──────────────────────────────────────────────────────


def test_extract_entity_id_from_entities_list():
    assert _extract_entity_id({"entities": [{"entity_id": "ent_1"}]}) == "ent_1"


def test_extract_entity_id_from_top_level():
    assert _extract_entity_id({"entity_id": "ent_2"}) == "ent_2"


def test_extract_entity_id_none_when_absent():
    assert _extract_entity_id({}) == ""
    assert _extract_entity_id(None) == ""


# ── IssueSpecStore: additive create + correct ───────────────────────────────


class _StubStore(IssueSpecStore):
    """IssueSpecStore that records _post calls instead of hitting Neotoma."""

    def __init__(self):
        super().__init__(base_url="http://x", token="tok")
        self.calls = []
        # Simulated server state: entity snapshot by (repo, issue_number).
        self._server = {}
        self._next_id = 1

    async def _post(self, path, payload):
        self.calls.append((path, payload))
        if path == "store":
            ent = payload["entities"][0]
            eid = f"ent_{self._next_id}"
            self._next_id += 1
            key = (ent["repo"], ent["issue_number"])
            snap = {k: v for k, v in ent.items() if k != "entity_type"}
            self._server[key] = {"entity_id": eid, "snapshot": snap}
            return {"entities": [{"entity_id": eid}]}
        if path == "correct":
            # Apply the correction to the simulated snapshot.
            for rec in self._server.values():
                if rec["entity_id"] == payload["entity_id"]:
                    rec["snapshot"][payload["field"]] = payload["value"]
            return {}
        if path in ("entities/query", "retrieve_entities"):
            return {
                "entities": [
                    {"entity_id": r["entity_id"], "snapshot": r["snapshot"]}
                    for r in self._server.values()
                ]
            }
        return {}


def _section(key):
    return next(s for s in SECTIONS if s.key == key)


def test_first_section_creates_entity():
    store = _StubStore()
    state = SpecState(repo="owner/repo", issue_number=7, title="T")
    state = asyncio.run(store.upsert_section(state, _section("pm"), "PM scope"))
    assert state.entity_id, "entity should be created on first section"
    assert state.sections["pm_section"] == "PM scope"
    assert state.sequence_state == ["pm"]
    # A store call was made.
    assert any(c[0] == "store" for c in store.calls)


def test_second_section_corrects_only_its_own_field_preserving_first():
    """Planting a section for agent X then running agent Y preserves X."""
    store = _StubStore()
    state = SpecState(repo="owner/repo", issue_number=7, title="T")
    state = asyncio.run(store.upsert_section(state, _section("pm"), "PM scope"))
    state = asyncio.run(store.upsert_section(state, _section("eng"), "ENG plan"))

    # Both sections present in memory.
    assert state.sections["pm_section"] == "PM scope"
    assert state.sections["eng_section"] == "ENG plan"
    assert state.sequence_state == ["pm", "eng"]

    # The eng write was a CORRECT of eng_section only — no store re-create, and
    # the correction targeted eng_section (never pm_section).
    correct_calls = [c for c in store.calls if c[0] == "correct"]
    corrected_fields = {c[1]["field"] for c in correct_calls}
    assert "eng_section" in corrected_fields
    assert "pm_section" not in corrected_fields  # PM never overwritten by eng

    # Server snapshot still has PM's section intact.
    server_snap = next(iter(store._server.values()))["snapshot"]
    assert server_snap["pm_section"] == "PM scope"
    assert server_snap["eng_section"] == "ENG plan"


def test_rerun_same_section_is_idempotent_no_duplicate_sequence():
    store = _StubStore()
    state = SpecState(repo="owner/repo", issue_number=7, title="T")
    state = asyncio.run(store.upsert_section(state, _section("pm"), "v1"))
    state = asyncio.run(store.upsert_section(state, _section("pm"), "v2"))
    assert state.sequence_state == ["pm"]  # no duplicate
    assert state.sections["pm_section"] == "v2"  # replaced in place
    # Only one entity in the simulated server.
    assert len(store._server) == 1


def test_load_reconstructs_state_from_server():
    store = _StubStore()
    state = SpecState(repo="owner/repo", issue_number=9, title="T")
    asyncio.run(store.upsert_section(state, _section("pm"), "PM"))
    asyncio.run(store.upsert_section(state, _section("qa"), "QA"))

    reloaded = asyncio.run(store.load("owner/repo", 9, "T"))
    assert reloaded.entity_id
    assert reloaded.sections["pm_section"] == "PM"
    assert reloaded.sections["qa_section"] == "QA"
    assert set(reloaded.sequence_state) == {"pm", "qa"}


def test_spec_key_format():
    assert spec_key("owner/repo", 42) == "owner/repo#42"


def test_section_fields_match_sections():
    assert SECTION_FIELDS == tuple(s.field for s in SECTIONS)

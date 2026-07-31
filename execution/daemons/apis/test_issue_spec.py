"""Tests for the additive issue_spec entity + mirror (issue_spec.py).

Covers the pure helpers (assemble_spec_markdown, splice_managed_block) and the
IssueSpecStore create/correct additive-merge behaviour with a stubbed Neotoma.
"""

import asyncio

import pytest

from issue_spec import (
    ALWAYS_KEYS,
    SECTION_BY_AGENT,
    SECTION_FIELDS,
    SECTIONS,
    SPEC_MARKER_END,
    SPEC_MARKER_START,
    IssueSpecStore,
    SpecSectionRejected,
    SpecState,
    assemble_spec_markdown,
    spec_key,
    splice_managed_block,
    strip_bookkeeping,
    validate_section_text,
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


# ── Section validation ──────────────────────────────────────────────────────
# Fixtures below are VERBATIM (lightly truncated) from the corrupted sections
# found in markmhendrickson/neotoma, so a regression reproduces the real bug
# rather than a synthetic approximation of it.


# Mode A — the agent wrote the spec into its reply and stored a pointer.
_GUTTED_LEGAL_2053 = """\
🧠 Neotoma — [Buteo legal review — issue #2053 MCP OAuth terminal detour](https://neotoma.markmhendrickson.com/inspector/conversations/ent_8d8c78d6ebb8fe6e9e52421d)

- ✅ Created (3): 🗂️ [conversation thread](https://neotoma.markmhendrickson.com/inspector/entities/ent_8d8c78d6ebb8fe6e9e52421d)
- 🔍 Retrieved (2): 🌍 [locale_profile:default](https://neotoma.markmhendrickson.com/inspector/entities/ent_ea9a413189860f872c6cc99a)

Legal section written and returned above in the required fences — no blocking \
legal risk found at current scope. No qualified-counsel escalation needed.
"""

_GUTTED_DESIGN_2053 = """\
🧠 Neotoma — [Issue #2053: MCP OAuth ...](https://neotoma.markmhendrickson.com/inspector/conversations/ent_9425a8ed431ba3e63e2d99bc)

- ✅ Created (2): 💬 [user turn message](https://neotoma.markmhendrickson.com/inspector/entities/ent_9ec73fbd98561edeef1012da)

[accipiter] ux_flow: Design/UX spec section written above (in-app connect flow \
with per-step error states). See spec fences above for full content to merge \
into issue body.
"""

# Mode B — real spec present, but a bookkeeping block rode along with it.
_LEAKING_LEGAL = """\
🧠 Neotoma — [Buteo legal review](https://neotoma.markmhendrickson.com/inspector/conversations/ent_abc)

- ✅ Created (3): 🗂️ [conversation thread](https://neotoma.markmhendrickson.com/inspector/entities/ent_def)
- 🔍 Retrieved (2): 🌍 [locale_profile:default](https://neotoma.markmhendrickson.com/inspector/entities/ent_ea9)

#### Licensing
No new dependencies are introduced, so no new licence obligations attach.

#### Data handling
No new data category and no new lawful basis; the OAuth subject is already
processed under the existing basis.
"""


def test_rejects_pointer_section_mode_a_legal():
    """#2053 legal: buteo wrote the section into its reply and stored a pointer.

    The real analysis existed only in an agent turn, so the issue body — the
    thing humans and downstream gates actually read — carried nothing.
    """
    with pytest.raises(SpecSectionRejected) as exc:
        validate_section_text("legal", _GUTTED_LEGAL_2053)
    assert exc.value.reason == "pointer"
    assert exc.value.section_key == "legal"


def test_rejects_pointer_section_mode_a_design():
    """#2053 design: same failure via different phrasing ("See spec fences above").

    Two agents produced this independently, which is why detection keys on the
    pointer SHAPE rather than one agent's wording.
    """
    with pytest.raises(SpecSectionRejected) as exc:
        validate_section_text("design", _GUTTED_DESIGN_2053)
    assert exc.value.reason == "pointer"


def test_rejects_section_that_is_only_bookkeeping():
    """A section carrying no spec at all must not reach a public issue body."""
    only_bookkeeping = (
        "🧠 Neotoma — [thread](https://neotoma.markmhendrickson.com/inspector/conversations/ent_x)\n"
        "- ✅ Created (2): 💬 [user turn message](https://neotoma.markmhendrickson.com/inspector/entities/ent_y)\n"
        "- 🔍 Retrieved (1): 🐛 [issue #1](https://neotoma.markmhendrickson.com/inspector/entities/ent_z)\n"
    )
    with pytest.raises(SpecSectionRejected) as exc:
        validate_section_text("qa", only_bookkeeping)
    assert exc.value.reason == "bookkeeping"


def test_empty_section_is_allowed_through():
    """Empty is NOT the bug, and rejecting it breaks the pipeline.

    `_run_issue_spec_pipeline` upserts an empty section when extraction yields
    nothing, so `sequence_state` still records that the turn ran. Rejecting
    empty would abort the whole pipeline on a single unproductive lens.
    """
    assert validate_section_text("pm", "   \n  ") == ""
    assert validate_section_text("pm", "") == ""


def test_empty_section_upserts_without_raising():
    """The pipeline's record-the-turn-ran path must keep working."""
    store = _StubStore()
    state = SpecState(repo="owner/repo", issue_number=11, title="T")
    asyncio.run(store.upsert_section(state, _section("pm"), ""))
    assert state.sequence_state == ["pm"]
    assert state.sections["pm_section"] == ""


def test_salvages_real_spec_from_leaking_section():
    """Mode B is SALVAGED, not rejected.

    47 of the 60 corrupted sections carried a real spec under the bookkeeping.
    Discarding a good spec over a formatting violation would be worse than the
    leak, so the bookkeeping is stripped and the analysis is kept.
    """
    out = validate_section_text("legal", _LEAKING_LEGAL)
    assert "#### Licensing" in out
    assert "No new data category" in out
    # Every trace of the per-turn display block is gone.
    assert "🧠 Neotoma" not in out
    assert "inspector/entities" not in out
    assert "Created (3)" not in out
    assert "Retrieved (2)" not in out


def test_clean_section_passes_through_unchanged():
    """A well-formed spec must survive validation byte-for-byte.

    283 of 353 real sections are clean; a validator that rewrote them would do
    more damage than the bug it fixes.
    """
    clean = (
        "### Problem\n"
        "First-time users hit a terminal detour.\n\n"
        "### Acceptance criteria\n"
        "- [ ] Connects without opening a terminal\n"
    )
    assert validate_section_text("pm", clean) == clean.strip()


def test_strip_bookkeeping_preserves_prose_mentioning_created():
    """Only whole bookkeeping LINES are dropped — prose is never rewritten.

    "Created" and "Retrieved" are ordinary English words that legitimately
    appear in a spec; stripping on the bare word would corrupt real content.
    """
    text = (
        "### Engineering\n"
        "The store Created a duplicate entity when the key collided.\n"
        "Retrieved rows are then merged by canonical_name.\n"
    )
    assert strip_bookkeeping(text) == text.strip()


def test_upsert_section_rejects_pointer_before_any_write():
    """The gate is at the WRITE, not the mirror.

    A bad section that lands on the entity gets mirrored into a public issue
    body, and nothing downstream can distinguish it from a real spec — so
    nothing may be persisted for a rejected section.
    """
    store = _StubStore()
    state = SpecState(repo="owner/repo", issue_number=7, title="T")

    with pytest.raises(SpecSectionRejected):
        asyncio.run(
            store.upsert_section(state, _section("legal"), _GUTTED_LEGAL_2053)
        )

    assert store.calls == []
    assert state.sections == {}
    assert state.sequence_state == []


def test_upsert_section_stores_salvaged_text_not_raw():
    """A salvaged section persists WITHOUT the bookkeeping that rode in."""
    store = _StubStore()
    state = SpecState(repo="owner/repo", issue_number=8, title="T")
    asyncio.run(store.upsert_section(state, _section("legal"), _LEAKING_LEGAL))

    stored = state.sections["legal_section"]
    assert "#### Licensing" in stored
    assert "inspector/entities" not in stored
    assert "🧠 Neotoma" not in stored


def test_spec_key_format():
    assert spec_key("owner/repo", 42) == "owner/repo#42"


def test_section_fields_match_sections():
    assert SECTION_FIELDS == tuple(s.field for s in SECTIONS)

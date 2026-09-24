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
    assert keys == ["basis", "pm", "design", "eng", "qa", "security", "legal"]


def test_design_basis_is_first_and_not_its_own_dispatch():
    """The design basis is stated by Pavo INSIDE the pm turn, so it is first in
    the mirrored spec but never a section the pipeline dispatches on its own
    (an extra agent run per issue is the cost this avoids)."""
    from issue_spec import DESIGN_BASIS, DISPATCHED_SECTIONS

    assert SECTIONS[0] is DESIGN_BASIS
    assert DESIGN_BASIS.agent == "pavo" and DESIGN_BASIS.lens == "pm"
    assert not DESIGN_BASIS.dispatched
    assert DESIGN_BASIS not in DISPATCHED_SECTIONS
    assert [s.key for s in DISPATCHED_SECTIONS] == [
        "pm", "design", "eng", "qa", "security", "legal"
    ]
    # The by-agent / by-lens maps must still resolve pavo / pm to the pm
    # SECTION — the basis shares both keys and must not shadow it.
    assert SECTION_BY_AGENT["pavo"].key == "pm"


def test_extract_design_basis_fenced_and_no_fallback():
    from issue_spec import extract_design_basis

    out = extract_design_basis(
        "narration\n<<<DESIGN_BASIS>>>\nDesign basis: docs/foundation/"
        "work_model.md#claim-and-lease\n<<<END_DESIGN_BASIS>>>\nmore"
    )
    assert out == "Design basis: docs/foundation/work_model.md#claim-and-lease"
    # No fences → '' (never inferred from prose), unlike the spec extractor.
    assert extract_design_basis("Design basis: docs/foundation/x.md") == ""
    assert extract_design_basis("") == "" and extract_design_basis(None) == ""
    assert extract_design_basis("<<<END_DESIGN_BASIS>>> x <<<DESIGN_BASIS>>>") == ""


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


# ── ateles#499: load() must query TARGETED, and a stale idempotency key ─────
# ── must not 400 forever once an entity it missed already exists ────────────
#
# Reproduces the real prod failure (474 store-failed 400s since 2026-08-09,
# e.g. markmhendrickson/ateles#403 and #1189): load() queried ONE UNFILTERED
# page (`limit: 200`, no snapshot_filters, no cursor walk) of the WHOLE
# issue_spec corpus. With 900+ entities in prod, an issue whose entity sorted
# past page 1 was invisible to load(), so upsert_section took the CREATE
# branch for an issue that already HAD an entity. Neotoma's identity resolver
# recognizes the repo+issue_number collision, but the create branch's
# idempotency key (`issue-spec-create-{key}`, a bare per-issue CONSTANT) had
# already been consumed by that issue's real first create with DIFFERENT
# content, so every subsequent mis-detected retry got a permanent, actionable
# ERR_IDEMPOTENCY_MISMATCH 400 — reproduced live against prod Neotoma with
# `commit: false` during diagnosis (see the PR description).
#
# The fix replaces the unfiltered scan with a TARGETED entities/query using
# snapshot_filters on {repo, issue_number} — the entity's own identity rule
# (confirmed live: composite:repo+issue_number) — as the DEFAULT path, not a
# scan. Hosted Neotoma is capacity-constrained (a bulk read already crashed
# it once, neotoma#2483, 2026-09-23), so a full-corpus scan is kept ONLY as a
# bounded fallback for when the targeted query itself errors, never
# triggered merely by finding zero matches.


class _PaginatingStubStore(IssueSpecStore):
    """Simulates a Neotoma corpus, honoring ``snapshot_filters`` on
    entities/query the way prod does, with a large enough corpus that an
    UNFILTERED query needs cursor pagination to reach a later row.

    ``page_size`` bounds an unfiltered (fallback-scan) page; a cursor threads
    through until the corpus is exhausted. ``fail_targeted_queries`` makes any
    call carrying ``snapshot_filters`` return an error (None), so tests can
    exercise load()'s bounded-fallback path deliberately.
    """

    def __init__(self, page_size=2, extra_entities=0, fail_targeted_queries=False):
        super().__init__(base_url="http://x", token="tok")
        self.calls = []
        self._server = {}
        self._next_id = 1
        self.page_size = page_size
        self.fail_targeted_queries = fail_targeted_queries
        # Pad the corpus with unrelated entities BEFORE the real one, so an
        # UNFILTERED, non-paginating scan misses it (mirrors prod: 900+
        # issue_spec rows, sorted by entity_id, with any one issue's row
        # potentially anywhere). The targeted query never needs to see these.
        for i in range(extra_entities):
            eid = f"ent_padding_{i:04d}"
            self._server[("padding/repo", i)] = {
                "entity_id": eid,
                "snapshot": {"repo": "padding/repo", "issue_number": i},
            }
        # Idempotency-key ledger: key -> content signature of the payload that
        # consumed it, so a mismatched replay can be detected like the real
        # Neotoma /store endpoint does.
        self._idempotency: dict[str, str] = {}

    def _content_signature(self, payload: dict) -> str:
        import json as _json

        return _json.dumps(payload.get("entities"), sort_keys=True, default=str)

    async def _post(self, path, payload):
        self.calls.append((path, payload))
        if path == "store":
            idem_key = payload.get("idempotency_key")
            sig = self._content_signature(payload)
            if idem_key is not None:
                prior_sig = self._idempotency.get(idem_key)
                if prior_sig is not None and prior_sig != sig:
                    # Real Neotoma behaviour: same key, different content ->
                    # 400 ERR_IDEMPOTENCY_MISMATCH, and _post's real
                    # implementation stashes the code on self.last_error_code.
                    self.last_error_code = "ERR_IDEMPOTENCY_MISMATCH"
                    self.last_error_status = 400
                    self.last_error = (
                        f'idempotency_key "{idem_key}" was already used with '
                        "different content"
                    )
                    return None
                self._idempotency[idem_key] = sig
            ent = payload["entities"][0]
            key = (ent["repo"], ent["issue_number"])
            snap = {k: v for k, v in ent.items() if k != "entity_type"}
            self.last_error_code = None
            self.last_error_status = None
            self.last_error = None
            existing = self._server.get(key)
            if existing is not None:
                # Real Neotoma resolves identity by the composite
                # repo+issue_number rule regardless of idempotency_key —
                # confirmed live with commit:false against prod during
                # diagnosis (action: "would_match_existing", entities_created:
                # 0). A fresh idempotency key does NOT mint a duplicate
                # entity; it MERGES the new fields into the existing row.
                existing["snapshot"].update(snap)
                return {"entities": [{"entity_id": existing["entity_id"]}]}
            eid = f"ent_{self._next_id}"
            self._next_id += 1
            self._server[key] = {"entity_id": eid, "snapshot": snap}
            return {"entities": [{"entity_id": eid}]}
        if path == "correct":
            for rec in self._server.values():
                if rec["entity_id"] == payload["entity_id"]:
                    rec["snapshot"][payload["field"]] = payload["value"]
            self.last_error_code = None
            self.last_error_status = None
            self.last_error = None
            return {}
        if path == "entities/query":
            self.last_error_code = None
            self.last_error_status = None
            self.last_error = None
            filters = payload.get("snapshot_filters")
            if filters:
                if self.fail_targeted_queries:
                    self.last_error_code = "ERR_UPSTREAM_TIMEOUT"
                    self.last_error_status = 502
                    self.last_error = "simulated targeted-query failure"
                    return None
                # Real Neotoma's snapshot_filters: eq match on each named
                # field, exactly the identity fields (repo, issue_number).
                want_repo = filters.get("repo", {}).get("value")
                want_issue = filters.get("issue_number", {}).get("value")
                matches = [
                    r
                    for r in self._server.values()
                    if r["snapshot"].get("repo") == want_repo
                    and str(r["snapshot"].get("issue_number")) == str(want_issue)
                ]
                return {
                    "entities": [
                        {"entity_id": r["entity_id"], "snapshot": r["snapshot"]}
                        for r in matches
                    ]
                }
            # Unfiltered (full-scan fallback) path: paginate.
            cursor = payload.get("cursor")
            all_items = list(self._server.values())
            start = int(cursor) if cursor else 0
            page = all_items[start : start + self.page_size]
            next_start = start + self.page_size
            resp = {
                "entities": [
                    {"entity_id": r["entity_id"], "snapshot": r["snapshot"]}
                    for r in page
                ]
            }
            if next_start < len(all_items):
                resp["next_cursor"] = str(next_start)
            return resp
        return {}


def test_load_uses_a_targeted_query_not_a_full_corpus_scan():
    """RED on the pre-fix load(): it read one UNFILTERED page of the whole
    corpus (limit: 200, no snapshot_filters). Hosted Neotoma is capacity-
    constrained (a bulk read already crashed it once, neotoma#2483) — the
    fix must query the ONE entity load() actually wants via
    snapshot_filters={repo, issue_number}, never scan the corpus by default.

    5 padding rows (entities load() has no reason to ever see) would force
    3+ pages under the OLD unfiltered pagination; a targeted query finds the
    real entity in exactly one call, filtered, regardless of corpus size.
    """
    store = _PaginatingStubStore(page_size=2, extra_entities=5)
    state = asyncio.run(
        store.upsert_section(
            SpecState(repo="owner/repo", issue_number=1189, title="T"),
            _section("pm"),
            "PM scope",
        )
    )
    assert state.entity_id, "entity should have been created"

    reloaded = asyncio.run(store.load("owner/repo", 1189, "T"))
    assert reloaded.entity_id == state.entity_id, (
        "load() must find the entity via a targeted query regardless of how "
        "many unrelated rows exist in the corpus"
    )
    assert reloaded.sections.get("pm_section") == "PM scope"

    query_calls = [c for c in store.calls if c[0] == "entities/query"]
    # Exactly ONE entities/query call for the load() above (the create
    # itself makes no query call) — this is the "small reads" contract: a
    # targeted lookup costs one request no matter the corpus size, where the
    # pre-fix pagination cost N requests proportional to corpus size.
    assert len(query_calls) == 1, (
        "load() must resolve in ONE targeted entities/query call, not a "
        f"multi-page scan; got {len(query_calls)} call(s)"
    )
    only_call = query_calls[0][1]
    assert only_call.get("snapshot_filters") == {
        "repo": {"op": "eq", "value": "owner/repo"},
        "issue_number": {"op": "eq", "value": 1189},
    }, (
        "load()'s entities/query call must carry snapshot_filters on repo + "
        f"issue_number, not scan unfiltered; got {only_call}"
    )
    # And it must never have paged through the corpus with cursor — the
    # padding rows exist specifically to catch a regression to the old
    # unfiltered-scan behaviour.
    assert not any("cursor" in c[1] for c in query_calls)


def test_load_falls_back_to_bounded_scan_only_when_the_targeted_query_errors():
    """The full-corpus scan is a FALLBACK for a targeted-query ERROR only —
    never the default path, and never triggered merely by zero matches (a
    genuinely new issue has zero matches on the targeted query too).
    """
    # Case 1: targeted query errors -> falls back and still finds the entity.
    store = _PaginatingStubStore(
        page_size=2, extra_entities=5, fail_targeted_queries=True
    )
    # Pre-seed the "existing entity" directly in the stub's server, bypassing
    # upsert_section (which would itself hit the failing targeted query).
    store._server[("owner/repo", 1189)] = {
        "entity_id": "ent_preexisting",
        "snapshot": {
            "repo": "owner/repo",
            "issue_number": 1189,
            "pm_section": "PM scope",
        },
    }
    reloaded = asyncio.run(store.load("owner/repo", 1189, "T"))
    assert reloaded.entity_id == "ent_preexisting", (
        "when the targeted query errors, load() must fall back to the "
        "bounded full-corpus scan rather than giving up"
    )
    query_calls = [c for c in store.calls if c[0] == "entities/query"]
    # The failed targeted attempt, THEN at least one unfiltered fallback page.
    assert query_calls[0][1].get("snapshot_filters"), (
        "the targeted query must still be tried FIRST, even though it will "
        "fail in this test"
    )
    assert any(not c[1].get("snapshot_filters") for c in query_calls[1:]), (
        "a fallback scan must have run after the targeted query errored"
    )

    # Case 2: targeted query SUCCEEDS with zero matches (a genuinely new
    # issue) -> must NOT fall back to a full scan at all.
    store2 = _PaginatingStubStore(page_size=2, extra_entities=5)
    reloaded2 = asyncio.run(store2.load("owner/repo", 99999, "T"))
    assert reloaded2.entity_id == ""
    query_calls2 = [c for c in store2.calls if c[0] == "entities/query"]
    assert len(query_calls2) == 1, (
        "zero matches on the targeted query is a normal outcome (a new "
        "issue) and must NOT trigger a full-corpus fallback scan; got "
        f"{len(query_calls2)} entities/query call(s)"
    )


def test_load_fails_closed_on_an_ambiguous_multi_match():
    """More than one match for repo+issue_number violates the entity's own
    identity rule (composite:repo+issue_number, confirmed live against prod)
    — load() must refuse to guess which row is authoritative rather than
    silently picking one and risking a correction against the wrong entity.
    """
    store = _PaginatingStubStore()
    store._server[("owner/repo", 1189)] = {
        "entity_id": "ent_a",
        "snapshot": {"repo": "owner/repo", "issue_number": 1189},
    }
    # Same (repo, issue_number) tuple can't literally be a second dict key,
    # so simulate the ambiguous-server-response shape directly.
    store._server[("owner/repo", 1189, "dup")] = {
        "entity_id": "ent_b",
        "snapshot": {"repo": "owner/repo", "issue_number": 1189},
    }

    state = asyncio.run(store.load("owner/repo", 1189, "T"))
    assert state.entity_id == "", (
        "an ambiguous (>1 match) targeted query result must fail CLOSED — "
        "no entity_id picked — rather than guessing"
    )


def test_missed_entity_does_not_400_forever_on_stale_idempotency_key():
    """RED on pre-fix upsert_section: a second dispatch for the SAME issue
    after load() (transiently) missed the entity must not permanently fail.

    Simulates the real failure sequence: pm section creates the entity
    (consuming idempotency key K with content C1); a later run's load() fails
    to see it and eng's upsert_section takes the CREATE branch again, reusing
    THE SAME per-issue-constant key with DIFFERENT content (C2, the eng
    section text). Pre-fix: the create branch always sent
    `issue-spec-create-{key}` with no variance, so C1 != C2 under the same
    key -> permanent ERR_IDEMPOTENCY_MISMATCH, and pre-fix's reload fallback
    used the SAME unfiltered, un-paginated load() that missed the entity in
    the first place, so it never recovered — the eng section is silently
    lost forever. Post-fix, the reload uses the TARGETED query, which finds
    the entity regardless of corpus size.
    """
    store = _PaginatingStubStore(page_size=500, extra_entities=0)
    state = asyncio.run(
        store.upsert_section(
            SpecState(repo="owner/repo", issue_number=1189, title="T"),
            _section("pm"),
            "PM scope",
        )
    )
    real_entity_id = state.entity_id
    assert real_entity_id

    # Simulate a subsequent dispatch whose load() missed the already-created
    # entity (the ateles#499 scenario) by starting a FRESH SpecState with no
    # entity_id, as load() would hand back on a miss.
    fresh_state = SpecState(repo="owner/repo", issue_number=1189, title="T")
    fresh_state = asyncio.run(
        store.upsert_section(fresh_state, _section("eng"), "ENG plan")
    )

    assert fresh_state.entity_id == real_entity_id, (
        "after a create collides on a stale idempotency key, upsert_section "
        "must recover the EXISTING entity_id (via a targeted reload) rather "
        "than permanently losing the section — this is the exact shape of "
        "the 474 store-failed 400s in ateles#499"
    )
    # The pm section written by the first call must survive untouched — the
    # recovery path must never overwrite a sibling section.
    server_snap = store._server[("owner/repo", 1189)]["snapshot"]
    assert server_snap.get("pm_section") == "PM scope"
    assert server_snap.get("eng_section") == "ENG plan"


def test_post_captures_status_and_error_code_on_http_error(monkeypatch):
    """RED on pre-fix _post: the response body (status + error_code) that
    Neotoma sends on a 400 was discarded — only the bare exception string
    ("400 Bad Request") reached the log, per ateles#499's own description
    ("store failures logged without a reason").
    """
    import httpx as httpx_mod

    class _FakeResponse:
        status_code = 400
        text = '{"error_code": "ERR_IDEMPOTENCY_MISMATCH", "message": "boom"}'

        def json(self):
            return {"error_code": "ERR_IDEMPOTENCY_MISMATCH", "message": "boom"}

        def raise_for_status(self):
            raise httpx_mod.HTTPStatusError(
                "400 Bad Request", request=None, response=self
            )

        @property
        def content(self):
            return self.text.encode()

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResponse()

    monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **k: _FakeClient())

    store = IssueSpecStore(base_url="http://x", token="tok")
    result = asyncio.run(store._post("store", {"entities": [{}]}))
    assert result is None
    assert store.last_error_status == 400
    assert store.last_error_code == "ERR_IDEMPOTENCY_MISMATCH", (
        "the error_code from the response BODY must be captured, not just "
        "the bare '400 Bad Request' exception string"
    )
    assert store.last_error and "boom" in store.last_error

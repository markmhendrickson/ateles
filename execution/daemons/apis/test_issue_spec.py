"""Tests for the additive issue_spec entity + mirror (issue_spec.py).

Covers the pure helpers (assemble_spec_markdown, splice_managed_block) and the
IssueSpecStore create/correct additive-merge behaviour with a stubbed Neotoma.

The read-path fix (targeted snapshot_filters query + fail-closed-on-ambiguous
match) is carried from PR #497 into entity_lookup.py / entity_lookup.resolve_entity
and is tested in test_issue_spec_entity_lookup.py, not here. This file covers
what remains specific to issue_spec.py: the additive create/correct contract,
and this PR's OWN addition beyond #492/#497's scope — the create branch's
idempotency-key fix and the _post() error-body capture.
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
    _redact,
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
        if path == "entities/query":
            # entity_lookup.resolve_entity's targeted query carries
            # snapshot_filters; honor it so load() finds the right row
            # (mirrors real Neotoma's eq-filter semantics).
            filters = payload.get("snapshot_filters")
            items = self._server.values()
            if filters:
                items = [
                    r
                    for r in items
                    if all(
                        str(r["snapshot"].get(field)) == str(spec["value"])
                        for field, spec in filters.items()
                    )
                ]
            return {
                "entities": [
                    {"entity_id": r["entity_id"], "snapshot": r["snapshot"]}
                    for r in items
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


# ── This PR's own addition: idempotency-key fix + error-body capture ───────
#
# The READ-path fix (targeted snapshot_filters query, fail-closed on an
# ambiguous match) is carried from PR #497 into entity_lookup.py and tested
# in test_issue_spec_entity_lookup.py — not duplicated here.
#
# What remains PR-specific: the real prod failure reproduced live against
# prod Neotoma (commit:false dry-runs; see the PR description) was that
# IssueSpecStore.load() read one UNFILTERED page (limit: 200, no
# snapshot_filters, no cursor walk) of the WHOLE issue_spec corpus. With
# 900+ entities in prod, an issue whose entity sorted past page 1 was
# invisible to load(), so upsert_section took the CREATE branch for an issue
# that already HAD an entity. Neotoma's identity resolver recognizes the
# repo+issue_number collision, but the create branch's idempotency key
# (`issue-spec-create-{key}`, a bare per-issue CONSTANT) had already been
# consumed by that issue's real first create with DIFFERENT content, so
# every subsequent mis-detected retry got a permanent, actionable
# ERR_IDEMPOTENCY_MISMATCH 400 — 474 occurrences since 2026-08-09.
#
# #492/#498 (via #497's entity_lookup.resolve_entity, now adopted in
# issue_spec.py) fixes the READ side. #492 is explicitly read-only in scope
# (its own Security/Arch section: "No idempotency_key requirement applies to
# the lookup path itself") — it does NOT touch the create branch's
# idempotency key, so the 474 400s are NOT fixed by #492/#497 alone. The fix
# below is this PR's own, separate addition.


class _PaginatingStubStore(IssueSpecStore):
    """Stub Neotoma honoring entity_lookup.resolve_entity's real request
    shapes: a targeted snapshot_filters query, then — only if that
    errors — a recency-sorted scan (sort_by/offset, no snapshot_filters).
    Also honors idempotency-key collisions on store, matching real Neotoma's
    behaviour (verified live against prod during diagnosis: same key +
    different content -> 400 ERR_IDEMPOTENCY_MISMATCH; a fresh key ->
    identity-based merge, never a duplicate entity).
    """

    def __init__(self, extra_entities=0):
        super().__init__(base_url="http://x", token="tok")
        self.calls = []
        self._server = {}
        self._next_id = 1
        # Padding rows are irrelevant to a TARGETED query (it filters
        # server-side) but exist so a regression to an unfiltered scan would
        # be caught by a test asserting the query is actually filtered.
        for i in range(extra_entities):
            eid = f"ent_padding_{i:04d}"
            self._server[("padding/repo", i)] = {
                "entity_id": eid,
                "snapshot": {"repo": "padding/repo", "issue_number": i},
            }
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
                # entity_lookup's targeted combos use whichever field-name
                # pair the caller supplied; match generically on ALL filters
                # present in this call (mirrors real snapshot_filters AND
                # semantics).
                matches = [
                    r
                    for r in self._server.values()
                    if all(
                        str(r["snapshot"].get(field)) == str(spec["value"])
                        for field, spec in filters.items()
                    )
                ]
                return {
                    "entities": [
                        {"entity_id": r["entity_id"], "snapshot": r["snapshot"]}
                        for r in matches
                    ]
                }
            # entity_lookup's recency-scan fallback: no snapshot_filters,
            # sort_by/offset instead of the old cursor pagination.
            all_items = list(self._server.values())
            offset = payload.get("offset", 0)
            limit = payload.get("limit", 100)
            page = all_items[offset : offset + limit]
            return {
                "entities": [
                    {"entity_id": r["entity_id"], "snapshot": r["snapshot"]}
                    for r in page
                ]
            }
        return {}


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
    used the SAME defective load() that missed the entity in the first place,
    so it never recovered — the eng section is silently lost forever.
    Post-fix, the reload uses entity_lookup.resolve_entity's targeted query,
    which finds the entity regardless of corpus size.
    """
    store = _PaginatingStubStore(extra_entities=0)
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
    # entity by starting a FRESH SpecState with no entity_id, as load() would
    # hand back on a miss.
    fresh_state = SpecState(repo="owner/repo", issue_number=1189, title="T")
    fresh_state = asyncio.run(
        store.upsert_section(fresh_state, _section("eng"), "ENG plan")
    )

    assert fresh_state.entity_id == real_entity_id, (
        "after a create collides on a stale idempotency key, upsert_section "
        "must recover the EXISTING entity_id (via a targeted reload) rather "
        "than permanently losing the section — this is the exact shape of "
        "the 474 store-failed 400s this PR fixes"
    )
    # The pm section written by the first call must survive untouched — the
    # recovery path must never overwrite a sibling section.
    server_snap = store._server[("owner/repo", 1189)]["snapshot"]
    assert server_snap.get("pm_section") == "PM scope"
    assert server_snap.get("eng_section") == "ENG plan"


def test_double_failure_logs_the_loss_explicitly(caplog):
    """When BOTH the create collides (ERR_IDEMPOTENCY_MISMATCH) AND the
    reload fallback also fails to recover an entity_id, the section write is
    genuinely lost for this run. This must produce one explicit ERROR log
    line naming the section and the issue — the only trace left, since
    nothing downstream consumes any other signal for this branch (see PR
    body: the previous unroutable_ledger call here was removed because
    nothing drains it).

    RED without the log call: deleting the `log.error(...)` line in this
    branch (verified locally by temporarily removing it) makes this test
    fail — there is no other observable effect of this branch to assert on,
    which is exactly why QA's original finding ("no test for this branch")
    could not be answered by testing the ledger call: there was nothing true
    to test until this log line became the branch's only real effect.
    """
    import logging

    class _DoubleFailureStore(IssueSpecStore):
        """First create attempt succeeds (plants pm). Second call always
        400s with ERR_IDEMPOTENCY_MISMATCH on create AND always fails the
        reload (entities/query returns None), so the double-failure branch
        is reached deterministically."""

        def __init__(self):
            super().__init__(base_url="http://x", token="tok")
            self._pm_planted = False

        async def _post(self, path, payload):
            if path == "store":
                # Every create attempt "collides" — simulates an entity that
                # already exists under a key this store already consumed.
                self.last_error_code = "ERR_IDEMPOTENCY_MISMATCH"
                self.last_error_status = 400
                self.last_error = "idempotency_key already used"
                return None
            if path == "entities/query":
                # The reload fallback ALSO fails outright (e.g. Neotoma is
                # unreachable) — the double-failure case.
                self.last_error_code = "ERR_UPSTREAM_TIMEOUT"
                self.last_error_status = 502
                self.last_error = "simulated reload failure"
                return None
            return {}

    store = _DoubleFailureStore()
    with caplog.at_level(logging.ERROR, logger="apis.issue_spec"):
        state = asyncio.run(
            store.upsert_section(
                SpecState(repo="owner/repo", issue_number=1189, title="T"),
                _section("eng"),
                "ENG plan",
            )
        )

    assert state.entity_id == "", "no entity_id can be recovered in this branch"
    error_messages = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
    ]
    matching = [
        m
        for m in error_messages
        if "eng" in m and "owner/repo#1189" in m and "LOST this run" in m
    ]
    assert matching, (
        "the double-failure branch (create collides AND reload also fails) "
        "must log exactly one ERROR line naming the section key ('eng'), "
        "the issue ('owner/repo#1189'), and that the write was lost — got: "
        f"{error_messages!r}"
    )


def test_post_captures_status_and_error_code_on_http_error(monkeypatch):
    """RED on pre-fix _post: the response body (status + error_code) that
    Neotoma sends on a 400 was discarded — only the bare exception string
    ("400 Bad Request") reached the log.
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


# ── _redact() — belt-and-braces Authorization-stripping (QA gap) ────────────


def test_redact_strips_header_form_bearer_token():
    """Header-shaped 'Authorization: Bearer <token>' must be redacted.

    RED without the fix: an earlier revision of _redact's regex anchored on
    a literal colon only (no `[:=]` alternation), which fails to match a
    body echoing the header in query-string/JSON-assignment form
    (`authorization=Bearer ...`) — proven by temporarily narrowing the
    pattern to `:` only, see the sibling assertion below.
    """
    out = _redact("Authorization: Bearer sk-abc123XYZsecret")
    assert "sk-abc123XYZsecret" not in out
    assert "<redacted>" in out
    assert out == "Authorization: <redacted>"


def test_redact_strips_json_assignment_form_bearer_token():
    """A JSON-string-embedded 'authorization': 'Bearer ...' must also redact
    (the `[:=]` alternation + optional quote handling is what this proves)."""
    body = '{"authorization": "Bearer sk-realtoken999", "message": "boom"}'
    out = _redact(body)
    assert "sk-realtoken999" not in out
    assert "<redacted>" in out
    assert '"message": "boom"' in out, "unrelated fields must survive untouched"


def test_redact_case_insensitive_and_leaves_non_auth_text_alone():
    out = _redact("AUTHORIZATION: BEARER sk-xyz")
    assert "sk-xyz" not in out.lower() or "<redacted>" in out
    # Ordinary error text must pass through completely unchanged.
    plain = '{"error_code": "ERR_IDEMPOTENCY_MISMATCH", "message": "boom"}'
    assert _redact(plain) == plain


def test_redact_regex_is_exercised_not_vacuous():
    """Proves the test above can actually fail: a regex requiring a literal
    colon (rejecting the `=` form real error bodies could carry, e.g. a
    query-string echo) misses the assignment-form case.

    This mirrors the "colon-only" regex verified RED here, then confirms the
    real _redact (which alternates `[:=]`) is not that narrower pattern.
    """
    import re

    colon_only = re.compile(
        r'(?i)(authorization["\']?\s*:\s*["\']?)bearer\s+\S+'
    )
    assignment_form = 'authorization=Bearer sk-shouldberedacted'
    # RED: the narrower colon-only pattern does NOT catch the `=` form.
    assert colon_only.sub(r"\1<redacted>", assignment_form) == assignment_form, (
        "sanity check: a colon-only regex must NOT catch the assignment "
        "form — if this assertion itself fails, the sanity check is broken"
    )
    # GREEN: the real _redact DOES catch it (uses [:=] alternation).
    out = _redact(assignment_form)
    assert "sk-shouldberedacted" not in out
    assert "<redacted>" in out

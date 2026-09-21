"""
Tests for render_positioning_docs.py and the shared neotoma_mirror_lib.py
observation-id stamping helper.

Two kinds of test here:
  1. Pure-function tests (PII scrub, frontmatter/observation-id rendering) —
     no network, always run.
  2. A filesystem doc-sync contract test — asserts every file under
     docs/positioning/ carries the generated-mirror header and, when it has
     an `observation_ids:` block, that every entry in it is either a
     plausible observation id or the literal `unknown` sentinel. This is a
     pure filesystem check (same pattern as test_render_agent_docs.py) and
     always runs; it does not require live Neotoma.

Run with: pytest execution/scripts/test_render_positioning_docs.py -v
"""

from __future__ import annotations

import io
import re
import sys
import urllib.error
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

import render_positioning_docs as rpd  # noqa: E402
import neotoma_mirror_lib as nml  # noqa: E402
from neotoma_mirror_lib import observation_ids_block, yaml_scalar  # noqa: E402

POSITIONING_DIR = _REPO_ROOT / "docs" / "positioning"
OBS_ID_LINE_RE = re.compile(r"^\s+([a-zA-Z0-9_]+):\s+(unknown|[0-9a-f-]{8,}(?:,[0-9a-f-]{8,})*)\s*$")  # a field merged from several observations is a comma-joined list


@pytest.fixture(autouse=True)
def _clean_type_cache():
    rpd._ENTITY_TYPE_CACHE.clear()
    yield
    rpd._ENTITY_TYPE_CACHE.clear()


class TestObservationIdsBlock:
    def test_stamps_every_requested_field(self) -> None:
        provenance = {"a": "obs-1", "b": "obs-2"}
        lines = observation_ids_block(provenance, ("a", "b", "c"))
        assert lines[0] == "observation_ids:"
        assert "  a: obs-1" in lines
        assert "  b: obs-2" in lines
        # A field absent from provenance is stamped `unknown`, never omitted —
        # an unstamped field must read as "freshness not verifiable," never as
        # "this field is fine" (fail-closed discipline).
        assert "  c: unknown" in lines

    def test_empty_provenance_stamps_all_unknown(self) -> None:
        lines = observation_ids_block({}, ("x", "y"))
        assert lines == ["observation_ids:", "  x: unknown", "  y: unknown"]


class TestYamlScalar:
    def test_plain_scalar_unquoted(self) -> None:
        assert yaml_scalar("hello") == "hello"

    def test_scalar_with_colon_is_quoted(self) -> None:
        assert yaml_scalar("a: b") == '"a: b"'

    def test_empty_string_quoted(self) -> None:
        assert yaml_scalar("") == '""'


class TestEvaluatorPiiScrub:
    def test_known_names_are_scrubbed(self) -> None:
        text = "Rebecca reported an issue; Simon Bergeron agreed."
        scrubbed = rpd._scrub_evaluator_pii(text)
        assert "Rebecca" not in scrubbed
        assert "Bergeron" not in scrubbed
        assert "an evaluator" in scrubbed

    def test_known_entity_ids_are_scrubbed(self) -> None:
        text = "see (ent_aaa12264742df8597df6b15f) for detail"
        scrubbed = rpd._scrub_evaluator_pii(text)
        assert "ent_aaa12264742df8597df6b15f" not in scrubbed
        assert "[evaluator-record]" in scrubbed

    def test_source_list_drops_customer_development_note_markers(self) -> None:
        items = [
            "customer_development_note ent_f9bfda2a86f54dc1b72962cb (a call)",
            "docs/icp.md @ origin/main",
        ]
        kept = rpd._scrub_source_list(items)
        assert len(kept) == 1
        assert "docs/icp.md" in kept[0]

    def test_unrelated_text_untouched(self) -> None:
        text = "LangGraph's Item class has no attribution field."
        assert rpd._scrub_evaluator_pii(text) == text


UNLISTED_FEEDBACK_ID = "ent_" + "1" * 24  # not in _PII_ENTITY_IDS
UNLISTED_ANALYSIS_ID = "ent_" + "2" * 24
UNRESOLVED_ID = "ent_" + "3" * 24


class TestEntityIdScrubFailsClosedByType:
    """The scrub must not depend on a hand-maintained id list: an evaluator
    record cited by a NEW id has to be scrubbed because of its TYPE. Before
    this change the scrub was a pure allowlist of six ids and passed every
    other id through, so the first three tests below were red."""

    def test_unresolved_unknown_id_is_scrubbed(self) -> None:
        out = rpd._scrub_evaluator_pii(f"see ({UNLISTED_FEEDBACK_ID}) for detail")
        assert UNLISTED_FEEDBACK_ID not in out
        assert "[restricted-record]" in out

    def test_feedback_typed_id_is_scrubbed_when_resolved(self) -> None:
        rpd._ENTITY_TYPE_CACHE[UNLISTED_FEEDBACK_ID] = "feedback"
        out = rpd._scrub_evaluator_pii(f"evidence {UNLISTED_FEEDBACK_ID}")
        assert UNLISTED_FEEDBACK_ID not in out

    def test_safe_typed_id_is_kept(self) -> None:
        rpd._ENTITY_TYPE_CACHE[UNLISTED_ANALYSIS_ID] = "analysis"
        assert UNLISTED_ANALYSIS_ID in rpd._scrub_evaluator_pii(f"see {UNLISTED_ANALYSIS_ID}")

    def test_registered_entity_id_is_kept(self) -> None:
        registered = rpd.POSITIONING_ENTITIES["ateles"][0][0]
        assert registered in rpd._scrub_evaluator_pii(f"see {registered}")

    def test_hand_listed_pii_id_wins_even_if_typed_safe(self) -> None:
        listed = next(iter(rpd._PII_ENTITY_IDS))
        rpd._ENTITY_TYPE_CACHE[listed] = "analysis"
        assert listed not in rpd._scrub_evaluator_pii(f"see {listed}")

    def test_word_like_name_is_case_sensitive(self) -> None:
        # "Mark" is scrubbed as a name but the verb "mark" is ordinary prose.
        out = rpd._scrub_evaluator_pii("Mark said to mark the file")
        assert "the operator said to mark the file" == out

    def test_names_are_matched_case_insensitively(self) -> None:
        # Before: `\bRebecca\b` had no IGNORECASE, so these survived.
        out = rpd._scrub_evaluator_pii("rebecca and LARRY and rEbEcCa")
        assert "rebecca" not in out.lower() and "larry" not in out.lower()

    def test_resolve_cited_types_fills_cache_and_scrubs_new_feedback(self, monkeypatch) -> None:
        reg_id, reg_type, _slug = rpd.POSITIONING_ENTITIES["ateles"][0]
        snapshots = {reg_id: ({f: f"cites {UNLISTED_FEEDBACK_ID} and {UNLISTED_ANALYSIS_ID}" for f in rpd.STAMPED_FIELDS[reg_type]}, {})}
        types = {UNLISTED_FEEDBACK_ID: "feedback", UNLISTED_ANALYSIS_ID: "analysis"}

        def fake_request(url, token, payload=None, retries=5):
            return {"entity_type": types[url.rsplit("/", 1)[1]]}

        monkeypatch.setattr(rpd, "request", fake_request)
        rpd.resolve_cited_types("http://x", "t", snapshots)
        assert rpd._ENTITY_TYPE_CACHE == types
        rendered = rpd._scrub_evaluator_pii(f"{UNLISTED_FEEDBACK_ID} {UNLISTED_ANALYSIS_ID}")
        assert UNLISTED_FEEDBACK_ID not in rendered
        assert UNLISTED_ANALYSIS_ID in rendered

    def test_lookup_404_is_scrubbed_not_fatal(self, monkeypatch) -> None:
        def fake_request(url, token, payload=None, retries=5):
            raise nml.NeotomaHTTPError(404, "gone")

        monkeypatch.setattr(rpd, "request", fake_request)
        assert rpd.lookup_entity_type("http://x", "t", UNRESOLVED_ID) is None

    def test_lookup_auth_failure_is_fatal_not_swallowed(self, monkeypatch) -> None:
        def fake_request(url, token, payload=None, retries=5):
            raise nml.NeotomaHTTPError(401, "denied")

        monkeypatch.setattr(rpd, "request", fake_request)
        with pytest.raises(nml.NeotomaHTTPError):
            rpd.lookup_entity_type("http://x", "t", UNRESOLVED_ID)

    def test_end_to_end_render_scrubs_new_feedback_citation(self, monkeypatch) -> None:
        first = {p: es[:1] for p, es in list(rpd.POSITIONING_ENTITIES.items())[:1]}
        monkeypatch.setattr(rpd, "POSITIONING_ENTITIES", first)
        (product, [(eid, etype, slug)]) = next(iter(first.items()))
        snap = {f: "" for f in rpd.STAMPED_FIELDS[etype]}
        snap["name"] = "Persona"
        snap["archetype"] = f"backed by {UNLISTED_FEEDBACK_ID}"
        monkeypatch.setattr(rpd, "fetch_entity", lambda b, t, i: (snap, {}))
        monkeypatch.setattr(rpd, "request", lambda url, token, payload=None, retries=5: {"entity_type": "feedback"})
        targets, _ = rpd._targets_for_registry("http://x", "t")
        (content,) = targets.values()
        assert UNLISTED_FEEDBACK_ID not in content
        assert "[restricted-record]" in content


class TestUnknownNameSentinel:
    """A newly-cited evaluator whose NAME is not in _EVALUATOR_NAME_MAP must
    abort the render instead of leaking. Before this change nothing checked:
    a live entity cited two such evaluators (name + record id) and both
    rendered in plaintext."""

    def test_unknown_name_before_citation_aborts(self) -> None:
        with pytest.raises(rpd.UnscrubbedEvaluatorName):
            rpd._scrub_evaluator_pii(f"Zed Quuxley ({UNLISTED_FEEDBACK_ID}) said it was slow")

    def test_unknown_name_before_dash_citation_aborts(self) -> None:
        with pytest.raises(rpd.UnscrubbedEvaluatorName):
            rpd._scrub_evaluator_pii(f"an evaluator Zed Quuxley {UNLISTED_FEEDBACK_ID}")

    def test_unknown_name_after_citation_aborts(self) -> None:
        with pytest.raises(rpd.UnscrubbedEvaluatorName):
            rpd._scrub_evaluator_pii(f"{UNLISTED_FEEDBACK_ID} (Zed Quuxley, ~1 month)")

    def test_known_name_is_scrubbed_not_aborted(self) -> None:
        out = rpd._scrub_evaluator_pii(f"Sidney Brown ({UNLISTED_FEEDBACK_ID})")
        assert "Sidney" not in out and "Brown" not in out

    def test_non_person_phrase_next_to_citation_is_allowed(self) -> None:
        out = rpd._scrub_evaluator_pii(f"Claude Projects ({UNLISTED_FEEDBACK_ID})")
        assert "Claude Projects" in out

    def test_error_message_does_not_echo_the_name(self) -> None:
        with pytest.raises(rpd.UnscrubbedEvaluatorName) as ei:
            rpd._scrub_evaluator_pii(f"Zed Quuxley ({UNLISTED_FEEDBACK_ID})")
        assert "Quuxley" not in str(ei.value)


class TestGeneratedMarkerCoupling:
    def test_header_contains_orphan_marker(self) -> None:
        assert rpd.GENERATED_MARKER in rpd.HEADER


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestRequestErrorClassification:
    """neotoma_mirror_lib.request(): HTTPError is a URLError subclass, so it
    used to be retried 5x and reported as 'unreachable'. Before the fix,
    test_401_fails_fast_without_retry saw 5 attempts."""

    def _run(self, monkeypatch, exc):
        calls = {"n": 0, "sleeps": 0}

        def fake_urlopen(req, timeout=0):
            calls["n"] += 1
            raise exc

        monkeypatch.setattr(nml.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(nml.time, "sleep", lambda s: calls.__setitem__("sleeps", calls["sleeps"] + 1))
        return calls

    def _http(self, code):
        return urllib.error.HTTPError("http://x/e", code, "msg", {}, io.BytesIO(b""))

    @pytest.mark.parametrize("code", [401, 403, 404])
    def test_4xx_fails_fast_without_retry_and_names_status(self, monkeypatch, code) -> None:
        calls = self._run(monkeypatch, self._http(code))
        with pytest.raises(nml.NeotomaHTTPError) as ei:
            nml.request("http://x/e", "tok")
        assert calls["n"] == 1 and calls["sleeps"] == 0
        assert ei.value.status == code
        msg = str(ei.value)
        assert f"HTTP {code}" in msg and "unreachable" not in msg
        assert "tok" not in msg.replace("token", "")

    def test_5xx_is_retried_then_reported(self, monkeypatch) -> None:
        calls = self._run(monkeypatch, self._http(503))
        with pytest.raises(SystemExit) as ei:
            nml.request("http://x/e", "tok", retries=3)
        assert calls["n"] == 3
        assert "503" in str(ei.value)

    def test_network_error_is_retried(self, monkeypatch) -> None:
        calls = self._run(monkeypatch, urllib.error.URLError("boom"))
        with pytest.raises(SystemExit) as ei:
            nml.request("http://x/e", "tok", retries=3)
        assert calls["n"] == 3
        assert "unreachable" in str(ei.value)


class TestRenderEntityFile:
    def test_target_persona_renders_frontmatter_and_body(self) -> None:
        snapshot = {
            "name": "Test persona",
            "product": "Ateles",
            "archetype": "A solo operator",
            "messaging_bad": "Not for Rebecca-style enterprise teams",
        }
        provenance = {"name": "obs-1", "product": "obs-1", "archetype": "obs-2"}
        content = rpd.render_entity_file(
            "ent_test000000000000000000", "target_persona", snapshot, provenance
        )
        assert "entity_id: ent_test000000000000000000" in content
        assert "entity_type: target_persona" in content
        assert "observation_ids:" in content
        assert "## Archetype" in content
        assert "A solo operator" in content
        # PII scrub applies to body sections too.
        assert "Rebecca" not in content
        assert "an evaluator" in content

    def test_generated_mirror_header_present(self) -> None:
        content = rpd.render_entity_file(
            "ent_test000000000000000000", "target_persona", {"name": "x"}, {}
        )
        assert "generated mirror of a Neotoma entity" in content
        assert "Do not edit this file directly" in content


class TestDocSyncFilesystemContract:
    """Pure filesystem checks — always run, no live Neotoma required.

    Mirrors test_render_agent_docs.py's pattern: assert properties of
    whatever is currently checked in under docs/positioning/, rather than
    re-fetching from Neotoma.
    """

    def test_every_positioning_file_carries_generated_header(self) -> None:
        if not POSITIONING_DIR.exists():
            return  # nothing rendered yet in this checkout — not a failure
        for path in POSITIONING_DIR.rglob("*.md"):
            content = path.read_text()
            assert "Do not edit this file directly" in content, (
                f"{path} is under docs/positioning/ but has no generated-mirror header"
            )

    def test_observation_id_lines_are_well_formed(self) -> None:
        if not POSITIONING_DIR.exists():
            return
        for path in POSITIONING_DIR.rglob("*.md"):
            if path.name == "README.md":
                continue
            content = path.read_text()
            if "observation_ids:" not in content:
                continue
            block = content.split("observation_ids:", 1)[1].split("---", 1)[0]
            for line in block.splitlines():
                if not line.strip():
                    continue
                assert OBS_ID_LINE_RE.match(line), f"{path}: malformed observation_ids line: {line!r}"

    def test_no_known_evaluator_name_leaks_into_positioning_docs(self) -> None:
        if not POSITIONING_DIR.exists():
            return
        banned = ("Jeremiah Lee", "Simon Bergeron")
        for path in POSITIONING_DIR.rglob("*.md"):
            content = path.read_text()
            for name in banned:
                assert name not in content, f"{path} leaks evaluator name {name!r}"

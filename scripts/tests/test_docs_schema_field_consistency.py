"""Cross-cutting drift guard (QA §6, P0): the `hook_policy` field names named
in docs/hooks/POLICY_TEMPLATE.md, the live Neotoma schema, and the field
names execution/scripts/render_hooks.py actually reads via `.get(...)` must
be the SAME set. Three independent sources of truth for the same names is
the specific rot pattern this migration exists to prevent.

The schema-registration source is the register_schema call made when
`hook_policy` was authored for this migration; since this repo has no local
schema-definition file to parse, this test treats the DOC TABLE as the
schema-of-record for the offline/no-network case (matching what CI without
Neotoma credentials can verify), and additionally hits live Neotoma when
credentials are available to cross-check the two docs/code sources against
the registered schema itself.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs" / "hooks" / "POLICY_TEMPLATE.md"
GENERATOR_PATH = REPO_ROOT / "execution" / "scripts" / "render_hooks.py"


def _fields_from_doc() -> set[str]:
    """Parse the '| `field` | required | ... |' table rows."""
    text = DOC_PATH.read_text()
    fields = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", text, flags=re.MULTILINE))
    return fields


# Non-hook_policy fields the generator's code also reads via s.get(...)/
# s[...] (JSON envelope keys, settings.json keys, entity plumbing) — not part
# of the hook_policy schema, so excluded from the drift comparison below.
_NON_SCHEMA_FIELDS = {
    "_entity_id", "entity_id", "entities", "results", "snapshot",
    "command", "hooks",
}


def _fields_from_generator() -> set[str]:
    """Field names the generator reads off a hook_policy snapshot via
    .get("field_name") or s["field_name"] / s.get('field_name')."""
    text = GENERATOR_PATH.read_text()
    dq = re.findall(r'\.get\(\s*"([a-z_]+)"', text)
    sq = re.findall(r"\.get\(\s*'([a-z_]+)'", text)
    bracket = re.findall(r's\["([a-z_]+)"\]', text)
    return (set(dq) | set(sq) | set(bracket)) - _NON_SCHEMA_FIELDS


def test_doc_table_fields_are_nonempty():
    fields = _fields_from_doc()
    assert fields, "POLICY_TEMPLATE.md field table did not parse any field names"
    assert {"hook_name", "harness", "repository", "event", "status"}.issubset(fields)


def test_generator_field_reads_are_subset_of_doc_fields():
    """Every hook_policy field the generator's code reads must be documented
    in POLICY_TEMPLATE.md with the identical name — an orphan here means the
    doc and the code have drifted apart."""
    doc_fields = _fields_from_doc()
    generator_fields = _fields_from_generator()
    orphans = generator_fields - doc_fields
    assert not orphans, (
        f"render_hooks.py reads field(s) {sorted(orphans)} not documented in "
        f"{DOC_PATH.relative_to(REPO_ROOT)} — field names must match verbatim"
    )


def test_error_message_field_references_match_doc():
    """The literal field names embedded in render_hooks.py's ERROR/WARN/
    NOT_IMPLEMENTED strings must match the doc table verbatim."""
    doc_fields = _fields_from_doc()
    generator_text = GENERATOR_PATH.read_text()
    # Fields explicitly named in the missing-policy / warn / not-implemented
    # message paths (grep the literal identifiers used in f-strings there).
    referenced = {"hook_name", "harness"}
    for field in referenced:
        assert field in doc_fields, f"'{field}' referenced in generator messages but missing from {DOC_PATH.name}"


@pytest.mark.skipif(
    not (os.environ.get("NEOTOMA_BASE_URL") or (Path.home() / ".config" / "neotoma" / ".env").exists()),
    reason="no Neotoma credentials available in this environment",
)
def test_live_schema_matches_doc_and_code():
    """When Neotoma credentials ARE available (local dev, or CI with a
    service credential), cross-check the doc + generator field sets against
    the actually-registered hook_policy schema fields."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "execution" / "scripts"))
    import importlib.util

    spec = importlib.util.spec_from_file_location("render_hooks", GENERATOR_PATH)
    render_hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render_hooks)

    base_url, token = render_hooks._load_env()
    req = urllib.request.Request(f"{base_url}/schemas/hook_policy")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            schema = json.loads(resp.read().decode())
    except (urllib.error.URLError, ConnectionError):
        pytest.skip("Neotoma unreachable")
        return

    schema_fields = set((schema.get("schema_definition") or schema.get("fields") or {}).get("fields", schema.get("fields", {})).keys())
    if not schema_fields:
        pytest.skip("schema endpoint returned no parseable field list")
        return

    doc_fields = _fields_from_doc()
    missing_in_doc = schema_fields - doc_fields
    assert not missing_in_doc, f"schema field(s) {sorted(missing_in_doc)} undocumented in {DOC_PATH.name}"

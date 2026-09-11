from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_101 as decision_101  # noqa: E402


CONFORMANCE = """\
# Conformance

## The register of open design decisions

| # | Question | Pointer | Dependencies | Status |
|---|---|---|---|---|
| 101 | what fields the credential-binding edge carries | `authority_model.md#what-the-credential-binding-carries` | stage 1 | **ruled** |
"""

DATA_MODEL_OK = """\
# Data model

## Relationships

| Edge type | Source → target | Meaning | What derives from it |
|---|---|---|---|
| `principal_binding` | credential (edge-keyed; no credential entity) → principal | binds one presented credential; one edge per credential; fields: `credential_kind`, `credential_value`, `credential_issuer`, `expires_at` | credential-to-principal resolution (match live edges on kind+value[+issuer] → principal endpoint) |
"""

DATA_MODEL_LEGACY = """\
# Data model

## Relationships

| Edge type | Source → target | Meaning | What derives from it |
|---|---|---|---|
| `principal_binding` | agent → principal | the principal the agent acts as | attribution; delegation chains |
"""

DATA_MODEL_NO_EXPIRY = """\
# Data model

## Relationships

| Edge type | Source → target | Meaning | What derives from it |
|---|---|---|---|
| `principal_binding` | credential → principal | one edge per credential; fields: `credential_kind`, `credential_value`, `credential_issuer` | credential-to-principal resolution (match kind+value → principal) |
"""


AUTHORITY_RULED = f"""\
# Authority model

{decision_101.AUTHORITY_HEADING}

**Ruled** (decision 101, 2026-09-10): **`principal_binding` carries `credential_kind`,
`credential_value`, `credential_issuer`, and `expires_at`.**

**Endpoint / source.** For an AAuth credential the binding resolves `credential_value` +
`credential_issuer` to the `agent`; the human operator is then reached through that agent's
**separate acts-as** `principal_binding`, a second edge of this type whose endpoint is the
`operator`. A resolver takes the endpoint of the edge whose kind matches what was presented,
so the AAuth kind yields the agent (attribution, A-for-B) and the acts-as kind yields the
operator (decision 48's counting rule).
"""

# Both endpoints inverted. Every field, cardinality, and resolution token is
# still present and correct -- only the two endpoints are the wrong way round,
# which is the defect the endpoint assertion exists for.
AUTHORITY_ENDPOINTS_SWAPPED = AUTHORITY_RULED.replace(
    "to the `agent`", "to the `operator`", 1
).replace(
    "whose endpoint is the\n`operator`", "whose endpoint is the\n`agent`", 1
).replace(
    "the AAuth kind yields the agent", "the AAuth kind yields the operator", 1
).replace(
    "the acts-as kind yields the\noperator", "the acts-as kind yields the\nagent", 1
)

AUTHORITY_AAUTH_SWAPPED_ONLY = AUTHORITY_RULED.replace(
    "to the `agent`", "to the `operator`", 1
).replace(
    "the AAuth kind yields the agent", "the AAuth kind yields the operator", 1
)

AUTHORITY_ACTS_AS_SWAPPED_ONLY = AUTHORITY_RULED.replace(
    "whose endpoint is the\n`operator`", "whose endpoint is the\n`agent`", 1
).replace(
    "the acts-as kind yields the\noperator", "the acts-as kind yields the\nagent", 1
)

# The endpoint paragraph removed altogether: nothing states either endpoint.
AUTHORITY_NO_ENDPOINTS = AUTHORITY_RULED[
    : AUTHORITY_RULED.index("**Endpoint / source.**")
]

# A correct endpoint sentence in a LATER section must not vouch for a ruling
# section that states the inverse -- the assertion reads the ruling section
# only, so the swap is still caught.
AUTHORITY_SWAPPED_WITH_CORRECT_ELSEWHERE = (
    AUTHORITY_ENDPOINTS_SWAPPED
    + """
### The counting rule: an agent counts as its bound principal

Its AAuth edge ends at the **agent**, and its acts-as edge ends at the **operator**.
"""
)

AUTHORITY_OPEN = f"""\
# Authority model

{decision_101.AUTHORITY_HEADING}

**Open.** What the credential binding carries is not yet settled.
"""

AUTHORITY_NO_HEADING = """\
# Authority model

### Some other section that is not the decision-101 ruling

**Ruled** (decision 101, 2026-09-10): text under the wrong heading.
"""


def write_corpus(
    root: Path,
    conformance: str = CONFORMANCE,
    data_model: str = DATA_MODEL_OK,
    authority_model: str | None = AUTHORITY_RULED,
) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(conformance, encoding="utf-8")
    (fdir / "data_model.md").write_text(data_model, encoding="utf-8")
    if authority_model is not None:
        (fdir / "authority_model.md").write_text(authority_model, encoding="utf-8")


def test_passes_when_ruled_and_data_model_row_carries_fields(tmp_path: Path) -> None:
    write_corpus(tmp_path)

    assert decision_101.check(tmp_path) == []


def test_fails_on_legacy_principal_binding_row(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model=DATA_MODEL_LEGACY)

    problems = decision_101.check(tmp_path)

    assert problems
    assert any("principal_binding" in p for p in problems)
    assert any("legacy" in p for p in problems)


def test_fails_when_ruled_but_row_missing_expiry(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model=DATA_MODEL_NO_EXPIRY)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "expiry" in problems[0] or "expires_at" in problems[0]


def test_fails_when_register_row_101_absent(tmp_path: Path) -> None:
    conformance_without_row = "\n".join(
        line
        for line in CONFORMANCE.splitlines()
        if not line.startswith("| 101 |")
    )
    write_corpus(tmp_path, conformance=conformance_without_row)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "decision-101-register" in problems[0]
    assert 'no register row beginning "| 101 |"' in problems[0]


def test_noop_shape_when_register_not_ruled(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        conformance=CONFORMANCE.replace("**ruled**", "**open**"),
        data_model=DATA_MODEL_LEGACY,
    )

    assert decision_101.check(tmp_path) == []


def test_raises_when_conformance_file_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs" / "foundation" / "conformance.md").unlink()

    with pytest.raises(decision_101.CorpusProblem, match="conformance.md"):
        decision_101.check(tmp_path)


def test_raises_when_data_model_file_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs" / "foundation" / "data_model.md").unlink()

    with pytest.raises(decision_101.CorpusProblem, match="data_model.md"):
        decision_101.check(tmp_path)


def test_fails_when_ruling_section_still_open(tmp_path: Path) -> None:
    write_corpus(tmp_path, authority_model=AUTHORITY_OPEN)

    problems = decision_101.check(tmp_path)

    opener_problems = [p for p in problems if "must open with" in p]
    assert len(opener_problems) == 1
    assert "decision-101-authority" in opener_problems[0]
    assert "**Open.**" in opener_problems[0]
    # A reverted section carries no `**Endpoint / source.**` paragraph either,
    # so that assertion fires alongside, as one consolidated message — the
    # ruling being open is exactly when both are true.
    assert len(problems) == 2
    assert sum("decision-101-endpoints" in p for p in problems) == 1
    assert any("no" in p and "Endpoint / source" in p for p in problems)


def test_fails_when_authority_model_file_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path, authority_model=None)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "decision-101-authority" in problems[0]
    assert "missing while register row 101 is **ruled**" in problems[0]


def test_fails_when_ruling_heading_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path, authority_model=AUTHORITY_NO_HEADING)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "decision-101-authority" in problems[0]
    assert "no section" in problems[0]


def test_authority_section_unchecked_when_register_not_ruled(
    tmp_path: Path,
) -> None:
    write_corpus(
        tmp_path,
        conformance=CONFORMANCE.replace("**ruled**", "**open**"),
        authority_model=AUTHORITY_OPEN,
    )

    assert decision_101.check(tmp_path) == []


# --- The endpoint assignment (decision 101 with decision 48) -----------------
#
# Swapping the two endpoints leaves every field, cardinality, and resolution
# token intact, so every other assertion in this file stays green on a corpus
# that says the opposite of the ruling. Registration is one-way under G26, so
# a swap that reaches stage 1 is not correctable afterwards.


def test_fails_when_both_endpoints_are_swapped(tmp_path: Path) -> None:
    write_corpus(tmp_path, authority_model=AUTHORITY_ENDPOINTS_SWAPPED)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 2
    assert all("decision-101-endpoints" in p for p in problems)
    assert any(
        "the AAuth credential's endpoint is stated to resolve to the operator" in p
        for p in problems
    )
    assert any(
        "the acts-as binding's endpoint is stated to resolve to the agent" in p
        for p in problems
    )


def test_fails_when_only_the_aauth_endpoint_is_swapped(tmp_path: Path) -> None:
    write_corpus(tmp_path, authority_model=AUTHORITY_AAUTH_SWAPPED_ONLY)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "decision-101-endpoints" in problems[0]
    assert (
        "the AAuth credential's endpoint is stated to resolve to the operator"
        in problems[0]
    )
    assert "ends that edge at the **agent**" in problems[0]


def test_fails_when_only_the_acts_as_endpoint_is_swapped(tmp_path: Path) -> None:
    write_corpus(tmp_path, authority_model=AUTHORITY_ACTS_AS_SWAPPED_ONLY)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "decision-101-endpoints" in problems[0]
    assert (
        "the acts-as binding's endpoint is stated to resolve to the agent"
        in problems[0]
    )
    assert "ends that edge at the **operator**" in problems[0]


def test_fails_when_the_ruling_states_neither_endpoint(tmp_path: Path) -> None:
    write_corpus(tmp_path, authority_model=AUTHORITY_NO_ENDPOINTS)

    problems = decision_101.check(tmp_path)

    # No `**Endpoint / source.**` paragraph at all is one root cause, reported
    # as one consolidated message rather than two independent per-side ones.
    assert len(problems) == 1
    assert "decision-101-endpoints" in problems[0]
    assert "no" in problems[0] and "Endpoint / source" in problems[0]


def test_correct_endpoints_in_a_later_section_do_not_excuse_a_swap(
    tmp_path: Path,
) -> None:
    """The assertion reads the ruling section, not the whole document."""
    write_corpus(
        tmp_path, authority_model=AUTHORITY_SWAPPED_WITH_CORRECT_ELSEWHERE
    )

    problems = decision_101.check(tmp_path)

    assert len(problems) == 2
    assert all("decision-101-endpoints" in p for p in problems)


def test_endpoints_unchecked_when_register_not_ruled(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        conformance=CONFORMANCE.replace("**ruled**", "**open**"),
        authority_model=AUTHORITY_ENDPOINTS_SWAPPED,
    )

    assert decision_101.check(tmp_path) == []


# --- The resolution regex ---------------------------------------------------


def test_resolution_language_is_not_satisfied_by_the_word_attribution(
    tmp_path: Path,
) -> None:
    """An earlier RESOLUTION_RE matched any `principal` within 80 chars of a
    loose alternation, so the word "attribution" elsewhere in the row kept the
    check green after the resolution language was deleted."""
    row_without_resolution = DATA_MODEL_OK.replace(
        "credential-to-principal resolution "
        "(match live edges on kind+value[+issuer] → principal endpoint)",
        "attribution (the AAuth edge, resolving to the agent, "
        "records a write as A-for-B on that principal)",
    )
    assert "credential-to-principal resolution" not in row_without_resolution
    write_corpus(tmp_path, data_model=row_without_resolution)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "missing kind+value → principal resolution language" in problems[0]


# --- Adversarial review findings: contradiction by addition, and markdown --
#
# An adversarial review ran six attacks (covered above) that this checker
# caught, then found two more that it did not: appending a plain-English
# sentence asserting the opposite endpoint assignment to the same paragraph
# ("contradiction by addition"), and bold-splitting the words in the
# canonical sentence so no phrase regex matched it while a redundant, correct
# mention elsewhere in the (then whole-section) scan kept the check green
# ("regex evasion by markdown"). Both are regression-tested here against the
# fix: scoping to the `**Endpoint / source.**` paragraph, normalizing
# markdown emphasis before matching, and requiring every clause in that
# paragraph naming a kind to agree on its one expected principal.

AUTHORITY_CONTRADICTION_BY_ADDITION = AUTHORITY_RULED.replace(
    "operator (decision 48's counting rule).\n",
    "operator (decision 48's counting rule). Put another way: the credential "
    "presented over AAuth ultimately identifies the human behind the agent, "
    "so it is the delegated identity — the agent itself — that the acts-as "
    "relationship exists to reach.\n",
)

# `ag**e**nt` / `op**e**rator`: a `*` marker landing inside each word. The
# canonical sentence no longer matches any rigid whole-phrase regex once
# split this way; a redundant, untouched, correct mention of "the acts-as
# kind yields the operator" sits earlier in the real corpus's ruling section
# (in the "What the acts-as edge carries" paragraph) — reproduced here too,
# so the fixture matches the shape of the actual attack rather than a
# simplified one.
AUTHORITY_MARKDOWN_SPLIT = (
    AUTHORITY_RULED[: AUTHORITY_RULED.index("**Endpoint / source.**")]
    + '**What the acts-as edge carries is open.** The resolver sentence below '
    'treats it as matched from a presentation ("the acts-as kind yields the '
    "operator\").\n\n"
    + AUTHORITY_RULED[AUTHORITY_RULED.index("**Endpoint / source.**") :]
).replace(
    "the AAuth kind yields the agent (attribution, A-for-B) and the acts-as kind yields the\noperator (decision 48's counting rule).",
    "the AAuth kind yields the ag**e**nt (attribution, A-for-B) and the acts-as kind yields the\nop**e**rator (decision 48's counting rule).",
)


def test_fails_on_contradiction_by_addition(tmp_path: Path) -> None:
    """Appending a contradicting sentence to the canonical paragraph must be
    caught even though it uses no phrase either endpoint regex recognizes —
    this is what defeated the previous generation of the check."""
    write_corpus(tmp_path, authority_model=AUTHORITY_CONTRADICTION_BY_ADDITION)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "decision-101-endpoints" in problems[0]
    assert "AAuth" in problems[0]
    assert "inconsistently" in problems[0] or "ambiguous" in problems[0].lower()


def test_fails_on_markdown_emphasis_splitting_the_canonical_words(
    tmp_path: Path,
) -> None:
    """Bold-splitting the canonical sentence's words must be caught even with
    a redundant, correct, untouched mention of the acts-as endpoint earlier
    in the same ruling section — this is what defeated the previous
    generation of the check."""
    assert "ag**e**nt" in AUTHORITY_MARKDOWN_SPLIT
    assert "op**e**rator" in AUTHORITY_MARKDOWN_SPLIT
    write_corpus(tmp_path, authority_model=AUTHORITY_MARKDOWN_SPLIT)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "decision-101-markdown" in problems[0]
    assert "g**e" in problems[0] or "p**e" in problems[0]


# --- Two further attacks against the fixed implementation -------------------
#
# Invented while closing the two findings above, to probe the new
# implementation's own assumptions rather than only the old one's.

AUTHORITY_PARTIAL_SWAP_CONTRADICTS_FRAMING = AUTHORITY_RULED.replace(
    "operator (decision 48's counting rule).",
    "agent (decision 48's counting rule).",
)


def test_fails_when_canonical_sentence_contradicts_earlier_correct_framing(
    tmp_path: Path,
) -> None:
    """Swapping only the acts-as clause of the canonical resolver sentence
    while the paragraph's own earlier framing clause ("...a second edge of
    this type whose endpoint is the `operator`") still states it correctly
    must be caught as a contradiction between two clauses in the same
    paragraph, not silently resolved in either direction."""
    assert "whose endpoint is the\n`operator`" in AUTHORITY_PARTIAL_SWAP_CONTRADICTS_FRAMING
    write_corpus(tmp_path, authority_model=AUTHORITY_PARTIAL_SWAP_CONTRADICTS_FRAMING)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "decision-101-endpoints" in problems[0]
    assert "acts-as" in problems[0]
    assert "inconsistently" in problems[0]


AUTHORITY_ENDPOINT_LEAD_DROPPED = AUTHORITY_RULED.replace(
    "**Endpoint / source.** For an AAuth credential",
    "For an AAuth credential",
)


def test_fails_when_the_endpoint_paragraphs_own_lead_phrase_is_dropped(
    tmp_path: Path,
) -> None:
    """Softening or dropping the `**Endpoint / source.**` bold lead-in, while
    leaving the paragraph's own (correct) sentences untouched, must not make
    the check silently stop looking at the paragraph — it is the anchor the
    whole endpoint assertion depends on, so losing it must fail closed rather
    than pass by default."""
    assert "**Endpoint / source.**" not in AUTHORITY_ENDPOINT_LEAD_DROPPED
    write_corpus(tmp_path, authority_model=AUTHORITY_ENDPOINT_LEAD_DROPPED)

    problems = decision_101.check(tmp_path)

    assert len(problems) == 1
    assert "decision-101-endpoints" in problems[0]
    assert "no" in problems[0] and "Endpoint / source" in problems[0]

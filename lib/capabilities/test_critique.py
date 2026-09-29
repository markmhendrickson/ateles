import pytest

from lib.capabilities import slots
from lib.capabilities.critique import (
    STATUS_CRITIC_ERROR,
    STATUS_NO_CRITIC,
    STATUS_PASSED,
    STATUS_REFUSED,
    STATUS_ROUND_LIMIT,
    ConceptResult,
    CritiqueVerdict,
    render_and_critique,
    summarize_concepts,
)
from lib.capabilities.errors import CRITIQUE_ROUND_LIMIT, CAP_EXHAUSTED, GenerationRefused
from lib.capabilities.vendors import StubVendor

from .conftest import binding_row

SLOT = slots.IMAGE_GENERATION


def _setup(make_client, cost=1.0, cap=100):
    stub = StubVendor(cost_usd=cost)
    client = make_client(
        [binding_row(SLOT, constraints={"model_tier": "m", "monthly_cap_usd": cap})],
        adapters={"stub": stub},
    )
    return client, stub


def test_round_limit_not_ready_for_selection(make_client):
    client, stub = _setup(make_client)
    seen = []

    def critic(review):
        seen.append(review.round)
        return CritiqueVerdict(False, f"issue {review.round}")

    result = render_and_critique(SLOT, "brief", critique_fn=critic, max_rounds=3, client=client)
    assert stub.calls == 3 and seen == [1, 2, 3]  # exactly max_rounds, not +1, not unbounded
    assert result.ready_for_operator_selection is False
    assert result.status == STATUS_ROUND_LIMIT and result.refusal_code == CRITIQUE_ROUND_LIMIT
    assert len(result.notes) == 3 and all("fail" in n for n in result.notes)
    assert result.final is not None  # the failure is returned, not omitted


def test_pass_sets_ready_for_selection(make_client):
    client, stub = _setup(make_client)
    seen = []

    def critic(review):
        seen.append(review.round)
        return CritiqueVerdict(review.round == 2, "ok" if review.round == 2 else "weak")

    result = render_and_critique(SLOT, "brief", critique_fn=critic, max_rounds=3, client=client)
    assert seen == [1, 2] and stub.calls == 2  # round 3 never ran
    assert result.ready_for_operator_selection is True and result.status == STATUS_PASSED


def test_revision_prompt_feeds_the_next_round(make_client):
    client, stub = _setup(make_client)
    prompts = []

    def critic(review):
        prompts.append(review.prompt)
        return CritiqueVerdict(review.round == 2, "too busy")

    render_and_critique(SLOT, "a mark", critique_fn=critic, client=client)
    assert prompts[0] == "a mark"
    assert "too busy" in prompts[1] and "a mark" in prompts[1]


def test_critique_refusal_mid_loop_does_not_promote(make_client):
    client, stub = _setup(make_client, cost=1.0, cap=1.0)  # affords exactly one round

    result = render_and_critique(
        SLOT, "brief", critique_fn=lambda r: CritiqueVerdict(False, "no"), max_rounds=3, client=client
    )
    assert result.status == STATUS_REFUSED and result.refusal_code == CAP_EXHAUSTED
    assert result.ready_for_operator_selection is False
    assert any(CAP_EXHAUSTED in n for n in result.notes)
    assert stub.calls == 1


def test_refusal_on_first_round_has_no_generation_and_is_not_ready(make_client):
    client = make_client([])  # no binding
    result = render_and_critique(SLOT, "brief", critique_fn=lambda r: CritiqueVerdict(True), client=client)
    assert result.status == STATUS_REFUSED and result.refusal_code == "BINDING_MISSING"
    assert result.ready_for_operator_selection is False and result.final is None


def test_no_critic_means_nothing_is_generated_or_promoted(make_client):
    client, stub = _setup(make_client)
    result = render_and_critique(SLOT, "brief", client=client)
    assert result.status == STATUS_NO_CRITIC and result.ready_for_operator_selection is False
    assert stub.calls == 0


@pytest.mark.parametrize("bad", [lambda r: True, lambda r: None, lambda r: 1 / 0])
def test_a_broken_critic_never_promotes(make_client, bad):
    client, stub = _setup(make_client)
    result = render_and_critique(SLOT, "brief", critique_fn=bad, client=client)
    assert result.status == STATUS_CRITIC_ERROR and result.ready_for_operator_selection is False
    assert stub.calls == 1


@pytest.mark.parametrize("rounds", [0, -1, 11, True, "3", None])
def test_max_rounds_is_bounded(make_client, rounds):
    client, _ = _setup(make_client)
    with pytest.raises(ValueError):
        render_and_critique(SLOT, "b", critique_fn=lambda r: CritiqueVerdict(True), max_rounds=rounds, client=client)


def test_empty_ready_set_has_reason_counts(make_client):
    client, _ = _setup(make_client)
    no_binding = make_client([])
    concepts = [
        render_and_critique(SLOT, "a", critique_fn=lambda r: CritiqueVerdict(False, "x"), max_rounds=1, client=client),
        render_and_critique(SLOT, "b", critique_fn=lambda r: CritiqueVerdict(False, "x"), max_rounds=2, client=client),
        render_and_critique(SLOT, "c", critique_fn=lambda r: CritiqueVerdict(True), client=no_binding),
        render_and_critique(SLOT, "d", client=client),
    ]
    summary = summarize_concepts(concepts)
    assert summary.ready == []                       # explicit empty list
    assert len(summary.not_ready) == 4
    assert sum(summary.reason_counts.values()) == 4  # accounts for every input
    assert summary.reason_counts == {STATUS_ROUND_LIMIT: 2, STATUS_REFUSED: 1, STATUS_NO_CRITIC: 1}


def test_summary_counts_passed_concepts(make_client):
    client, _ = _setup(make_client)
    good = render_and_critique(SLOT, "a", critique_fn=lambda r: CritiqueVerdict(True), client=client)
    bad = render_and_critique(SLOT, "b", critique_fn=lambda r: CritiqueVerdict(False), max_rounds=1, client=client)
    s = summarize_concepts([good, bad])
    assert s.ready == [good] and s.not_ready == [bad]
    assert sum(s.reason_counts.values()) == 2


def test_ready_flag_is_assigned_in_exactly_one_place():
    import re
    from pathlib import Path

    src = (Path(__file__).parent / "critique.py").read_text()
    assignments = re.findall(r"ready_for_operator_selection\s*=\s*True", src)
    assert len(assignments) == 1

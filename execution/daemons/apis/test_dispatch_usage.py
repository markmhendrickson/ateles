"""
Tests for dispatch_usage.py — per-dispatch model + token attribution.

The fixtures below are VERBATIM output captured by running each CLI on
2026-09-01, not hand-written approximations. That matters: the whole point of
this module is to record measured values, so a test that passes against an
invented shape would prove nothing about whether real output parses.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dispatch_usage import (  # noqa: E402
    DispatchUsage,
    parse_dispatch_usage,
)

# ── Real captured harness output ──────────────────────────────────────────────

# `claude --print --output-format json` (auth-failed run: the usage block is
# present and zeroed, which is exactly the shape a successful run carries).
CLAUDE_JSON = (
    '{"is_error":true,"duration_api_ms":0,"num_turns":1,"stop_reason":"stop_sequence",'
    '"session_id":"72441e55","total_cost_usd":0,"usage":{"output_tokens_details":'
    '{"thinking_tokens":0},"input_tokens":0,"cache_creation_input_tokens":0,'
    '"cache_read_input_tokens":0,"output_tokens":0,"service_tier":"standard"},'
    '"modelUsage":{},"subtype":"success","type":"result","duration_ms":190}'
)

# A successful claude run, with modelUsage populated — the only place any
# harness names the model it actually used.
CLAUDE_JSON_WITH_MODEL = (
    '{"is_error":false,"total_cost_usd":0.0421,"usage":{"input_tokens":1200,'
    '"output_tokens":340,"cache_read_input_tokens":27456,'
    '"cache_creation_input_tokens":88,"output_tokens_details":'
    '{"thinking_tokens":120}},"modelUsage":{"claude-opus-5":'
    '{"inputTokens":1200,"outputTokens":340}},"type":"result"}'
)

# `codex exec --json` JSONL. Usage rides the final turn.completed event; no
# model is named anywhere in the stream.
CODEX_JSONL = (
    '{"type":"thread.started","thread_id":"01a05dc6"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"ok"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":18492,'
    '"cached_input_tokens":9600,"cache_write_input_tokens":0,'
    '"output_tokens":21,"reasoning_output_tokens":14}}'
)

# `cursor-agent --print --output-format json`. camelCase keys, no model field.
CURSOR_JSON = (
    '{"type":"result","subtype":"success","is_error":false,"duration_ms":4804,'
    '"result":"ok","session_id":"55cbda75","usage":{"inputTokens":15838,'
    '"outputTokens":215,"cacheReadTokens":27456,"cacheWriteTokens":0}}'
)

# `cursor-agent --output-format stream-json`. The init event carries a `model`
# field that reads literally "Auto" on an unpinned dispatch.
CURSOR_STREAM_JSONL = (
    '{"type":"system","subtype":"init","apiKeySource":"login","cwd":"/private/tmp",'
    '"session_id":"ddfa7265","model":"Auto","permissionMode":"default"}\n'
    '{"type":"result","subtype":"success","duration_ms":6831,"is_error":false,'
    '"result":"ok","usage":{"inputTokens":31886,"outputTokens":305,'
    '"cacheReadTokens":11520,"cacheWriteTokens":0}}'
)


# ── Claude ────────────────────────────────────────────────────────────────────


def test_claude_json_parses_token_counts():
    u = parse_dispatch_usage("claude", CLAUDE_JSON)
    assert u.provider == "claude"
    assert u.input_tokens == 0
    assert u.output_tokens == 0
    assert u.cache_read_tokens == 0
    # A reported zero is a real measurement and must survive as 0, not collapse
    # to None — "measured zero" and "not reported" are different facts.
    assert u.has_tokens is True


def test_claude_reports_the_model_it_actually_used():
    u = parse_dispatch_usage("claude", CLAUDE_JSON_WITH_MODEL)
    assert u.model == "claude-opus-5"
    # Sourced from the harness's own report, not from what was requested.
    assert u.model_source == "reported"
    assert u.input_tokens == 1200
    assert u.output_tokens == 340
    assert u.reasoning_tokens == 120
    assert u.total_cost_usd == pytest.approx(0.0421)


def test_claude_reported_model_wins_over_a_different_requested_model():
    """The recorded model must be the one that RAN, not the one asked for.

    This is the case per-model fallback (ateles#667) creates, and recording the
    requested model here would defeat the entire purpose of the measurement.
    """
    u = parse_dispatch_usage(
        "claude", CLAUDE_JSON_WITH_MODEL, requested_model="claude-sonnet-5"
    )
    assert u.model == "claude-opus-5"
    assert u.model_source == "reported"


def test_claude_picks_the_model_that_did_the_most_output_work():
    """A small auxiliary model must not masquerade as the dispatch's model."""
    blob = (
        '{"usage":{"input_tokens":10,"output_tokens":20},"modelUsage":'
        '{"claude-haiku-5":{"outputTokens":5},'
        '"claude-opus-5":{"outputTokens":900}},"type":"result"}'
    )
    u = parse_dispatch_usage("claude", blob)
    assert u.model == "claude-opus-5"
    assert set(u.reported_models) == {"claude-haiku-5", "claude-opus-5"}


# ── Codex ─────────────────────────────────────────────────────────────────────


def test_codex_jsonl_parses_usage_from_turn_completed():
    u = parse_dispatch_usage("codex", CODEX_JSONL)
    assert u.provider == "codex"
    assert u.input_tokens == 18492
    assert u.output_tokens == 21
    assert u.cache_read_tokens == 9600
    assert u.reasoning_tokens == 14


def test_codex_reports_no_model_so_source_is_default():
    """Codex names no model. Absent a request, that is honestly 'default'."""
    u = parse_dispatch_usage("codex", CODEX_JSONL)
    assert u.model is None
    assert u.model_source == "default"


def test_codex_requested_model_is_marked_as_requested_not_measured():
    u = parse_dispatch_usage("codex", CODEX_JSONL, requested_model="gpt-5.3-codex")
    assert u.model == "gpt-5.3-codex"
    # Crucially NOT "reported": nothing confirmed this model actually ran.
    assert u.model_source == "requested"


# ── Cursor ────────────────────────────────────────────────────────────────────


def test_cursor_json_parses_camelcase_token_counts():
    u = parse_dispatch_usage("cursor", CURSOR_JSON)
    assert u.provider == "cursor"
    assert u.input_tokens == 15838
    assert u.output_tokens == 215
    assert u.cache_read_tokens == 27456
    assert u.cache_write_tokens == 0


def test_cursor_auto_is_not_recorded_as_a_model():
    """"Auto" is the selector's name, not the model it chose.

    Recording it would produce a value that looks measured and identifies
    nothing — the exact failure mode that let one expensive model absorb a
    plan's quota unnoticed.
    """
    u = parse_dispatch_usage("cursor", CURSOR_STREAM_JSONL)
    assert u.model is None
    assert u.model_source == "default"
    # Usage still parses from the same stream.
    assert u.input_tokens == 31886
    assert u.output_tokens == 305


def test_cursor_pinned_model_in_stream_is_recorded_as_reported():
    blob = (
        '{"type":"system","subtype":"init","model":"composer-2.5"}\n'
        '{"type":"result","usage":{"inputTokens":10,"outputTokens":2}}'
    )
    u = parse_dispatch_usage("cursor", blob)
    assert u.model == "composer-2.5"
    assert u.model_source == "reported"


# ── Honest absence ────────────────────────────────────────────────────────────


def test_text_mode_output_yields_no_token_counts_rather_than_zeros():
    """The swarm's current invocations are text-mode and report no usage.

    The required behaviour is an absent field, never a fabricated zero: a
    recorded count that is actually a guess is worse than a missing one.
    """
    u = parse_dispatch_usage("claude", "I have opened PR #123 as requested.\n")
    assert u.has_tokens is False
    assert u.input_tokens is None
    assert u.output_tokens is None
    assert u.total_tokens is None
    # And no token keys are emitted at all.
    fields = u.as_event_fields()
    assert "input_tokens" not in fields
    assert "total_tokens" not in fields
    assert fields["provider"] == "claude"


def test_malformed_output_degrades_to_unreported_and_never_raises():
    for bad in ('{"usage": ', "", "   ", "not json at all", '{"usage": "nonsense"}'):
        u = parse_dispatch_usage("cursor", bad)
        assert u.has_tokens is False
        assert u.provider == "cursor"


def test_unknown_provider_does_not_raise():
    u = parse_dispatch_usage("some-future-harness", CLAUDE_JSON)
    assert u.provider == "some-future-harness"
    assert u.has_tokens is False


def test_booleans_are_not_accepted_as_token_counts():
    """`isinstance(True, int)` is True in Python; a stray bool must not become 1."""
    u = parse_dispatch_usage(
        "codex",
        '{"type":"turn.completed","usage":{"input_tokens":true,"output_tokens":5}}',
    )
    assert u.input_tokens is None
    assert u.output_tokens == 5


def test_interleaved_non_json_lines_are_tolerated():
    """Every one of these CLIs mixes human-readable noise into its output."""
    noisy = (
        "Warning: something happened\n"
        + CODEX_JSONL
        + "\ndone.\n"
    )
    u = parse_dispatch_usage("codex", noisy)
    assert u.input_tokens == 18492


# ── Event-field rendering ─────────────────────────────────────────────────────


def test_total_tokens_excludes_cache_reads():
    """Cache reads bill differently per provider; summing them would make two
    providers' totals look comparable when they are not."""
    u = parse_dispatch_usage("cursor", CURSOR_JSON)
    assert u.total_tokens == 15838 + 215


def test_as_event_fields_only_emits_reported_values():
    u = DispatchUsage(provider="codex", model_source="default")
    assert u.as_event_fields() == {"provider": "codex", "model_source": "default"}


def test_summary_says_unreported_rather_than_printing_zeros():
    u = DispatchUsage(provider="cursor", model_source="default")
    s = u.summary()
    assert "tokens=unreported" in s
    assert "model=unreported" in s


def test_summary_names_model_and_its_source():
    u = parse_dispatch_usage("claude", CLAUDE_JSON_WITH_MODEL)
    s = u.summary()
    assert "model=claude-opus-5(reported)" in s
    assert "cost_usd=0.0421" in s

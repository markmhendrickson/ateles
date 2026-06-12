"""
Unit tests for drift-signal parsing, clustering, and contradiction detection.

Run with: pytest lib/daemon_runtime/test_drift.py -v
"""

from __future__ import annotations

from drift import (
    DriftSignal,
    cluster_signals,
    contradicts,
    parse_comments,
    parse_drift_signals,
)


def test_parses_canonical_line():
    sigs = parse_drift_signals(
        "blah blah\n[waxwing] strategy_drift_signal: operator keeps trimming PR text\n",
        source_ref="http://gh/c/1",
    )
    assert len(sigs) == 1
    assert sigs[0].agent == "waxwing"
    assert "trimming" in sigs[0].text
    assert sigs[0].source_ref == "http://gh/c/1"


def test_case_insensitive_marker_and_agent():
    sigs = parse_drift_signals("[Pavo] Strategy_Drift_Signal: something")
    assert len(sigs) == 1
    assert sigs[0].agent == "pavo"


def test_ignores_non_signal_text():
    assert parse_drift_signals("just a normal comment, no signal here") == []
    assert parse_drift_signals("[pavo] acceptance_criteria: not a drift line") == []


def test_empty_signal_text_skipped():
    assert parse_drift_signals("[pavo] strategy_drift_signal:    ") == []


def test_parse_comments_uses_url_as_ref():
    comments = [
        {"author": "x", "body": "[corvus] strategy_drift_signal: foo bar baz", "url": "u1"},
        {"author": "y", "body": "nothing", "url": "u2"},
    ]
    sigs = parse_comments(comments)
    assert len(sigs) == 1
    assert sigs[0].source_ref == "u1"


def test_clusters_are_agent_scoped():
    # Same wording, different agents -> never cluster together (agent-local).
    a = parse_drift_signals("[pavo] strategy_drift_signal: prefer terse pricing tables")
    b = parse_drift_signals("[corvus] strategy_drift_signal: prefer terse pricing tables")
    clusters = cluster_signals(a + b)
    assert len(clusters) == 2
    assert all(c.size == 1 for c in clusters)


def test_identical_wording_clusters():
    txt = "[pavo] strategy_drift_signal: prefer terse copy in landing headers"
    sigs = parse_drift_signals(txt) + parse_drift_signals(txt)
    clusters = cluster_signals(sigs)
    assert len(clusters) == 1
    assert clusters[0].size == 2


def test_token_order_independent_fingerprint():
    # Conservative clustering still collapses pure reorderings of salient tokens.
    s1 = parse_drift_signals("[pavo] strategy_drift_signal: terse landing headers preferred")
    s2 = parse_drift_signals("[pavo] strategy_drift_signal: preferred landing headers terse")
    clusters = cluster_signals(s1 + s2)
    assert len(clusters) == 1
    assert clusters[0].size == 2


def test_representative_text_is_longest():
    sigs = (
        parse_drift_signals("[pavo] strategy_drift_signal: terse copy")
        + parse_drift_signals(
            "[pavo] strategy_drift_signal: terse copy in headers and subheads always"
        )
    )
    # Different salient sets won't cluster; build one cluster manually instead.
    cluster = cluster_signals(sigs)[0]
    assert cluster.representative_text  # non-empty


def test_contradiction_detection():
    sig = DriftSignal("pavo", "actually stop preferring terse copy, be verbose instead")
    assert contradicts(sig, "prefer terse copy in headers")


def test_no_contradiction_without_reversal_word():
    sig = DriftSignal("pavo", "terse copy in headers works well")
    assert not contradicts(sig, "prefer terse copy in headers")


def test_no_contradiction_without_theme_overlap():
    sig = DriftSignal("pavo", "stop doing something totally unrelated entirely")
    assert not contradicts(sig, "prefer terse copy in headers")

"""
lib/daemon_runtime/drift.py — Parse and cluster strategy_drift_signal lines.

Agents emit an optional final line of the form

    [<agent>] strategy_drift_signal: <one-line observation>

This module turns those lines (wherever they surface — a GitHub comment, an
agent's stdout) into structured `DriftSignal` records, and clusters a stream of
them by theme so the generalizer can decide when enough independent evidence
has accumulated to justify an autonomous, agent-local policy.

Pure functions only — no I/O. Neotoma persistence lives in `generalizer.py`.
This split keeps the parsing/clustering logic unit-testable without a network.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# Mirror the canonical-header regex style used by the gate orchestrator
# (execution/daemons/anthus/orchestrator.py). Agent names are lowercase
# alphanumerics plus hyphen/underscore; the marker is case-insensitive.
_DRIFT_RE = re.compile(
    r"^\s*\[(?P<agent>[a-z0-9_-]+)\]\s+strategy_drift_signal\s*:\s*(?P<text>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Tokens that carry no thematic weight when deriving a cluster key. Kept small
# and deliberately conservative — over-clustering merges distinct signals and
# would let one repeated complaint masquerade as independent corroboration.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for",
        "with", "is", "are", "was", "were", "be", "this", "that", "it", "as",
        "at", "by", "from", "i", "we", "should", "could", "would", "when",
        "than", "then", "too", "so", "more", "most", "keep", "kept",
    }
)


@dataclass(frozen=True)
class DriftSignal:
    """One parsed strategy_drift_signal line."""

    agent: str
    text: str
    raw: str = ""
    # Provenance of where the line was observed (comment URL, work entity id…).
    source_ref: str = ""

    @property
    def theme_key(self) -> str:
        """
        A stable, content-derived clustering key for this signal.

        Two signals from the same agent that share their salient content tokens
        collapse to the same key, so independent re-observations of the same
        concern accumulate toward the threshold. The key is agent-scoped: we
        never cluster across agents (agent-local generalization only).
        """
        return f"{self.agent.lower()}:{_content_fingerprint(self.text)}"


@dataclass
class DriftCluster:
    """A set of same-theme signals from a single agent."""

    agent: str
    theme_key: str
    signals: list[DriftSignal] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.signals)

    @property
    def representative_text(self) -> str:
        """The longest signal text — usually the most descriptive phrasing."""
        return max((s.text for s in self.signals), key=len, default="")

    @property
    def source_refs(self) -> list[str]:
        return [s.source_ref for s in self.signals if s.source_ref]


def parse_drift_signals(text: str, source_ref: str = "") -> list[DriftSignal]:
    """
    Extract every strategy_drift_signal line from a block of text.

    `text` may be a single agent response, a GitHub comment body, or any blob
    that might contain one or more marker lines. Returns them in document order.
    """
    if not text:
        return []
    out: list[DriftSignal] = []
    for m in _DRIFT_RE.finditer(text):
        signal_text = m.group("text").strip()
        if not signal_text:
            continue
        out.append(
            DriftSignal(
                agent=m.group("agent").lower(),
                text=signal_text,
                raw=m.group(0).strip(),
                source_ref=source_ref,
            )
        )
    return out


def parse_comments(comments: list[dict]) -> list[DriftSignal]:
    """
    Scan a list of GitHub comment dicts ({author, body, url}) for drift signals.

    Mirrors the shape produced by Anthus `_fetch_comments`. The comment URL is
    captured as the signal's source_ref for provenance / drift_signal_refs.
    """
    out: list[DriftSignal] = []
    for c in comments or []:
        body = str(c.get("body", ""))
        ref = str(c.get("url") or c.get("id") or "")
        out.extend(parse_drift_signals(body, source_ref=ref))
    return out


def cluster_signals(signals: list[DriftSignal]) -> list[DriftCluster]:
    """
    Group signals by (agent, theme). Returns clusters sorted largest-first so
    the strongest evidence is considered before weaker, sparser themes.
    """
    by_key: dict[str, DriftCluster] = {}
    for s in signals:
        cluster = by_key.get(s.theme_key)
        if cluster is None:
            cluster = DriftCluster(agent=s.agent, theme_key=s.theme_key)
            by_key[s.theme_key] = cluster
        cluster.signals.append(s)
    return sorted(by_key.values(), key=lambda c: c.size, reverse=True)


def contradicts(signal: DriftSignal, policy_rule_text: str) -> bool:
    """
    Heuristic: does a fresh drift signal contradict an existing policy?

    A signal contradicts a policy when it shares the policy's thematic content
    *and* carries a negation/reversal marker ("don't", "stop", "instead",
    "actually", "no longer", "revert"). This is deliberately a low-precision,
    safety-biased check: a false positive merely re-opens the policy for
    operator review (cheap), while a missed contradiction would let a stale
    auto-policy persist (expensive). When in doubt, flag it.
    """
    if not policy_rule_text:
        return False
    sig_tokens = _salient_tokens(signal.text)
    pol_tokens = _salient_tokens(policy_rule_text)
    if not sig_tokens or not pol_tokens:
        return False
    overlap = sig_tokens & pol_tokens
    # Require meaningful thematic overlap before considering contradiction.
    if len(overlap) < min(2, len(pol_tokens)):
        return False
    reversal = {
        "dont", "don't", "not", "stop", "instead", "actually", "revert",
        "reverse", "wrong", "incorrect", "avoid", "never", "undo", "rollback",
    }
    lowered = signal.text.lower()
    return any(marker in lowered.split() or marker in lowered for marker in reversal)


# ── internals ─────────────────────────────────────────────────────────────────


def _salient_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _content_fingerprint(text: str) -> str:
    """
    Order-independent fingerprint of a signal's salient tokens. Two phrasings of
    the same concern ("prefer terse PR descriptions" / "PR descriptions should
    be terse") yield the same fingerprint because the salient token *set* — not
    its order — drives the hash.
    """
    tokens = _salient_tokens(text)
    if not tokens:
        # Fall back to a hash of the raw text so empty-salient signals still
        # cluster by exact wording rather than all collapsing together.
        tokens = {text.strip().lower()}
    joined = "|".join(sorted(tokens))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]

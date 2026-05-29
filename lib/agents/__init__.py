"""
lib/agents/ — T4 invocable agent skills for the Ateles swarm.

Each module is a stateless skill invoked by Anthus (the swarm coordinator)
or directly by driver scripts. T4 agents have no daemon — they execute
inline and return structured results.

Current skills:
    triage      — Turdus's Claude-based email classifier
    dispatch    — Anthus's label-based routing table (minimal Phase 6 slice)
    buteo       — Legal / contract redline review
    pavo        — Commercial framing + reply drafting
"""

from . import buteo, dispatch, pavo, runner, triage  # noqa: F401

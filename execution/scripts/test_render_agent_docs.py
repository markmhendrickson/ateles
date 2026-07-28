"""
Doc-sync contract test for the ateles#255 tool_allowlist fix.

Two things must hold for the 9 agents whose tool_allowlist was corrected:
  1. Their rendered docs/agents/*.md and .claude/skills/*/SKILL.md mirrors
     contain ZERO occurrences of the banned `bash:<command>` prefix pattern.
  2. Re-running render_agent_docs.py against the live (corrected) Neotoma
     source produces byte-identical output to what's on disk — i.e. these
     mirrors are not hand-edited drift, they are a faithful render of the
     corrected Neotoma agent_definition entities.

(2) requires live Neotoma (NEOTOMA_BASE_URL) and is skipped when unset, since
this repo's other network-dependent checks follow the same pattern (see
scripts/lint.sh). (1) is a pure filesystem check and always runs.

Run with: pytest execution/scripts/test_render_agent_docs.py -v
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

import render_agent_docs  # noqa: E402

BASH_PREFIX_RE = re.compile(r'"bash:[^"]*"')

# The 9 agents whose tool_allowlist was corrected in ateles#255.
CORRECTED_AGENTS = [
    "cicada",
    "vanellus",
    "corvus",
    "robin",
    "phoenicurus",
    "pavo",
    "struthio",
    "waxwing",
    "regulus",
]


class TestNoBashPrefixInMirrors:
    @pytest.mark.parametrize("agent", CORRECTED_AGENTS)
    def test_docs_agents_mirror_has_no_bash_prefix(self, agent: str) -> None:
        path = _REPO_ROOT / "docs" / "agents" / f"{agent}.md"
        assert path.exists(), f"expected mirror file missing: {path}"
        content = path.read_text()
        matches = BASH_PREFIX_RE.findall(content)
        assert not matches, (
            f"{path} still contains banned bash: prefix grant(s): {matches}"
        )

    @pytest.mark.parametrize("agent", CORRECTED_AGENTS)
    def test_skill_mirror_has_no_bash_prefix(self, agent: str) -> None:
        path = _REPO_ROOT / ".claude" / "skills" / agent / "SKILL.md"
        if not path.exists():
            pytest.skip(
                f"{agent} has no SKILL.md mirror (no operational prompt / proposed status)"
            )
        content = path.read_text()
        matches = BASH_PREFIX_RE.findall(content)
        assert not matches, (
            f"{path} still contains banned bash: prefix grant(s): {matches}"
        )


@pytest.mark.skipif(
    not os.environ.get("NEOTOMA_BASE_URL"),
    reason="requires live Neotoma (NEOTOMA_BASE_URL unset)",
)
class TestMirrorsMatchFreshRender:
    def test_check_mode_reports_no_drift_for_corrected_agents(self) -> None:
        """render_agent_docs.py --check reports failures per-file; the 9
        corrected agents' mirrors must NOT be among any reported failures
        (independent of unrelated pre-existing drift elsewhere in the tree)."""
        base_url, token = render_agent_docs._load_env()
        agents = render_agent_docs.fetch_agents(base_url, token)
        targets = render_agent_docs._targets(agents)

        stale_corrected: list[str] = []
        for path, content in targets.items():
            canonical = content if content.endswith("\n") else content + "\n"
            rel = str(path.relative_to(_REPO_ROOT))
            agent_name = None
            if rel.startswith("docs/agents/") and rel.endswith(".md"):
                agent_name = rel[len("docs/agents/") : -len(".md")]
            elif rel.startswith(".claude/skills/"):
                agent_name = rel[len(".claude/skills/") :].split("/")[0]
            if agent_name not in CORRECTED_AGENTS:
                continue
            on_disk = path.read_text() if path.exists() else ""
            if on_disk != canonical:
                stale_corrected.append(rel)

        assert not stale_corrected, (
            f"corrected-agent mirrors are stale relative to Neotoma: {stale_corrected}"
        )

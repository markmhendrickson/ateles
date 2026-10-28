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


_ONE_AGENT = {
    "name": "ateles",
    "status": "active",
    "tier": "core",
    "prompt_markdown": "operational prompt",
}


def _bind_mirror_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every path check() resolves at one tmp tree.

    check() does path.relative_to(REPO_ROOT). A skills dir outside that root
    raises, and the test errors instead of asserting the exit status.
    """
    monkeypatch.setattr(render_agent_docs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        render_agent_docs, "AGENTS_DOC_DIR", tmp_path / "docs" / "agents"
    )
    monkeypatch.setattr(
        render_agent_docs, "SKILLS_DIR", tmp_path / ".claude" / "skills"
    )


def _write_canonical_targets(agents: list[dict]) -> None:
    for path, content in render_agent_docs._targets(agents).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        text = content if content.endswith("\n") else content + "\n"
        path.write_text(text)


class TestMirrorCheckContract:
    """--check is selected by argv, not by calling check() directly.

    No live Neotoma and no skip when NEOTOMA_BASE_URL is unset: CI never sets it.
    """

    def _prepare(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agents: list[dict]
    ) -> None:
        _bind_mirror_roots(monkeypatch, tmp_path)
        monkeypatch.setattr(
            render_agent_docs, "_load_env", lambda: ("http://example.invalid", "token")
        )
        monkeypatch.setattr(
            render_agent_docs, "fetch_agents", lambda _base, _token: agents
        )
        monkeypatch.setattr(sys, "argv", ["render_agent_docs.py", "--check"])
        _write_canonical_targets(agents)

    def test_planted_line_makes_check_exit_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._prepare(monkeypatch, tmp_path, [dict(_ONE_AGENT)])
        skill = render_agent_docs.SKILLS_DIR / "ateles" / "SKILL.md"
        skill.write_text(skill.read_text() + "planted-extra-line\n")

        assert render_agent_docs.main() == 1
        out = capsys.readouterr().out
        assert "AGENT MIRROR CHECK FAILED — disk differs from Neotoma:" in out
        assert ".claude/skills/ateles/SKILL.md" in out
        fix_lines = [line for line in out.splitlines() if "Fix:" in line]
        assert fix_lines == [render_agent_docs.MIRROR_FIX_LINE]
        assert "--check" not in fix_lines[0]

    def test_matching_mirrors_exit_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._prepare(monkeypatch, tmp_path, [dict(_ONE_AGENT)])

        assert render_agent_docs.main() == 0
        out = capsys.readouterr().out
        assert "agent mirror check OK" in out
        assert "AGENT MIRROR CHECK FAILED" not in out

    def test_check_empty_does_not_print_ok_or_fix(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _bind_mirror_roots(monkeypatch, tmp_path)
        _write_canonical_targets([])

        assert render_agent_docs.check([]) == 1
        out = capsys.readouterr().out
        # Literal, not the constant: a paraphrase of the signed empty-state
        # text used to pass because the assertion compared the constant to itself.
        # "OK" is a substring of NEOTOMA_BEARER_TOKEN, so match the OK line.
        signed = (
            "AGENT MIRROR CHECK FAILED — Neotoma returned no agent_definition rows; "
            "refusing to treat that as a match.\n"
            "Check NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN; "
            "do not regenerate against an empty result."
        )
        assert render_agent_docs.ZERO_ROWS_LINE == signed
        assert out.strip() == signed
        assert "agent mirror check OK" not in out
        assert "Fix:" not in out

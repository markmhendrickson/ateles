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


class TestSkipWithoutTokenExitMatrix:
    """Exit-code matrix for ``--skip-without-token`` (ateles#717 QA).

    Mirrors the credential/unreachable SKIP contract in
    ``scripts/linters/test_validate_tool_allowlist.py``, but against
    ``render_agent_docs.main()`` in-process so unreachable cases do not burn
    the live ``_request`` retry sleep.
    """

    def _sandbox_home(self, monkeypatch, tmp_path) -> None:
        # _load_env() falls back to ~/.config/neotoma/.env — sandbox HOME so a
        # developer's live token cannot leak into these unconfigured cases.
        monkeypatch.setenv("HOME", str(tmp_path))

    def test_skip_without_token_exits_0_when_env_missing(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        self._sandbox_home(monkeypatch, tmp_path)
        monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
        monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
        monkeypatch.setattr(
            sys,
            "argv",
            ["render_agent_docs.py", "--check", "--skip-without-token"],
        )

        assert render_agent_docs.main() == 0
        out = capsys.readouterr().out
        assert "SKIP" in out
        assert "not configured" in out

    def test_skip_without_token_exits_0_when_neotoma_unreachable(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        self._sandbox_home(monkeypatch, tmp_path)
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.invalid")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "dummy-token-for-skip-test")
        monkeypatch.setattr(
            sys,
            "argv",
            ["render_agent_docs.py", "--check", "--skip-without-token"],
        )

        def _unreachable(*_a, **_k):
            raise SystemExit(
                "Neotoma unreachable after 5 tries: <urlopen error timed out>"
            )

        monkeypatch.setattr(render_agent_docs, "fetch_agents", _unreachable)

        assert render_agent_docs.main() == 0
        out = capsys.readouterr().out
        assert "SKIP" in out
        assert "unreachable" in out

    def test_skip_without_token_still_fails_on_real_drift(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        self._sandbox_home(monkeypatch, tmp_path)
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.invalid")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "dummy-token-for-skip-test")
        monkeypatch.setattr(
            sys,
            "argv",
            ["render_agent_docs.py", "--check", "--skip-without-token"],
        )
        monkeypatch.setattr(
            render_agent_docs, "fetch_agents", lambda *_a, **_k: []
        )
        # Gate honesty: once Neotoma is reachable, SKIP must not swallow drift.
        monkeypatch.setattr(render_agent_docs, "check", lambda _agents: 1)

        assert render_agent_docs.main() == 1
        out = capsys.readouterr().out
        assert "not configured" not in out
        assert "unreachable" not in out

    def test_check_without_skip_flag_still_hard_fails_unconfigured(
        self, monkeypatch, tmp_path
    ) -> None:
        self._sandbox_home(monkeypatch, tmp_path)
        monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
        monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
        monkeypatch.setattr(sys, "argv", ["render_agent_docs.py", "--check"])

        with pytest.raises(SystemExit) as excinfo:
            render_agent_docs.main()
        # sys.exit(str) → SystemExit with a non-zero / non-None code message.
        assert excinfo.value.code not in (0, None)

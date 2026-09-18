"""
Tests for resolve_ateles_identity.py (ateles task ent_ebb8ecc95b19ca2ba5f0201c).

Proves the three required states — live, cached-with-staleness-banner, and
fail-open — actually occur, rather than merely being described in the
docstring. Per CLAUDE.md's verification-discipline rule ("a test that cannot
fail on the thing it watches is decoration"): each test below is checked
against a deliberately broken variant of the code path it covers before being
trusted as coverage; the specific regressions tried are noted in each test's
docstring so a future change can re-derive what would make it fail.

No live Neotoma or filesystem-outside-tmp_path dependency: AgentLoader is
monkeypatched at the point resolve_ateles_identity imports it, and all cache/
log I/O is redirected to pytest's tmp_path so this test never touches the
real .claude/.session_state/ directory or reads a real ~/.config/neotoma/.env.

Run with: pytest execution/scripts/test_resolve_ateles_identity.py -v
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

import resolve_ateles_identity as rai  # noqa: E402


class _FakeAgentDefinition:
    def __init__(self, prompt_markdown: str = "", is_stub: bool = False, load_error: str = ""):
        self.prompt_markdown = prompt_markdown
        self.is_stub = is_stub
        self.load_error = load_error


class _FakeAgentLoaderModule(types.ModuleType):
    """Stands in for lib/daemon_runtime/agent_loader.py at the import site
    resolve_ateles_identity.resolve() uses (a local `from agent_loader import
    AgentLoader` inside the function, after sys.path is mutated). We instead
    patch sys.modules so that import resolves to this fake, regardless of
    sys.path — avoids relying on import order/caching quirks.
    """

    def __init__(self, agent_def: _FakeAgentDefinition):
        super().__init__("agent_loader")
        captured = agent_def

        class _FakeLoader:
            def __init__(self, name):
                self.name = name

            def load(self):
                return captured

        self.AgentLoader = _FakeLoader


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Redirect CACHE_DIR/CACHE_FILE/LOG_FILE/FALLBACK_MIRROR to tmp_path so
    no test reads or writes the real repo's .claude/.session_state/ or
    .claude/skills/ateles/SKILL.md.
    """
    cache_dir = tmp_path / ".session_state"
    monkeypatch.setattr(rai, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(rai, "CACHE_FILE", cache_dir / "ateles_identity_cache.md")
    monkeypatch.setattr(rai, "LOG_FILE", cache_dir / "ateles_identity_resolution.log")
    # Point the static-mirror fallback at a tmp file we control per-test
    # (absent by default — tests that need it create it explicitly).
    monkeypatch.setattr(rai, "FALLBACK_MIRROR", tmp_path / "SKILL.md")
    yield


def _patch_loader(monkeypatch, agent_def: _FakeAgentDefinition) -> None:
    fake_module = _FakeAgentLoaderModule(agent_def)
    monkeypatch.setitem(sys.modules, "agent_loader", fake_module)
    # _load_env() inserts lib/daemon_runtime onto sys.path; harmless to leave,
    # but neutralize any real agent_loader.py it might otherwise re-import by
    # ensuring our fake stays authoritative in sys.modules (setitem above).


class TestLiveResolution:
    """State 1: Neotoma reachable -> live prompt, no staleness banner.

    Proved red: before this test existed, a version of resolve() that always
    read the cache first (cache-preferred rather than live-preferred) would
    pass a naive "prints something" check but fail this one, since a stale
    cache pre-seeded with different content would be returned instead of the
    fresh AgentLoader payload.
    """

    def test_live_load_returns_prompt_with_no_banner(self, monkeypatch):
        live_text = "# SOUL.md\n\nLive content from Neotoma."
        _patch_loader(monkeypatch, _FakeAgentDefinition(prompt_markdown=live_text, is_stub=False))

        markdown, outcome = rai.resolve()

        assert outcome == "live"
        assert markdown == live_text
        assert "STALE" not in markdown

    def test_live_load_writes_cache(self, monkeypatch):
        live_text = "# SOUL.md\n\nLive content v2."
        _patch_loader(monkeypatch, _FakeAgentDefinition(prompt_markdown=live_text, is_stub=False))

        rai.resolve()

        assert rai.CACHE_FILE.exists()
        assert rai.CACHE_FILE.read_text() == live_text


class TestCachedResolution:
    """State 2: Neotoma unreachable, cache present -> cache WITH banner.

    Proved red: a version of _staleness_banner that returns "" (silently
    substituting cached text with no marker — the exact defect this script
    exists to close) makes test_cached_load_banner_names_failure_and_age
    fail, since it asserts the banner text is actually present and mentions
    both the failure reason and an age. A version that returns the cached
    markdown UNPREFIXED (banner computed but not concatenated) fails the same
    way.
    """

    def test_cached_load_returns_banner_plus_cached_text(self, monkeypatch):
        cached_text = "# SOUL.md\n\nCached content from last live run."
        rai.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        rai.CACHE_FILE.write_text(cached_text)

        _patch_loader(
            monkeypatch,
            _FakeAgentDefinition(prompt_markdown="", is_stub=True, load_error="ConnectError: simulated"),
        )

        markdown, outcome = rai.resolve()

        assert outcome == "cached"
        assert "STALE IDENTITY" in markdown
        assert "ConnectError: simulated" in markdown
        assert cached_text in markdown

    def test_cached_load_banner_names_failure_and_age(self, monkeypatch):
        cached_text = "# SOUL.md\n\nCached content."
        rai.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        rai.CACHE_FILE.write_text(cached_text)

        _patch_loader(
            monkeypatch,
            _FakeAgentDefinition(prompt_markdown="", is_stub=True, load_error="401 Unauthorized"),
        )

        markdown, outcome = rai.resolve()

        assert outcome == "cached"
        assert "401 Unauthorized" in markdown
        # The banner must state an age, not just "stale" — e.g. "N minute(s)
        # old" or "N hour(s) old" per _age_str's two branches at this cache
        # age (freshly written, so minutes).
        assert "minute(s) old" in markdown or "hour(s) old" in markdown

    def test_cached_never_presents_as_current(self, monkeypatch):
        """The literal defect this script exists to close: cached text must
        never be indistinguishable from a live result. Silence in the banner
        wording is the failure mode.
        """
        cached_text = "# SOUL.md\n\nCached content."
        rai.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        rai.CACHE_FILE.write_text(cached_text)
        _patch_loader(
            monkeypatch,
            _FakeAgentDefinition(prompt_markdown="", is_stub=True, load_error="timeout"),
        )

        markdown, outcome = rai.resolve()

        assert outcome != "live"
        assert markdown.strip() != cached_text.strip()  # banner must change the text materially


class TestNoCacheFallback:
    """State 2b: Neotoma unreachable AND no session cache -> git-tracked
    static mirror, still with a banner (never silent).
    """

    def test_falls_back_to_static_mirror_with_banner(self, monkeypatch):
        static_text = "# SOUL.md\n\nStatic git-tracked mirror content."
        rai.FALLBACK_MIRROR.write_text(static_text)
        _patch_loader(
            monkeypatch,
            _FakeAgentDefinition(prompt_markdown="", is_stub=True, load_error="DNS failure"),
        )

        markdown, outcome = rai.resolve()

        assert outcome == "failed_no_cache"
        assert "STALE IDENTITY" in markdown
        assert "no session cache exists" in markdown
        assert static_text in markdown

    def test_no_cache_no_static_mirror_returns_empty(self, monkeypatch):
        """Neither cache nor static mirror exists (e.g. partial checkout).
        Must return an empty string, never raise -- the caller (bash hook)
        is responsible for the final fallback layer.
        """
        _patch_loader(
            monkeypatch,
            _FakeAgentDefinition(prompt_markdown="", is_stub=True, load_error="unreachable"),
        )

        markdown, outcome = rai.resolve()

        assert outcome == "failed_no_cache"
        assert markdown == ""


class TestFailOpen:
    """State 3: an unexpected exception anywhere in resolve() must not
    propagate past main() -- exit 0, nothing printed, outcome logged as
    "error". Proved red: removing the try/except in main() around
    resolve() makes this test fail with the injected RuntimeError instead
    of exiting cleanly.
    """

    def test_main_swallows_unexpected_exception(self, monkeypatch, capsys):
        def _boom():
            raise RuntimeError("simulated unexpected failure")

        monkeypatch.setattr(rai, "resolve", _boom)

        exit_code = rai.main()

        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == ""

    def test_main_logs_error_outcome_on_exception(self, monkeypatch):
        def _boom():
            raise RuntimeError("simulated unexpected failure")

        monkeypatch.setattr(rai, "resolve", _boom)

        rai.main()

        assert rai.LOG_FILE.exists()
        rows = [json.loads(line) for line in rai.LOG_FILE.read_text().splitlines()]
        assert rows[-1]["outcome"] == "error"
        assert "simulated unexpected failure" in rows[-1]["detail"]


class TestResolutionLogging:
    """Every resolution outcome is appended as one auditable JSON line,
    independent of whether the model faithfully reports it in conversation.
    """

    @pytest.mark.parametrize(
        "agent_def,expected_outcome",
        [
            (_FakeAgentDefinition(prompt_markdown="content", is_stub=False), "live"),
        ],
    )
    def test_live_outcome_logged(self, monkeypatch, agent_def, expected_outcome):
        _patch_loader(monkeypatch, agent_def)

        rai.main()

        rows = [json.loads(line) for line in rai.LOG_FILE.read_text().splitlines()]
        assert rows[-1]["outcome"] == expected_outcome
        assert "ts" in rows[-1]

    def test_cached_outcome_logs_cache_age(self, monkeypatch):
        rai.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        rai.CACHE_FILE.write_text("cached")
        _patch_loader(
            monkeypatch,
            _FakeAgentDefinition(prompt_markdown="", is_stub=True, load_error="down"),
        )

        rai.main()

        rows = [json.loads(line) for line in rai.LOG_FILE.read_text().splitlines()]
        assert rows[-1]["outcome"] == "cached"
        assert "cache_age_seconds=" in rows[-1]["detail"]


class TestAgeFormatting:
    """_age_str's two branches (minutes vs hours) -- a banner reporting the
    wrong unit would understate or overstate staleness.
    """

    def test_age_under_hour_in_minutes(self):
        assert "minute(s)" in rai._age_str(300)

    def test_age_over_hour_in_hours(self):
        assert "hour(s)" in rai._age_str(7200)

    def test_age_over_day_in_days(self):
        assert "day(s)" in rai._age_str(200000)

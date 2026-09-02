"""
Binding tests: the foundation reading list reaches the PROMPTS the arch gate
and review lenses actually run with, and the pm turn's design basis is stored
and handed to the arch gate.

These sit beside test_foundation.py (which tests the loader) and are kept out
of test_swarm_dispatch.py, which several open PRs edit. They reuse that file's
pipeline harness so the pipeline test exercises the real _handle_issue_opened
loop, not a re-implementation of it.

Every assertion here is on prompt CONTENT — the sentinel sentence from a
fixture document, the mechanical verdict on a stated basis — never on a path
being listed. Reverting the wiring in swarm_dispatch.py (the reading_block /
design_basis_block calls) fails every test in this file; the loader tests in
test_foundation.py keep passing, which is exactly the gap this file closes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from github_gateway import SwarmTrigger  # noqa: E402
from issue_spec import DESIGN_BASIS, SECTIONS, assemble_spec_markdown  # noqa: E402
from review_panel import Lens, lens_by_name  # noqa: E402
from swarm_dispatch import SwarmDispatcher  # noqa: E402
from test_foundation import FIXTURE_CONFORMANCE, GATES, PRINCIPLES, SENTINEL  # noqa: E402
from test_swarm_dispatch import (  # noqa: E402
    SkillResult,
    _config,
    _FakeSpecStore,
    _install_pipeline_stubs,
    _issue_trigger,
    _StubNotifier,
)

SPEC_BODY = (
    "**Scope:** {skill}-section body with real substance to pass the "
    "not-just-narration floor."
)
BASIS = "Design basis: docs/foundation/principles.md#what-fires-it — governs the gate"


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    fdir = tmp_path / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(FIXTURE_CONFORMANCE)
    (fdir / "principles.md").write_text(PRINCIPLES)
    monkeypatch.setenv("ATELES_FOUNDATION_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def bare_root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("ATELES_FOUNDATION_ROOT", str(tmp_path))
    return tmp_path


def _pr(body: str = "Implements the thing.") -> SwarmTrigger:
    return SwarmTrigger(
        kind="pr_opened",
        repository="owner/repo",
        number=7,
        title="feat: a thing",
        body=body,
        author="cicada",
        html_url="https://github.com/owner/repo/pull/7",
        delivery_id="d-7",
        action="opened",
        head_ref="feat/thing",
        base_ref="main",
    )


def _section(key: str):
    return next(s for s in SECTIONS if s.key == key)


# ── Review lenses (PR panel) ─────────────────────────────────────────────────


class TestPanelistPromptCarriesTheReadingList:
    def test_arch_lens_gets_kernel_content_and_keyed_doc(self, root: Path) -> None:
        (root / "docs" / "foundation" / "gates_and_workflows.md").write_text(GATES)
        prompt = SwarmDispatcher._panelist_prompt(
            _pr(),
            lens_by_name("arch"),
            "",
            None,
            changed_files=["execution/daemons/apis/swarm_dispatch.py", "README.md"],
        )
        assert SENTINEL in prompt  # kernel doc CONTENT reached the lens
        assert "SENTINEL-GATES-BODY" in prompt  # keyed by the changed path
        assert "keyed by `execution/daemons/apis/swarm_dispatch.py`" in prompt
        assert (
            "`docs/foundation/work_model.md` — kernel, always read; not yet written"
            in prompt
        )

    def test_fires_the_moment_a_doc_lands(self, root: Path) -> None:
        args = (_pr(), lens_by_name("qa"), "", None)
        kw = dict(changed_files=["execution/daemons/apis/review_panel.py"])
        before = SwarmDispatcher._panelist_prompt(*args, **kw)
        assert "SENTINEL-GATES-BODY" not in before
        (root / "docs" / "foundation" / "gates_and_workflows.md").write_text(GATES)
        after = SwarmDispatcher._panelist_prompt(*args, **kw)
        assert "SENTINEL-GATES-BODY" in after

    def test_every_reviewing_lens_gets_the_kernel(self, root: Path) -> None:
        for name in ("pm", "arch", "ux", "qa", "security", "legal"):
            lens = lens_by_name(name)
            assert lens is not None, name
            prompt = SwarmDispatcher._panelist_prompt(
                _pr(), lens, "", None, changed_files=[]
            )
            assert SENTINEL in prompt, name

    def test_forward_looking_lens_gets_nothing(self, root: Path) -> None:
        lens = Lens(
            agent="corvus", lens="downstream", gate="", checks="x", forward_looking=True
        )
        prompt = SwarmDispatcher._panelist_prompt(
            _pr(), lens, "", None, changed_files=["x.py"]
        )
        assert "Design foundation" not in prompt and SENTINEL not in prompt

    def test_pm_and_arch_get_the_pr_body_basis_check_others_do_not(
        self, root: Path
    ) -> None:
        body = f"Implements the thing.\n\n{BASIS}\n"
        arch = SwarmDispatcher._panelist_prompt(
            _pr(body), lens_by_name("arch"), "", None, changed_files=[]
        )
        assert "mechanical check of the PR body" in arch
        assert "cites `docs/foundation/principles.md#what-fires-it` (present)" in arch
        pm = SwarmDispatcher._panelist_prompt(
            _pr(body), lens_by_name("pm"), "", None, changed_files=[]
        )
        assert "mechanical check of the PR body" in pm
        qa = SwarmDispatcher._panelist_prompt(
            _pr(body), lens_by_name("qa"), "", None, changed_files=[]
        )
        assert "mechanical check of the PR body" not in qa

    def test_missing_and_false_basis_are_reported_as_blocking(self, root: Path) -> None:
        missing = SwarmDispatcher._panelist_prompt(
            _pr("no basis here"), lens_by_name("arch"), "", None, changed_files=[]
        )
        assert "Result: INVALID" in missing or "Result: MISSING" in missing
        assert "[BLOCKING] design-basis" in missing
        false = SwarmDispatcher._panelist_prompt(
            _pr("Design basis: docs/foundation/failure_posture.md#halt"),
            lens_by_name("arch"),
            "",
            None,
            changed_files=[],
        )
        assert "INVALID — cites a document not on this checkout" in false

    def test_no_reading_list_leaves_the_prompt_unchanged(self, bare_root: Path) -> None:
        prompt = SwarmDispatcher._panelist_prompt(
            _pr(),
            lens_by_name("arch"),
            "",
            None,
            changed_files=["execution/daemons/apis/swarm_dispatch.py"],
        )
        assert "Design foundation" not in prompt
        assert "mechanical check" not in prompt


# ── Issue spec (pm states the basis, arch checks it) ─────────────────────────


class TestSpecSectionPrompt:
    def test_pm_gets_kernel_and_is_asked_for_a_fenced_basis(self, root: Path) -> None:
        prompt = SwarmDispatcher._spec_section_prompt(
            _issue_trigger(), _section("pm"), ""
        )
        assert SENTINEL in prompt
        assert "<<<DESIGN_BASIS>>>" in prompt and "<<<END_DESIGN_BASIS>>>" in prompt
        assert "no design applies" in prompt
        assert "gate_status.pm" in prompt  # the gate sign-off block survives

    def test_arch_gets_kernel_and_the_check_of_the_stated_basis(
        self, root: Path
    ) -> None:
        prompt = SwarmDispatcher._spec_section_prompt(
            _issue_trigger(), _section("security"), "", design_basis=BASIS
        )
        assert SENTINEL in prompt
        assert "mechanical check of the issue's Design basis section" in prompt
        assert "cites `docs/foundation/principles.md#what-fires-it` (present)" in prompt
        assert "<<<DESIGN_BASIS>>>" not in prompt  # arch checks; it does not state

    def test_arch_sees_missing_when_pm_stated_none(self, root: Path) -> None:
        prompt = SwarmDispatcher._spec_section_prompt(
            _issue_trigger(), _section("security"), "", design_basis=""
        )
        assert "Result: MISSING — no design basis stated" in prompt
        assert "[BLOCKING] design-basis" in prompt

    def test_other_lenses_get_no_block(self, root: Path) -> None:
        for key in ("design", "eng", "qa", "legal"):
            prompt = SwarmDispatcher._spec_section_prompt(
                _issue_trigger(), _section(key), ""
            )
            assert "Design foundation" not in prompt, key
            assert "<<<DESIGN_BASIS>>>" not in prompt, key

    def test_no_reading_list_leaves_pm_prompt_unchanged(self, bare_root: Path) -> None:
        prompt = SwarmDispatcher._spec_section_prompt(
            _issue_trigger(), _section("pm"), ""
        )
        assert "<<<DESIGN_BASIS>>>" not in prompt and "Design foundation" not in prompt


def test_design_basis_renders_first_in_the_mirrored_spec() -> None:
    md = assemble_spec_markdown({"pm_section": "PM TEXT", DESIGN_BASIS.field: BASIS})
    assert md.index("### Design basis") < md.index("### Product / Scope (PM)")
    assert BASIS in md


# ── The real pipeline loop ───────────────────────────────────────────────────


class _LensStub:
    def __init__(self, agent: str) -> None:
        self.agent = agent


def _run_pipeline(
    monkeypatch, *, pavo_emits_basis: bool
) -> tuple[dict, _FakeSpecStore]:
    prompts: dict[str, str] = {}

    async def fake_run_skill(skill, prompt, **kwargs):
        prompts[skill] = prompt
        out = f"<<<SPEC_SECTION>>>{SPEC_BODY.format(skill=skill)}<<<END_SPEC_SECTION>>>"
        if skill == "pavo" and pavo_emits_basis:
            out += f"\n<<<DESIGN_BASIS>>>\n{BASIS}\n<<<END_DESIGN_BASIS>>>\n"
        return SkillResult(skill, True, 0, out, "")

    _install_pipeline_stubs(
        monkeypatch,
        fake_run_skill,
        select_agents=lambda *a, **kw: [_LensStub("waxwing")],
    )
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._handle_issue_opened(_issue_trigger()))
    return prompts, _FakeSpecStore.instances[-1]


def test_pipeline_stores_pm_basis_and_hands_it_to_the_arch_gate(
    monkeypatch, root
) -> None:
    prompts, store = _run_pipeline(monkeypatch, pavo_emits_basis=True)
    # The basis is never its own dispatch: pavo ran once, and the basis was
    # stored right after the pm section from that same run.
    assert [k for k, _ in store.upserts] == ["pm", "basis", "eng", "qa", "security"]
    assert dict(store.upserts)["basis"] == BASIS
    assert dict(store.upserts)["pm"] == SPEC_BODY.format(skill="pavo")
    assert list(prompts) == ["lanius", "pavo", "cicada", "phoenicurus", "waxwing"]
    # pm was handed the kernel and asked for the basis …
    assert SENTINEL in prompts["pavo"] and "<<<DESIGN_BASIS>>>" in prompts["pavo"]
    # … and arch received the mechanical check of what pm stated.
    assert (
        "cites `docs/foundation/principles.md#what-fires-it` (present)"
        in prompts["waxwing"]
    )
    assert SENTINEL in prompts["waxwing"]
    # Lenses in between got neither.
    assert "Design foundation" not in prompts["cicada"]


def test_pipeline_reports_missing_basis_to_arch_when_pm_emitted_none(
    monkeypatch, root
) -> None:
    prompts, store = _run_pipeline(monkeypatch, pavo_emits_basis=False)
    assert dict(store.upserts)["basis"] == ""
    assert "Result: MISSING — no design basis stated" in prompts["waxwing"]

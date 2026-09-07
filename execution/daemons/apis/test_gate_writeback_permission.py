"""
A reviewer must be able to record its own gate verdict (ateles#795).

## The failure these cover

A review panel runs, every lens signs off, zero blocking findings — and the
merge is withheld anyway, because the gate the reviewer was supposed to update
is still `pending`. The reviewer was *prevented from writing its own result*.

Vanellus, on PR #791:

    Accipiter's own ux review notes its gate writeback to the issue entity was
    denied this session (Neotoma correct() required approval while policy is
    never), so gate_status.ux is still pending in the system of record even
    though the lens verdict itself is favorable.

Gate inheritance is a hard stop that outranks `APIS_AUTONOMY_AUTO_MERGE=1`, so
no amount of review quality clears it. Three distinct defects hold that loop
shut, one test class each:

1. **The writeback is denied.** Not by Neotoma — the `agent_grant` admitting
   `issue` on retrieve+correct has been live and `active` since ateles#769, and
   the instance policy is permissive. It is denied by the local harness: a
   headless `claude --print` child runs in `default` permission mode, where an
   MCP write tool it was not explicitly granted raises an approval prompt a
   non-interactive child cannot answer.

2. **A denial is invisible.** A denied writeback and a review that never ran
   both leave the gate reading `pending`. They need opposite fixes, and
   collapsing them is what let #791 sit.

3. **The gate is read stale.** `pending_gates` is parsed from Lanius's stdout
   *before* the panel runs, and the panel is what clears gates — so the merge
   decision consults a snapshot that cannot contain the panel's own writebacks.
   That is #788: every lens signed off, `arch: signed_off` was live, and the
   dispatcher withheld the merge citing `arch` pending.

Run: pytest execution/daemons/apis/test_gate_writeback_permission.py -v
"""

from __future__ import annotations

import inspect

import pytest

import skill_runner
import swarm_dispatch as sd
from review_panel import LENSES


# ── 1. The gate-owning lens can write its own gate ────────────────────────────


class TestGateWritebackIsGranted:
    """The writeback tools reach the child as an explicit grant."""

    def test_restricted_agent_gets_the_gate_writeback_tools_by_name(self):
        """An agent with its own allowlist keeps it AND gains the three tools.

        The `mcp__mcpsrv_neotoma__*` wildcard is not sufficient evidence that
        the writeback is pre-approved: Accipiter had that wildcard on PR #791
        and its `correct()` was still denied. Exact tool names are.
        """
        own = ["Bash", "mcp__mcpsrv_neotoma__retrieve_entities"]
        allowed = skill_runner.gate_writeback_allowlist(own)

        for tool in skill_runner.GATE_WRITEBACK_TOOLS:
            assert tool in allowed, f"{tool} missing — the writeback is prompt-gated"
        # The agent's own grants survive, in order: extending a grant list must
        # never rebuild it (ateles#762 dropped grants exactly that way).
        assert allowed[: len(own)] == own

    def test_unrestricted_agent_still_gets_an_explicit_grant(self):
        """`['*']` is not the permissive state it reads as.

        With `tools == ['*']` the dispatcher previously passed NO
        `--allowed-tools` flag at all, leaving the child in `default` permission
        mode where every MCP write tool prompts. The most-trusted agents were
        the ones whose gate writeback was surest to be denied.
        """
        allowed = skill_runner.gate_writeback_allowlist(["*"])

        assert allowed[0] == "*", "the agent must not be narrowed by this fix"
        for tool in skill_runner.GATE_WRITEBACK_TOOLS:
            assert tool in allowed

    def test_the_grant_is_idempotent(self):
        """Applying it twice adds nothing — no duplicate CLI rules."""
        once = skill_runner.gate_writeback_allowlist(["Bash"])
        assert skill_runner.gate_writeback_allowlist(once) == once

    def test_the_grant_is_narrow(self):
        """Least privilege: exactly the read-back-and-write path, nothing more.

        This is the assertion that fails if someone later "fixes" a permission
        problem by widening this constant. A gate writeback is an internal
        governance write on one entity type — it is not a licence to store new
        entities, delete them, or reach any non-Neotoma surface.
        """
        granted = set(skill_runner.GATE_WRITEBACK_TOOLS)

        assert granted == {
            "mcp__mcpsrv_neotoma__retrieve_entity_by_identifier",
            "mcp__mcpsrv_neotoma__retrieve_entity_snapshot",
            "mcp__mcpsrv_neotoma__correct",
        }
        for forbidden in (
            "mcp__mcpsrv_neotoma__store",
            "mcp__mcpsrv_neotoma__delete_entity",
            "mcp__mcpsrv_neotoma__submit_entity",
            "Bash",
        ):
            assert forbidden not in granted, (
                f"{forbidden} is not needed to record a gate verdict — the "
                "grant must stay at least privilege"
            )

    def test_no_blanket_permission_bypass_on_the_claude_path(self):
        """The fix must not be a permission-mode escape hatch.

        A blanket permission bypass would lift every gate on the child (shell,
        network, the whole MCP surface) to fix one governance write. Assert
        against the argv the adapter actually builds, not against the module's
        source text — the source discusses these flags in a comment explaining
        why they were rejected, and a substring search over prose cannot tell
        an instruction from a note about one. That is the same joined-blob trap
        `test_prose_discussing_the_failure_mode_is_not_a_denial` covers below.
        """
        cmd, _ = skill_runner._provider_command(
            "claude", "claude", "system prompt", "work prompt", cwd=None
        )

        for flag in ("--dangerously-skip-permissions", "--permission-mode"):
            assert flag not in cmd, (
                f"{flag} is passed to the claude child — a blanket permission "
                "bypass is not an acceptable fix for a scoped gate writeback"
            )


# ── 2. A denied writeback surfaces rather than silently no-oping ──────────────


class TestDeniedWritebackIsVisible:
    """`pending` must stop meaning two different things."""

    @pytest.mark.parametrize(
        "text",
        [
            # The attestation as the prompt asks for it: line-leading, reason
            # following. This is what a genuinely silenced lens emits.
            "GATE WRITEBACK DENIED: Neotoma correct() raised an approval "
            "prompt this non-interactive child cannot answer",
            "gate writeback denied: missing agent_grant for issue.correct",
            # Models format. A leading bullet or bold still declares.
            "- GATE WRITEBACK DENIED: permission prompt unanswerable",
            "**GATE WRITEBACK DENIED:** tool not in allowlist",
            # Embedded in a fuller reply, on its own line.
            "review:ux\nFindings: none.\n"
            "GATE WRITEBACK DENIED: correct() was refused\n**BLOCKED**",
        ],
    )
    def test_a_declared_denial_is_detected(self, text):
        """Detection must not weaken: a missed denial reverts to the ateles#795
        bug, where the gate sticks `pending` and is indistinguishable from a
        review that never ran. That is the worse failure of the two."""
        assert sd.detect_gate_writeback_denial(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "**SIGNED_OFF** — gate_status.ux corrected and read back as signed_off.",
            "No blocking findings from the ux lens.",
            "",
            "The merge was denied by branch protection.",
        ],
    )
    def test_a_clean_review_is_not_flagged(self, text):
        """A false positive here BLOCKS a good PR, so the bar is high."""
        assert sd.detect_gate_writeback_denial(text) is False

    def test_a_review_quoting_this_modules_own_test_strings_is_not_a_denial(self):
        """The self-referential false positive Loxia raised on PR #800.

        The first revision matched guessed PROSE phrasings. Per-line anchoring
        defeated the joined-blob trap for paragraphs, but not for a reviewer
        quoting a trigger phrase as its own STANDALONE LINE — and the review
        most likely to do that is a review of this very change, where those
        phrases live as single lines in this file. Every string below is one
        this module's own first revision carried as a detection fixture; a
        panelist reviewing PR #800 and echoing them must come back clean.

        A guard that misfires on text ABOUT the thing it guards is a known
        shape in this repo, not a hypothetical: ateles#768, where the Gmail
        send-gate blocked a `git commit` whose message quoted a Gmail helper.
        """
        review = (
            "review:arch\n"
            "The detector's fixtures include these as standalone lines:\n"
            "gate writeback to the issue entity was denied this session\n"
            "Neotoma correct() required approval while policy is never\n"
            "GATE_WRITEBACK: denied\n"
            "Could not write gate_status for this lens.\n"
            "unable to correct gate_status on the parent issue\n"
            "Each would have tripped the previous prose signatures verbatim.\n"
            "**APPROVE**\n"
        )
        assert sd.detect_gate_writeback_denial(review) is False

    def test_discussing_the_attestation_token_inline_is_not_a_denial(self):
        """Prose ABOUT the marker mentions it mid-sentence, in backticks.

        The marker survives quotation where a prose phrase could not, because
        declaring requires the reserved token at line start — which is exactly
        what writing about the mechanism does not do.
        """
        review = (
            "review:arch\n"
            "A refused lens now emits `GATE WRITEBACK DENIED:` and the "
            "dispatcher escalates on that token.\n"
            "The constant is GATE_WRITEBACK_DENIED_ATTESTATION.\n"
            "**APPROVE**\n"
        )
        assert sd.detect_gate_writeback_denial(review) is False

    def test_the_prompt_tells_the_lens_to_emit_the_attestation(self):
        """A marker nobody is told to write detects nothing.

        Unlike the prose signatures it replaces, this contract only holds if
        the gate-writeback instructions name the exact token — the same
        prompt/parser pairing MERGE_REFUSED_MARKER already uses.
        """
        src = inspect.getsource(sd.SwarmDispatcher._panelist_prompt)
        assert "GATE_WRITEBACK_DENIED_ATTESTATION" in src, (
            "the panelist prompt must instruct the lens to emit the "
            "attestation, or a real denial is never declared"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "I could not correct gate_status — the tool call was denied.",
            "I was denied permission to write the gate.",
            "My correct() call was refused by the local harness.",
            "- I could not update gate_status on the parent issue.",
            "We were unable to record gate_status for this lens.",
        ],
    )
    def test_a_paraphrased_first_person_denial_still_detected(self, text):
        """The marker is the contract, but a prompt is not a guarantee.

        A model that paraphrases must not slip through: a MISSED denial reverts
        to the ateles#795 bug exactly — the gate sticks `pending` and is again
        indistinguishable from a review that never ran. A false positive only
        costs one visible comment, so the net stays.
        """
        assert sd.detect_gate_writeback_denial(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            # Third-person description of the mechanism — what a REVIEW of this
            # change writes, and what the first revision wrongly matched.
            "The lens could not correct gate_status, so the gate stays pending.",
            "Accipiter's writeback was denied this session.",
            "A denied writeback leaves gate_status pending.",
            "This PR detects when correct() was refused.",
        ],
    )
    def test_third_person_description_is_not_a_denial(self, text):
        """First person is what separates a claim from a description.

        The fallback must not reopen the hole the marker closed: only the
        acting agent says "I could not"; prose about the mechanism says "the
        lens could not".
        """
        assert sd.detect_gate_writeback_denial(text) is False

    def test_detection_reads_every_supplied_stream(self):
        """A denial on stderr counts as much as one on stdout."""
        assert sd.detect_gate_writeback_denial(
            "**SIGNED_OFF**", "GATE WRITEBACK DENIED: correct() refused"
        ) is True


# ── 3. The dispatcher reads gate_status at decision time ──────────────────────


class _GateState:
    def __init__(self, gate_status: dict[str, str], found: bool = True) -> None:
        self.gate_status = gate_status
        self.found = found


def _dispatcher_with_gate_state(monkeypatch, state, raises=None):
    """A dispatcher whose IssueGateStore returns `state` (or raises)."""

    class _Store:
        def __init__(self, *a, **kw) -> None:
            pass

        async def load(self, repository, number):
            if raises is not None:
                raise raises
            return state

    monkeypatch.setattr(sd, "IssueGateStore", _Store)
    d = sd.SwarmDispatcher.__new__(sd.SwarmDispatcher)
    d.config = type(
        "C", (), {"neotoma_base_url": "https://example.invalid", "neotoma_token": "t"}
    )()
    return d


@pytest.mark.asyncio
class TestPendingGatesAreReReadAtDecisionTime:
    async def test_a_gate_signed_during_the_panel_is_no_longer_pending(
        self, monkeypatch
    ):
        """ateles#788 exactly.

        The pre-panel snapshot says `arch` is pending. During the panel Waxwing
        signs off and writes the entity. At merge-decision time the record says
        `signed_off`, and the dispatcher must believe the record, not its own
        stale snapshot.
        """
        d = _dispatcher_with_gate_state(
            monkeypatch, _GateState({"arch": "signed_off", "pm": "signed_off"})
        )

        result = await d._refresh_pending_gates("o/r", 788, {"arch"})

        assert result == set(), (
            "the gate cleared in the record during the panel — withholding the "
            "merge here is the #788 stale read"
        )

    async def test_a_genuinely_pending_gate_still_blocks(self, monkeypatch):
        """The re-read must not become a way to clear gates."""
        d = _dispatcher_with_gate_state(
            monkeypatch, _GateState({"arch": "pending", "ux": "signed_off"})
        )

        assert await d._refresh_pending_gates("o/r", 788, {"arch", "ux"}) == {"arch"}

    async def test_a_read_failure_keeps_the_snapshot(self, monkeypatch):
        """Fails SAFE: a broken read must never CLEAR a gate.

        The worst case is the status quo — a stale block the operator can see —
        never an unearned merge.
        """
        d = _dispatcher_with_gate_state(
            monkeypatch, None, raises=RuntimeError("neotoma down")
        )

        assert await d._refresh_pending_gates("o/r", 788, {"arch"}) == {"arch"}

    async def test_a_missing_entity_keeps_the_snapshot(self, monkeypatch):
        d = _dispatcher_with_gate_state(monkeypatch, _GateState({}, found=False))

        assert await d._refresh_pending_gates("o/r", 788, {"arch"}) == {"arch"}

    async def test_no_parent_issue_is_a_no_op(self, monkeypatch):
        """A PR with no parent has no issue entity to read."""
        d = _dispatcher_with_gate_state(monkeypatch, _GateState({}))

        assert await d._refresh_pending_gates("o/r", None, {"arch"}) == {"arch"}

    async def test_every_cleared_state_counts_as_cleared(self, monkeypatch):
        """`waived` and `not_required` clear a gate as surely as `signed_off`."""
        for cleared in sorted(sd.CLEARED_GATE_STATES):
            d = _dispatcher_with_gate_state(monkeypatch, _GateState({"ux": cleared}))
            assert await d._refresh_pending_gates("o/r", 1, {"ux"}) == set(), (
                f"{cleared!r} is a cleared state and must not block"
            )


# ── The invariant that ties the three together ────────────────────────────────


def test_every_gate_owning_lens_is_covered_by_the_grant():
    """Derived from the LENSES registry, so a lens added later is covered.

    ateles#769 learned this the hard way: the issue's own table named four
    lenses and missed a fifth (buteo/legal), which only surfaced because the
    check derived its list from the registry rather than from the table.
    """
    gate_owners = [lens for lens in LENSES if lens.gate]

    assert gate_owners, "no gate-owning lenses found — the registry moved"
    # The grant is not per-agent: it rides every claude dispatch, so every gate
    # owner is covered by construction. Assert the property that makes that
    # true rather than re-listing the agents.
    assert "mcp__mcpsrv_neotoma__correct" in skill_runner.GATE_WRITEBACK_TOOLS

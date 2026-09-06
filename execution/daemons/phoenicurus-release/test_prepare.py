"""
Tests for prepare.py idempotency locks — the auto-release --on-merge path.

Two independent locks:
  - the scheduled path (once per calendar day, .phoenicurus_prepare_last_run)
  - the on-merge path (once per origin/main commit, .phoenicurus_prepare_last_sha)

The subtle case is a TRANSIENT deferral (CI in progress or CI red) in on-merge
mode: it must NOT stamp the SHA, so the check_suite-completion retry can prepare
that same head once CI settles. Stamping there is the bug Loxia flagged on
ateles#253 — it would burn the per-commit lock on a run that did nothing.

Run: pytest execution/daemons/phoenicurus-release/test_prepare.py -v
"""

from __future__ import annotations

import pytest

import prepare


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point both lock files at a temp dir so tests never touch real state."""
    monkeypatch.setattr(prepare, "MERGE_STATE_FILE", tmp_path / ".sha")
    monkeypatch.setattr(prepare, "STATE_FILE", tmp_path / ".day")
    return tmp_path


SHA = "a" * 40


def test_on_merge_terminal_run_stamps_sha(isolated_state):
    assert not prepare._already_ran_for_sha(SHA)
    prepare._mark_ran(on_merge=True, head=SHA)
    assert prepare._already_ran_for_sha(SHA), "a completed on-merge run locks its SHA"


def test_on_merge_new_commit_not_locked_by_prior(isolated_state):
    prepare._mark_ran(on_merge=True, head=SHA)
    assert not prepare._already_ran_for_sha("b" * 40), "a new merge commit is a fresh run"


def test_transient_deferral_does_not_stamp_sha(isolated_state):
    # CI in-progress / red: the SHA must stay unlocked so the check_suite retry
    # can prepare this head once CI goes green.
    prepare._mark_ran(on_merge=True, head=SHA, transient=True)
    assert not prepare._already_ran_for_sha(SHA), (
        "a transient on-merge deferral must leave the SHA unstamped for retry"
    )


def test_scheduled_path_stamps_day_even_when_transient(isolated_state):
    # The scheduled sweep is deliberately once-a-day; a same-day retry is not
    # wanted there, so it stamps regardless of transience.
    prepare._mark_ran(on_merge=False, head="", transient=True)
    assert prepare._already_ran_today()


def test_locks_are_independent(isolated_state):
    # An on-merge run must not consume the daily lock (or the scheduled safety
    # net would be suppressed by webhook activity), and vice versa.
    prepare._mark_ran(on_merge=True, head=SHA)
    assert not prepare._already_ran_today(), "on-merge run must not consume the daily lock"
    prepare._mark_ran(on_merge=False, head="")
    assert prepare._already_ran_for_sha(SHA), "daily stamp must not clear the SHA lock"


# ── email_send: release notifications also go to the operator's inbox ────────
#
# Release RCs now email the operator (via gws +send, same transport as the rest
# of the swarm) in addition to Telegram. email_send MUST be fail-open: any
# missing config or send failure returns False so the caller keeps Telegram as
# the guaranteed channel — a release notification is never blocked on email.

from unittest.mock import patch, MagicMock  # noqa: E402


def test_email_send_skips_when_operator_email_unset(monkeypatch):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "")
    # Must not even look for gws when there's no recipient.
    with patch("shutil.which") as which:
        assert prepare.email_send("subj", "body") is False
        assert not which.called


def test_email_send_returns_false_when_gws_missing(monkeypatch):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    with patch("shutil.which", return_value=None):
        assert prepare.email_send("subj", "body") is False


def test_email_send_builds_gws_command_with_from(monkeypatch):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "swarm@example.com")
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("shutil.which", return_value="/usr/bin/gws"), \
         patch.object(prepare.subprocess, "run", side_effect=fake_run):
        assert prepare.email_send("🚀 subj", "body text") is True
    cmd = calls["cmd"]
    assert cmd[:3] == ["/usr/bin/gws", "gmail", "+send"]
    assert "--to" in cmd and "op@example.com" in cmd
    assert "--from" in cmd and "swarm@example.com" in cmd
    assert "--subject" in cmd and "🚀 subj" in cmd
    assert "--body" in cmd
    body_arg = cmd[cmd.index("--body") + 1]
    assert "body text" in body_arg
    assert "--html" in cmd


# ── _plain_to_html: markdown-ish daemon notices rendered for email ───────────

def test_plain_to_html_heading_levels_map_one_to_one():
    assert prepare._plain_to_html("# Title") == "<h1>Title</h1>"
    assert prepare._plain_to_html("## Section") == "<h2>Section</h2>"


def test_plain_to_html_bold_and_code():
    assert prepare._plain_to_html("**hello**") == "<p><strong>hello</strong></p>"
    assert prepare._plain_to_html("`gws gmail +send`") == "<p><code>gws gmail +send</code></p>"


def test_plain_to_html_code_span_contents_are_literal():
    # Markdown code spans are not re-interpreted as markdown — `**x**` inside
    # backticks must stay literal text, not become <strong> nested in <code>.
    assert prepare._plain_to_html("`**not bold**`") == "<p><code>**not bold**</code></p>"


def test_plain_to_html_bullets():
    assert prepare._plain_to_html("- item one\n- item two") == (
        "<ul>\n<li>item one</li>\n<li>item two</li>\n</ul>"
    )


def test_plain_to_html_markdown_link():
    assert prepare._plain_to_html("[here](https://example.com)") == (
        '<p><a href="https://example.com">here</a></p>'
    )


def test_plain_to_html_escapes_quotes_in_url_no_attribute_injection():
    # A literal `"` in a URL must not break out of the generated href attribute.
    out = prepare._plain_to_html('[label](https://example.com/"onmouseover="alert(1))')
    assert "&quot;" in out
    assert 'onmouseover="alert' not in out


def test_plain_to_html_no_inline_styling():
    # Semantic tags only — no font-family/font-size/color/style so mail inherits
    # the client's own typography rather than fighting it.
    out = prepare._plain_to_html(
        "# Heading\n\n**bold** and `code` and a bullet:\n- one\n- two\n"
        "[link](https://example.com)"
    )
    assert "font-family" not in out
    assert "font-size" not in out
    assert "color" not in out
    assert "style=" not in out


def test_email_send_body_is_html_not_raw_markdown(monkeypatch):
    # Effect-level check: a realistic daemon notice sent through email_send()
    # must arrive as converted HTML, not the raw markdown source. Asserting only
    # "the input string appears in --body" (as the pre-existing test does) would
    # also pass if --html conversion were silently dropped — this asserts the
    # actual reported defect (literal `**`/`- ` markers reaching Gmail) cannot
    # recur. Ports the #338 sibling coverage onto this branch for panel re-run.
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "")
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    markdown_notice = (
        "## Release blocked\n\n**CI is red** on `origin/main`.\n"
        "- retry after fix\n- check the run"
    )
    with patch("shutil.which", return_value="/usr/bin/gws"), \
         patch.object(prepare.subprocess, "run", side_effect=fake_run):
        assert prepare.email_send("🔴 subj", markdown_notice) is True
    cmd = calls["cmd"]
    assert "--html" in cmd
    body_arg = cmd[cmd.index("--body") + 1]
    assert body_arg == prepare._plain_to_html(markdown_notice)
    assert "<h2>Release blocked</h2>" in body_arg
    assert "<strong>CI is red</strong>" in body_arg
    assert "<code>origin/main</code>" in body_arg
    assert "<ul>" in body_arg and "<li>retry after fix</li>" in body_arg
    # The raw markdown markers must not survive into the sent body.
    assert "**" not in body_arg
    assert "\n- retry after fix" not in body_arg
    assert "##" not in body_arg


def test_email_send_fail_open_on_nonzero_rc(monkeypatch):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "")
    with patch("shutil.which", return_value="/usr/bin/gws"), \
         patch.object(prepare.subprocess, "run",
                      return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
        assert prepare.email_send("subj", "body") is False


def test_email_send_fail_open_on_exception(monkeypatch):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    with patch("shutil.which", return_value="/usr/bin/gws"), \
         patch.object(prepare.subprocess, "run", side_effect=OSError("no gws")):
        assert prepare.email_send("subj", "body") is False


def test_agent_prompt_includes_email_step_when_configured(monkeypatch):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "swarm@example.com")
    p = prepare._build_agent_prompt("v0.19.0", 3)
    assert "gws gmail +send" in p
    assert "op@example.com" in p
    assert "approve" in p.lower() and "skip" in p.lower()
    # The swarm sender, when configured, is passed as --from.
    assert '--from "swarm@example.com"' in p


def test_agent_prompt_carries_html_email_contract(monkeypatch):
    """The prompt must instruct the agent to send HTML, not raw markdown.

    This is path-1 of ateles#336. The release notes are markdown; sent as a
    plain --body they reach Gmail as a literal wall of `##` and `-`. The
    agent is a black box driven only by this prompt, so the prompt IS the
    contract — if these instructions drop out, the operator silently starts
    receiving unreadable release mail again with every unit test still green.
    """
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "")
    p = prepare._build_agent_prompt("v0.20.0", 3)

    # (a) The flag itself must be named, and named in the sample command.
    assert "--html" in p
    assert "--html" in _email_send_command(p), \
        "the sample gws send command must carry --html"

    # (b) The imperative must be unmissable, not merely implied.
    assert "SEND AS HTML" in p
    assert "ALWAYS include `--html`" in p

    # (c) The markdown -> HTML conversion instruction, element by element.
    assert "Convert markdown → HTML" in p
    for markdown_token, html_tag in (
        ("##", "<h2>"),
        ("-", "<ul><li>"),
        ("**bold**", "<strong>"),
        ("backticks", "<code>"),
    ):
        assert markdown_token in p and html_tag in p, \
            f"conversion rule for {markdown_token} -> {html_tag} is missing"

    # (d) Do not pin a font size — the client default must apply.
    assert "font-size" in p and "Do NOT inline" in p

    # (e) The RC PR must be a real link, not a bare URL.
    assert "<a href>" in p

    # (f) The machine-parsed approval token Turdus greps out of the reply.
    #     It must survive as plain text, so it may not live only in an href.
    assert "release-approve: <TAG>" in p
    assert "NOT only inside an href" in p


def _email_send_command(prompt: str) -> str:
    """The single `gws gmail +send ...` line out of the agent prompt."""
    for line in prompt.splitlines():
        if "gws gmail +send" in line:
            return line
    raise AssertionError("no `gws gmail +send` command found in the prompt")


def test_agent_prompt_notes_skip_when_email_unconfigured(monkeypatch):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "")
    p = prepare._build_agent_prompt("v0.19.0", 3)
    assert "Email notification skipped" in p
    assert "gws gmail +send" not in p
    # No email step at all means no HTML contract to carry.
    assert "SEND AS HTML" not in p
    assert "--html" not in p


def test_notify_operator_sends_both_channels(monkeypatch):
    # The synchronous hard-block notices (CI red, spawn failure, crash) go to
    # BOTH Telegram and email — this is where email_send() is wired into the
    # run flow (the rich RC email is sent by the spawned agent separately).
    sent = {}
    monkeypatch.setattr(prepare, "telegram_send", lambda t: sent.setdefault("tg", t))
    monkeypatch.setattr(prepare, "email_send",
                        lambda subj, body: sent.setdefault("email", (subj, body)) or True)
    prepare.notify_operator("🔴 something broke\nmore detail")
    assert sent["tg"] == "🔴 something broke\nmore detail"
    # email subject defaults to the first line; body is the full text
    assert sent["email"][0] == "🔴 something broke"
    assert "more detail" in sent["email"][1]


def test_notify_operator_email_failure_does_not_block_telegram(monkeypatch):
    order = []
    monkeypatch.setattr(prepare, "telegram_send", lambda t: order.append("tg"))
    def boom(subj, body):
        order.append("email")
        raise RuntimeError("email exploded")
    monkeypatch.setattr(prepare, "email_send", boom)
    # email_send is itself fail-open, but even if it raised, Telegram already ran
    try:
        prepare.notify_operator("msg")
    except RuntimeError:
        pass
    assert "tg" in order  # Telegram fired first, regardless of email outcome


# ── _agent_env: prefer Max OAuth over pay-per-token API key ──────────────────
#
# The first live prepare run (2026-07-27) died on "Credit balance is too low":
# the headless agent inherited both credentials and `claude --print` used the
# empty-credit ANTHROPIC_API_KEY. _agent_env drops the API key from the CHILD
# env when the OAuth token is present, so the agent bills the subscription.


def test_agent_env_prefers_oauth_when_both_set(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    env = prepare._agent_env()
    assert "ANTHROPIC_API_KEY" not in env, "API key must be dropped from child env"
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "oauth-tok"


def test_agent_env_leaves_api_key_only_untouched(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    env = prepare._agent_env()
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-xxx", "no OAuth → keep the API key"


def test_agent_env_does_not_mutate_daemon_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    prepare._agent_env()
    import os as _os
    assert _os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-xxx", (
        "the daemon's own env must be untouched — only the child copy is trimmed"
    )

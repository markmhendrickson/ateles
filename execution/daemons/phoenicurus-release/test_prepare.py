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

import os

import pytest

import prepare
import verify_guard_live as live


@pytest.fixture(autouse=True)
def approval_addresses(monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.com")


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point both lock files at a temp dir so tests never touch real state."""
    monkeypatch.setattr(prepare, "MERGE_STATE_FILE", tmp_path / ".sha")
    monkeypatch.setattr(prepare, "STATE_FILE", tmp_path / ".day")
    return tmp_path


SHA = "a" * 40


@pytest.mark.parametrize("case,expected", (
    ("refused", 0), ("declined", 2), ("absent", 2),
    ("control_unseen", 2), ("hook_error", 2),
))
def test_live_binding_requires_hook_receipts(monkeypatch, tmp_path, capsys, case, expected):
    monkeypatch.setattr(live, "_load_prepare", lambda: object())
    monkeypatch.setattr(live.tempfile, "mkdtemp", lambda **_: str(tmp_path))
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: None)
    counts = iter((1, 2, 2))
    monkeypatch.setattr(live, "_count", lambda _repo: next(counts))

    def ask(_prepare, _repo, command):
        if "--no-verify" not in command:
            return 0, [] if case == "control_unseen" else [{"decision": "allow"}]
        if case == "refused":
            return 0, [{"decision": "deny", "reason": "git commit --no-verify"}]
        if case == "hook_error":
            return 1, [{"decision": "deny", "reason": "internal guard error"}]
        return 0, []

    monkeypatch.setattr(live, "_ask", ask)
    assert live.main() == expected
    output = capsys.readouterr().out
    assert ("PASS:" in output) == (case == "refused")


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
    monkeypatch.delenv("OPERATOR_EMAIL")
    # Must not even look for gws when there's no recipient.
    with patch("shutil.which") as which:
        assert prepare.email_send("subj", "body") is False
        assert not which.called


def test_email_send_returns_false_when_gws_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    with patch("shutil.which", return_value=None):
        assert prepare.email_send("subj", "body") is False


def test_email_send_builds_gws_command_with_from(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "swarm@example.com")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "/synthetic/ambient-gws")
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["env"] = kw["env"]
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
    assert calls["env"]["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == str(tmp_path)


def test_failure_notice_skips_email_without_swarm_mailbox(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", str(tmp_path))
    with patch("shutil.which", return_value="/usr/bin/gws"), \
         patch.object(prepare.subprocess, "run") as run, \
         patch.object(prepare, "telegram_send"):
        prepare.notify_operator("synthetic failure notice")
    run.assert_not_called()


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


def test_email_send_body_is_html_not_raw_markdown(monkeypatch, tmp_path):
    # Effect-level check: a realistic daemon notice sent through email_send()
    # must arrive as converted HTML, not the raw markdown source. Asserting only
    # "the input string appears in --body" (as the pre-existing test does) would
    # also pass if --html conversion were silently dropped — this asserts the
    # actual reported defect (literal `**`/`- ` markers reaching Gmail) cannot
    # recur. Ports the #338 sibling coverage onto this branch for panel re-run.
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
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


def test_email_send_fail_open_on_nonzero_rc(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    with patch("shutil.which", return_value="/usr/bin/gws"), \
         patch.object(prepare.subprocess, "run",
                      return_value=MagicMock(returncode=1, stdout="", stderr="boom")) as run:
        assert prepare.email_send("subj", "body") is False
        run.assert_called_once()


def test_email_send_fail_open_on_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    with patch("shutil.which", return_value="/usr/bin/gws"), \
         patch.object(prepare.subprocess, "run", side_effect=OSError("no gws")) as run:
        assert prepare.email_send("subj", "body") is False
        run.assert_called_once()


def test_agent_prompt_includes_email_step_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "swarm@example.com")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    p = prepare._build_agent_prompt("v0.19.0", 3)
    assert "gws gmail +send" in p
    assert "op@example.com" in p
    assert "approve" in p.lower() and "skip" in p.lower()
    email_step = p.split("11. Email the", 1)[-1].split("12. Send a Telegram", 1)[0]
    assert "email replies cannot discard" in email_step
    assert "Reply <code>approve &lt;TAG&gt;</code>" in email_step
    assert "or <code>skip &lt;TAG&gt;</code> to discard" not in email_step
    # The swarm sender, when configured, is passed as --from.
    assert '--from "swarm@example.com"' in p


def test_agent_prompt_carries_html_email_contract(monkeypatch, tmp_path):
    """The prompt must instruct the agent to send HTML, not raw markdown.

    This is path-1 of ateles#336. The release notes are markdown; sent as a
    plain --body they reach Gmail as a literal wall of `##` and `-`. The
    agent is a black box driven only by this prompt, so the prompt IS the
    contract — if these instructions drop out, the operator silently starts
    receiving unreadable release mail again with every unit test still green.
    """
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", "")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
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

    # (f) The visible token enables mailbox discovery; the subject selects
    #     the release. It must survive in the quoted plain text.
    assert "release-approve: <TAG>" in p
    assert "mailbox" in p and "subject" in p
    assert "only inside an href" in p


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


def test_rc_email_absent_swarm_directory_uses_telegram_only(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR", raising=False)
    prompt = prepare._build_agent_prompt("v0.19.0", 3)
    assert "gws gmail +send" not in prompt
    assert "Do not run gws" in prompt
    assert "Email approval unavailable; approve through Telegram instead." in prompt
    chosen = prepare._agent_env()["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"]
    assert chosen != str(tmp_path) and os.listdir(chosen) == []


@pytest.mark.parametrize("swarm,cfg", [
    (None, True),
    ("malformed", True),
    ("op@example.com", True),
    ("swarm@example.com", False),
])
def test_unreadable_mailbox_never_offers_or_sends_approval(
    monkeypatch, tmp_path, swarm, cfg
):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setattr(prepare, "SWARM_EMAIL", swarm or "")
    if swarm is None:
        monkeypatch.delenv("ATELES_SWARM_EMAIL")
    else:
        monkeypatch.setenv("ATELES_SWARM_EMAIL", swarm)
    config_dir = tmp_path if cfg else tmp_path / "missing"
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(config_dir))

    prompt = prepare._build_agent_prompt("v1.2.3", 1)
    assert "gws gmail +send" not in prompt
    assert "Email approval unavailable; approve through Telegram instead." in prompt
    assert "Do not run gws" in prompt
    with patch("shutil.which", return_value="/synthetic/gws"), \
         patch.object(prepare.subprocess, "run") as run:
        assert prepare.email_send("Release v1.2.3 ready to approve", "notes") is False
    run.assert_not_called()
    chosen = prepare._agent_env()["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"]
    assert chosen != str(config_dir) and os.listdir(chosen) == []


def test_rc_email_send_failure_is_in_telegram_instructions(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    prompt = prepare._build_agent_prompt("v0.19.0", 3)
    assert prompt.index("gws gmail +send") < prompt.index("12. Send a Telegram")
    assert ("Email approval unavailable because the RC email failed to\n"
            "    send; approve through Telegram instead.") in prompt
    assert "Include the email-approval status sentence" in prompt


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


# ── _agent_env: allowlist + prefer Max OAuth over pay-per-token API key ──────
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


# ── Prepare agent must not skip git hooks (ent_13f7f83c89d6adcac957dd96) ─────
#
# The prepare agent used --no-verify on at least six release runs. The daemon
# handed it the operator's storage selectors and arbitrary ambient variables,
# which made release-worktree tests fail for environment reasons. The child now
# starts from the shared runner allowlist, not a denylist.

_PREPARE_CHILD_FORBIDDEN = (
    "NEOTOMA_ENV",
    "NEOTOMA_DATA_DIR",
    "NEOTOMA_SQLITE_PATH",
    "NEOTOMA_RAW_STORAGE_DIR",
    "NEOTOMA_DB_BACKEND",
    "NEOTOMA_DB_URL",
    "NEOTOMA_EVENT_LOG_PATH",
    "NEOTOMA_EVENT_LOG_DIR",
    "NEOTOMA_LOGS_DIR",
    "NEOTOMA_MCP_USE_LOCAL_PORT_FILE",
    "NEOTOMA_BASE_URL",
    "NEOTOMA_PROJECT_ROOT",
    "NEOTOMA_REPO_ROOT",
    "HUSKY",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "OPENAI_API_KEY",
    "TOTALLY_UNKNOWN_AMBIENT_VARIABLE",
)


@pytest.mark.parametrize("name", _PREPARE_CHILD_FORBIDDEN)
def test_agent_env_drops_storage_git_and_unknown_ambient_names(monkeypatch, name):
    monkeypatch.setenv(name, "canary")
    env = prepare._agent_env()
    assert name not in env
    import os as _os
    assert _os.environ.get(name) == "canary", "daemon env untouched"


def test_agent_env_maps_swarm_gws_config(monkeypatch, tmp_path):
    # The RC email must go out as the swarm mailbox: a configured swarm gws
    # directory becomes the child's GOOGLE_WORKSPACE_CLI_CONFIG_DIR and wins
    # over an already-set value, the same precedence email_channel uses.
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "/synthetic/operator-gws")
    env = prepare._agent_env()
    assert env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == str(tmp_path)
    assert "ATELES_SWARM_GWS_CONFIG_DIR" not in env
    import os as _os
    assert _os.environ["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == "/synthetic/operator-gws"

    monkeypatch.delenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR")
    env = prepare._agent_env()
    assert env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == str(tmp_path)


def test_agent_env_blocks_default_gws_login_when_swarm_dir_absent(monkeypatch, tmp_path):
    # The mapping is the shared email_channel helper, called rather than copied.
    from lib.approval import email_channel
    assert prepare.gws_config_dir_env is email_channel.gws_config_dir_env
    ambient = tmp_path / "operator-gws"
    ambient.mkdir()
    (ambient / "credentials.json").write_text("{}")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", str(ambient))
    for swarm in (None, "", str(tmp_path / "missing-swarm-dir")):
        if swarm is None:
            monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR", raising=False)
        else:
            monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", swarm)
        chosen = prepare._agent_env()["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"]
        # Never the ambient (operator) directory, never gws's default: an
        # existing, empty directory, so gws has no login to send with.
        assert chosen != str(ambient)
        assert os.path.isdir(chosen) and os.listdir(chosen) == []


def test_agent_env_keeps_only_needed_release_additions(monkeypatch):
    monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("PATH", "/synthetic/bin")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "/synthetic/gws")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/synthetic/claude")
    env = prepare._agent_env()
    assert env["PATH"] == "/synthetic/bin"
    assert env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] != "/synthetic/gws"
    assert os.listdir(env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"]) == []
    assert env["CLAUDE_CONFIG_DIR"] == "/synthetic/claude"


def test_agent_settings_loads_the_bypass_guard():
    import json as _json
    settings = _json.loads(prepare._agent_settings())
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash|Edit|Write|MultiEdit"
    command = entry["hooks"][0]["command"]
    assert str(prepare.HOOK_BYPASS_GUARD) in command
    assert prepare.HOOK_BYPASS_GUARD.is_file()


def test_agent_settings_guard_refuses_no_verify():
    """The command the agent is given, run as the harness would run it."""
    import json as _json
    import shlex as _shlex
    import subprocess as _sp
    hook = _json.loads(prepare._agent_settings())["hooks"]["PreToolUse"][0]["hooks"][0]
    payload = _json.dumps({"tool_name": "Bash",
                           "tool_input": {"command": "git commit --no-verify -m x"}})
    proc = _sp.run(_shlex.split(hook["command"]), input=payload,
                   capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
    assert _json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_agent_settings_guard_refuses_file_tool_config_write():
    import json as _json
    import shlex as _shlex
    import subprocess as _sp
    entry = _json.loads(prepare._agent_settings())["hooks"]["PreToolUse"][0]
    assert set(entry["matcher"].split("|")) == {"Bash", "Edit", "Write", "MultiEdit"}
    for tool in ("Edit", "Write", "MultiEdit"):
        payload = _json.dumps({"tool_name": tool, "tool_input": {"file_path": ".git/config"}})
        proc = _sp.run(_shlex.split(entry["hooks"][0]["command"]), input=payload,
                       capture_output=True, text=True, timeout=30)
        assert proc.returncode == 2, (tool, proc.stdout, proc.stderr)


def test_spawn_passes_settings_to_claude(monkeypatch, tmp_path):
    import shutil as _shutil
    from unittest.mock import patch as _patch
    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(prepare, "AGENT_LOG", tmp_path / "agent.log")
    monkeypatch.setattr(prepare, "_record_spawn_pid", lambda head, pid: None)
    monkeypatch.setenv("NEOTOMA_ENV", "production")
    monkeypatch.setattr(prepare, "_agent_github_identity_ok", lambda env: True)
    with _patch.object(prepare.subprocess, "Popen") as mock_popen:
        assert prepare.spawn_prepare_agent("v0.18.8", 4, dry_run=False) is True
    argv = mock_popen.call_args[0][0]
    i = argv.index("--settings")
    assert argv[i + 1] == prepare._agent_settings()
    assert "NEOTOMA_ENV" not in mock_popen.call_args[1]["env"]


def test_spawn_refuses_without_guard(monkeypatch, tmp_path):
    import shutil as _shutil
    from unittest.mock import patch as _patch
    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(prepare, "HOOK_BYPASS_GUARD", tmp_path / "missing.py")
    with _patch.object(prepare, "notify_operator") as notify, \
            _patch.object(prepare.subprocess, "Popen") as mock_popen:
        assert prepare.spawn_prepare_agent("v0.18.8", 4, dry_run=False) is False
    mock_popen.assert_not_called()
    notify.assert_called_once()


def test_agent_env_passes_the_swarm_github_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x" * 40)
    monkeypatch.setenv("GH_TOKEN", "y" * 40)
    env = prepare._agent_env()
    assert env["GITHUB_TOKEN"] == "x" * 40 and env["GH_TOKEN"] == "y" * 40


def test_spawn_refuses_when_github_identity_is_wrong(monkeypatch, tmp_path):
    """Red before: the agent spawned with whatever GitHub identity gh fell back to."""
    import shutil as _shutil
    from unittest.mock import patch as _patch
    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(prepare, "AGENT_LOG", tmp_path / "agent.log")
    failed = prepare.subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    with _patch.object(prepare.subprocess, "run", return_value=failed) as mock_run, \
            _patch.object(prepare, "notify_operator") as notify, \
            _patch.object(prepare.subprocess, "Popen") as mock_popen:
        assert prepare.spawn_prepare_agent("v0.18.8", 4, dry_run=False) is False
    mock_popen.assert_not_called()
    notify.assert_called_once()
    argv = mock_run.call_args[0][0]
    assert argv[-2:] == ["--expect-login", prepare.RC_GITHUB_LOGIN]


def test_github_identity_check_failing_to_run_counts_as_unverified(monkeypatch):
    def boom(*a, **k):
        raise OSError("no such file")
    monkeypatch.setattr(prepare.subprocess, "run", boom)
    assert prepare._agent_github_identity_ok({}) is False


def test_prompt_forbids_hook_bypass_and_names_skip_tests():
    p = prepare._build_agent_prompt("v0.19.0", 3)
    assert "--no-verify" in p and "Never skip git hooks" in p
    assert 'SKIP_TESTS=1 SKIP_TESTS_REASON="<why>"' in p
    # The guard is an allowlist; the brief names the plain-git shape it allows.
    assert "plain `git` command" in p and "no wrapper such as `env`" in p

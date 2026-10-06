"""Tests for ``render_daemon_plist.py`` and the installers that use it.

Every secret-shaped value here is a synthetic canary. The properties proved:

* rendering with canary secrets in the template, in the process environment and
  in a would-be operator environment produces a plist that contains none of
  them, and none of the credential-named keys;
* a template that cannot be rendered safely is refused and writes nothing;
* every tracked template renders cleanly, so a secret committed into one fails
  this suite;
* no installer copies a plist into place without going through the renderer.
"""

from __future__ import annotations

import os
import plistlib
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS.parent.parent
sys.path.insert(0, str(_SCRIPTS))

import render_daemon_plist as rdp  # noqa: E402

CANARIES = {
    "EXAMPLE_SERVICE_TOKEN": "canary-value-service",
    "EXAMPLE_WEBHOOK_SECRET": "canary-value-webhook",
    "EXAMPLE_AGENT_PAT": "canary-value-agent",
    "EXAMPLE_WALLET_MNEMONIC": "canary value wallet words",
    "EXAMPLE_APPROVAL_SECRET": "canary-value-approval",
    "EXAMPLE_OAUTH_TOKEN": "canary-value-oauth",
}

TEMPLATE_WITH_CANARIES = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string><HOME>/venv/bin/python3</string>
        <string><HOME>/app/daemon.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string><HOME></string>
        <key>EXAMPLE_BASE_URL</key>
        <string>https://neotoma.example.test</string>
        <key>ATELES_PRIVATE_KEYS_DIR</key>
        <string><HOME>/private/keys</string>
{secret_entries}
    </dict>
</dict>
</plist>
"""


def _template(extra_env: dict[str, str]) -> str:
    entries = "\n".join(
        f"        <key>{name}</key>\n        <string>{value}</string>"
        for name, value in extra_env.items()
    )
    return TEMPLATE_WITH_CANARIES.replace("{secret_entries}", entries)


def _assert_no_canary(content: bytes) -> None:
    text = content.decode("utf-8")
    for name, value in CANARIES.items():
        assert value not in text, name
        assert f"<key>{name}</key>" not in text, name


def test_canary_values_in_the_template_never_reach_the_output():
    result = rdp.render(_template(CANARIES), home="/Users/example")
    _assert_no_canary(result.content)
    assert set(result.dropped_credential_keys) == set(CANARIES)
    parsed = plistlib.loads(result.content)
    assert parsed["EnvironmentVariables"]["HOME"] == "/Users/example"
    assert parsed["EnvironmentVariables"]["EXAMPLE_BASE_URL"] == (
        "https://neotoma.example.test"
    )
    assert parsed["EnvironmentVariables"]["ATELES_PRIVATE_KEYS_DIR"] == (
        "/Users/example/private/keys"
    )
    assert parsed["ProgramArguments"][0] == "/Users/example/venv/bin/python3"


def test_placeholder_values_for_credentials_are_dropped_too():
    template = _template({name: f"__{name}__" for name in CANARIES})
    result = rdp.render(template, home="/Users/example")
    assert set(result.dropped_credential_keys) == set(CANARIES)
    text = result.content.decode("utf-8")
    for name in CANARIES:
        assert name not in text


def test_ambient_environment_cannot_reach_the_output(monkeypatch):
    for name, value in CANARIES.items():
        monkeypatch.setenv(name, value)
    result = rdp.render(_template({}), home="/Users/example")
    _assert_no_canary(result.content)
    assert result.dropped_credential_keys == ()


def test_cli_with_canaries_in_template_and_environment_writes_a_clean_file(
    tmp_path, monkeypatch
):
    template = tmp_path / "daemon.plist.tmpl"
    template.write_text(_template(CANARIES), encoding="utf-8")
    out = tmp_path / "installed" / "com.example.daemon.plist"
    child_env = {**os.environ, **CANARIES}
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS / "render_daemon_plist.py"),
            str(template),
            str(out),
            "--home",
            "/Users/example",
        ],
        capture_output=True,
        text=True,
        env=child_env,
    )
    assert result.returncode == 0, result.stderr
    _assert_no_canary(out.read_bytes())
    # Names are reported, values never are.
    for value in CANARIES.values():
        assert value not in result.stdout + result.stderr
    assert "EXAMPLE_SERVICE_TOKEN" in result.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_stdout_mode_emits_the_clean_plist(tmp_path):
    template = tmp_path / "daemon.plist.tmpl"
    template.write_text(_template(CANARIES), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS / "render_daemon_plist.py"),
            str(template),
            "-",
            "--home",
            "/h",
        ],
        capture_output=True,
    )
    assert result.returncode == 0
    _assert_no_canary(result.stdout)


@pytest.mark.parametrize(
    "name,value",
    [
        ("SERVICE_ENDPOINT_ID", "AbCdEf0123456789AbCdEf0123456789"),
        ("SOME_SETTING", "eyJhbGciOiJIUzI1NiJ9.payload.signature"),
        ("WEBHOOK_TARGET", "0123456789abcdef0123456789abcdef0123"),
    ],
)
def test_opaque_value_under_an_innocent_name_is_refused(tmp_path, name, value):
    template = tmp_path / "t.plist"
    template.write_text(_template({name: value}), encoding="utf-8")
    out = tmp_path / "out.plist"
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS / "render_daemon_plist.py"),
            str(template),
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert not out.exists()
    assert value not in result.stdout + result.stderr
    assert name in result.stderr


@pytest.mark.parametrize(
    "value",
    [
        "https://neotoma.example.test",
        "/Users/example/.local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "Europe/Madrid",
        "claude,codex,cursor",
        "service.example.test",
        "operator.name.longaddress@example.test",
        "__SOME_PLACEHOLDER_VALUE_HERE__",
        "/var/tmp/ateles/checkpoint-denials",
    ],
)
def test_ordinary_values_are_not_mistaken_for_secrets(value):
    result = rdp.render(_template({"SOME_SETTING": value}), home="/h")
    assert (
        plistlib.loads(result.content)["EnvironmentVariables"]["SOME_SETTING"] == value
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["/bin/daemon", "--bearer-token", "canary-flag-value-0123456789"],
        ["/bin/daemon", "--api-key=canary-flag-value-0123456789"],
        ["/bin/daemon", "--client-secret", "canary-flag-value-0123456789"],
    ],
)
def test_credential_flag_with_a_value_in_program_arguments_is_refused(arguments):
    items = "\n".join(f"        <string>{a}</string>" for a in arguments)
    template = (
        '<?xml version="1.0"?><plist version="1.0"><dict>'
        "<key>Label</key><string>x</string>"
        f"<key>ProgramArguments</key><array>{items}</array>"
        "</dict></plist>"
    )
    with pytest.raises(rdp.PlistRenderError) as excinfo:
        rdp.render(template, home="/h")
    assert "canary-flag-value" not in str(excinfo.value)


def test_credential_flag_without_a_value_is_allowed():
    template = (
        '<?xml version="1.0"?><plist version="1.0"><dict>'
        "<key>Label</key><string>x</string>"
        "<key>ProgramArguments</key><array><string>/bin/daemon</string>"
        "<string>--token-from-store</string><string>--verbose</string></array>"
        "</dict></plist>"
    )
    rdp.render(template, home="/h")


def test_invalid_template_is_refused_and_writes_nothing(tmp_path):
    template = tmp_path / "bad.plist"
    template.write_text("<plist><dict><key>Label</key>", encoding="utf-8")
    out = tmp_path / "out.plist"
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS / "render_daemon_plist.py"),
            str(template),
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert not out.exists()
    assert list(tmp_path.glob(".out.plist.*")) == []


def test_home_with_xml_metacharacters_is_escaped():
    result = rdp.render(_template({}), home="/Users/a&b<c>")
    parsed = plistlib.loads(result.content)
    assert parsed["EnvironmentVariables"]["HOME"] == "/Users/a&b<c>"


def test_existing_output_is_replaced_not_appended(tmp_path):
    template = tmp_path / "t.plist"
    template.write_text(_template({}), encoding="utf-8")
    out = tmp_path / "out.plist"
    out.write_text("previous content holding " + CANARIES["EXAMPLE_SERVICE_TOKEN"])
    rdp.render_file(template, out, home="/h")
    assert CANARIES["EXAMPLE_SERVICE_TOKEN"] not in out.read_text(encoding="utf-8")


# ── Everything tracked in the repo renders clean --------------------------------


def _tracked_plist_sources() -> list[Path]:
    listing = subprocess.run(
        [
            "git",
            "-C",
            str(_REPO_ROOT),
            "ls-files",
            "*.plist",
            "*.plist.tmpl",
            "*.plist.template",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [_REPO_ROOT / path for path in listing]


@pytest.mark.parametrize(
    "path", _tracked_plist_sources(), ids=lambda p: str(p.relative_to(_REPO_ROOT))
)
def test_every_tracked_plist_source_renders_without_credentials(path):
    text = path.read_text(encoding="utf-8")
    # The autodeploy template uses its own {{HOME}} marker.
    text = text.replace("{{HOME}}", rdp.HOME_PLACEHOLDER)
    result = rdp.render(text, home="/Users/example")
    assert result.dropped_credential_keys == (), (
        f"{path.name} carries credential-named EnvironmentVariables"
    )


# ── Installers cannot bypass the renderer ----------------------------------------

_INSTALL_SCRIPTS = sorted((_REPO_ROOT / "execution" / "daemons").glob("*/install.sh"))
_PLIST_COPY_RE = re.compile(
    r"^\s*cp\b[^\n]*\.plist|^\s*cp\b[^\n]*\$PLIST", re.MULTILINE
)


def test_installers_were_found():
    assert len(_INSTALL_SCRIPTS) >= 8


@pytest.mark.parametrize("script", _INSTALL_SCRIPTS, ids=lambda p: p.parent.name)
def test_no_installer_copies_a_plist_without_rendering_it(script):
    text = script.read_text(encoding="utf-8")
    copies = _PLIST_COPY_RE.findall(text)
    assert copies == [], (
        f"{script.parent.name}/install.sh copies a plist directly; route it "
        "through execution/scripts/render_daemon_plist.py"
    )


def _symlinks_a_tracked_plist(script: Path) -> bool:
    """An installer that links the tracked plist instead of copying it.

    The link target is the repo file itself, which the tracked-plist test above
    renders and holds to the same rules, so there is no separate content for the
    installer to leak. The cutover tooling depends on the installed plist being
    a link into the checkout, so such an installer is not rewritten to copy.
    """
    text = script.read_text(encoding="utf-8")
    return bool(
        re.search(r'^\s*ln\s+-sf?\s+"\$SCRIPT_DIR/\$PLIST"', text, re.MULTILINE)
    )


@pytest.mark.parametrize(
    "script",
    [
        s
        for s in _INSTALL_SCRIPTS
        if "LaunchAgents" in s.read_text(encoding="utf-8")
        and not _symlinks_a_tracked_plist(s)
    ],
    ids=lambda p: p.parent.name,
)
def test_installers_that_write_launch_agents_call_the_renderer(script):
    assert "render_daemon_plist.py" in script.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "script",
    [s for s in _INSTALL_SCRIPTS if _symlinks_a_tracked_plist(s)],
    ids=lambda p: p.parent.name,
)
def test_symlinking_installers_point_at_a_tracked_plist(script):
    text = script.read_text(encoding="utf-8")
    plist_name = re.search(r'^PLIST="([^"]+)"', text, re.MULTILINE).group(1)
    tracked = {p.name for p in _tracked_plist_sources()}
    assert plist_name in tracked, (
        f"{script.parent.name} links {plist_name}, which is not a tracked plist, "
        "so nothing holds its content to the no-credentials rule"
    )

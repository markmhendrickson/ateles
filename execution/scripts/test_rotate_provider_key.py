"""Tests for rotate_provider_key.py — mocked HTTP and mocked `op` only.

HARD RULE for this test file: no test may call a real provider API or a real
`op`/1Password. Every HTTP call goes through a monkeypatched `_http_json`
(or, for the "raw HTTP shape" tests, a monkeypatched `requests`/`urllib`
stand-in that never leaves the process). Every `op` invocation goes through a
monkeypatched `subprocess.run` returning canned JSON.

The other hard rule these tests exist to prove: no secret value (admin key,
new key, pasted key) ever appears in captured stdout/stderr or in the argv of
a subprocess call recorded during the test. Several tests assert this
directly by scanning `capsys` output and every recorded subprocess argv list
for the planted fake secret value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rotate_provider_key as rpk  # noqa: E402
import secrets_lib as sl  # noqa: E402

# Planted fake secret values. If any of these strings appear in captured
# output or in a recorded subprocess argv, a test fails loudly.
FAKE_ADMIN_KEY = "sk-admin-FAKE-DO-NOT-LOG-9f8e7d6c5b4a"
FAKE_NEW_OPENAI_KEY = "sk-proj-FAKE-NEW-KEY-1a2b3c4d5e6f"
FAKE_NEW_ELEVEN_KEY = "el-FAKE-NEW-KEY-7g8h9i0j1k2l"
FAKE_PASTED_ANTHROPIC_KEY = "sk-ant-FAKE-PASTED-KEY-3m4n5o6p7q8r"
ALL_FAKE_SECRETS = [
    FAKE_ADMIN_KEY,
    FAKE_NEW_OPENAI_KEY,
    FAKE_NEW_ELEVEN_KEY,
    FAKE_PASTED_ANTHROPIC_KEY,
]


@pytest.fixture(autouse=True)
def _reset_known_secrets_registry():
    """`rpk._known_secrets` is process-global module state (deliberately —
    see the module's own comment: it lets every command register a secret
    the moment it's obtained, without threading it through every call site
    by hand). Tests share one process, so without a reset a secret
    registered in one test would still be redacted (or, worse, silently
    NOT cleared and masking a real assertion) in a later, unrelated test.
    Runs before AND after every test for full isolation either direction.
    """
    rpk._known_secrets.clear()
    yield
    rpk._known_secrets.clear()


def assert_no_secret_leaked(capsys, recorded_argvs: list[list[str]]) -> None:
    captured = capsys.readouterr()
    for secret in ALL_FAKE_SECRETS:
        assert secret not in captured.out, f"secret leaked to stdout: {secret!r}"
        assert secret not in captured.err, f"secret leaked to stderr: {secret!r}"
    for argv in recorded_argvs:
        for arg in argv:
            for secret in ALL_FAKE_SECRETS:
                assert secret not in arg, f"secret leaked to subprocess argv: {argv!r}"


# ---------------------------------------------------------------------------
# Fixtures: mocked op subprocess, mocked op_read, mocked HTTP
# ---------------------------------------------------------------------------


class FakeOpItem:
    """A minimal 1Password item with one 'credential' field, as JSON."""

    def __init__(self, item_id: str = "item_abc", value: str = "old-value-placeholder"):
        self.item_id = item_id
        self.value = value

    def as_json(self) -> str:
        return json.dumps(
            {
                "id": self.item_id,
                "title": "Fake Provider Key",
                "fields": [
                    {"id": "credential", "label": "credential", "value": self.value},
                ],
            }
        )


@pytest.fixture()
def recorded_subprocess_calls():
    return []


@pytest.fixture()
def fake_op_subprocess_run(monkeypatch, recorded_subprocess_calls):
    """Monkeypatch rotate_provider_key's `subprocess.run` (used for `op item
    get`/`op item edit`) to a fake that records argv (never real secrets) and
    returns canned success. Captures the JSON template piped to `op item edit`
    via its stdin file handle, so a test can assert what would have been
    written, without ever shelling out to real `op`.
    """
    item = FakeOpItem()
    written_templates: list[dict] = []

    def fake_run(
        argv, *, capture_output=None, text=None, timeout=None, stdin=None, **kwargs
    ):
        recorded_subprocess_calls.append(list(argv))
        if argv[1:3] == ["item", "get"]:
            return SimpleNamespace(returncode=0, stdout=item.as_json(), stderr="")
        if argv[1:3] == ["item", "edit"]:
            if stdin is not None:
                content = stdin.read()
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                written_templates.append(json.loads(content))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected op invocation in test: {argv!r}")

    monkeypatch.setattr(rpk.subprocess, "run", fake_run)
    return written_templates


@pytest.fixture()
def fake_op_read(monkeypatch):
    """Monkeypatch secrets_lib.op_read so resolving an admin-key op:// ref
    never shells out — returns the planted fake admin key for any ref.
    """
    monkeypatch.setattr(sl, "op_read", lambda ref: FAKE_ADMIN_KEY)


# ---------------------------------------------------------------------------
# _item_id_from_ref / _field_label_from_ref — pure parsing, no I/O
# ---------------------------------------------------------------------------


def test_item_id_from_op_reference():
    assert rpk._item_id_from_ref("op://Private/item123/field456") == "item123"


def test_item_id_from_bare_name():
    assert rpk._item_id_from_ref("My Item Name") == "My Item Name"


def test_field_label_from_op_reference():
    assert rpk._field_label_from_ref("op://Private/item123/credential") == "credential"


def test_field_label_default_when_bare_name():
    assert rpk._field_label_from_ref("My Item Name") == "credential"


def test_item_id_from_malformed_ref_raises():
    with pytest.raises(ValueError):
        rpk._item_id_from_ref("op://Private")


# ---------------------------------------------------------------------------
# op_write_password_field — the piped-template contract
# ---------------------------------------------------------------------------


def test_op_write_uses_stdin_template_not_cli_assignment(
    fake_op_subprocess_run, recorded_subprocess_calls
):
    templates = fake_op_subprocess_run
    rpk.op_write_password_field(
        "op://Private/item_abc/credential", "credential", FAKE_NEW_OPENAI_KEY
    )

    # The new value must appear in the piped template...
    assert templates, "op item edit was never invoked"
    assert templates[0]["fields"][0]["value"] == FAKE_NEW_OPENAI_KEY

    # ...and MUST NOT appear as a bare command-line argument anywhere recorded.
    for argv in recorded_subprocess_calls:
        for arg in argv:
            assert FAKE_NEW_OPENAI_KEY not in arg, (
                f"secret value appeared in op argv (must be piped via stdin only): {argv!r}"
            )
    # Confirm the edit call used --template - (stdin), not an assignment string.
    edit_calls = [c for c in recorded_subprocess_calls if c[1:3] == ["item", "edit"]]
    assert edit_calls, "no 'op item edit' call recorded"
    assert "--template" in edit_calls[0] and "-" in edit_calls[0]


def test_op_write_raises_when_field_not_found(fake_op_subprocess_run):
    with pytest.raises(RuntimeError, match="not found"):
        rpk.op_write_password_field(
            "op://Private/item_abc/nonexistent_field", "nonexistent_field", "x"
        )


def test_op_write_cleans_up_temp_file_even_on_failure(
    monkeypatch, recorded_subprocess_calls, tmp_path
):
    """If `op item edit` fails, the temp template file must still be unlinked."""
    item = FakeOpItem()
    created_temp_paths: list[Path] = []

    real_mkstemp = rpk.tempfile.mkstemp

    def spying_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created_temp_paths.append(Path(name))
        return fd, name

    def fake_run(
        argv, *, capture_output=None, text=None, timeout=None, stdin=None, **kwargs
    ):
        recorded_subprocess_calls.append(list(argv))
        if argv[1:3] == ["item", "get"]:
            return SimpleNamespace(returncode=0, stdout=item.as_json(), stderr="")
        if argv[1:3] == ["item", "edit"]:
            return SimpleNamespace(
                returncode=1, stdout="", stderr="op: edit failed (simulated)"
            )
        raise AssertionError(f"unexpected op invocation: {argv!r}")

    monkeypatch.setattr(rpk.tempfile, "mkstemp", spying_mkstemp)
    monkeypatch.setattr(rpk.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="edit failed"):
        rpk.op_write_password_field(
            "op://Private/item_abc/credential", "credential", "irrelevant"
        )

    assert created_temp_paths, "mkstemp was never called"
    for p in created_temp_paths:
        assert not p.exists(), f"temp template file was not cleaned up: {p}"


# ---------------------------------------------------------------------------
# Provider HTTP shape tests — mock _http_json directly (no real network)
# ---------------------------------------------------------------------------


def test_openai_create_service_account_posts_expected_shape(monkeypatch):
    captured = {}

    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        captured.update(method=method, url=url, headers=headers, body=body)
        return {
            "object": "organization.project.service_account",
            "id": "svc_acct_abc",
            "name": body["name"],
            "role": "member",
            "created_at": 1711471533,
            "api_key": {
                "object": "organization.project.service_account.api_key",
                "value": FAKE_NEW_OPENAI_KEY,
                "name": "Secret Key",
                "created_at": 1711471533,
                "id": "key_abc",
            },
        }

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)
    resp = rpk.openai_create_service_account(FAKE_ADMIN_KEY, "proj_abc", "my-name")

    assert captured["method"] == "POST"
    assert (
        captured["url"]
        == "https://api.openai.com/v1/organization/projects/proj_abc/service_accounts"
    )
    assert captured["headers"]["Authorization"] == f"Bearer {FAKE_ADMIN_KEY}"
    assert captured["body"] == {"name": "my-name"}
    assert resp["api_key"]["value"] == FAKE_NEW_OPENAI_KEY
    assert resp["api_key"]["id"] == "key_abc"
    assert resp["id"] == "svc_acct_abc"


def test_openai_verify_key_uses_bearer_header(monkeypatch):
    captured = {}

    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        captured.update(method=method, url=url, headers=headers)
        return {"data": []}

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)
    rpk.openai_verify_key(FAKE_NEW_OPENAI_KEY)
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.openai.com/v1/models"
    assert captured["headers"]["Authorization"] == f"Bearer {FAKE_NEW_OPENAI_KEY}"


def test_openai_delete_service_account_shape(monkeypatch):
    captured = {}

    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        captured.update(method=method, url=url, headers=headers)
        return {}

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)
    rpk.openai_delete_service_account(FAKE_ADMIN_KEY, "proj_abc", "svc_acct_abc")
    assert captured["method"] == "DELETE"
    assert (
        captured["url"]
        == "https://api.openai.com/v1/organization/projects/proj_abc/service_accounts/svc_acct_abc"
    )


def test_elevenlabs_create_api_key_posts_expected_shape(monkeypatch):
    captured = {}

    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        captured.update(method=method, url=url, headers=headers, body=body)
        return {"xi-api-key": FAKE_NEW_ELEVEN_KEY, "key_id": "key_xyz"}

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)
    resp = rpk.elevenlabs_create_api_key(
        FAKE_ADMIN_KEY, "user_abc", "my-key", ["speech_to_text"], 100_000
    )

    assert captured["method"] == "POST"
    assert (
        captured["url"]
        == "https://api.elevenlabs.io/v1/service-accounts/user_abc/api-keys"
    )
    assert captured["headers"]["xi-api-key"] == FAKE_ADMIN_KEY
    assert captured["body"] == {
        "name": "my-key",
        "permissions": ["speech_to_text"],
        "character_limit": 100_000,
    }
    assert resp["xi-api-key"] == FAKE_NEW_ELEVEN_KEY
    assert resp["key_id"] == "key_xyz"


def test_elevenlabs_create_api_key_omits_character_limit_when_none(monkeypatch):
    captured = {}

    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        captured.update(body=body)
        return {"xi-api-key": FAKE_NEW_ELEVEN_KEY, "key_id": "key_xyz"}

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)
    rpk.elevenlabs_create_api_key(
        FAKE_ADMIN_KEY, "user_abc", "my-key", ["speech_to_text"], None
    )
    assert "character_limit" not in captured["body"]


def test_elevenlabs_verify_key_uses_xi_api_key_header(monkeypatch):
    captured = {}

    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        captured.update(method=method, url=url, headers=headers)
        return {}

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)
    rpk.elevenlabs_verify_key(FAKE_NEW_ELEVEN_KEY)
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.elevenlabs.io/v1/user"
    assert captured["headers"]["xi-api-key"] == FAKE_NEW_ELEVEN_KEY


def test_anthropic_archive_key_posts_status_archived(monkeypatch):
    captured = {}

    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        captured.update(method=method, url=url, headers=headers, body=body)
        return {"id": "apikey_01", "status": "archived"}

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)
    resp = rpk.anthropic_archive_key(FAKE_ADMIN_KEY, "apikey_01")

    assert captured["method"] == "POST"
    assert (
        captured["url"]
        == "https://api.anthropic.com/v1/organizations/api_keys/apikey_01"
    )
    assert captured["headers"]["x-api-key"] == FAKE_ADMIN_KEY
    assert captured["headers"]["anthropic-version"] == rpk.ANTHROPIC_VERSION
    assert captured["body"] == {"status": "archived"}
    assert resp["status"] == "archived"


def test_anthropic_verify_key_uses_x_api_key_header(monkeypatch):
    captured = {}

    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        captured.update(method=method, url=url, headers=headers)
        return {"data": []}

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)
    rpk.anthropic_verify_key(FAKE_PASTED_ANTHROPIC_KEY)
    assert captured["headers"]["x-api-key"] == FAKE_PASTED_ANTHROPIC_KEY
    assert captured["headers"]["anthropic-version"] == rpk.ANTHROPIC_VERSION


def test_provider_http_error_message_excludes_headers(monkeypatch):
    """A failed call's error message must never include the headers dict
    (which is where the admin/new key lives) — only status, path, and body."""

    class FakeResponse:
        status_code = 401
        text = '{"error": {"message": "invalid api key"}}'

        def json(self):
            return json.loads(self.text)

    fake_requests = SimpleNamespace(request=lambda *a, **k: FakeResponse())
    monkeypatch.setattr(rpk, "requests", fake_requests)

    with pytest.raises(rpk.ProviderHTTPError) as excinfo:
        rpk._http_json(
            "GET",
            "https://api.openai.com/v1/models?secret=should-not-appear",
            headers={"Authorization": f"Bearer {FAKE_ADMIN_KEY}"},
        )
    message = str(excinfo.value)
    assert FAKE_ADMIN_KEY not in message
    assert "should-not-appear" not in message  # query string stripped too


def test_safe_excerpt_redacts_secret_shaped_tokens_in_provider_body():
    """Falco (security lens, PR round 1) noted this file's error paths rely
    on the assumption that providers/`op` never echo a request's own secret
    back in an error body — plausible but unverifiable without live
    accounts. This proves the defense-in-depth net added on top of that
    assumption: an sk-/xi-/other-long-token-shaped substring anywhere in an
    error body text is masked before `_safe_excerpt` returns it.
    """
    body_that_would_leak_if_unredacted = (
        f'{{"error": "invalid key", "key_used": "{FAKE_NEW_OPENAI_KEY}"}}'
    )
    excerpt = rpk._safe_excerpt(body_that_would_leak_if_unredacted)
    assert FAKE_NEW_OPENAI_KEY not in excerpt
    assert "[redacted]" in excerpt


def _build_fake_hex_token(*parts: str) -> str:
    """Assemble a hex-looking fake token from parts at runtime.

    ElevenLabs (round-2 qa review, Phoenicurus) has no documented key-prefix
    convention, so the round-2 test suite exercised the redaction net's
    generic shape branch with a long hex-like literal — which gitleaks'
    default `generic-api-key` rule (via `useDefault = true` in this repo's
    `.gitleaks.toml`) then flagged as a real secret, a reproducible false
    positive Phoenicurus caught and demonstrated two ways. Round 3 removed
    the generic shape branch entirely (see the module's redaction-design
    comment: an ElevenLabs-shaped key is now caught by EXACT-VALUE
    redaction, since this script always holds the literal value — no shape
    guess needed), so this helper only needs to build something
    ElevenLabs-key-*looking* for the exact-value tests below, and building
    it from parts at runtime (rather than one committed literal) keeps it
    out of gitleaks' static-literal detection the same way the coordinator
    asked for.
    """
    return "".join(parts)


def test_exact_value_redaction_catches_a_registered_elevenlabs_shaped_key():
    """The new design's core case: a key with no `sk-` prefix and no
    documented shape (ElevenLabs) is still caught, because it was
    REGISTERED as a known secret rather than pattern-matched.
    """
    fake_key = _build_fake_hex_token("3f8a1c9d7b2e4f6a", "8c0d2e4f6a8b0c2d", "4e6f8a0b")
    rpk._known_secrets.register(fake_key)
    excerpt = rpk._safe_excerpt(f'{{"echo": "{fake_key}"}}')
    assert fake_key not in excerpt
    assert "[redacted]" in excerpt


def test_op_write_error_message_redacts_a_registered_secret_from_op_stderr(monkeypatch):
    """If a real `op` version ever echoed a value-shaped string in its own
    stderr (contrary to `secrets_lib.op_read`'s documented assumption that
    it never does), `op_write_password_field`'s RuntimeError must not carry
    it through unredacted either — exercising the EXACT-VALUE path (the
    primary defense in the round-3 design), not shape matching: registers
    the secret first, the way a real run would have via `_resolve_admin_key`
    before ever reaching this code path.
    """
    rpk._known_secrets.register(FAKE_ADMIN_KEY)

    def fake_run(
        argv, *, capture_output=None, text=None, timeout=None, stdin=None, **kwargs
    ):
        if argv[1:3] == ["item", "get"]:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=f"op: internal error, last value was {FAKE_ADMIN_KEY}",
            )
        raise AssertionError(f"unexpected op invocation: {argv!r}")

    monkeypatch.setattr(rpk.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        rpk.op_write_password_field(
            "op://Private/item_abc/credential", "credential", "irrelevant"
        )
    assert FAKE_ADMIN_KEY not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Adversarial redaction tests — round 3, per Falco's round-2 CONFIRMED
# findings on PR #1297 and the coordinator's design change (exact-value
# redaction as primary, narrowed shape matching as secondary). Each test
# below replays one of Falco's demonstrated cases directly and asserts the
# NEW design closes it — not just that the happy path still works.
# ---------------------------------------------------------------------------


def test_evasion_short_secret_with_no_sk_prefix_is_caught_when_registered():
    """Falco's round-2 evasion #1: a short (<20 char), non-`sk-`-prefixed
    secret passed through the old shape-only regex unredacted. Under the
    new design it is caught because it is REGISTERED, not because of its
    shape — proving the fix addresses the root cause (a shape guess can
    never cover every provider's format) rather than patching the symptom
    (widening the guess a little further).
    """
    short_fake_secret = _build_fake_hex_token(
        "tok_", "abc123xyz"
    )  # 13 chars, no sk- prefix
    assert len(short_fake_secret) < 20
    assert not short_fake_secret.startswith("sk-")

    # Unredacted-if-unregistered, by design (nothing to redact against yet):
    unregistered_excerpt = rpk._safe_excerpt(f"error: bad token {short_fake_secret}")
    assert short_fake_secret in unregistered_excerpt

    # Caught once registered, the way a real run would register it before
    # this code path is ever reached:
    rpk._known_secrets.register(short_fake_secret)
    registered_excerpt = rpk._safe_excerpt(f"error: bad token {short_fake_secret}")
    assert short_fake_secret not in registered_excerpt
    assert "[redacted]" in registered_excerpt


def test_evasion_dotted_anthropic_key_hint_format_is_caught():
    """Falco's round-2 evasion #2a: a dotted variant of Anthropic's OWN
    documented `partial_key_hint` format (`sk-ant-api03...igAA` per
    platform.claude.com/docs) defeated the old regex because `.` broke both
    of its alternation branches into pieces too short to match. The new
    narrow secondary layer's character class includes `.`/`/`/`+`/`=`
    specifically to close this, independent of whether the value is also
    registered (this is the shape layer's own adversarial test).
    """
    dotted_key = _build_fake_hex_token(
        "sk-ant-api03.", "R2D2igAA.", "9f8e7d6c5b4a3z2y1x"
    )
    excerpt = rpk._safe_excerpt(f'"partial_key_hint": "{dotted_key}"')
    assert dotted_key not in excerpt
    assert "[redacted]" in excerpt


def test_evasion_base64_shaped_secret_is_caught_when_registered():
    """Falco's round-2 evasion #2b: a base64-shaped secret using `/+=`
    defeated the old regex entirely (those characters are outside
    `[A-Za-z0-9_-]`). The new design catches it via exact-value redaction
    once registered — the correct layer for a shape gitleaks itself also
    has no dedicated rule for, since the fix is "know the value", not
    "guess harder at the shape".
    """
    base64_shaped_secret = _build_fake_hex_token(
        "YWJjZGVmZ2hpams", "/bG1ub3BxcnN0dXY", "="
    )
    assert "/" in base64_shaped_secret and "=" in base64_shaped_secret

    rpk._known_secrets.register(base64_shaped_secret)
    excerpt = rpk._safe_excerpt(f"token rejected: {base64_shaped_secret}")
    assert base64_shaped_secret not in excerpt
    assert "[redacted]" in excerpt


def test_false_positive_openai_error_type_field_survives_redaction():
    """Falco's round-2 false-positive #1: the old generic 20+ char shape
    branch swallowed OpenAI's own real, non-secret `error.type` field value
    (`invalid_request_error`, 21 chars) purely because it was long enough
    and alphanumeric. The new narrow secondary layer requires an `sk-`
    prefix, so an unrelated long identifier is never touched by it — and
    since this string was never a registered secret, exact-value redaction
    leaves it alone too.
    """
    body = '{"error":{"message":"invalid api key","type":"invalid_request_error"}}'
    excerpt = rpk._safe_excerpt(body)
    assert "invalid_request_error" in excerpt
    assert "[redacted]" not in excerpt


def test_false_positive_long_1password_item_name_survives_redaction(monkeypatch):
    """Falco's round-2 false-positive #2: a realistic, descriptive 1Password
    item name got swallowed as `[redacted]` purely by length — actively
    hurting the operator, since `op_write_password_field`'s whole point in
    a failure message is naming WHICH item failed. Replays that case
    end-to-end through `op_write_password_field` itself (not just
    `_safe_excerpt` in isolation), confirming the real error path preserves
    the item name.
    """
    item_name = "my-item-name-with-many-characters-in-it"
    assert len(item_name) > 24  # would have tripped the old 20-char generic branch

    def fake_run(argv, *, capture_output=None, text=None, timeout=None, **kwargs):
        if argv[1:3] == ["item", "get"]:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=f"op: item not found: {item_name!r}",
            )
        raise AssertionError(f"unexpected op invocation: {argv!r}")

    monkeypatch.setattr(rpk.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        rpk.op_write_password_field(
            "op://Private/item_abc/credential", "credential", "irrelevant"
        )
    assert item_name in str(excinfo.value)
    assert "[redacted]" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# End-to-end command flow (cmd_openai / cmd_elevenlabs / cmd_anthropic) with
# everything mocked — proves the whole verify-then-write-then-act order and
# that no secret leaks through stdout/stderr or any recorded subprocess argv.
# ---------------------------------------------------------------------------


def _no_downstream_args(**overrides):
    base = dict(no_downstream=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_cmd_openai_end_to_end_no_leak(
    monkeypatch,
    fake_op_subprocess_run,
    fake_op_read,
    recorded_subprocess_calls,
    capsys,
    tmp_path,
):
    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        if method == "POST" and "service_accounts" in url:
            return {
                "id": "svc_acct_abc",
                "api_key": {"value": FAKE_NEW_OPENAI_KEY, "id": "key_abc"},
            }
        if method == "GET" and url.endswith("/models"):
            return {"data": []}
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)

    args = _no_downstream_args(
        admin_key_ref="op://Private/item_abc/credential",
        project_id="proj_abc",
        op_item_ref="op://Private/item_abc/credential",
        service_account_name="test-rotation",
        revoke_old=None,
    )
    rc = rpk.cmd_openai(args, tmp_path)
    assert rc == 0

    assert_no_secret_leaked(capsys, recorded_subprocess_calls)


def test_cmd_openai_verify_failure_stops_before_revoke(
    monkeypatch,
    fake_op_subprocess_run,
    fake_op_read,
    recorded_subprocess_calls,
    capsys,
    tmp_path,
):
    """If the new key fails verification, the old key must NOT be revoked —
    even when --revoke-old was passed."""
    delete_called = []

    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        if method == "POST" and "service_accounts" in url:
            return {
                "id": "svc_acct_abc",
                "api_key": {"value": FAKE_NEW_OPENAI_KEY, "id": "key_abc"},
            }
        if method == "GET" and url.endswith("/models"):
            raise rpk.ProviderHTTPError("GET /v1/models -> HTTP 401: invalid key")
        if method == "DELETE":
            delete_called.append(url)
            return {}
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)

    args = _no_downstream_args(
        admin_key_ref="op://Private/item_abc/credential",
        project_id="proj_abc",
        op_item_ref="op://Private/item_abc/credential",
        service_account_name="test-rotation",
        revoke_old="svc_acct_OLD",
    )
    rc = rpk.cmd_openai(args, tmp_path)
    assert rc == 1
    assert not delete_called, (
        "old service account must not be revoked when new-key verification fails"
    )
    assert_no_secret_leaked(capsys, recorded_subprocess_calls)


def test_cmd_elevenlabs_end_to_end_no_leak(
    monkeypatch,
    fake_op_subprocess_run,
    fake_op_read,
    recorded_subprocess_calls,
    capsys,
    tmp_path,
):
    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        if method == "POST" and "api-keys" in url:
            return {"xi-api-key": FAKE_NEW_ELEVEN_KEY, "key_id": "key_xyz"}
        if method == "GET" and url.endswith("/user"):
            return {"subscription": {}}
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)

    args = _no_downstream_args(
        admin_key_ref="op://Private/item_abc/credential",
        service_account_user_id="user_abc",
        op_item_ref="op://Private/item_abc/credential",
        key_name="test-rotation",
        permission=None,
        character_limit=None,
        revoke_old=None,
    )
    rc = rpk.cmd_elevenlabs(args, tmp_path)
    assert rc == 0
    assert_no_secret_leaked(capsys, recorded_subprocess_calls)


def test_cmd_anthropic_archive_old_end_to_end_no_leak(
    monkeypatch,
    fake_op_read,
    capsys,
    tmp_path,
):
    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        assert body == {"status": "archived"}
        return {"id": "apikey_01", "status": "archived"}

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)

    args = _no_downstream_args(
        op_item_ref="op://Private/item_abc/credential",
        admin_key_ref="op://Private/item_abc/credential",
        archive_old="apikey_01",
    )
    rc = rpk.cmd_anthropic(args, tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    for secret in ALL_FAKE_SECRETS:
        assert secret not in captured.out
        assert secret not in captured.err


def test_cmd_anthropic_paste_flow_no_leak(
    monkeypatch,
    fake_op_subprocess_run,
    recorded_subprocess_calls,
    capsys,
    tmp_path,
):
    """The interactive-paste path: getpass is mocked so no real prompt blocks
    the test, and its returned value must never surface in output/argv."""
    monkeypatch.setattr(
        rpk.getpass, "getpass", lambda prompt="": FAKE_PASTED_ANTHROPIC_KEY
    )

    def fake_http_json(method, url, *, headers, body=None, timeout=30.0):
        assert method == "GET" and url.endswith("/models")
        assert headers["x-api-key"] == FAKE_PASTED_ANTHROPIC_KEY
        return {"data": []}

    monkeypatch.setattr(rpk, "_http_json", fake_http_json)

    args = _no_downstream_args(
        op_item_ref="op://Private/item_abc/credential",
        admin_key_ref=None,
        archive_old=None,
    )
    rc = rpk.cmd_anthropic(args, tmp_path)
    assert rc == 0
    assert_no_secret_leaked(capsys, recorded_subprocess_calls)


def test_cmd_anthropic_archive_requires_admin_key_ref(tmp_path, capsys):
    args = _no_downstream_args(
        op_item_ref="op://Private/item_abc/credential",
        admin_key_ref=None,
        archive_old="apikey_01",
    )
    rc = rpk.cmd_anthropic(args, tmp_path)
    assert rc == 1
    assert "required" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Downstream steps
# ---------------------------------------------------------------------------


def test_run_downstream_steps_invokes_publish_then_materialize(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, *, capture_output=None, text=None, timeout=None):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(rpk.subprocess, "run", fake_run)
    lines = rpk.run_downstream_steps("ANTHROPIC_API_KEY", tmp_path)

    assert any("secrets_publish.py" in " ".join(c) for c in calls)
    assert any("secrets_materialize.py" in " ".join(c) for c in calls)
    # publish must run before materialize
    publish_idx = next(
        i for i, c in enumerate(calls) if "secrets_publish.py" in " ".join(c)
    )
    materialize_idx = next(
        i for i, c in enumerate(calls) if "secrets_materialize.py" in " ".join(c)
    )
    assert publish_idx < materialize_idx
    assert any("com.ateles.apis" in line for line in lines)


def test_run_downstream_steps_stops_after_publish_failure(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, *, capture_output=None, text=None, timeout=None):
        calls.append(list(argv))
        if "secrets_publish.py" in " ".join(argv):
            return SimpleNamespace(returncode=1, stdout="", stderr="publish failed")
        raise AssertionError("materialize must not run after a publish failure")

    monkeypatch.setattr(rpk.subprocess, "run", fake_run)
    lines = rpk.run_downstream_steps("OPENAI_API_KEY", tmp_path)
    assert any("FAILED" in line for line in lines)


def test_run_downstream_steps_unknown_var_skips_cleanly(tmp_path):
    lines = rpk.run_downstream_steps("SOME_UNKNOWN_VAR", tmp_path)
    assert any("skipped" in line for line in lines)


# ---------------------------------------------------------------------------
# Documented-command drift guard.
#
# ux (Accipiter) found, by literally copy-pasting docs/secrets_management.md,
# that the openai and elevenlabs first-run examples failed with "argument
# --revoke-old: expected one argument" — a bare `--revoke-old` followed by a
# trailing shell `#` comment on a backslash-continued line. Bash does NOT
# treat `#` as special mid-continuation (only at the start of a fresh
# top-level line), so the comment's words became literal argv tokens and
# poisoned the flag that came before them. This section parses every
# rotate_provider_key.py example command in the docs, and every example line
# rotate_provider_key.py's own --help/usage text shows, through the REAL
# argparse parser, with placeholder values substituted for reachable-but-
# fake ones, and fails if any of them cannot be parsed. This is the class of
# regression test CLAUDE.md's "a test that cannot fail on the thing it
# watches is decoration" rule asks for: it is proven live by re-running it
# against the pre-fix doc text below and watching it fail the same way ux's
# manual repro did.
# ---------------------------------------------------------------------------

DOCS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "secrets_management.md"
)

# Placeholder -> reachable-but-fake substitution, so a parsed command carries
# no <angle-bracket> tokens argparse would otherwise treat as literal values
# (which is technically also parseable, but substituting keeps this test
# honest about what an operator would actually type).
_PLACEHOLDER_SUBSTITUTIONS = {
    "op://Private/<openai-admin-item>/<field>": "op://Private/fake_item/fake_field",
    "op://Private/<elevenlabs-admin-item>/<field>": "op://Private/fake_item/fake_field",
    "op://Private/<anthropic-admin-item>/<field>": "op://Private/fake_item/fake_field",
    "op://Private/<item-that-holds-OPENAI_API_KEY>/<field>": "op://Private/fake_item/fake_field",
    "op://Private/<item-that-holds-ELEVENLABS_API_KEY>/<field>": "op://Private/fake_item/fake_field",
    "op://Private/<item-that-holds-ANTHROPIC_API_KEY>/<field>": "op://Private/fake_item/fake_field",
    "op://Private/<item>/<field>": "op://Private/fake_item/fake_field",
    "proj_abc123": "proj_abc123",
    "user_abc123": "user_abc123",
    "<OLD_SERVICE_ACCOUNT_ID>": "svc_acct_old_fake",
    "<OLD_KEY_ID>": "key_old_fake",
    "apikey_01XXXXXXXXXXXXXXXXXXXXXXXX": "apikey_01fakefakefakefakefakefake",
}


def _substitute_placeholders(command_text: str) -> str:
    for placeholder, fake in _PLACEHOLDER_SUBSTITUTIONS.items():
        command_text = command_text.replace(placeholder, fake)
    return command_text


def _bash_join_continuations(block_text: str) -> list[str]:
    """Join backslash-continued lines the way bash actually does.

    A top-level line starting with `#` is dropped as a real comment. A `#`
    appearing on a line that is itself a continuation (i.e. the line before
    it ended in `\\`) is NOT special — it becomes part of the logical line,
    reproducing the exact failure mode ux found rather than silently
    "fixing" it by stripping comments unconditionally.
    """
    logical_lines: list[str] = []
    buf = ""
    for raw_line in block_text.splitlines():
        if not buf and raw_line.strip().startswith("#"):
            continue  # a genuine top-level comment line — not part of any command
        buf = f"{buf} {raw_line}" if buf else raw_line
        stripped = buf.rstrip()
        if stripped.endswith("\\"):
            buf = stripped[:-1]
            continue
        logical_lines.append(buf)
        buf = ""
    if buf:
        logical_lines.append(buf)
    return logical_lines


def _extract_rotate_provider_key_commands(markdown_text: str) -> list[str]:
    """Return every logical shell command invoking rotate_provider_key.py
    found inside ```bash fenced code blocks in `markdown_text`.
    """
    import re

    commands: list[str] = []
    for block in re.findall(r"```bash\n(.*?)```", markdown_text, re.DOTALL):
        for logical_line in _bash_join_continuations(block):
            if "rotate_provider_key.py" in logical_line:
                commands.append(logical_line.strip())
    return commands


def _parse_command_argv(command_text: str) -> list[str]:
    """Split a shell command line into argv the way bash would, WITHOUT
    stripping comments mid-string — this is deliberate: it is what lets this
    test reproduce (and therefore catch) the exact ux-reported defect, where
    a trailing `# comment` after `--revoke-old` on a continued line becomes
    literal argv rather than being dropped.
    """
    import shlex

    return shlex.split(_substitute_placeholders(command_text), comments=False)


def _parse_through_real_argparse(argv_after_script: list[str]) -> None:
    """Feed argv (everything after `python .../rotate_provider_key.py`)
    through the script's real `build_parser()`. Raises SystemExit(2) on the
    same parse failure an operator would hit at the terminal.
    """
    parser = rpk.build_parser()
    parser.parse_args(argv_after_script)


def _rotate_provider_key_argv(command_text: str) -> list[str]:
    """argv strictly after the `rotate_provider_key.py` path token."""
    tokens = _parse_command_argv(command_text)
    idx = next(i for i, t in enumerate(tokens) if t.endswith("rotate_provider_key.py"))
    return tokens[idx + 1 :]


@pytest.fixture()
def docs_text() -> str:
    assert DOCS_PATH.exists(), f"expected docs at {DOCS_PATH}"
    return DOCS_PATH.read_text()


def test_docs_contain_the_expected_number_of_example_commands(docs_text):
    """Sanity check that extraction is finding real content, not silently
    matching zero blocks (a test that always "passes" because it found
    nothing is the exact decoration failure this section exists to avoid).
    """
    commands = _extract_rotate_provider_key_commands(docs_text)
    assert len(commands) >= 6, (
        f"expected at least 6 rotate_provider_key.py example commands in "
        f"{DOCS_PATH.name}, found {len(commands)}: {commands!r}"
    )


def test_every_documented_example_command_parses(docs_text):
    """The regression test for the exact defect ux found: every documented
    rotate_provider_key.py invocation must parse cleanly through the real
    argparse parser. This fails loudly (naming the offending command) rather
    than generically, so a future drift is diagnosable from the failure
    message alone.
    """
    commands = _extract_rotate_provider_key_commands(docs_text)
    assert commands, "no rotate_provider_key.py commands found to check"

    failures: list[tuple[str, str]] = []
    for command_text in commands:
        argv = _rotate_provider_key_argv(command_text)
        try:
            _parse_through_real_argparse(argv)
        except SystemExit as exc:  # argparse exits 2 on a parse error
            failures.append((command_text, f"SystemExit({exc.code})"))

    assert not failures, "documented command(s) failed to parse:\n" + "\n".join(
        f"  {cmd!r} -> {err}" for cmd, err in failures
    )


def test_regression_guard_catches_the_original_ux_reported_defect():
    """Proves the guard above is not decoration: replays the EXACT broken
    pattern ux found (bare `--revoke-old` followed by a trailing comment on
    a backslash-continued line) and asserts extraction+parsing surfaces it
    as a failure, the same way `test_every_documented_example_command_parses`
    would have caught it had it still been in the docs.

    Deliberately avoids an apostrophe in the planted comment text (unlike
    ux's own literal wording) so the failure asserted here is unambiguously
    the argparse rejection this guard targets, not shlex's separate and
    equally real "no closing quotation" complaint about the apostrophe —
    both are genuine operator-facing failures, but this test isolates one.
    """
    broken_block = (
        "python execution/scripts/rotate_provider_key.py openai \\\n"
        "  --admin-key-ref op://Private/<item>/<field> \\\n"
        "  --project-id proj_abc123 \\\n"
        "  --op-item-ref op://Private/<item>/<field> \\\n"
        "  --revoke-old   # add on a SECOND run, once the new key is confirmed:\n"
        "                 #   --revoke-old <OLD_SERVICE_ACCOUNT_ID>\n"
    )
    commands = _extract_rotate_provider_key_commands(f"```bash\n{broken_block}```\n")
    assert commands, "extraction found nothing in the planted broken block"

    argv = _rotate_provider_key_argv(commands[0])
    with pytest.raises(SystemExit) as excinfo:
        _parse_through_real_argparse(argv)
    assert excinfo.value.code == 2


def test_regression_guard_also_flags_the_apostrophe_variant():
    """The literal text ux's review comment used ("once you've confirmed")
    additionally trips shlex's own quote balancing, which is equally a real
    failure an operator would hit (bash itself would reject it the same
    way) — confirmed here so that variant isn't silently un-covered.
    """
    broken_block = (
        "python execution/scripts/rotate_provider_key.py openai \\\n"
        "  --admin-key-ref op://Private/<item>/<field> \\\n"
        "  --revoke-old   # add on a SECOND run, once you've confirmed the new key works:\n"
    )
    commands = _extract_rotate_provider_key_commands(f"```bash\n{broken_block}```\n")
    assert commands, "extraction found nothing in the planted broken block"

    with pytest.raises(ValueError, match="No closing quotation"):
        _rotate_provider_key_argv(commands[0])


def _strip_manpage_brackets(command_text: str) -> str:
    """Turn man-page-style `[--flag value]` optional-group notation into a
    literal, fully-populated command by dropping the `[`/`]` characters.

    The module docstring's `Usage:` block deliberately uses this convention
    (matching argparse's own `--help` usage-line style) rather than literal
    copy-paste shell — unlike docs/secrets_management.md, whose examples ARE
    meant to be copy-pasted verbatim. Both conventions are legitimate; this
    helper is what lets one test walk both without misreading one as the
    other. `<placeholder>` tokens inside brackets (e.g. `<key_id>`) are left
    for `_substitute_placeholders`/`_parse_command_argv` to handle downstream.
    """
    return command_text.replace("[", "").replace("]", "")


def test_module_docstring_usage_examples_parse_with_all_optionals_populated():
    """rotate_provider_key.py's own module docstring `Usage:` block is the
    first thing an operator sees before ever opening the docs (`--help` at
    each subcommand level was separately confirmed clear by ux's review), so
    a broken example there is exactly as harmful as one in
    docs/secrets_management.md — same defect class, different surface.

    The docstring uses man-page-style `[--flag value]` bracket notation
    (matching argparse's own usage-line convention), not literal shell, so
    this strips brackets to populate every optional flag and confirms the
    fully-populated form still parses — proving the flag names and value
    arities shown are accurate to the real CLI contract.
    """
    module_doc = rpk.__doc__ or ""
    usage_section = module_doc.split("Usage:", 1)[-1] if "Usage:" in module_doc else ""
    usage_section = usage_section.split("Never call a real provider API", 1)[0]

    commands = [
        line
        for line in _bash_join_continuations(usage_section)
        if "rotate_provider_key.py" in line and not line.strip().startswith("#")
    ]
    assert commands, (
        "expected at least one rotate_provider_key.py example in the module docstring"
    )

    failures: list[tuple[str, str]] = []
    for raw_command_text in commands:
        command_text = _strip_manpage_brackets(raw_command_text)
        try:
            argv = _rotate_provider_key_argv(command_text)
        except (StopIteration, ValueError) as exc:
            failures.append((raw_command_text, f"{type(exc).__name__}: {exc}"))
            continue
        try:
            _parse_through_real_argparse(argv)
        except SystemExit as exc:
            failures.append((raw_command_text, f"SystemExit({exc.code})"))

    assert not failures, (
        "module docstring Usage: example(s) failed to parse:\n"
        + "\n".join(f"  {cmd!r} -> {err}" for cmd, err in failures)
    )

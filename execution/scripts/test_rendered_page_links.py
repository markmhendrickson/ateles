"""Recipient-effect and failure-boundary tests; every endpoint/identity is synthetic."""

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from execution.lib import rendered_page_links as links

CONFIG = "ent_" + "1" * 24
PAGE_A = "ent_" + "a" * 24
PAGE_B = "ent_" + "b" * 24
OLD_PAGE = "ent_" + "c" * 24


def binding():
    return {
        "version": 1,
        "publisher_host": socket.gethostname(),
        "repository": "example/site",
        "branch": "main",
        "workflow": "deploy.yml",
        "secret_name": "PAGE_LINKS_JSON",
        "secret_updated_at": "initial",
        "expected_github_login": "publisher",
        "expected_user_id": "graph-owner",
        "github_host": "github.example",
        "template_path": "scripts/short-link-template.json",
        "site_origin": "https://site.example",
        "target_origin": "https://records.example",
        "registry": {"existing": OLD_PAGE},
    }


class Store:
    def __init__(self):
        # Mirrors the actual REST shape: owner is not exposed on entity reads.
        self.record = {
            "entity_id": CONFIG,
            "entity_type": "deployment_configuration",
            "entity_version": "v0",
            "snapshot": {"deploy_branch": "main", "short_links": binding()},
        }
        self.pages = {
            PAGE_A: {"title": "Shared brief"},
            PAGE_B: {"title": "Shared brief"},
        }
        self.published = []
        self.writes = []
        self.identity = "graph-owner"

    def read(self, entity_id):
        if entity_id == CONFIG:
            return copy.deepcopy(self.record)
        return {
            "entity_id": entity_id,
            "entity_type": "rendered_page",
            "snapshot": self.pages[entity_id],
        }

    def check_identity(self, expected):
        if expected != self.identity:
            raise links.PublicationError("neotoma_identity_mismatch")

    def publish(self, fields):
        self.published.append(fields)
        entity = fields.get("entity_id", PAGE_A)
        token = "synthetic+/=&token"
        from urllib.parse import urlencode

        return {
            "entity_id": entity,
            "access_token": token,
            "share_url": f"https://records.example/entities/{entity}/html?{urlencode({'access_token': token})}",
        }

    def update_binding(self, entity_id, record, binding):
        assert record["entity_version"] == self.record["entity_version"], (
            "stale CAS must fail"
        )
        self.writes.append(copy.deepcopy(binding))
        self.record["snapshot"]["short_links"] = copy.deepcopy(binding)
        self.record["entity_version"] = f"v{len(self.writes)}"
        return self.read(entity_id)


class GitHub:
    def __init__(self):
        self.payloads = []
        self.dispatches = []
        self.metadata = "initial"
        self.fail_wait = False
        self.identity_ok = True

    def check_identity(self, binding):
        if not self.identity_ok:
            raise links.PublicationError("github_identity_mismatch")

    def write_secret(self, binding, payload):
        if binding["secret_updated_at"] != self.metadata:
            raise links.PublicationError("secret_changed_outside_publisher")
        time.sleep(0.01)
        self.payloads.append(json.loads(payload))
        self.metadata = f"write-{len(self.payloads)}"
        return self.metadata

    def source_sha(self, binding):
        return "f" * 40

    def template(self, binding, sha):
        return {}

    def dispatch(self, binding, request_id):
        self.dispatches.append(request_id)

    def wait(self, binding, request_id, **_):
        assert request_id in self.dispatches
        if self.fail_wait:
            raise links.PublicationError("alias_deployment_pending")
        return {"databaseId": 123, "conclusion": "success"}


class Reader:
    def __init__(self):
        self.urls = []
        self.verified = []
        self.fail = False

    def get(self, url):
        self.urls.append(url)
        return "actual page content"

    def verify_aliases(self, binding, template):
        if self.fail:
            raise links.PublicationError("served_alias_destination_mismatch")
        self.verified.append(copy.deepcopy(binding["registry"]))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://records.example")
    store, github, reader = Store(), GitHub(), Reader()
    publisher = links.Publisher(store, github, reader, lock_root=tmp_path)
    return publisher, store, github, reader


def test_publish_creates_verified_short_link_reuses_it_and_preserves_prior_aliases(
    setup,
):
    publisher, store, github, reader = setup
    first = publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert first["alias_status"] == "verified"
    assert urlsplit(first["share_url"]).path == "/p/shared-brief/"
    assert parse_qs(urlsplit(first["share_url"]).query) == {"t": ["synthetic+/=&token"]}
    assert github.payloads == [
        {
            "origin": "https://records.example",
            "links": {"existing": OLD_PAGE, "shared-brief": PAGE_A},
        }
    ]
    assert reader.verified[-1] == github.payloads[-1]["links"]
    assert "pending" not in store.record["snapshot"]["short_links"]
    store.pages[PAGE_A]["title"] = "A changed title"
    second = publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert second["share_url"] == first["share_url"]
    assert len(github.dispatches) == 1
    serialized = json.dumps(store.writes) + json.dumps(github.payloads)
    assert "synthetic+/=&token" not in serialized and "access_token" not in serialized


def test_concurrent_publications_cannot_drop_an_alias_or_overwrite_same_title(setup):
    publisher, store, github, _ = setup
    outputs = []
    errors = []

    def run(page):
        try:
            outputs.append(
                publisher.publish(CONFIG, {"audience": "guest", "entity_id": page})
            )
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=run, args=(page,)) for page in (PAGE_A, PAGE_B)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert errors == []
    assert all(out["alias_status"] == "verified" for out in outputs)
    final = github.payloads[-1]["links"]
    assert set(final.values()) == {OLD_PAGE, PAGE_A, PAGE_B}
    assert len(github.dispatches) == 2


def test_deployment_failure_returns_verified_original_and_resume_never_redispatches(
    setup,
):
    publisher, store, github, _ = setup
    github.fail_wait = True
    result = publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert result["alias_status"] == "fallback"
    assert urlsplit(result["share_url"]).hostname == "records.example"
    assert result["outstanding_work"]
    assert (
        store.record["snapshot"]["short_links"]["pending"]["phase"]
        == "dispatch_requested"
    )
    assert len(github.dispatches) == 1
    github.fail_wait = False
    resumed = publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert resumed["alias_status"] == "verified"
    assert len(github.dispatches) == 1


@pytest.mark.parametrize(
    "problem", ["host", "github", "tenant", "origin", "registry", "version"]
)
def test_misbound_configuration_refuses_before_minting_a_guest_token(setup, problem):
    publisher, store, github, _ = setup
    b = store.record["snapshot"]["short_links"]
    if problem == "host":
        b["publisher_host"] = "another-host"
    if problem == "github":
        github.identity_ok = False
    if problem == "tenant":
        store.identity = "another-graph"
    if problem == "origin":
        b["target_origin"] = "https://another.example"
    if problem == "registry":
        del b["registry"]
    if problem == "version":
        del store.record["entity_version"]
    with pytest.raises(links.PublicationError):
        publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert store.published == [] and github.payloads == []


def test_member_links_do_not_mint_guest_access_or_touch_alias_configuration(setup):
    publisher, store, github, _ = setup
    result = publisher.publish(
        CONFIG, {"audience": "member", "member_url": "https://members.example/page"}
    )
    assert result["alias_status"] == "not_applicable"
    assert result["share_url"] == "https://members.example/page"
    assert store.published == [] and store.writes == [] and github.dispatches == []
    with pytest.raises(links.PublicationError, match="explicit_recipient"):
        publisher.publish(CONFIG, {"entity_id": PAGE_A})


def test_requested_collision_and_outside_secret_write_fail_to_original_without_loss(
    setup,
):
    publisher, store, github, _ = setup
    result = publisher.publish(
        CONFIG, {"audience": "guest", "entity_id": PAGE_A, "slug": "existing"}
    )
    assert result["alias_status"] == "fallback"
    assert store.writes == []
    github.metadata = "changed-by-another-writer"
    result = publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert result["alias_status"] == "fallback"
    assert github.payloads == [] and github.dispatches == []
    assert store.record["snapshot"]["short_links"]["registry"]["existing"] == OLD_PAGE


def test_real_neotoma_adapter_preserves_request_shape_cas_and_no_owner_field(
    monkeypatch,
):
    fixture = Store()
    calls = []

    def request(method, path, body=None, **_):
        calls.append((method, path, body))
        if path == "/me":
            return {"user_id": "graph-owner"}
        if path == "/correct":
            fixture.update_binding(CONFIG, fixture.record, body["value"])
            return {"success": True}
        if path == "/mcp":
            return {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"entity_id": PAGE_A})}
                    ]
                },
            }
        return fixture.read(CONFIG)

    monkeypatch.setattr(links.neotoma_http, "request_json", request)
    adapter = links.Neotoma()
    record = adapter.read(CONFIG)
    assert "user_id" not in record
    adapter.check_identity("graph-owner")
    adapter.publish({"entity_id": PAGE_A})
    adapter.update_binding(CONFIG, record, binding())
    assert calls[2][0:2] == ("POST", "/mcp")
    assert calls[2][2]["method"] == "tools/call"
    assert calls[2][2]["params"]["name"] == "publish_rendered_page"
    assert calls[2][2]["params"]["arguments"] == {"entity_id": PAGE_A}
    correction = next(body for _, path, body in calls if path == "/correct")
    assert correction["expected_version"] == "v0"
    assert correction["field"] == "short_links"


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "rpc_error",
        "tool_error",
        "wrong_id",
        "malformed_content",
        "http_error",
        "redirect",
    ],
)
def test_native_publish_uses_real_mcp_http_boundary_and_sanitizes_errors(
    monkeypatch, failure
):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []
    payload = {
        "title": "Synthetic page",
        "html_body": "<p>Private body marker</p>",
        "idempotency_key": "stable-create-key",
    }
    published = {
        "entity_id": PAGE_A,
        "access_token": "synthetic-guest-token",
        "share_url": "https://records.example/page",
        "conformance_warnings": [{"code": "THEME_REQUIRED", "severity": "warn"}],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, dict(self.headers), body))
            if self.path != "/mcp":
                self.send_response(404)
                self.end_headers()
                return
            if failure in {"http_error", "redirect"}:
                self.send_response(503 if failure == "http_error" else 307)
                self.send_header("Location", "/unexpected-redirect")
                self.end_headers()
                self.wfile.write(b"synthetic-guest-token private upstream URL/body")
                return
            result = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [{"type": "text", "text": json.dumps(published)}]
                },
            }
            if failure == "rpc_error":
                result = {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {
                        "code": -32603,
                        "message": "synthetic-guest-token private upstream URL/body",
                    },
                }
            if failure == "tool_error":
                result["result"]["isError"] = True
            if failure == "wrong_id":
                result["id"] = "different-request"
            if failure == "malformed_content":
                result["result"]["content"][0]["text"] = (
                    "private invalid JSON synthetic-guest-token"
                )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("NEOTOMA_BASE_URL", f"http://localhost:{server.server_port}")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-neotoma-bearer")
    try:
        if failure:
            with pytest.raises(links.PublicationError) as error:
                links.Neotoma().publish(payload)
            assert str(error.value) in {
                "native_publication_failed",
                "native_publication_response_invalid",
                "native_publication_transport_failed",
            }
            assert "synthetic-guest-token" not in str(error.value)
        else:
            assert links.Neotoma().publish(payload) == published
        assert len(requests) == 1, "native effects and redirects must never be retried"
        path, headers, body = requests[0]
        headers = {key.lower(): value for key, value in headers.items()}
        assert path == "/mcp"
        assert headers["authorization"] == "Bearer synthetic-neotoma-bearer"
        assert headers["mcp-protocol-version"] == "2026-07-28"
        assert (
            headers["mcp-method"] == "tools/call"
            and headers["mcp-name"] == "publish_rendered_page"
        )
        assert body["params"]["arguments"] == payload
        assert body["params"]["_meta"] == {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {
                "name": "ateles-rendered-page-links",
                "version": "1.0",
            },
        }
        assert "private" not in path.lower() and "synthetic-" not in path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_actual_gh_subprocess_keeps_registry_on_stdin_and_sanitizes_errors(
    tmp_path, monkeypatch
):
    script = tmp_path / "gh"
    calls = tmp_path / "calls.jsonl"
    script.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "args=sys.argv[1:]\n"
        f"with open({str(calls)!r},'a') as f:f.write(json.dumps({{'args':args,'stdin':sys.stdin.read(),'host':os.environ.get('GH_HOST'),'credential':os.environ.get('GH_TOKEN'),'neotoma_present':'NEOTOMA_BEARER_TOKEN' in os.environ}})+'\\n')\n"
        "if args[:2]==['api','user']: print('publisher')\n"
        "elif 'actions/secrets' in ' '.join(args): print(json.dumps({'updated_at':'initial'}))\n"
        "elif args[0]=='api': print('main')\n"
        "elif args[0]=='secret': pass\n"
        "else: print('upstream-private-token',file=sys.stderr);sys.exit(1)\n"
    )
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TEST_GH_CALLS", str(calls))
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-process-secret")
    monkeypatch.setenv("GH_TOKEN", "synthetic-existing-gh-token")
    github = links.GitHub()
    github.check_identity(binding())
    monkeypatch.setenv("GH_TOKEN", "synthetic-different-gh-account")
    payload = json.dumps(
        {"origin": "https://records.example", "links": {"existing": OLD_PAGE}}
    )
    assert github.write_secret(binding(), payload) == "initial"
    rows = [json.loads(line) for line in calls.read_text().splitlines()]
    assert {row["credential"] for row in rows} == {"synthetic-existing-gh-token"}
    sent = next(row for row in rows if row["args"][:2] == ["secret", "set"])
    assert sent["stdin"] == payload
    assert sent["host"] == "github.example"
    assert sent["neotoma_present"] is False
    assert payload not in json.dumps(sent["args"])
    dispatch_binding = binding()
    dispatch_binding["pending"] = {"source_sha": "f" * 40}
    with pytest.raises(links.PublicationError) as error:
        github.dispatch(dispatch_binding, "0" * 32)
    assert "upstream-private-token" not in str(error.value)


def test_served_page_verification_requires_actual_destination_and_controls():
    import base64
    import hashlib

    reader = links.Reader()
    b = binding()
    target = f"https://records.example/entities/{OLD_PAGE}/html"
    template = {
        "script": "const destination = new URL({{DESTINATION_JSON}});",
        "html": "<meta content=\"script-src 'sha256-{{CSP_DIGEST}}'\"><script>{{SCRIPT}}</script>",
    }
    script = template["script"].replace("{{DESTINATION_JSON}}", json.dumps(target))
    html = (
        template["html"]
        .replace(
            "{{CSP_DIGEST}}",
            base64.b64encode(hashlib.sha256(script.encode()).digest()).decode(),
        )
        .replace("{{SCRIPT}}", script)
    )
    reader.get = lambda _: html
    reader.verify_aliases(b, template)
    for altered in (
        html.replace(target, "https://other.example/page"),
        html + "<script>fetch(location.search)</script>",
        html.replace("sha256-", "mutated-"),
    ):
        reader.get = lambda _, altered=altered: altered
        with pytest.raises(links.PublicationError, match="template_mismatch"):
            reader.verify_aliases(b, template)


def test_cli_never_echoes_invalid_sensitive_input(tmp_path):
    script = (
        Path(__file__).resolve().parents[2]
        / "execution/scripts/publish_rendered_page.py"
    )
    result = subprocess.run(
        [sys.executable, str(script), "--configuration", CONFIG],
        input='{"secret":"synthetic-private-token"',
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert "synthetic-private-token" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["error_code"] == "publication_request_failed"


def test_inline_publication_requires_and_forwards_stable_idempotency(setup):
    publisher, store, _, _ = setup
    with pytest.raises(links.PublicationError, match="idempotency_required"):
        publisher.publish(
            CONFIG,
            {"audience": "guest", "title": "Shared brief", "html_body": "<p>Body</p>"},
        )
    assert store.published == []
    request = {
        "audience": "guest",
        "title": "Shared brief",
        "html_body": "<p>Body</p>",
        "idempotency_key": "publication-1",
    }
    assert publisher.publish(CONFIG, request)["alias_status"] == "verified"
    assert publisher.publish(CONFIG, request)["alias_status"] == "verified"
    assert all(
        fields["idempotency_key"] == "publication-1" for fields in store.published
    )
    assert set(store.record["snapshot"]["short_links"]["registry"].values()) == {
        OLD_PAGE,
        PAGE_A,
    }


def test_pending_transaction_refuses_changed_binding_and_keeps_original_registry(setup):
    publisher, store, github, _ = setup
    github.fail_wait = True
    assert (
        publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})[
            "alias_status"
        ]
        == "fallback"
    )
    pending = copy.deepcopy(store.record["snapshot"]["short_links"]["pending"])
    assert pending["entity_id"] == PAGE_A
    assert pending["source_sha"] == "f" * 40
    store.record["snapshot"]["short_links"]["registry"]["unapproved-change"] = PAGE_B
    with pytest.raises(links.PublicationError, match="binding_changed"):
        publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_B})
    assert len(store.published) == 1
    assert len(github.dispatches) == 1
    assert store.record["snapshot"]["short_links"]["pending"] == pending


def test_pending_prior_page_finishes_before_another_request_without_adopting_its_result(
    setup,
):
    publisher, store, github, _ = setup
    github.fail_wait = True
    publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    github.fail_wait = False
    result = publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_B})
    assert result["entity_id"] == PAGE_B
    assert len(github.dispatches) == 2
    assert set(github.payloads[-1]["links"].values()) == {OLD_PAGE, PAGE_A, PAGE_B}


def test_conformance_warning_blocks_all_sharing_and_retains_codes(setup):
    publisher, store, github, reader = setup
    original = store.publish

    def warned(fields):
        result = original(fields)
        result["conformance_warnings"] = [
            {
                "code": "RENDERED_PAGE_THEME_REQUIRED",
                "severity": "block",
                "message": "private content",
            }
        ]
        return result

    store.publish = warned
    with pytest.raises(links.PublicationError, match="conformance_requires") as error:
        publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert error.value.conformance_warnings == [
        {"code": "RENDERED_PAGE_THEME_REQUIRED", "severity": "block"}
    ]
    assert reader.urls == [] and github.dispatches == []


def test_completed_deploy_must_hold_across_spaced_reads_and_match_source(monkeypatch):
    github = links.GitHub()
    current = [0]
    monkeypatch.setattr(links.time, "monotonic", lambda: current[0])
    b = binding()
    b["pending"] = {"source_sha": "f" * 40}
    reads = []

    def run(_, **kwargs):
        assert kwargs["host"] == "github.example"
        reads.append(current[0])
        return json.dumps(
            [
                {
                    "databaseId": 1,
                    "displayTitle": "Rendered page aliases opaque",
                    "status": "completed",
                    "conclusion": "success",
                    "headSha": "f" * 40,
                }
            ]
        )

    github._run = run
    github.wait(
        b,
        "opaque",
        deadline=100,
        sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
    )
    assert reads == [0, 15, 30]
    b["pending"]["source_sha"] = "e" * 40
    with pytest.raises(links.PublicationError, match="source_mismatch"):
        github.wait(
            b,
            "opaque",
            deadline=100,
            sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
        )


def test_secret_effect_without_receipt_holds_without_overwriting_unknown_value(setup):
    publisher, store, github, _ = setup
    update = store.update_binding

    def lose_receipt(entity, record, b):
        if b.get("pending", {}).get("phase") == "secret_updated":
            raise links.PublicationError("configuration_write_not_verified")
        return update(entity, record, b)

    store.update_binding = lose_receipt
    result = publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert result["alias_status"] == "fallback"
    assert result["error_code"] == "configuration_write_not_verified"
    store.update_binding = update
    with pytest.raises(links.PublicationError, match="secret_changed_outside"):
        publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert len(github.payloads) == 1 and github.dispatches == []
    assert len(store.published) == 1


def test_ambiguous_dispatch_never_repeats_and_preserves_recovery_state(setup):
    publisher, store, github, _ = setup

    def lost_dispatch(*_):
        raise links.PublicationError("github_request_failed")

    def absent_run(*_, **kwargs):
        raise links.PublicationError("alias_deployment_pending")

    github.dispatch = lost_dispatch
    result = publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert result["alias_status"] == "fallback"
    assert (
        store.record["snapshot"]["short_links"]["pending"]["phase"]
        == "dispatch_requested"
    )
    github.wait = absent_run
    with pytest.raises(links.PublicationError, match="deployment_pending"):
        publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_B})
    assert len(store.published) == 1 and len(github.payloads) == 1


def test_existing_page_content_must_be_corrected_before_publish(setup):
    publisher, store, _, _ = setup
    with pytest.raises(links.PublicationError, match="prior_content_update"):
        publisher.publish(
            CONFIG,
            {"audience": "guest", "entity_id": PAGE_A, "html_body": "<p>Changed</p>"},
        )
    assert store.published == []


def test_recipient_http_boundary_sends_no_auth_and_refuses_redirects():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(200 if self.path == "/page" else 302)
            self.send_header("Location", "/page")
            self.end_headers()
            self.wfile.write(b"recipient content")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert links.Reader().get(base + "/page") == "recipient content"
        with pytest.raises(links.PublicationError, match="recipient_page_not_verified"):
            links.Reader().get(base + "/redirect")
        assert len(seen) == 2 and all(
            "Authorization" not in headers for headers in seen
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_keyring_credential_is_captured_once_and_explicit_empty_is_rejected(
    tmp_path, monkeypatch
):
    script = tmp_path / "gh"
    calls = tmp_path / "auth-calls"
    script.write_text(
        "#!/usr/bin/env python3\nimport os,sys\n"
        "if sys.argv[1:3]==['auth','token']:\n"
        f" with open({str(calls)!r},'a') as f:f.write('capture\\n')\n"
        " print('synthetic-keyring-token')\n"
        "else:\n"
        " assert os.environ['GH_TOKEN']=='synthetic-keyring-token'\n"
        " print('publisher' if sys.argv[2]=='user' else 'main')\n"
    )
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    github = links.GitHub()
    github.check_identity(binding())
    github.check_identity(binding())
    assert calls.read_text() == "capture\n"
    monkeypatch.setenv("GH_TOKEN", "")
    with pytest.raises(links.PublicationError, match="credential_missing"):
        links.GitHub().check_identity(binding())
    assert calls.read_text() == "capture\n"


def test_template_fetch_is_data_at_exact_source_and_pending_source_is_bound(setup):
    import base64

    github = links.GitHub()
    calls = []
    template = {
        "version": 1,
        "script": "{{DESTINATION_JSON}}",
        "html": "{{CSP_DIGEST}}{{SCRIPT}}",
    }

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return base64.b64encode(json.dumps(template).encode()).decode()

    github._run = run
    assert github.template(binding(), "e" * 40) == template
    assert (
        calls[0][0][1]
        == "repos/example/site/contents/scripts/short-link-template.json?ref="
        + "e" * 40
    )
    publisher, store, fake_github, _ = setup
    fake_github.fail_wait = True
    publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    store.record["snapshot"]["short_links"]["pending"]["source_sha"] = "e" * 40
    with pytest.raises(links.PublicationError, match="binding_changed"):
        publisher.publish(CONFIG, {"audience": "guest", "entity_id": PAGE_A})
    assert len(store.published) == 1

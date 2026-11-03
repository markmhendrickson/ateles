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

from lib.capabilities import rendered_page_links as links

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

    def verify_aliases(self, binding):
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
        return fixture.read(CONFIG)

    monkeypatch.setattr(links.neotoma_http, "request_json", request)
    adapter = links.Neotoma()
    record = adapter.read(CONFIG)
    assert "user_id" not in record
    adapter.check_identity("graph-owner")
    adapter.publish({"entity_id": PAGE_A})
    adapter.update_binding(CONFIG, record, binding())
    assert calls[2] == ("POST", "/rendered-pages/publish", {"entity_id": PAGE_A})
    correction = next(body for _, path, body in calls if path == "/correct")
    assert correction["expected_version"] == "v0"
    assert correction["field"] == "short_links"


def test_actual_gh_subprocess_keeps_registry_on_stdin_and_sanitizes_errors(
    tmp_path, monkeypatch
):
    script = tmp_path / "gh"
    calls = tmp_path / "calls.jsonl"
    script.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "args=sys.argv[1:]\n"
        "with open(os.environ['TEST_GH_CALLS'],'a') as f:f.write(json.dumps({'args':args,'stdin':sys.stdin.read()})+'\\n')\n"
        "if args[:2]==['api','user']: print('publisher')\n"
        "elif 'actions/secrets' in ' '.join(args): print(json.dumps({'updated_at':'initial'}))\n"
        "elif args[0]=='api': print('main')\n"
        "elif args[0]=='secret': pass\n"
        "else: print('upstream-private-token',file=sys.stderr);sys.exit(1)\n"
    )
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TEST_GH_CALLS", str(calls))
    github = links.GitHub()
    payload = json.dumps(
        {"origin": "https://records.example", "links": {"existing": OLD_PAGE}}
    )
    assert github.write_secret(binding(), payload) == "initial"
    rows = [json.loads(line) for line in calls.read_text().splitlines()]
    sent = next(row for row in rows if row["args"][:2] == ["secret", "set"])
    assert sent["stdin"] == payload
    assert payload not in json.dumps(sent["args"])
    with pytest.raises(links.PublicationError) as error:
        github.dispatch(binding(), "0" * 32)
    assert "upstream-private-token" not in str(error.value)


def test_served_page_verification_requires_actual_destination_and_controls():
    reader = links.Reader()
    b = binding()
    target = f"https://records.example/entities/{OLD_PAGE}/html"
    html = (
        f"const destination = new URL({json.dumps(target)});"
        'name="robots" content="noindex,nofollow,noarchive" '
        'name="referrer" content="no-referrer" '
        "destination.searchParams.set('access_token', token)"
    )
    reader.get = lambda _: html
    reader.verify_aliases(b)
    reader.get = lambda _: html.replace(target, "https://other.example/page")
    with pytest.raises(links.PublicationError, match="destination_mismatch"):
        reader.verify_aliases(b)


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

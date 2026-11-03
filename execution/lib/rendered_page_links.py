"""Agent publication entry point for private, registered rendered-page aliases.

The deployment_configuration binding owns the complete token-free registry.
One configured host serializes registry, encrypted-secret and Pages effects.
Neotoma's native publish operation remains unchanged.
"""

from __future__ import annotations

import contextlib
import base64
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from lib.capabilities import neotoma_http

BINDING_FIELD = "short_links"
ENTITY_RE = re.compile(r"ent_[a-f0-9]{24}\Z")
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class PublicationError(RuntimeError):
    """Stable, secret-free boundary failure; never include upstream output."""

    def __init__(self, code: str, *, warnings: list | None = None):
        super().__init__(code)
        self.conformance_warnings = warnings


def origin(value: Any) -> str:
    if not isinstance(value, str) or re.search(r"\s|[\\\x00-\x1f\x7f]", value):
        raise PublicationError("invalid_origin")
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port
    except ValueError:
        raise PublicationError("invalid_origin") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc != parsed.netloc.lower()
    ):
        raise PublicationError("invalid_origin")
    return value


def binding_from(record: dict) -> dict:
    if record.get("entity_type") != "deployment_configuration":
        raise PublicationError("wrong_configuration_type")
    snap = record.get("snapshot", {})
    binding = copy.deepcopy(snap.get(BINDING_FIELD))
    if not isinstance(binding, dict) or binding.get("version") != 1:
        raise PublicationError("short_link_binding_missing")
    for key in (
        "publisher_host",
        "expected_github_login",
        "expected_user_id",
        "github_host",
        "branch",
        "workflow",
        "secret_name",
        "secret_updated_at",
        "template_path",
    ):
        if not isinstance(binding.get(key), str) or not binding[key].strip():
            raise PublicationError("short_link_binding_incomplete")
    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", binding.get("repository", "")
    ):
        raise PublicationError("invalid_repository")
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", binding["github_host"]):
        raise PublicationError("invalid_github_host")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", binding["secret_name"]):
        raise PublicationError("invalid_secret_name")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", binding["workflow"]):
        raise PublicationError("invalid_workflow")
    if not re.fullmatch(
        r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.json", binding["template_path"]
    ):
        raise PublicationError("invalid_template_path")
    origin(binding.get("site_origin"))
    origin(binding.get("target_origin"))
    if binding["branch"] != snap.get("deploy_branch"):
        raise PublicationError("deployment_branch_mismatch")
    registry = binding.get("registry")
    if not isinstance(registry, dict):
        raise PublicationError("canonical_registry_not_imported")
    for slug, entity_id in registry.items():
        if (
            not valid_slug(slug)
            or not isinstance(entity_id, str)
            or not ENTITY_RE.fullmatch(entity_id)
        ):
            raise PublicationError("invalid_registry")
    if (
        not isinstance(record.get("entity_version"), str)
        or not record["entity_version"]
    ):
        raise PublicationError("configuration_version_missing")
    return binding


def valid_slug(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) <= 80 and bool(SLUG_RE.fullmatch(value))
    )


def choose_slug(
    registry: dict, entity_id: str, title: str, requested: str | None
) -> str:
    existing = sorted(slug for slug, target in registry.items() if target == entity_id)
    if existing:
        return existing[0]
    if requested is not None:
        if not valid_slug(requested):
            raise PublicationError("invalid_requested_slug")
        if requested in registry:
            raise PublicationError("requested_slug_in_use")
        return requested
    ascii_title = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    )
    base = (
        re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")[:53].rstrip("-")
        or "page"
    )
    if base not in registry:
        return base
    # The full page id suffix is deterministic; never steal an existing alias.
    candidate = f"{base}-{entity_id[4:]}"
    if candidate in registry:
        raise PublicationError("generated_slug_in_use")
    return candidate


@contextlib.contextmanager
def publisher_lock(binding: dict, *, lock_root: Path | None = None):
    if socket.gethostname() != binding["publisher_host"]:
        raise PublicationError("wrong_publisher_host")
    root = lock_root or Path.home() / ".config" / "ateles" / "rendered-page-links"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(
        f"{binding['repository']}:{binding['secret_name']}".encode()
    ).hexdigest()
    # Like the existing repository-fetch lock, the inode is retained for waiters.
    fd = os.open(root / f"{key}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Neotoma:
    def read(self, entity_id: str) -> dict:
        if not ENTITY_RE.fullmatch(entity_id):
            raise PublicationError("invalid_entity_id")
        result = neotoma_http.request_json("GET", f"/entities/{entity_id}", timeout=120)
        if result.get("entity_id") != entity_id:
            raise PublicationError("entity_read_mismatch")
        return result

    def check_identity(self, expected_user_id: str) -> None:
        # GET /entities/:id is owner-scoped server-side and deliberately omits
        # user_id from its snapshot response. /me supplies the authenticated graph.
        if (
            neotoma_http.request_json("GET", "/me", timeout=120).get("user_id")
            != expected_user_id
        ):
            raise PublicationError("neotoma_identity_mismatch")

    def publish(self, fields: dict) -> dict:
        return neotoma_http.request_json(
            "POST", "/rendered-pages/publish", fields, timeout=120
        )

    def update_binding(self, entity_id: str, record: dict, binding: dict) -> dict:
        neotoma_http.request_json(
            "POST",
            "/correct",
            {
                "entity_id": entity_id,
                "entity_type": "deployment_configuration",
                "field": BINDING_FIELD,
                "value": binding,
                "expected_version": record["entity_version"],
                "idempotency_key": "rendered-page-binding-" + uuid.uuid4().hex,
            },
            timeout=120,
        )
        current = self.read(entity_id)
        if current.get("snapshot", {}).get(BINDING_FIELD) != binding:
            raise PublicationError("configuration_write_not_verified")
        return current


class GitHub:
    def __init__(self):
        self._credential = None
        self._host = None

    def _run(self, args: list[str], *, stdin: str | None = None, host: str) -> str:
        names = {
            "HOME",
            "PATH",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "GH_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
        }
        environment = {
            name: value for name, value in os.environ.items() if name in names
        }
        environment.update({"GH_HOST": host, "GH_PROMPT_DISABLED": "1"})
        if self._credential is None:
            for name in ("GH_TOKEN", "GITHUB_TOKEN"):
                if name in os.environ:
                    value = os.environ[name]
                    if not value.strip():
                        raise PublicationError("github_credential_missing")
                    self._credential = value
                    break
            if self._credential is None:
                try:
                    captured = subprocess.run(
                        ["gh", "auth", "token", "--hostname", host],
                        capture_output=True,
                        text=True,
                        timeout=120,
                        env=environment,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    raise PublicationError("github_credential_unavailable") from None
                if captured.returncode or not captured.stdout.strip():
                    raise PublicationError("github_credential_unavailable")
                self._credential = captured.stdout.strip()
            self._host = host
        if host != self._host:
            raise PublicationError("github_credential_host_changed")
        environment["GH_TOKEN"] = self._credential
        try:
            result = subprocess.run(
                ["gh", *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise PublicationError("github_request_failed") from None
        if result.returncode:
            raise PublicationError("github_request_failed")
        return result.stdout.strip()

    def check_identity(self, binding: dict) -> None:
        if (
            self._run(["api", "user", "--jq", ".login"], host=binding["github_host"])
            != binding["expected_github_login"]
        ):
            raise PublicationError("github_identity_mismatch")
        branch = self._run(
            ["api", f"repos/{binding['repository']}", "--jq", ".default_branch"],
            host=binding["github_host"],
        )
        if branch != binding["branch"]:
            raise PublicationError("github_default_branch_mismatch")

    def secret_updated_at(self, binding: dict) -> str:
        data = json.loads(
            self._run(
                [
                    "api",
                    f"repos/{binding['repository']}/actions/secrets/{binding['secret_name']}",
                ],
                host=binding["github_host"],
            )
        )
        value = data.get("updated_at")
        if not isinstance(value, str):
            raise PublicationError("secret_metadata_missing")
        return value

    def source_sha(self, binding: dict) -> str:
        sha = self._run(
            [
                "api",
                f"repos/{binding['repository']}/commits/{binding['branch']}",
                "--jq",
                ".sha",
            ],
            host=binding["github_host"],
        )
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise PublicationError("production_source_unavailable")
        return sha

    def template(self, binding: dict, sha: str) -> dict:
        encoded = self._run(
            [
                "api",
                f"repos/{binding['repository']}/contents/{binding['template_path']}?ref={sha}",
                "--jq",
                ".content",
            ],
            host=binding["github_host"],
        )
        try:
            template = json.loads(base64.b64decode(encoded).decode("utf-8"))
            if (
                set(template) != {"version", "html", "script"}
                or template["version"] != 1
                or template["script"].count("{{DESTINATION_JSON}}") != 1
                or template["html"].count("{{SCRIPT}}") != 1
                or template["html"].count("{{CSP_DIGEST}}") != 1
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise PublicationError("invalid_alias_template") from None
        return template

    def write_secret(self, binding: dict, payload: str) -> str:
        self.check_identity(binding)
        if self.secret_updated_at(binding) != binding["secret_updated_at"]:
            raise PublicationError("secret_changed_outside_publisher")
        self._run(
            ["secret", "set", binding["secret_name"], "--repo", binding["repository"]],
            stdin=payload,
            host=binding["github_host"],
        )
        return self.secret_updated_at(binding)

    def dispatch(self, binding: dict, request_id: str) -> None:
        self.check_identity(binding)
        self._run(
            [
                "workflow",
                "run",
                binding["workflow"],
                "--repo",
                binding["repository"],
                "--ref",
                binding["branch"],
                "-f",
                f"alias_request_id={request_id}",
                "-f",
                f"expected_source_sha={binding['pending']['source_sha']}",
            ],
            host=binding["github_host"],
        )

    def wait(
        self, binding: dict, request_id: str, *, deadline: float, sleep=time.sleep
    ) -> dict:
        previous = None
        stable = 0
        while time.monotonic() < deadline:
            runs = json.loads(
                self._run(
                    [
                        "run",
                        "list",
                        "--repo",
                        binding["repository"],
                        "--workflow",
                        binding["workflow"],
                        "--branch",
                        binding["branch"],
                        "--event",
                        "workflow_dispatch",
                        "--limit",
                        "100",
                        "--json",
                        "databaseId,displayTitle,status,conclusion,headSha",
                    ],
                    host=binding["github_host"],
                )
            )
            matches = [
                run
                for run in runs
                if run.get("displayTitle") == f"Rendered page aliases {request_id}"
            ]
            if len(matches) > 1:
                raise PublicationError("ambiguous_deployment_run")
            if matches and matches[0].get("status") == "completed":
                state = tuple(
                    matches[0].get(key)
                    for key in ("databaseId", "status", "conclusion", "headSha")
                )
                stable = stable + 1 if state == previous else 1
                previous = state
                if stable >= 3:
                    if matches[0].get("conclusion") != "success":
                        raise PublicationError("alias_deployment_failed")
                    if matches[0].get("headSha") != binding["pending"]["source_sha"]:
                        raise PublicationError("deployment_source_mismatch")
                    return matches[0]
            else:
                stable, previous = 0, None
            sleep(min(15, max(0, deadline - time.monotonic())))
        raise PublicationError("alias_deployment_pending")


class Reader:
    def get(self, url: str) -> str:
        request = urllib.request.Request(
            url, headers={"User-Agent": "ateles-rendered-page-links/1.0"}
        )
        # No bearer headers; guest verification must follow the recipient path.
        try:
            with urllib.request.build_opener(neotoma_http._NoRedirect).open(
                request, timeout=60
            ) as response:
                if response.status != 200:
                    raise PublicationError("recipient_page_not_verified")
                return response.read().decode("utf-8")
        except (OSError, UnicodeError, urllib.error.HTTPError):
            raise PublicationError("recipient_page_not_verified") from None

    def verify_aliases(self, binding: dict, template: dict) -> None:
        for slug, entity_id in binding["registry"].items():
            html = self.get(f"{binding['site_origin']}/p/{slug}/")
            destination = f"{binding['target_origin']}/entities/{entity_id}/html"
            script = template["script"].replace(
                "{{DESTINATION_JSON}}",
                json.dumps(destination, ensure_ascii=False).replace("<", "\\u003c"),
            )
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            expected = (
                template["html"]
                .replace("{{CSP_DIGEST}}", digest)
                .replace("{{SCRIPT}}", script)
            )
            if html != expected:
                raise PublicationError("served_alias_template_mismatch")


class Publisher:
    def __init__(
        self,
        neotoma=None,
        github=None,
        reader=None,
        *,
        lock_root=None,
        wait_seconds=1200,
    ):
        self.neotoma = neotoma or Neotoma()
        self.github = github or GitHub()
        self.reader = reader or Reader()
        self.lock_root = lock_root
        self.wait_seconds = wait_seconds

    def publish(self, configuration_id: str, request: dict) -> dict:
        if request.get("audience") == "member":
            # The owning member workflow supplies and verifies its authenticated link.
            member = request.get("member_url")
            if not isinstance(member, str) or not member.startswith("https://"):
                raise PublicationError("member_url_required")
            return {
                "alias_status": "not_applicable",
                "share_url": request.get("member_url"),
                "verification": "member_path_requires_caller_verification",
            }
        if request.get("audience") != "guest":
            raise PublicationError("explicit_recipient_audience_required")
        allowed = {
            "audience",
            "entity_id",
            "title",
            "html_body",
            "custom_css",
            "meta_description",
            "idempotency_key",
            "slug",
        }
        if set(request) - allowed:
            raise PublicationError("unexpected_publication_field")
        if request.get("entity_id") and set(request) & {
            "title",
            "html_body",
            "custom_css",
            "meta_description",
        }:
            raise PublicationError("existing_page_requires_prior_content_update")
        if not request.get("entity_id") and not request.get("idempotency_key"):
            raise PublicationError("inline_publication_idempotency_required")
        record = self.neotoma.read(configuration_id)
        initial = binding_from(record)
        if initial["target_origin"] != neotoma_http.base_url():
            raise PublicationError("destination_instance_mismatch")
        with publisher_lock(initial, lock_root=self.lock_root):
            # Re-read after waiting: another publisher may have added aliases.
            record = self.neotoma.read(configuration_id)
            binding = binding_from(record)
            if binding["publisher_host"] != socket.gethostname() or any(
                binding[k] != initial[k]
                for k in (
                    "repository",
                    "secret_name",
                    "site_origin",
                    "target_origin",
                    "branch",
                    "workflow",
                    "publisher_host",
                    "github_host",
                    "expected_github_login",
                    "expected_user_id",
                    "template_path",
                )
            ):
                raise PublicationError("publisher_binding_changed")
            self.github.check_identity(binding)
            self.neotoma.check_identity(binding["expected_user_id"])
            self._finish_pending(configuration_id, record, binding)
            record = self.neotoma.read(configuration_id)
            binding = binding_from(record)
            fields = {
                key: value
                for key, value in request.items()
                if key not in {"audience", "slug"}
            }
            published = self.neotoma.publish(fields)
            entity_id, token, original = self._publication_result(published, binding)
            self.reader.get(original)
            # After publication, failures preserve the verified original, never a predicted alias.
            try:
                page = self.neotoma.read(entity_id)
                if page.get("entity_type") != "rendered_page":
                    raise PublicationError("published_page_not_verified")
                page_fields = page.get("snapshot", {})
                desired = request.get("slug") or page_fields.get("slug") or None
                slug = choose_slug(
                    binding["registry"],
                    entity_id,
                    str(page_fields.get("title") or request.get("title") or "page"),
                    desired,
                )
                if binding["registry"].get(slug) != entity_id:
                    binding["registry"][slug] = entity_id
                    binding["pending"] = {
                        "request_id": uuid.uuid4().hex,
                        "phase": "registry_committed",
                        "entity_id": entity_id,
                        "input_digest": hashlib.sha256(
                            json.dumps(request, sort_keys=True).encode()
                        ).hexdigest(),
                        "source_sha": self.github.source_sha(binding),
                    }
                    binding["pending"]["binding_digest"] = self._binding_digest(
                        configuration_id, binding
                    )
                    record = self.neotoma.update_binding(
                        configuration_id, record, binding
                    )
                if binding.get("pending"):
                    self._finish_pending(configuration_id, record, binding)
                else:
                    self.reader.verify_aliases(
                        binding,
                        self.github.template(binding, self.github.source_sha(binding)),
                    )
                short = f"{binding['site_origin']}/p/{slug}/?{urllib.parse.urlencode({'t': token})}"
                return {
                    "entity_id": entity_id,
                    "share_url": short,
                    "alias_status": "verified",
                }
            except Exception as exc:
                # No upstream message/output/URL is copied into a diagnostic.
                return {
                    "entity_id": entity_id,
                    "share_url": original,
                    "alias_status": "fallback",
                    "outstanding_work": "rendered_page_alias_registration_or_verification",
                    "error_code": str(exc)
                    if isinstance(exc, PublicationError)
                    else "alias_operation_failed",
                }

    @staticmethod
    def _publication_result(result: dict, binding: dict) -> tuple[str, str, str]:
        entity_id, token, share_url = (
            result.get(k) for k in ("entity_id", "access_token", "share_url")
        )
        if result.get("conformance_warnings"):
            warnings = []
            for warning in result["conformance_warnings"]:
                code = warning.get("code") if isinstance(warning, dict) else None
                severity = (
                    warning.get("severity") if isinstance(warning, dict) else None
                )
                warnings.append(
                    {
                        "code": code
                        if isinstance(code, str)
                        and re.fullmatch(r"[A-Z0-9_]{1,100}", code)
                        else "RENDERED_PAGE_CONFORMANCE",
                        "severity": severity
                        if severity in {"warn", "block"}
                        else "block",
                    }
                )
            raise PublicationError(
                "page_conformance_requires_correction", warnings=warnings
            )
        if (
            not isinstance(entity_id, str)
            or not ENTITY_RE.fullmatch(entity_id)
            or not isinstance(token, str)
            or not token
            or len(token) > 4096
            or re.search(r"\s|[\x00-\x1f\x7f\ufffd]", token)
        ):
            raise PublicationError("invalid_publication_result")
        expected = f"{binding['target_origin']}/entities/{entity_id}/html"
        parsed = urllib.parse.urlsplit(share_url if isinstance(share_url, str) else "")
        if (
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}" != expected
            or parsed.fragment
            or urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            != [("access_token", token)]
        ):
            raise PublicationError("publication_destination_mismatch")
        return entity_id, token, share_url

    def _finish_pending(
        self, configuration_id: str, record: dict, binding: dict
    ) -> None:
        pending = binding.get("pending")
        if not pending:
            return
        if not isinstance(pending, dict) or not re.fullmatch(
            r"[a-f0-9]{32}", str(pending.get("request_id", ""))
        ):
            raise PublicationError("invalid_pending_publication")
        if (
            set(pending)
            != {
                "request_id",
                "phase",
                "entity_id",
                "input_digest",
                "source_sha",
                "binding_digest",
            }
            or not ENTITY_RE.fullmatch(str(pending.get("entity_id", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(pending.get("input_digest", "")))
            or not re.fullmatch(r"[a-f0-9]{40}", str(pending.get("source_sha", "")))
            or pending.get("binding_digest")
            != self._binding_digest(configuration_id, binding)
        ):
            raise PublicationError("pending_publication_binding_changed")
        if pending.get("phase") == "registry_committed":
            payload = json.dumps(
                {"origin": binding["target_origin"], "links": binding["registry"]},
                sort_keys=True,
            )
            binding["secret_updated_at"] = self.github.write_secret(binding, payload)
            pending["phase"] = "secret_updated"
            record = self.neotoma.update_binding(configuration_id, record, binding)
        if pending.get("phase") == "secret_updated":
            # Persist BEFORE dispatch. An ambiguous send is only observed on recovery, never repeated.
            pending["phase"] = "dispatch_requested"
            record = self.neotoma.update_binding(configuration_id, record, binding)
            self.github.dispatch(binding, pending["request_id"])
        if pending.get("phase") != "dispatch_requested":
            raise PublicationError("invalid_pending_phase")
        self.github.wait(
            binding,
            pending["request_id"],
            deadline=time.monotonic() + self.wait_seconds,
        )
        self.reader.verify_aliases(
            binding, self.github.template(binding, pending["source_sha"])
        )
        del binding["pending"]
        self.neotoma.update_binding(configuration_id, record, binding)

    @staticmethod
    def _binding_digest(configuration_id: str, binding: dict) -> str:
        keys = (
            "publisher_host",
            "repository",
            "branch",
            "workflow",
            "secret_name",
            "expected_user_id",
            "github_host",
            "expected_github_login",
            "site_origin",
            "target_origin",
            "registry",
            "template_path",
        )
        bound = {key: binding[key] for key in keys}
        bound["configuration_id"] = configuration_id
        if isinstance(binding.get("pending"), dict):
            bound["transaction"] = {
                key: binding["pending"].get(key)
                for key in ("request_id", "entity_id", "input_digest", "source_sha")
            }
        return hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()

"""Complete-profile comparison core; never contacts a provider or deploys.

Extracted from the reviewed private instance validator. Trusted canonical
bindings, temporary input normalization and command construction are separate
adapters; this core never learns a baseline from the candidate being checked.
"""

from __future__ import annotations

import hashlib
import json
import re


class Refused(ValueError):
    """A safe structural refusal, without echoing private profile contents."""


def require(condition, message):
    if not condition:
        raise Refused(message)


def digest(value):
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError, UnicodeError):
        raise Refused("unsupported profile encoding") from None
    return hashlib.sha256(encoded).hexdigest()


def stable_source(config):
    require(isinstance(config, dict), "invalid source configuration")
    try:
        value = json.loads(json.dumps(config, allow_nan=False))
    except (TypeError, ValueError, UnicodeError):
        raise Refused("unsupported source configuration") from None
    value.pop("image", None)
    metadata = value.get("metadata", {})
    require(isinstance(metadata, dict), "invalid source metadata")
    for key in ("fly_release_id", "fly_release_version"):
        metadata.pop(key, None)
    return value


def check_profiles(inventory, saved_config, baseline, image, phase="before"):
    """Require pinned source/retained profiles and the phase's saved input.

    Baseline selection/authenticity belongs to the canonical private adapter.
    This preparation-only core does not authorize runtime binding changes.
    """
    require(phase in ("before", "after"), "unknown phase")
    fields = {
        "source_id",
        "source_state",
        "region",
        "source_static_config_sha256",
        "retained_machines",
        "saved_config_before_sha256",
        "saved_config_after_sha256",
        "image_repository",
    }
    require(
        isinstance(baseline, dict) and set(baseline) == fields,
        "invalid complete baseline",
    )
    require(
        all(
            isinstance(baseline[k], str) and baseline[k]
            for k in ("source_id", "region", "image_repository")
        ),
        "invalid target binding",
    )
    require(
        all(
            isinstance(baseline[k], str) and re.fullmatch(r"[0-9a-f]{64}", baseline[k])
            for k in (
                "source_static_config_sha256",
                "saved_config_before_sha256",
                "saved_config_after_sha256",
            )
        ),
        "invalid profile digest",
    )
    retained = baseline["retained_machines"]
    require(isinstance(retained, dict), "invalid retained inventory")
    require(
        isinstance(inventory, list) and all(isinstance(m, dict) for m in inventory),
        "invalid inventory",
    )
    require(
        all(isinstance(m.get("id"), str) and m["id"] for m in inventory),
        "invalid machine identity",
    )
    machines = {m.get("id"): m for m in inventory}
    require(
        len(machines) == len(inventory)
        and set(machines) == {baseline["source_id"], *retained}
        and baseline["source_id"] not in retained,
        "unexpected machine inventory",
    )
    source = machines[baseline["source_id"]]
    require(
        all(m.get("region") == baseline["region"] for m in machines.values()),
        "region drift",
    )
    states = baseline["source_state"]
    if isinstance(states, dict):
        require(
            set(states) == {"before", "after"}
            and all(
                isinstance(v, list)
                and v
                and all(isinstance(x, str) and x in ("started", "stopped") for x in v)
                and len(v) == len(set(v))
                for v in states.values()
            ),
            "invalid phase-state allowance",
        )
        allowed = states[phase]
    else:
        require(
            isinstance(states, str) and states in ("started", "stopped"),
            "invalid source state",
        )
        allowed = [states]
    require(source.get("state") in allowed, "source state drift")
    require(
        digest(stable_source(source.get("config")))
        == baseline["source_static_config_sha256"],
        "source profile drift",
    )
    for key, expected in retained.items():
        require(
            isinstance(expected, dict) and set(expected) == {"state", "config_sha256"},
            "invalid retained baseline",
        )
        require(
            machines[key].get("state") == expected["state"]
            and digest(machines[key].get("config")) == expected["config_sha256"],
            "retained profile drift",
        )
    require(
        digest(saved_config) == baseline[f"saved_config_{phase}_sha256"],
        "saved configuration drift",
    )
    require(
        isinstance(image, str)
        and re.fullmatch(
            re.escape(baseline["image_repository"]) + r"@sha256:[0-9a-f]{64}", image
        ),
        "invalid immutable image",
    )
    if phase == "after":
        require(
            isinstance(source.get("image_ref"), dict)
            and source["image_ref"].get("digest") == image.split("@", 1)[1],
            "source image drift",
        )
    return True


RECONCILIATION_PATHS = frozenset(
    {
        ("http_service", "auto_stop_machines"),
        ("http_service", "min_machines_running"),
        ("vm", 0, "memory"),
        ("vm", 0, "cpus"),
        ("restart", 0, "policy"),
        ("restart", 0, "retries"),
        ("http_service", "checks", 0, "path"),
        ("http_service", "checks", 0, "interval"),
        ("http_service", "checks", 0, "timeout"),
        ("http_service", "checks", 0, "grace_period"),
    }
)


def input_changes(manifest):
    return (
        manifest["input_changes"]
        if manifest.get("version") in (2, 3, 4)
        else manifest["idle_changes"]
    )


def _reconciled_input(saved, manifest):
    import copy

    changes = manifest["input_changes"]
    require(isinstance(changes, list), "invalid input corrections")
    paths = []
    for c in changes:
        require(
            isinstance(c, dict)
            and set(c)
            == {"path", "before_present", "before", "after_present", "after"},
            "invalid correction binding",
        )
        require(
            isinstance(c["path"], list)
            and all(type(k) in (str, int) for k in c["path"]),
            "invalid correction path",
        )
        paths.append(tuple(c["path"]))
    require(
        not changes or (len(paths) == 10 and set(paths) == RECONCILIATION_PATHS),
        "unsupported normalization",
    )
    result = copy.deepcopy(saved)
    if changes:
        for key in ("vm", "restart"):
            require(
                isinstance(result.get(key), list)
                and len(result[key]) == 1
                and isinstance(result[key][0], dict),
                "unselected input array",
            )
        service = result.get("http_service")
        require(
            isinstance(service, dict)
            and isinstance(service.get("checks"), list)
            and len(service["checks"]) == 1
            and isinstance(service["checks"][0], dict),
            "unselected HTTP check array",
        )
    for c in changes:
        parent = result
        for key in c["path"][:-1]:
            require(
                (type(key) is str and isinstance(parent, dict) and key in parent)
                or (
                    type(key) is int
                    and isinstance(parent, list)
                    and 0 <= key < len(parent)
                ),
                "correction parent absent",
            )
            parent = parent[key]
        key = c["path"][-1]
        require(isinstance(parent, dict), "correction scalar parent invalid")
        present = key in parent
        require(
            type(c["before_present"]) is bool
            and c["before_present"] == present
            and (present or c["before"] is None)
            and (
                not present
                or (
                    type(parent[key]) is type(c["before"])
                    and parent[key] == c["before"]
                )
            )
            and c["after_present"] is True,
            "correction presence/value drift",
        )
        require(
            present or tuple(c["path"]) == ("restart", 0, "retries"),
            "unselected absent insertion",
        )
        value = c["after"]
        if key in ("min_machines_running", "cpus", "retries"):
            require(
                type(value) is int and value >= (1 if key == "cpus" else 0),
                "invalid correction integer",
            )
        elif key == "auto_stop_machines":
            require(
                type(value) is bool
                or (type(value) is str and value in ("off", "stop")),
                "unknown idle encoding",
            )
        else:
            require(type(value) is str and bool(value), "invalid correction scalar")
        parent[key] = value
    require(
        digest(result) == manifest["profile"]["saved_config_after_sha256"],
        "normalized configuration drift",
    )
    return result


def normalized_input(saved, manifest):
    """Apply the selected closed correction version, never learn a policy."""
    if manifest.get("version") == 2:
        return _reconciled_input(saved, manifest)
    import copy

    changes = manifest["idle_changes"]
    require(
        isinstance(changes, list)
        and all(
            isinstance(c, dict)
            and isinstance(c.get("path"), list)
            and all(isinstance(k, str) for k in c["path"])
            for c in changes
        ),
        "invalid idle correction",
    )
    expected_paths = {
        ("http_service", "auto_stop_machines"),
        ("http_service", "min_machines_running"),
    }
    require(
        not changes
        or (
            len(changes) == 2
            and {tuple(c.get("path", [])) for c in changes} == expected_paths
        ),
        "unsupported normalization",
    )
    result = copy.deepcopy(saved)
    for change in changes:
        require(
            isinstance(change, dict) and set(change) == {"path", "before", "after"},
            "invalid correction binding",
        )
        section, field = change["path"]
        require(
            isinstance(result.get(section), dict)
            and field in result[section]
            and type(result[section][field]) is type(change["before"])
            and result[section][field] == change["before"],
            "idle input drift",
        )
        if field == "auto_stop_machines":
            require(
                type(change["after"]) is bool
                or (
                    type(change["after"]) is str and change["after"] in ("off", "stop")
                ),
                "unknown idle encoding",
            )
        else:
            require(
                type(change["after"]) is int and change["after"] >= 0,
                "invalid idle count",
            )
        result[section][field] = change["after"]
    require(
        digest(result) == manifest["profile"]["saved_config_after_sha256"],
        "normalized configuration drift",
    )
    return result


def _selected_saved_command_metadata(descriptor, version=3):
    require(type(version) is int and version in (3, 4), "unselected metadata version")
    selected = {
        "path": "/deploy",
        "before_present": version == 4,
        "before": {"strategy": "rolling"} if version == 4 else None,
        "after_present": True,
        "after": {"strategy": "rolling"},
    }
    require(
        isinstance(descriptor, dict) and digest(descriptor) == digest(selected),
        "unsupported saved command metadata",
    )
    return selected


def _saved_command_input(saved, manifest, phase):
    """Bind the one admitted absent-to-rolling saved metadata transition.

    Expected after derives from the pinned before, never from observed drift.
    This version has no deployment-input edits and admits no other deploy shape.
    """
    import copy

    selected = _selected_saved_command_metadata(manifest["saved_command_metadata"])
    require(manifest["input_changes"] == [], "saved metadata cannot edit input")
    require(
        manifest["input_toml"]["before_sha256"]
        == manifest["input_toml"]["after_sha256"],
        "saved metadata raw input differs",
    )
    before = copy.deepcopy(saved)
    if phase == "after":
        require(
            "deploy" in before
            and digest(before["deploy"]) == digest(selected["after"]),
            "saved deployment metadata drift",
        )
        del before["deploy"]
    require("deploy" not in before, "saved deploy was not absent")
    require(
        digest(before) == manifest["profile"]["saved_config_before_sha256"],
        "saved before cannot be recovered",
    )
    after = {**copy.deepcopy(before), "deploy": copy.deepcopy(selected["after"])}
    require(
        digest(after) == manifest["profile"]["saved_config_after_sha256"],
        "command-derived saved after differs",
    )
    return before


def _unchanged_saved_command_input(saved, manifest):
    """Version4 preserves a separately selected whole present rolling input."""
    import copy

    selected = _selected_saved_command_metadata(manifest["saved_command_metadata"], 4)
    require(manifest["input_changes"] == [], "unchanged metadata cannot edit input")
    require(
        manifest["input_toml"]["before_sha256"]
        == manifest["input_toml"]["after_sha256"],
        "unchanged metadata raw input differs",
    )
    require(
        manifest["profile"]["saved_config_before_sha256"]
        == manifest["profile"]["saved_config_after_sha256"],
        "unchanged metadata saved digests differ",
    )
    require(
        "deploy" in saved and digest(saved["deploy"]) == digest(selected["before"]),
        "unchanged saved deployment metadata drift",
    )
    require(
        digest(saved) == manifest["profile"]["saved_config_before_sha256"],
        "unchanged saved configuration drift",
    )
    return copy.deepcopy(saved)


def prepare(evidence, manifest, manifest_sha256, image, commit, phase="before"):
    """Validate private pinned evidence; emit argv, never execute deployment.

    The manifest's admission and evidence collection are owned by the private
    target adapter. A matching hash is immutability evidence, not admission.
    Image-only scope deliberately refuses an auth/environment transition.
    """
    require(isinstance(manifest, dict), "missing admitted manifest")
    require(digest(manifest) == manifest_sha256, "manifest binding changed")
    require(
        set(manifest)
        == (
            {
                "version",
                "deployment_configuration",
                "canonical_sha256",
                "app",
                "environment",
                "profile",
                (
                    "input_changes"
                    if manifest.get("version") in (2, 3, 4)
                    else "idle_changes"
                ),
                "secrets_sha256",
                "volumes_sha256",
                "tool_version",
                "command",
                "packaging_gate",
            }
            | ({"input_toml"} if manifest.get("version") in (2, 3, 4) else set())
            | (
                {"saved_command_metadata"}
                if manifest.get("version") in (3, 4)
                else set()
            )
        )
        and type(manifest["version"]) is int
        and manifest["version"] in (1, 2, 3, 4),
        "unsupported manifest",
    )
    require(
        isinstance(evidence, dict)
        and set(evidence)
        == (
            {"canonical", "inventory", "saved", "secrets", "volumes", "tool_version"}
            | (
                {"provider_saved_json", "provider_saved_toml"}
                if manifest["version"] in (3, 4)
                else set()
            )
        ),
        "incomplete evidence",
    )
    canonical = evidence["canonical"]
    require(
        isinstance(canonical, dict)
        and canonical.get("entity_id") == manifest["deployment_configuration"]
        and isinstance(canonical.get("snapshot"), dict)
        and digest(canonical.get("snapshot")) == manifest["canonical_sha256"],
        "canonical target drift",
    )
    snapshot = canonical["snapshot"]
    require(
        snapshot.get("fly_app") == manifest["app"]
        and snapshot.get("environment") == manifest["environment"]
        and snapshot.get("region") == manifest["profile"]["region"],
        "canonical app/environment/region drift",
    )
    require(
        isinstance(evidence["saved"], dict)
        and evidence["saved"].get("app") == manifest["app"],
        "wrong saved app",
    )
    require(
        isinstance(snapshot.get("secret_names"), list)
        and all(isinstance(x, str) and x for x in snapshot["secret_names"])
        and isinstance(evidence["secrets"], list)
        and all(
            isinstance(x, dict)
            and set(x) == {"name", "status"}
            and isinstance(x["name"], str)
            and isinstance(x["status"], str)
            for x in evidence["secrets"]
        )
        and len({x["name"] for x in evidence["secrets"]}) == len(evidence["secrets"])
        and set(snapshot["secret_names"]) == {x["name"] for x in evidence["secrets"]},
        "canonical secret-name authority mismatch",
    )
    require(
        isinstance(evidence["volumes"], list)
        and all(isinstance(x, dict) for x in evidence["volumes"]),
        "invalid volume inventory",
    )
    require(
        digest(evidence["secrets"]) == manifest["secrets_sha256"],
        "secret metadata drift",
    )
    require(
        digest(evidence["volumes"]) == manifest["volumes_sha256"],
        "volume/encryption drift",
    )
    require(
        evidence["tool_version"] == manifest["tool_version"],
        "unreviewed installed tool",
    )
    require(
        isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit),
        "invalid commit",
    )
    if isinstance(manifest["profile"].get("source_state"), dict):
        require(
            snapshot.get("always_on") is False,
            "dynamic idle state not canonically admitted",
        )
        source_rows = [
            m
            for m in evidence["inventory"]
            if m.get("id") == manifest["profile"].get("source_id")
        ]
        require(len(source_rows) == 1, "ambiguous idle source")
        services = source_rows[0].get("config", {}).get("services")
        require(
            isinstance(services, list)
            and len(services) == 1
            and services[0].get("autostop") is True
            and services[0].get("autostart") is True
            and services[0].get("min_machines_running") == 0,
            "dynamic idle policy differs",
        )
    check_profiles(
        evidence["inventory"], evidence["saved"], manifest["profile"], image, phase
    )
    if manifest["version"] in (3, 4):
        normalized = (
            _saved_command_input(evidence["saved"], manifest, phase)
            if manifest["version"] == 3
            else _unchanged_saved_command_input(evidence["saved"], manifest)
        )
        import tomllib

        require(
            isinstance(evidence["provider_saved_toml"], str),
            "invalid provider saved TOML type",
        )
        try:
            provider_parsed = tomllib.loads(evidence["provider_saved_toml"])
        except (TypeError, tomllib.TOMLDecodeError):
            raise Refused("invalid provider saved TOML") from None
        require(
            digest(provider_parsed) == digest(evidence["saved"]),
            "provider TOML differs",
        )
        require(
            digest(evidence["provider_saved_json"])
            == digest(installed_config_projection(evidence["saved"])),
            "provider JSON projection differs",
        )
    else:
        normalized = (
            normalized_input(evidence["saved"], manifest)
            if phase == "before"
            else evidence["saved"]
        )
    # Pin both representations to the independently admitted running policy.
    # Only single-service idle corrections are supported; never guess a group.
    if input_changes(manifest):
        source = next(
            m
            for m in evidence["inventory"]
            if m["id"] == manifest["profile"]["source_id"]
        )
        services = source["config"].get("services")
        require(
            isinstance(services, list) and len(services) == 1, "ambiguous idle service"
        )
        policy = normalized["http_service"]
        require(
            type(services[0].get("autostop")) is bool
            and services[0]["autostop"]
            == (
                policy["auto_stop_machines"] is True
                or policy["auto_stop_machines"] == "stop"
            )
            and services[0].get("min_machines_running")
            == policy["min_machines_running"],
            "normalization differs from admitted machine",
        )
    if manifest["version"] in (2, 3, 4):
        raw_binding = manifest["input_toml"]
        require(
            isinstance(raw_binding, dict)
            and set(raw_binding) == {"before_sha256", "after_sha256"}
            and all(
                isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                for v in raw_binding.values()
            ),
            "invalid raw input binding",
        )
    command = manifest["command"]
    require(
        isinstance(command, dict)
        and set(command) == {"skip_release_command", "build_arguments", "release"}
        and type(command["skip_release_command"]) is bool
        and isinstance(command["build_arguments"], list)
        and all(isinstance(x, str) and x for x in command["build_arguments"]),
        "invalid command binding",
    )
    require(
        command["build_arguments"] == [],
        "unselected build arguments in immutable-image method",
    )
    check_release_contract(
        evidence["saved"] if manifest["version"] in (3, 4) else normalized, command
    )
    # The reviewed immutable-image method cannot smuggle flags via an app or ID.
    for value in (
        manifest["app"],
        manifest["profile"]["source_id"],
        manifest["profile"]["region"],
        *manifest["profile"]["retained_machines"],
    ):
        require(
            isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", value),
            "invalid command target",
        )
    result = {
        "normalized": normalized,
        "commit": commit,
        "scope": "PROFILE CHECK ONLY; packaging/tool/candidate gates precede command emission",
    }
    if manifest["version"] in (3, 4):
        result.update(
            {
                "provider_saved": evidence["saved"],
                "saved_command_metadata": manifest["saved_command_metadata"],
                "saved_config_before_sha256": manifest["profile"][
                    "saved_config_before_sha256"
                ],
                "saved_config_after_sha256": manifest["profile"][
                    "saved_config_after_sha256"
                ],
                "provider_saved_toml_sha256": hashlib.sha256(
                    evidence["provider_saved_toml"].encode("utf-8")
                ).hexdigest(),
                "provider_saved_json_sha256": digest(evidence["provider_saved_json"]),
            }
        )
        if manifest["version"] == 4:
            result["saved_config_actual_sha256"] = digest(evidence["saved"])
    return result


def check_projected_http_checks(saved, source_config):
    """Compare the selected single HTTP-service check projection exactly.

    Fly HTTPService.ToService/toMachineService adds type=http and retains
    ServiceHTTPCheck JSON field names. This is not a full machine projection
    or permission to normalize health policy; unselected service shapes refuse.
    """
    service = saved.get("http_service")
    require(isinstance(service, dict), "missing HTTP service projection")
    require(not saved.get("services"), "unselected additional service projection")
    services = source_config.get("services")
    require(
        isinstance(services, list)
        and len(services) == 1
        and isinstance(services[0], dict),
        "ambiguous HTTP service projection",
    )
    current = services[0]
    if "internal_port" in service:
        require(
            current.get("internal_port") == service["internal_port"],
            "HTTP service target drift",
        )
    selected = service.get("checks", [])
    actual = current.get("checks", [])
    require(
        isinstance(selected, list) and isinstance(actual, list),
        "unsupported HTTP checks",
    )
    fields = {
        "interval",
        "timeout",
        "grace_period",
        "method",
        "path",
        "protocol",
        "tls_skip_verify",
        "tls_server_name",
        "headers",
    }
    expected = []
    for check in selected:
        require(
            isinstance(check, dict)
            and set(check) <= fields
            and all(v is not None for v in check.values()),
            "unsupported HTTP check input",
        )
        projected = dict(check)
        projected["type"] = "http"
        headers = projected.get("headers")
        if headers is not None:
            require(
                isinstance(headers, dict)
                and all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in headers.items()
                ),
                "unsupported HTTP headers",
            )
            projected["headers"] = [
                {"name": k, "values": [v]} for k, v in sorted(headers.items())
            ]
            if not projected["headers"]:
                projected.pop("headers")
        expected.append(projected)
    actual = json.loads(json.dumps(actual, allow_nan=False))
    for check in actual:
        require(
            isinstance(check, dict)
            and set(check) <= fields | {"type"}
            and check.get("type") == "http"
            and all(v is not None for v in check.values()),
            "unsupported machine HTTP checks",
        )
        if "headers" in check:
            headers = check["headers"]
            require(
                isinstance(headers, list)
                and all(
                    isinstance(h, dict)
                    and set(h) == {"name", "values"}
                    and isinstance(h["name"], str)
                    and isinstance(h["values"], list)
                    and len(h["values"]) == 1
                    and isinstance(h["values"][0], str)
                    for h in headers
                )
                and len({h["name"] for h in headers}) == len(headers),
                "unsupported machine HTTP headers",
            )
            check["headers"] = sorted(headers, key=lambda h: h["name"])
            if not headers:
                check.pop("headers")
    require(actual == expected, "saved checks differ from admitted machine")
    return True


def check_release_contract(saved, command):
    """Bind saved presence/value and the independently admitted disposition.

    A required command needs a real pinned candidate gate, executed by the CLI.
    The separately reviewed manifest is authority; no pass/approval Boolean is
    accepted as a gate. Existing skip must be explicitly selected, never inferred.
    """
    release = command["release"]
    require(
        isinstance(release, dict)
        and set(release) == {"present", "command", "disposition", "gate"}
        and type(release["present"]) is bool,
        "missing release contract",
    )
    deploy = saved.get("deploy", {})
    require(isinstance(deploy, dict), "malformed saved deploy input")
    present = "release_command" in deploy
    value = deploy.get("release_command")
    require(
        present == release["present"]
        and value == release["command"]
        and (value is None if not present else isinstance(value, str)),
        "saved release presence/value differs",
    )
    disposition = release["disposition"]
    if disposition == "absent":
        require(
            value in (None, "")
            and release["gate"] is None
            and command["skip_release_command"] is False,
            "release absence/skip differs",
        )
    elif disposition == "existing_skip":
        require(
            present
            and isinstance(value, str)
            and value.strip()
            and command["skip_release_command"] is True
            and release["gate"] is None,
            "unbound existing release skip",
        )
    elif disposition == "required":
        gate = release["gate"]
        require(
            present
            and isinstance(value, str)
            and value.strip()
            and command["skip_release_command"] is False
            and isinstance(gate, dict)
            and set(gate) == {"path", "sha256", "inputs", "command_sha256"}
            and gate["command_sha256"] == hashlib.sha256(value.encode()).hexdigest(),
            "required release gate missing or mismatched",
        )
    else:
        raise Refused("unknown release disposition")
    return release


# Source: flyctl v0.4.112 ca63052e, Config.Flatten/updateMachineConfig and
# launchInputForUpdate. This intentionally supports a closed ordinary app shape.
# Unselected advanced inputs refuse; a separately pinned pair is not equality.
FLY_PROJECTION_COMMIT = "ca63052e2526df073e9a1a4ad57bcbe5892f0543"


def check_complete_projection(
    saved,
    source_config,
    tool_version,
    *,
    saved_command_metadata=None,
    saved_command_metadata_version=3,
):
    """Require the supported installed Fly update to preserve ALL static fields.

    Input normalization happens elsewhere. This function neither corrects a
    mismatching input nor learns authority from it. Native image/release changes
    are the only fields omitted by stable_source; everything else is compared.
    """
    import copy

    require(
        isinstance(tool_version, str)
        and re.fullmatch(
            r"fly v0\.4\.112 [^ ]+ Commit: "
            + FLY_PROJECTION_COMMIT
            + r" BuildDate: [^ ]+",
            tool_version,
        ),
        "unsupported installed projection",
    )
    require(
        isinstance(saved, dict)
        and set(saved)
        <= {
            "app",
            "primary_region",
            "env",
            "http_service",
            "vm",
            "restart",
            "mounts",
            "deploy",
        },
        "unsupported saved machine input",
    )
    require(isinstance(source_config, dict), "invalid machine projection")
    # Fields the installed update explicitly preserves are copied below. Unknown
    # native fields/containers/standbys refuse rather than guessing their effects.
    require(
        set(source_config)
        <= {
            "image",
            "env",
            "init",
            "guest",
            "metadata",
            "mounts",
            "services",
            "metrics",
            "checks",
            "statics",
            "files",
            "restart",
            "stop_config",
            "schedule",
            "auto_destroy",
            "dns",
            "processes",
            "rootfs",
            "cache_drive",
            "spot",
            "size",
            "disable_machine_autostart",
        },
        "unsupported native machine shape",
    )
    deploy = saved.get("deploy", {})
    if saved_command_metadata is not None:
        _selected_saved_command_metadata(
            saved_command_metadata, saved_command_metadata_version
        )
    require(
        isinstance(deploy, dict)
        and set(deploy)
        <= ({"strategy"} if saved_command_metadata is not None else {"release_command"})
        and (
            "strategy" not in deploy
            or (saved_command_metadata is not None and deploy["strategy"] == "rolling")
        )
        and all(isinstance(v, str) for v in deploy.values()),
        "unsupported deploy input",
    )
    expected = copy.deepcopy(source_config)
    env = saved.get("env", {})
    require(
        isinstance(env, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()),
        "unsupported environment input",
    )
    region = saved.get("primary_region", "")
    require(isinstance(region, str), "unsupported primary region")
    expected["env"] = {**env, "FLY_PROCESS_GROUP": "app"}
    if region:
        expected["env"]["PRIMARY_REGION"] = region
    metadata = expected.get("metadata", {})
    require(isinstance(metadata, dict), "unsupported native metadata")
    expected["metadata"] = {
        **metadata,
        "fly_flyctl_version": "0.4.112",
        "fly_platform_version": "v2",
        "fly_process_group": "app",
    }
    init = expected.get("init", {})
    require(
        isinstance(init, dict)
        and set(init)
        <= {
            "cmd",
            "entrypoint",
            "exec",
            "swap_size_mb",
            "tty",
            "kernel_args",
        },
        "unsupported native init",
    )
    for field in ("cmd", "entrypoint", "exec", "swap_size_mb"):
        init.pop(field, None)
    expected["init"] = init
    # updateMachineConfig clears these when no selected saved input supplies them.
    for field in ("metrics", "checks", "statics", "files", "stop_config"):
        expected.pop(field, None)
    service = saved.get("http_service")
    require(
        isinstance(service, dict)
        and set(service)
        <= {
            "internal_port",
            "force_https",
            "auto_stop_machines",
            "auto_start_machines",
            "min_machines_running",
            "processes",
            "concurrency",
            "checks",
        },
        "unsupported HTTP service input",
    )
    require(
        service.get("processes", ["app"]) in ([], ["app"]),
        "unsupported process selection",
    )
    require(
        type(service.get("internal_port")) is int
        and 0 < service["internal_port"] <= 65535,
        "unsupported service port",
    )
    force = service.get("force_https", False)
    require(type(force) is bool, "unsupported HTTPS flag")
    projected = {
        "protocol": "tcp",
        "internal_port": service["internal_port"],
        "ports": [
            {"port": 80, "handlers": ["http"]},
            {"port": 443, "handlers": ["http", "tls"]},
        ],
        "force_instance_key": None,
    }
    if force:
        projected["ports"][0]["force_https"] = True
    for inp, native in (
        ("auto_stop_machines", "autostop"),
        ("auto_start_machines", "autostart"),
        ("min_machines_running", "min_machines_running"),
    ):
        if inp in service:
            value = service[inp]
            if inp == "auto_stop_machines":
                require(
                    type(value) is bool or value in ("off", "stop", "suspend"),
                    "unknown idle projection",
                )
                value = (value == "stop") if value in ("off", "stop") else value
            elif inp == "auto_start_machines":
                require(type(value) is bool, "invalid start projection")
            else:
                require(type(value) is int and value >= 0, "invalid minimum projection")
            projected[native] = value
    if "concurrency" in service:
        concurrency = service["concurrency"]
        require(
            isinstance(concurrency, dict)
            and set(concurrency) <= {"type", "hard_limit", "soft_limit"}
            and concurrency.get("type", "") in ("", "connections", "requests")
            and all(
                type(v) is int and v >= 0 for k, v in concurrency.items() if k != "type"
            ),
            "unsupported concurrency",
        )
        projected["concurrency"] = {k: v for k, v in concurrency.items() if v}
    check_projected_http_checks(saved, source_config)
    checks = copy.deepcopy(service.get("checks", []))
    for check in checks:
        check["type"] = "http"
        if "headers" in check:
            headers = check.pop("headers")
            if headers:
                check["headers"] = [
                    {"name": k, "values": [v]} for k, v in sorted(headers.items())
                ]
    if checks:
        projected["checks"] = checks
    expected["services"] = [projected]
    # Go map iteration changes header ordering but not header identity/value.
    actual = copy.deepcopy(source_config)
    for native in actual.get("services", []):
        for check in native.get("checks", []):
            if "headers" in check:
                check["headers"] = sorted(check["headers"], key=lambda h: h["name"])
    vm = saved.get("vm", [])
    require(isinstance(vm, list) and len(vm) <= 1, "unsupported compute selection")
    if vm:
        guest = vm[0]
        require(
            isinstance(guest, dict)
            and set(guest) <= {"memory", "memory_mb", "cpu_kind", "cpus", "processes"}
            and guest.get("processes", ["app"]) in ([], ["app"]),
            "unsupported compute input",
        )
        require(
            not ("memory" in guest and "memory_mb" in guest), "ambiguous compute memory"
        )
        memory = 256  # native DefaultVMSize=shared-cpu-1x
        if "memory" in guest:
            require(isinstance(guest["memory"], str), "unsupported memory encoding")
            match = re.fullmatch(r"([1-9][0-9]*)(MB|GB)", guest["memory"], re.I)
            require(match is not None, "unsupported memory encoding")
            memory = int(match[1]) * (1024 if match[2].upper() == "GB" else 1)
        if "memory_mb" in guest:
            require(
                type(guest["memory_mb"]) is int and guest["memory_mb"] > 0,
                "invalid compute memory",
            )
            memory = guest["memory_mb"]
        cpus, kind = guest.get("cpus", 1), guest.get("cpu_kind", "shared")
        require(
            type(cpus) is int and cpus > 0 and kind in ("shared", "performance"),
            "unsupported compute CPU",
        )
        expected["guest"] = {"cpu_kind": kind, "cpus": cpus, "memory_mb": memory}
    restart = saved.get("restart", [])
    require(
        isinstance(restart, list) and len(restart) <= 1, "unsupported restart selection"
    )
    expected.pop("restart", None)
    if restart:
        rule = restart[0]
        require(
            isinstance(rule, dict)
            and set(rule) <= {"policy", "retries", "processes"}
            and rule.get("processes", ["app"]) in ([], ["app"])
            and rule.get("policy") in ("always", "on-failure", "never"),
            "unsupported restart input",
        )
        expected["restart"] = {
            "policy": "no" if rule["policy"] == "never" else rule["policy"]
        }
        if "retries" in rule:
            require(
                type(rule["retries"]) is int and rule["retries"] >= 0,
                "invalid restart retries",
            )
            if rule["retries"]:
                expected["restart"]["max_retries"] = rule["retries"]
    mounts = saved.get("mounts", [])
    old = source_config.get("mounts", [])
    require(
        isinstance(mounts, list)
        and isinstance(old, list)
        and len(mounts) == len(old)
        and len(mounts) <= 1,
        "mount replacement unsupported",
    )
    expected.pop("mounts", None)
    if mounts:
        mount = mounts[0]
        require(
            isinstance(mount, dict)
            and set(mount) == {"source", "destination"}
            and all(isinstance(v, str) and v for v in mount.values())
            and isinstance(old[0], dict)
            and old[0].get("name")
            and old[0]["name"] == mount["source"]
            and old[0].get("path") == mount["destination"]
            and isinstance(old[0].get("volume"), str)
            and old[0]["volume"],
            "unproved mount identity/path",
        )
        expected["mounts"] = copy.deepcopy(old)
        for field in ("extend_threshold_percent", "add_size_gb", "size_gb_limit"):
            expected["mounts"][0].pop(field, None)
    require(
        digest(stable_source(expected)) == digest(stable_source(actual)),
        "saved input changes complete admitted machine profile",
    )
    return {
        "source_commit": FLY_PROJECTION_COMMIT,
        "complete_static_match": True,
        "supported_shape": "single-app-http-image-only",
    }


def _deployment_argv(manifest, image):
    """Internal serialization; the preparation CLI owns all preceding gates."""
    command = manifest["command"]
    argv = [
        "fly",
        "deploy",
        "--app",
        manifest["app"],
        "--config",
        "<owned-private-config>",
        "--image",
        image,
        "--primary-region",
        manifest["profile"]["region"],
        "--ha=false",
        "--strategy",
        "rolling",
        "--only-machines",
        manifest["profile"]["source_id"],
        "--update-only",
        "--no-public-ips",
        "--deploy-retries",
        "0",
    ]
    if manifest["profile"]["retained_machines"]:
        argv.extend(
            [
                "--exclude-machines",
                ",".join(sorted(manifest["profile"]["retained_machines"])),
            ]
        )
    if command["skip_release_command"]:
        argv.append("--skip-release-command")
    return argv


def render_normalized_toml(
    raw_text, saved, normalized, changes, *, with_edits=False, auth_insertions=()
):
    """Replace only selected scalar spans; retain comments/newlines and all else.

    The closed shape admits one absent retry key insertion at the end of its
    selected restart table. Every rendered byte span is returned to the private
    receipt when requested; reparsing must match the independently pinned result.
    """
    import tomllib

    try:
        parsed = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise Refused("invalid saved TOML") from exc
    require(parsed == saved, "saved TOML does not match evidence")
    allowed_auth = frozenset(
        ("env", name)
        for name in (
            "THEODORE_AUTH_MODE",
            "THEODORE_OIDC_ORIGIN",
            "THEODORE_OIDC_ENVIRONMENT",
            "THEODORE_OIDC_MACHINE_ID",
        )
    )
    require(
        isinstance(auth_insertions, (tuple, frozenset))
        and set(auth_insertions) <= allowed_auth,
        "unselected auth insertion path",
    )
    wanted = {tuple(c["path"]): c for c in changes}
    require(len(wanted) == len(changes), "duplicate correction path")
    section = ()
    offsets = 0
    edits = []
    hits = set()
    table_ends = {}
    table_indents = {}
    seen_tables = set()
    scalar = re.compile(
        r"([ \t]*)([A-Za-z_][A-Za-z0-9_-]*)([ \t]*=[ \t]*)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|true|false|[-+]?[0-9]+)([ \t]*(?:#[^\r\n]*)?)(\r?\n)?$"
    )
    for line in raw_text.splitlines(keepends=True):
        header = re.fullmatch(
            r"[ \t]*(\[\[?)([A-Za-z_][A-Za-z0-9_.]*)(\]\]?)[ \t]*(?:#[^\r\n]*)?(?:\r?\n)?",
            line,
        )
        if header:
            table_ends[section] = offsets
            names = tuple(header[2].split("."))
            if header[1] == "[[" and header[3] == "]]":
                require(names not in seen_tables, "unselected repeated array table")
                seen_tables.add(names)
                section = names + (0,)
            else:
                require(header[1] == "[" and header[3] == "]", "invalid table encoding")
                section = names
        else:
            match = scalar.fullmatch(line)
            if match:
                table_indents[section] = match[1]
                path = section + (match[2],)
                if path in wanted:
                    c = wanted[path]
                    require(
                        c.get("before_present", True) is True and path not in hits,
                        "scalar presence drift",
                    )
                    hits.add(path)
                    edits.append(
                        {
                            "path": list(path),
                            "start": offsets + match.start(4),
                            "end": offsets + match.end(4),
                            "before": match[4],
                            "after": json.dumps(c["after"], ensure_ascii=True),
                        }
                    )
        offsets += len(line)
    table_ends[section] = offsets
    for path, c in wanted.items():
        if path not in hits:
            require(
                (path == ("restart", 0, "retries") or path in auth_insertions)
                and c.get("before_present") is False,
                "selected scalar not rendered",
            )
            require(path[:-1] in table_ends, "missing insertion table")
            position = table_ends[path[:-1]]
            require(
                position == 0 or raw_text[position - 1] == "\n",
                "missing insertion newline",
            )
            newline = "\r\n" if "\r\n" in raw_text else "\n"
            edits.append(
                {
                    "path": list(path),
                    "start": position,
                    "end": position,
                    "before": "",
                    "after": table_indents.get(path[:-1], "")
                    + path[-1]
                    + " = "
                    + json.dumps(c["after"], ensure_ascii=True)
                    + newline,
                }
            )
            hits.add(path)
    require(hits == set(wanted), "correction not rendered")
    edits.sort(key=lambda e: (e["start"], e["end"]))
    require(
        all(a["end"] <= b["start"] for a, b in zip(edits, edits[1:])),
        "overlapping correction spans",
    )
    rendered = raw_text
    for edit in reversed(edits):
        require(
            raw_text[edit["start"] : edit["end"]] == edit["before"], "raw span changed"
        )
        rendered = rendered[: edit["start"]] + edit["after"] + rendered[edit["end"] :]
    require(tomllib.loads(rendered) == normalized, "TOML correction altered profile")
    byte_edits = [
        {
            **edit,
            "start": len(raw_text[: edit["start"]].encode("utf-8")),
            "end": len(raw_text[: edit["end"]].encode("utf-8")),
        }
        for edit in edits
    ]
    return (rendered, byte_edits) if with_edits else rendered


def installed_config_projection(config):
    """Fly's reviewed MachineAutostop JSON encoding: off/stop -> false/true.

    Only this named representation is projected. Full surrounding configuration
    must remain exact, including suspend/unknown values which cannot be learned.
    """
    import copy

    result = copy.deepcopy(config)
    service = result.get("http_service")
    if isinstance(service, dict) and service.get("auto_stop_machines") in (
        "off",
        "stop",
    ):
        service["auto_stop_machines"] = service["auto_stop_machines"] == "stop"
    return result

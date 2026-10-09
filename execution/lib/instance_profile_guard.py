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


def normalized_input(saved, manifest):
    """Apply only the separately pinned idle correction, never learn a policy."""
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
        == {
            "version",
            "deployment_configuration",
            "canonical_sha256",
            "app",
            "environment",
            "profile",
            "idle_changes",
            "secrets_sha256",
            "volumes_sha256",
            "tool_version",
            "command",
            "packaging_gate",
        }
        and type(manifest["version"]) is int
        and manifest["version"] == 1,
        "unsupported manifest",
    )
    require(
        isinstance(evidence, dict)
        and set(evidence)
        == {"canonical", "inventory", "saved", "secrets", "volumes", "tool_version"},
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
    normalized = (
        normalized_input(evidence["saved"], manifest)
        if phase == "before"
        else evidence["saved"]
    )
    # Pin both representations to the independently admitted running policy.
    # Only single-service idle corrections are supported; never guess a group.
    if manifest["idle_changes"]:
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
    command = manifest["command"]
    require(
        isinstance(command, dict)
        and set(command) == {"skip_release_command", "build_arguments"}
        and type(command["skip_release_command"]) is bool
        and isinstance(command["build_arguments"], list)
        and all(isinstance(x, str) and x for x in command["build_arguments"]),
        "invalid command binding",
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
    return {
        "normalized": normalized,
        "commit": commit,
        "scope": "PROFILE CHECK ONLY; packaging/tool/candidate gates precede command emission",
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


def render_normalized_toml(raw_text, saved, normalized, changes):
    """Preserve every input byte except the two pinned scalar values."""
    import tomllib

    require(tomllib.loads(raw_text) == saved, "saved TOML does not match evidence")
    lines = raw_text.splitlines(keepends=True)
    active = False
    hits = set()
    by_key = {c["path"][1]: c for c in changes}
    for index, line in enumerate(lines):
        if line.strip().startswith("["):
            active = line.strip() == "[http_service]"
        if active:
            match = re.fullmatch(r"(\s*)([A-Za-z_]+)(\s*=\s*)([^\r\n]*)(\r?\n)?", line)
            if match and match[2] in by_key:
                key = match[2]
                require(key not in hits, "duplicate idle key")
                hits.add(key)
                value = json.dumps(by_key[key]["after"], ensure_ascii=True)
                lines[index] = match[1] + key + match[3] + value + (match[5] or "")
    require(hits == set(by_key), "idle key not rendered")
    rendered = "".join(lines)
    require(tomllib.loads(rendered) == normalized, "TOML correction altered profile")
    return rendered


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

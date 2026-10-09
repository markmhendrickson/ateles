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
            for k in ("source_id", "source_state", "region", "image_repository")
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
    require(source.get("state") == baseline["source_state"], "source state drift")
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
            source.get("image_ref", {}).get("digest") == image.split("@", 1)[1],
            "source image drift",
        )
    return True

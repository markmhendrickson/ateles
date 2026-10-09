"""Complete auth phase readbacks, without deployment command admission.

The independently selected record pins full before/after artifacts and phase
readbacks. The existing deployment CLI still owns packaging, release, local
parser and command emission. Passing this preflight never substitutes for it.
"""

from execution.lib.instance_auth_profile_guard import (
    AUTH_PHASES,
    AUTH_SECRET_NAMES,
    _closed,
    _secret_rows,
    check_auth_phase,
    expected_auth_config,
)
from execution.lib.instance_profile_guard import digest, require, stable_source


def check_phase_evidence(packet, current, manifest, phase):
    _closed(
        packet,
        {"before", "after", "phase_sha256", "before_protected_fields"},
        "invalid full auth profile packet",
    )
    _closed(packet["phase_sha256"], AUTH_PHASES, "incomplete phase evidence pins")
    require(phase in AUTH_PHASES, "unknown auth phase")
    require(
        digest(current) == packet["phase_sha256"][phase],
        "selected phase evidence drift",
    )
    before, after = packet["before"], packet["after"]
    fields = {"inventory", "saved", "secrets", "volumes"}
    for evidence in (before, after, current):
        _closed(evidence, fields, "incomplete whole auth evidence")
        require(
            isinstance(evidence["inventory"], list)
            and len(evidence["inventory"]) == 1
            and isinstance(evidence["inventory"][0], dict)
            and evidence["inventory"][0].get("id") == manifest["binding"]["machine_id"],
            "auth singleton machine differs",
        )
        _secret_rows(evidence["secrets"])
        require(isinstance(evidence["volumes"], list), "invalid volume evidence")
    profiles = manifest["profiles"]
    source_before = before["inventory"][0]
    source_after = after["inventory"][0]
    require(
        digest(before["saved"]) == profiles["before_config_sha256"]
        and digest(after["saved"]) == profiles["after_config_sha256"]
        and digest(stable_source(source_before.get("config")))
        == profiles["before_static_sha256"]
        and digest(stable_source(source_after.get("config")))
        == profiles["after_static_sha256"]
        and digest(before["secrets"]) == profiles["before_secret_metadata_sha256"],
        "full before/after profile pin differs",
    )
    binding = manifest["binding"]
    require(
        after["saved"] == expected_auth_config(before["saved"], binding)
        and stable_source(source_after["config"])
        == stable_source(expected_auth_config(source_before["config"], binding)),
        "nonauth expected-after profile changed",
    )
    require(
        before["volumes"] == after["volumes"] == current["volumes"],
        "auth volume inventory changed",
    )

    # Retain every provider row field. Only complete config and immutable image
    # identity are compared separately; state/region/launch fields stay exact.
    def row_static(row):
        return {k: v for k, v in row.items() if k not in ("config", "image_ref")}

    require(
        row_static(source_before) == row_static(source_after), "machine row changed"
    )
    deployed = phase == "auth_deploy_after"
    expected = after if deployed else before
    actual_source = current["inventory"][0]
    selected_source = expected["inventory"][0]
    require(
        row_static(actual_source) == row_static(selected_source)
        and stable_source(actual_source.get("config"))
        == stable_source(selected_source.get("config")),
        "current complete machine profile differs",
    )
    require(current["saved"] == expected["saved"], "current saved profile differs")
    if deployed:
        image = manifest["candidate"]["image"]
        require(
            actual_source.get("image_ref") == source_after.get("image_ref")
            and isinstance(actual_source.get("image_ref"), dict)
            and actual_source["image_ref"].get("digest") == image.split("@", 1)[1]
            and actual_source["config"].get("image") == image,
            "auth deployed image differs",
        )
    else:
        require(
            actual_source.get("image_ref") == source_before.get("image_ref")
            and actual_source["config"].get("image")
            == source_before["config"].get("image"),
            "auth staging changed image",
        )
    old = _secret_rows(before["secrets"])
    before_refs = packet["before_protected_fields"]
    _closed(
        before_refs, set(old) & AUTH_SECRET_NAMES, "old protected membership differs"
    )
    # Before reading credentials, validate all phase metadata. This exact set
    # will subsequently be checked against the protected consumer's result.
    phase_result = check_auth_phase(
        before["saved"],
        after["saved"],
        current["saved"],
        before["secrets"],
        current["secrets"],
        binding,
        phase,
        verified_continuity_names=tuple(before_refs),
    )
    return {
        **phase_result,
        "old_protected_names": sorted(before_refs),
        "old_versions_exposed": sorted(
            name for name in before_refs if "version" in old[name]
        ),
        "whole_machine_profile_matches": True,
        "volume_inventory_matches": True,
    }

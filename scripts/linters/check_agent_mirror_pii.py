#!/usr/bin/env python3
"""
check_agent_mirror_pii.py — refuse to publish operator/third-party specifics
into a public agent-prompt mirror.

WHY THIS EXISTS (and why it is NOT redundant with gitleaks or
check_hardcoded_config.py):

  .gitleaks.toml guards third-party PII with STRUCTURAL rules (email shape,
  IBAN shape, phone shape, national-ID shape) — but it deliberately allowlists
  the entire `.claude/` tree ("env var names in skill docs, not secrets"),
  which is exactly where a rendered agent prompt or SKILL.md mirror lives.
  It also has no rule for a Bitcoin address, which has no fixed prefix/length
  gitleaks' default ruleset recognizes as PII.

  check_hardcoded_config.py guards operator-specific literals baked into
  DAEMON/RUNTIME CODE (lib/, execution/daemons/, execution/scripts/) — it does
  not scan `.claude/skills/` or `docs/agents/`, the generated PUBLIC mirrors of
  Neotoma `agent_definition.prompt_markdown`.

  Neither one is the control agent_policy `ent_c3c5e4a9350250cbf69e08bf` and
  `ent_f2e21d651669c24183b2b4eb` require: "agent prompts are always public and
  PII-free" / "a prompt states what the agent does; specifics resolve from
  context entities at runtime." This linter is that control, mechanically
  enforced at the one boundary those policies describe in prose: the rendered
  file that ships to the public repo.

  Motivated by a live near-miss: an agent was dispatched to render active
  Neotoma `standing_rule` entities into `.claude/skills/ateles/SKILL.md`
  (public repo). It flagged operator PII as a blocker before writing anything
  and the task was redirected for unrelated reasons — so nothing leaked. This
  linter is the mechanical backstop for the next time a renderer is not so
  careful, per docs/foundation/principles.md#1 ("a mechanism that does not
  bind is not a control") and ateles PR #1092, which made a sibling mirror
  check (`render_agent_docs.py --check`) binding in `scripts/lint.sh` instead
  of `|| true` for the same reason.

WHAT IT CHECKS, on every file in scope:

  1. STRUCTURAL: a Bitcoin address (bech32 `bc1...` or base58 `1.../3...`) —
     content-agnostic, catches an address gitleaks' default ruleset does not.
  2. SEMANTIC, LIVE-SOURCED: a value gitleaks cannot detect by shape because
     it has none — a payee's name, a vendor's business name, an IBAN
     recipient name, a health provider name, a BTC address stored on a
     `contact`/`payment_profile`. Read at lint time from Neotoma via the SAME
     env resolution and REST call `render_agent_docs.py` already uses
     (`_load_env` / `_request`, imported directly rather than reimplemented —
     see docs/foundation's "extend, don't parallel-build" discipline) so this
     linter can never disagree with the renderer about how to reach Neotoma.

FAIL-CLOSED — and where this deliberately DIFFERS from
validate_tool_allowlist.py's precedent:

  validate_tool_allowlist.py SKIPS (exit 0) when Neotoma is unreachable,
  reasoned there as "an infra gap, not a grant-grammar defect." That is right
  for a linter validating entities it already fetched — nothing NEW needs
  validating if the fetch failed, and the last commit was already checked
  when Neotoma was up.

  This linter is different in kind: it is a content gate on files ABOUT TO BE
  COMMITTED to a public repo. If it silently skipped on a Neotoma outage, a
  PR containing a real BTC address or payee name would merge with a green
  check during exactly the window this control exists for. So: Neotoma
  unreachable EXITS NON-ZERO (UNVERIFIED), not 0. The structural (BTC address)
  check has no such dependency and always runs regardless.
  Use --allow-unverified only for a machine deliberately offline (mirrors the
  explicit, NAMED NEOTOMA_BASE_URL-unset skip already used for
  validate_tool_allowlist.py in scripts/lint.sh — never a bare `|| true`).

  A value field that is present but not a string, or a visibility field with
  an unrecognized value, is treated as PRIVATE (scanned), not skipped.

SCOPE: .claude/skills/**/SKILL.md, docs/agents/*.md — the generated PUBLIC
mirrors of agent prompts. Different scope from check_agent_roster.py (runtime
routing code) and check_hardcoded_config.py (daemon code).

SUPPRESSION: append `<!-- agent-mirror-pii-ok: <reason> -->` to a line that
legitimately contains a matched string with no operator-specific meaning (a
worked example, a public documentation address). Use sparingly — the correct
fix for a real finding is removing the specific from Neotoma
`agent_definition.prompt_markdown` and pointing the prompt at a context
entity instead (agent_policy ent_f2e21d651669c24183b2b4eb).

Usage:
  python3 scripts/linters/check_agent_mirror_pii.py [file1 file2 ...]
  # no args -> scans the default mirror scope
  python3 scripts/linters/check_agent_mirror_pii.py --allow-unverified
  # skip (not fail) the semantic check when Neotoma is unreachable
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "execution" / "scripts"))
import render_agent_docs  # noqa: E402  — reuse its Neotoma env/fetch, not a copy

SUPPRESS = "agent-mirror-pii-ok"

DEFAULT_GLOBS = (
    ".claude/skills/**/SKILL.md",
    "docs/agents/*.md",
)

# Bitcoin address shapes gitleaks' default ruleset and .gitleaks.toml do not
# cover. Anchored to word boundaries so a longer alphanumeric token (a hash,
# a UUID) is not mistaken for an address; bech32 min length matches mainnet
# P2WPKH (42 chars) through P2TR/longer, base58 covers P2PKH ("1...") and
# P2SH ("3...").
BTC_ADDRESS_RE = re.compile(
    r"\b(?:bc1[ac-hj-np-z02-9]{25,90}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b"
)

NEOTOMA_ENTITY_TYPES_FOR_SEMANTIC_CHECK = ("contact", "payment_profile")

# Fields per entity type whose VALUE, if it appears verbatim in a mirror, is a
# finding. Deliberately excludes fields whose value doubles as a common English
# word or generic label (e.g. contact.contact_type="personal") to keep the
# false-positive rate low; a name, address, phone, IBAN, or amount has no such
# collision risk.
SENSITIVE_FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "contact": ("name", "full_name", "phone", "email", "btc_address", "address"),
    "payment_profile": (
        "wise_recipient_name",
        "wise_iban",
        "btc_address",
        "label",
    ),
}


def _iter_scope_files(args: list[str]) -> list[Path]:
    if args:
        return [Path(a) for a in args]
    files: list[Path] = []
    for pattern in DEFAULT_GLOBS:
        files.extend(sorted(REPO_ROOT.glob(pattern)))
    return files


def _is_suppressed(line: str) -> bool:
    return SUPPRESS in line


def _check_structural(path: Path, text: str) -> list[str]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _is_suppressed(line):
            continue
        for m in BTC_ADDRESS_RE.finditer(line):
            findings.append(
                f"{path}:{lineno}: BTC address literal ({m.group(0)[:10]}…) — "
                f"move the address into a `payment_profile`/`contact` entity "
                f"and have the prompt resolve it at runtime "
                f"(agent_policy ent_f2e21d651669c24183b2b4eb)"
            )
    return findings


def _fetch_sensitive_values() -> list[tuple[str, str]] | None:
    """Return [(value, provenance_label), ...] for private contact/payment
    values live from Neotoma, or None if Neotoma could not be reached/parsed
    (caller must treat None as UNVERIFIED, never as "no findings"). Uses
    render_agent_docs._load_env / _request directly — same env resolution
    (NEOTOMA_BASE_URL/NEOTOMA_BEARER_TOKEN, falling back to
    ~/.config/neotoma/.env) and same REST route (POST /entities/query) the
    renderer itself uses, so this linter can never disagree with it about how
    to reach Neotoma."""
    try:
        base_url, token = render_agent_docs._load_env()
    except SystemExit:
        return None
    if not token:
        return None

    values: list[tuple[str, str]] = []
    for entity_type in NEOTOMA_ENTITY_TYPES_FOR_SEMANTIC_CHECK:
        try:
            data = render_agent_docs._request(
                f"{base_url}/entities/query",
                token,
                {"entity_type": entity_type, "limit": 200},
            )
        except SystemExit:
            return None

        entities = data.get("entities") or data.get("results")
        if not isinstance(entities, list):
            return None

        fields = SENSITIVE_FIELDS_BY_TYPE.get(entity_type, ())
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            snapshot = render_agent_docs._unwrap(entity)
            if not isinstance(snapshot, dict):
                continue
            # FAIL-CLOSED: an entity with no visibility field, or a value that
            # is not the string "public", is treated as private and eligible
            # for the scan. Only an explicit visibility=="public" is excluded.
            visibility = snapshot.get("visibility")
            if isinstance(visibility, str) and visibility.strip().lower() == "public":
                continue
            eid = entity.get("entity_id") or snapshot.get("entity_id", "?")
            for field in fields:
                value = snapshot.get(field)
                # MIN LENGTH 4, not 3: a short given name ("Ari", "Oli") is a
                # frequent SUBSTRING of ordinary English prose ("di-ARI-ze",
                # "p-OLI-cy") even under word-boundary matching applied to
                # multi-word values below — for a bare single token this linter
                # additionally requires the match to be its own whole word (see
                # _check_semantic), but a single 3-char token is still too
                # collision-prone to be worth the noise. Names/labels below
                # this length are not scanned; a real leak of that shape is
                # vanishingly unlikely to be the ONLY signal of a leak (an
                # address, IBAN, or fuller name would also be present).
                if isinstance(value, str) and len(value.strip()) >= 4:
                    values.append((value.strip(), f"{entity_type}:{field}:{eid}"))
    return values


# Matches gitleaks'-style structural values (no natural-language collision
# risk): IBANs, BTC addresses, phone numbers. These are matched as plain
# substrings because their shape cannot appear accidentally inside prose.
_STRUCTURAL_FIELD_SUFFIXES = ("iban", "btc_address", "phone")


def _check_semantic(
    path: Path, text: str, sensitive_values: list[tuple[str, str]]
) -> list[str]:
    findings = []
    lines = text.splitlines()
    for value, provenance in sensitive_values:
        is_structural = any(
            provenance.split(":")[1] == suffix
            or provenance.split(":")[1].endswith(f"_{suffix}")
            for suffix in _STRUCTURAL_FIELD_SUFFIXES
        )
        if is_structural:
            pattern = re.compile(re.escape(value.lower()))
        else:
            # WORD-BOUNDARY match for names/labels: a bare substring match let
            # a 3-4 char given name match inside an unrelated word (e.g. a
            # contact named "Ari" matched inside "RECORD_MEETING_DIARIZE").
            # \b anchors on both ends so the value must appear as its own
            # token (or the first/last token of a multi-word value), not
            # embedded inside a longer identifier or word.
            pattern = re.compile(r"\b" + re.escape(value.lower()) + r"\b")
        for lineno, line in enumerate(lines, start=1):
            if _is_suppressed(line):
                continue
            if pattern.search(line.lower()):
                findings.append(
                    f"{path}:{lineno}: operator-specific value from "
                    f"{provenance} appears verbatim — move the specific into "
                    f"the context entity and have the prompt resolve it at "
                    f"runtime (agent_policy ent_f2e21d651669c24183b2b4eb), "
                    f"never inline it in a public mirror"
                )
    return findings


def main() -> int:
    args = sys.argv[1:]
    allow_unverified = "--allow-unverified" in args
    args = [a for a in args if a != "--allow-unverified"]

    files = _iter_scope_files(args)
    if not files:
        print("check_agent_mirror_pii: no files in scope, nothing to check")
        return 0

    all_findings: list[str] = []
    texts: dict[Path, str] = {}
    for path in files:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            all_findings.append(
                f"{path}: could not read file ({exc}) — treating as failing"
            )
            continue
        texts[path] = text
        all_findings.extend(_check_structural(path, text))

    sensitive_values = _fetch_sensitive_values()
    if sensitive_values is None:
        if allow_unverified:
            print(
                "check_agent_mirror_pii: Neotoma unreachable — semantic check "
                "SKIPPED (--allow-unverified). Structural (BTC address) check "
                "still ran."
            )
        else:
            all_findings.append(
                "semantic PII check UNVERIFIED: could not reach Neotoma "
                "(NEOTOMA_BASE_URL/NEOTOMA_BEARER_TOKEN) to load known "
                "operator-specific values. Failing closed — a rule with no "
                "enforcement is not the same as a rule that passed "
                "(docs/foundation/principles.md#1). Re-run with "
                "--allow-unverified only on a deliberately offline host."
            )
    else:
        for path, text in texts.items():
            all_findings.extend(_check_semantic(path, text, sensitive_values))

    if all_findings:
        print("check_agent_mirror_pii: FAILED")
        for finding in all_findings:
            print(f"  {finding}")
        return 1

    verified = (
        "semantic check verified live"
        if sensitive_values is not None
        else "semantic check skipped"
    )
    print(f"check_agent_mirror_pii: OK — {len(texts)} file(s) scanned, {verified}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

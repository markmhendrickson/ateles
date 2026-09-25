#!/usr/bin/env python3
"""Direct tests for `render_rule_inventory.screen_for_pii` / `safe_statement`.

Both repos are PUBLIC (module docstring, "Public-data posture"), and the
inventory's entire safety argument rests on these two functions: `safe_statement`
is what stands between an entity's raw text and a committed public file, and
`screen_for_pii` is the screen it calls. Neither had a direct test -- the
existing suites (`test_rule_inventory_determinism.py`,
`test_canonical_rule_inventory_boundary.py`) exercise them only incidentally,
through whole-file rendering and the CI trust boundary. A screen this central
needs tests that fail when IT breaks, not only when a rendered file changes
shape around it.

Motivated by a live bug this file also guards against (see
`PiiAllowWholeMatchTest`): `PII_ALLOW.search()` matched an allowed token as a
SUBSTRING of an attacker-controlled string, so `alice@ateles-swarm.test`,
`alice@neotoma.test`, `evil-noreply@github.com` and
`x@markmhendrickson.com.attacker.io` all cleared the screen as "clean". Fixed
by switching to `fullmatch` against an exact allowed suffix.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import render_rule_inventory as renderer

REPO_ROOT = Path(__file__).resolve().parents[2]


class ScreenForPiiPatternTest(unittest.TestCase):
    """Each PII_PATTERNS entry actually fires, and clean text passes."""

    def test_empty_text_is_clean(self) -> None:
        self.assertEqual(renderer.screen_for_pii(""), (True, []))

    def test_ordinary_rule_text_is_clean(self) -> None:
        clean, reasons = renderer.screen_for_pii(
            "Never bypass the pre-commit hook with --no-verify."
        )
        self.assertEqual((clean, reasons), (True, []))

    def test_btc_address_is_flagged(self) -> None:
        # Synthetic, structurally-valid-shaped bech32 literal -- not a real
        # wallet. gitleaks allowlist: not applicable, this is a bc1 literal
        # under 62 chars matching the module's own btc_address pattern, and
        # carries no resemblance to a funded address.
        clean, reasons = renderer.screen_for_pii(
            "pay out to bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq please"
        )
        self.assertFalse(clean)
        self.assertIn("btc_address", reasons)

    def test_iban_is_flagged(self) -> None:
        clean, reasons = renderer.screen_for_pii(
            "wire it to ES121234123412341234 today"
        )
        self.assertFalse(clean)
        self.assertIn("iban", reasons)

    def test_eur_amount_is_flagged(self) -> None:
        clean, reasons = renderer.screen_for_pii("pay 45 EUR for the session")
        self.assertFalse(clean)
        self.assertIn("eur_amount", reasons)

    def test_usd_amount_is_flagged(self) -> None:
        clean, reasons = renderer.screen_for_pii("invoice was $120 this month")
        self.assertFalse(clean)
        self.assertIn("usd_amount", reasons)

    def test_email_is_flagged(self) -> None:
        clean, reasons = renderer.screen_for_pii("contact alice@thirdparty.example")
        self.assertFalse(clean)
        self.assertIn("email", reasons)

    def test_phone_is_flagged(self) -> None:
        clean, reasons = renderer.screen_for_pii("call +34 612 345 678 tomorrow")
        self.assertFalse(clean)
        self.assertIn("phone", reasons)

    def test_stx_address_is_flagged(self) -> None:
        clean, reasons = renderer.screen_for_pii(
            "send it to SP2J6ZY48GV1EZ5V2V5RB9MP66SW86PYKKPVKG2CE"
        )
        self.assertFalse(clean)
        self.assertIn("stx_address", reasons)

    def test_multiple_reasons_are_all_reported_and_sorted(self) -> None:
        clean, reasons = renderer.screen_for_pii(
            "pay $50 to alice@thirdparty.example for the yoga session"
        )
        self.assertFalse(clean)
        # sorted(set(...)) per the function's own contract
        self.assertEqual(reasons, sorted(set(reasons)))
        self.assertIn("usd_amount", reasons)
        self.assertIn("email", reasons)
        self.assertIn("operator_personal_domain", reasons)


class ScreenForPiiDomainTest(unittest.TestCase):
    """The domain screen catches proper-noun PII no pattern can recognize."""

    def test_yoga_domain_is_flagged_even_with_no_structured_value(self) -> None:
        clean, reasons = renderer.screen_for_pii(
            "Reschedule the yoga session with the instructor for Thursday"
        )
        self.assertFalse(clean)
        self.assertEqual(reasons, ["operator_personal_domain"])

    def test_medical_domain_is_flagged(self) -> None:
        clean, reasons = renderer.screen_for_pii(
            "Follow up with the clinic about the blood test results"
        )
        self.assertFalse(clean)
        self.assertIn("operator_personal_domain", reasons)

    def test_unrelated_text_does_not_trip_the_domain_screen(self) -> None:
        clean, reasons = renderer.screen_for_pii(
            "Dispatch the task to the owning agent rather than working inline."
        )
        self.assertEqual((clean, reasons), (True, []))


class PiiAllowWholeMatchTest(unittest.TestCase):
    """B3: PII_ALLOW must whole-match, never merely contain, an allowed email.

    Each `bad` case previously returned (True, []) -- clean -- under
    `PII_ALLOW.search()`, because the allowed token appeared somewhere inside
    an otherwise attacker-controlled address. Fixed by anchoring every
    alternative to `^...$` and matching with `fullmatch`.
    """

    def test_crafted_addresses_smuggling_an_allowed_substring_are_flagged(
        self,
    ) -> None:
        crafted = (
            "alice@ateles-swarm.test",
            "evil-noreply@github.com",
            "alice@neotoma.test",
            "x@markmhendrickson.com.attacker.io",
        )
        for address in crafted:
            with self.subTest(address=address):
                clean, reasons = renderer.screen_for_pii(f"contact {address}")
                self.assertFalse(clean, f"{address} must NOT pass the screen")
                self.assertIn("email", reasons)
                self.assertEqual(renderer.safe_statement(address), renderer.WITHHELD)

    def test_genuinely_allowed_addresses_still_pass(self) -> None:
        allowed = (
            "alice@example.com",
            "noreply@anthropic.com",
            "noreply@github.com",
            "apis@ateles-swarm",
            "formica@ateles-swarm",
            "mark@markmhendrickson.com",
            "noreply@anthropic.com",
        )
        for address in allowed:
            with self.subTest(address=address):
                clean, reasons = renderer.screen_for_pii(f"contact {address}")
                self.assertEqual((clean, reasons), (True, []))

    def test_allow_pattern_rejects_partial_matches_directly(self) -> None:
        # Directly against the compiled pattern, independent of screen_for_pii,
        # so a future change to the PATTERNS tuple cannot mask a regression
        # here by simply not matching the crafted string as an "email" at all.
        self.assertIsNone(renderer.PII_ALLOW.fullmatch("alice@ateles-swarm.test"))
        self.assertIsNone(renderer.PII_ALLOW.fullmatch("alice@neotoma.test"))
        self.assertIsNone(
            renderer.PII_ALLOW.fullmatch("x@markmhendrickson.com.attacker.io")
        )
        self.assertIsNone(renderer.PII_ALLOW.fullmatch("evil-noreply@github.com"))
        self.assertIsNotNone(renderer.PII_ALLOW.fullmatch("apis@ateles-swarm"))
        self.assertIsNotNone(renderer.PII_ALLOW.fullmatch("mark@markmhendrickson.com"))


class SafeStatementTest(unittest.TestCase):
    """`safe_statement` never emits an operator value; it emits WITHHELD instead."""

    def test_clean_text_is_flattened_and_truncated(self) -> None:
        text = "  Never   bypass the  pre-commit hook with --no-verify.  "
        self.assertEqual(
            renderer.safe_statement(text),
            "Never bypass the pre-commit hook with --no-verify.",
        )

    def test_pii_bearing_text_is_withheld_not_partially_emitted(self) -> None:
        result = renderer.safe_statement("pay alice@thirdparty.example $50")
        self.assertEqual(result, renderer.WITHHELD)
        self.assertNotIn("alice", result)
        self.assertNotIn("thirdparty", result)

    def test_domain_flagged_text_is_withheld(self) -> None:
        result = renderer.safe_statement("book the gym session for Tuesday")
        self.assertEqual(result, renderer.WITHHELD)

    def test_sha_and_issue_number_are_redacted_before_markdown_strip(self) -> None:
        # A 40-hex-char SHA is digit-dense enough to also trip the `phone`
        # pattern screen (nine-plus consecutive digit groups), so this fixture
        # keeps the sha short (7 hex chars, the minimum VOLATILE matches) and
        # separates it from the issue number, to exercise VOLATILE redaction
        # in isolation rather than the withholding path already covered above.
        text = "Fixed in commit abc1234 -- see the discussion, closes #1158"
        result = renderer.safe_statement(text)
        self.assertNotIn("abc1234", result)
        self.assertNotIn("#1158", result)
        self.assertIn("<sha>", result)
        self.assertIn("<issue>", result)

    def test_markdown_sigils_are_stripped(self) -> None:
        text = "Use `git worktree add` and **never** stash [see #42]"
        result = renderer.safe_statement(text)
        for sigil in ("`", "*", "[", "]"):
            self.assertNotIn(sigil, result)

    def test_overlong_text_is_truncated_with_ellipsis(self) -> None:
        text = "word " * 60
        result = renderer.safe_statement(text, limit=40)
        self.assertLessEqual(len(result), 40)
        self.assertTrue(result.endswith("…"))

    def test_no_text_falls_back_to_placeholder(self) -> None:
        self.assertEqual(renderer.safe_statement("   "), "(no text)")

    def test_statement_safe_text_property_always_withholds(self) -> None:
        # Statement.safe_text never emits the underlying text, even when the
        # text itself is clean -- the public inventory measures kind and
        # location, not value (module docstring).
        stmt = renderer.Statement(
            store="CLAUDE.md",
            location="CLAUDE.md",
            locator="L1",
            text="Never bypass the pre-commit hook with --no-verify.",
        )
        self.assertEqual(stmt.safe_text, renderer.WITHHELD)


if __name__ == "__main__":
    unittest.main()

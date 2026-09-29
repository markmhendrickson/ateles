"""Typed refusals for the media-generation capability client.

Every refuse path raises ``GenerationRefused`` with a stable ``code`` the
caller can branch on, a ``hint`` that names the next step, and (when known)
the spend figures. Codes are defined once here and imported everywhere; do not
re-type them as literals in adapters, docs, or tests.

Hints never tell a caller to export a key into an agent or process
environment: generation credentials stay with the host-side client
(see ``docs/dev/generation_capability_client.md``).
"""

from __future__ import annotations

BINDING_MISSING = "BINDING_MISSING"
CAP_UNSET = "CAP_UNSET"
CAP_UNREADABLE = "CAP_UNREADABLE"
CAP_EXHAUSTED = "CAP_EXHAUSTED"
CREDENTIAL_UNRESOLVED = "CREDENTIAL_UNRESOLVED"
VENDOR_ERROR = "VENDOR_ERROR"
FALLBACK_EXHAUSTED = "FALLBACK_EXHAUSTED"
EMPTY_RESULT = "EMPTY_RESULT"
CRITIQUE_ROUND_LIMIT = "CRITIQUE_ROUND_LIMIT"

CODES: tuple[str, ...] = (
    BINDING_MISSING,
    CAP_UNSET,
    CAP_UNREADABLE,
    CAP_EXHAUSTED,
    CREDENTIAL_UNRESOLVED,
    VENDOR_ERROR,
    FALLBACK_EXHAUSTED,
    EMPTY_RESULT,
    CRITIQUE_ROUND_LIMIT,
)

# Codes raised before any vendor request can be made. A test asserts that the
# vendor call count is zero for each of these.
REFUSE_BEFORE_VENDOR: tuple[str, ...] = (
    BINDING_MISSING,
    CAP_UNSET,
    CAP_UNREADABLE,
    CAP_EXHAUSTED,
    CREDENTIAL_UNRESOLVED,
)


class CapabilityError(Exception):
    """Single library base exception for the capability client."""


class GenerationRefused(CapabilityError):
    """A generation call was refused or failed; branch on ``code``."""

    def __init__(
        self,
        code: str,
        slot: str | None,
        message: str,
        hint: str,
        *,
        spent_usd: float | None = None,
        cap_usd: float | None = None,
        remaining_usd: float | None = None,
        vendors_tried: tuple[str, ...] = (),
        retryable: bool | None = None,
    ) -> None:
        if code not in CODES:
            raise ValueError(f"unknown GenerationRefused code: {code!r}")
        self.code = code
        self.slot = slot
        self.message = message
        self.hint = hint
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd
        self.remaining_usd = remaining_usd
        self.vendors_tried = tuple(vendors_tried)
        self.retryable = retryable
        super().__init__(self.render())

    def render(self) -> str:
        parts = [f"[{self.code}] slot={self.slot}: {self.message}"]
        if self.cap_usd is not None:
            parts.append(
                f"(spent_usd={self.spent_usd}, cap_usd={self.cap_usd}, "
                f"remaining_usd={self.remaining_usd})"
            )
        parts.append(f"Hint: {self.hint}")
        return " ".join(parts)


class VendorFailure(CapabilityError):
    """Raised by a vendor adapter for an upstream failure after authorization.

    ``http_status`` is the upstream status when there was one; ``None`` means a
    transport failure (timeout, connection reset). ``generate`` converts this
    into ``GenerationRefused(VENDOR_ERROR)`` with a retryable/operator-fix
    classification.
    """

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        self.http_status = http_status
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        if self.http_status is None:
            return True
        return self.http_status in (408, 429) or self.http_status >= 500


class EmptyArtifact(CapabilityError):
    """Raised by an adapter when the vendor answered but produced no bytes."""


class RasterizerUnavailable(CapabilityError):
    """No SVG rasterizer is installed on this host. Not a refusal: the paid
    artifact is kept and the caller is told the preview could not be made."""

# handlers package for monedula payment daemon
#
# Handlers are instantiated dynamically from PaymentProfile env-var config.
# Use load_handlers() to get the active handler list for the current environment.

from .btc_transfer import BtcTransferHandler
from .payment_profile import (
    PaymentProfile,
    load_profiles,
    load_profiles_with_neotoma_fallback,
)
from .wise_transfer import WiseTransferHandler

try:
    from ..handler_base import PaymentHandler
except ImportError:
    from handler_base import PaymentHandler  # type: ignore[no-redef]


def load_handlers(strandings: list | None = None) -> list[PaymentHandler]:
    """
    Load all active payment handlers.

    Phase 5+: tries Neotoma payment_profile entities first, falls back to
    env-var-defined profiles (MONEDULA_PROFILES=THERAPY,YOGA).

    ``strandings`` collects active profiles that could not be loaded, so the
    caller can escalate them rather than let them vanish into the log
    (ateles#553).
    """
    profiles = load_profiles_with_neotoma_fallback(strandings)
    handlers: list[PaymentHandler] = []
    for profile in profiles:
        if profile.payment_type == "wise":
            handlers.append(WiseTransferHandler(profile))
        elif profile.payment_type == "btc":
            handlers.append(BtcTransferHandler(profile))
    return handlers


__all__ = [
    "PaymentProfile",
    "PaymentHandler",
    "WiseTransferHandler",
    "BtcTransferHandler",
    "load_profiles",
    "load_profiles_with_neotoma_fallback",
    "load_handlers",
]

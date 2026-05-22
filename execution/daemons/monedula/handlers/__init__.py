# handlers package for monedula payment daemon
#
# Handlers are instantiated dynamically from PaymentProfile env-var config.
# Use load_handlers() to get the active handler list for the current environment.

from .btc_transfer import BtcTransferHandler
from .payment_profile import PaymentProfile, load_profiles
from .wise_transfer import WiseTransferHandler

try:
    from ..handler_base import PaymentHandler
except ImportError:
    from handler_base import PaymentHandler  # type: ignore[no-redef]


def load_handlers() -> list[PaymentHandler]:
    """
    Load all active payment handlers from env-var-defined PaymentProfiles.
    Set MONEDULA_PROFILES=THERAPY,YOGA (or any prefix list) to activate profiles.
    """
    profiles = load_profiles()
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
    "load_handlers",
]

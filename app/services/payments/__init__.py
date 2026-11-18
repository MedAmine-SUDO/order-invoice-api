from app.core.config import settings
from app.services.payments.base import Checkout, PaymentError, PaymentProvider
from app.services.payments.konnect import KonnectProvider
from app.services.payments.mock import MockProvider
from app.services.payments.stripe import StripeProvider

__all__ = ["Checkout", "PaymentError", "PaymentProvider", "get_payment_provider"]


def get_payment_provider() -> PaymentProvider:
    """FastAPI dependency: the gateway picked by PAYMENT_PROVIDER. Overridden in tests."""
    match settings.payment_provider:
        case "konnect":
            return KonnectProvider()
        case "stripe":
            return StripeProvider()
        case _:
            return MockProvider()

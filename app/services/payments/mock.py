import secrets

from app.core.config import settings
from app.models import Order, Payment, PaymentMethod
from app.services.payments.base import Checkout


class MockProvider:
    """A built-in sandbox gateway: a checkout page with "Pay" and "Decline" buttons.

    It lets the live demo take payments without any merchant account.
    """

    method = PaymentMethod.mock

    def create_checkout(self, payment: Payment, order: Order) -> Checkout:
        ref = "mock_" + secrets.token_urlsafe(16)
        return Checkout(provider_ref=ref, pay_url=f"{settings.public_base_url}/sandbox/pay/{ref}")

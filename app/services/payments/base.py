from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.models import Order, Payment, PaymentMethod

# Currencies whose minor unit is 1/1000 (Stripe and Konnect both count TND in millimes).
THREE_DECIMAL_CURRENCIES = {"BHD", "JOD", "KWD", "OMR", "TND"}


def to_minor_units(amount: Decimal, currency: str) -> int:
    factor = 1000 if currency.upper() in THREE_DECIMAL_CURRENCIES else 100
    return int((amount * factor).to_integral_value())


class PaymentError(Exception):
    """The gateway refused or could not be reached."""


@dataclass
class Checkout:
    provider_ref: str
    pay_url: str


class PaymentProvider(Protocol):
    method: PaymentMethod

    def create_checkout(self, payment: Payment, order: Order) -> Checkout: ...

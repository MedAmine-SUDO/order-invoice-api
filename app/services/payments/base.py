import hashlib
import hmac
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.models import Order, Payment, PaymentMethod

# Currencies whose minor unit is 1/1000 (Flouci, Konnect and Stripe count TND in millimes).
THREE_DECIMAL_CURRENCIES = {"BHD", "JOD", "KWD", "OMR", "TND"}


def to_minor_units(amount: Decimal, currency: str) -> int:
    factor = 1000 if currency.upper() in THREE_DECIMAL_CURRENCIES else 100
    return int((amount * factor).to_integral_value())


SIGNATURE_TOLERANCE_SECONDS = 300


class PaymentError(Exception):
    """The gateway refused or could not be reached."""


def verify_signature(payload: bytes, header: str, secret: str) -> None:
    """Check a `t=<timestamp>,v1=<hex HMAC-SHA256 of "t.payload">` header.

    The scheme Stripe and Flouci both use. Old timestamps are refused, so a captured
    webhook can't be replayed.
    """
    if not secret:
        # An empty key would let anyone forge a valid signature.
        raise PaymentError("Webhook secret is not configured")
    pairs = [p.strip().split("=", 1) for p in header.split(",") if "=" in p]
    timestamp = next((v for k, v in pairs if k == "t"), "")
    signatures = [v for k, v in pairs if k == "v1"]
    if not timestamp.isdigit() or not signatures:
        raise PaymentError("Malformed signature header")
    if abs(time.time() - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
        raise PaymentError("Webhook timestamp outside tolerance")
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise PaymentError("Invalid webhook signature")


@dataclass
class Checkout:
    provider_ref: str
    pay_url: str


class PaymentProvider(Protocol):
    method: PaymentMethod

    def create_checkout(self, payment: Payment, order: Order) -> Checkout: ...

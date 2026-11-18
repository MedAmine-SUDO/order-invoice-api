import hashlib
import hmac
import json
import time

import httpx

from app.core.config import settings
from app.models import Order, Payment, PaymentMethod
from app.services.payments.base import (
    THREE_DECIMAL_CURRENCIES,
    Checkout,
    PaymentError,
    to_minor_units,
)

SIGNATURE_TOLERANCE_SECONDS = 300


class StripeProvider:
    """Stripe Checkout in test mode, called over plain HTTP (no SDK needed)."""

    method = PaymentMethod.stripe

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            base_url="https://api.stripe.com/v1",
            auth=(settings.stripe_secret_key, ""),
            timeout=15,
        )

    def create_checkout(self, payment: Payment, order: Order) -> Checkout:
        amount = to_minor_units(payment.amount, payment.currency)
        if payment.currency.upper() in THREE_DECIMAL_CURRENCIES:
            # Stripe requires three-decimal amounts to end in 0.
            amount = round(amount, -1)
        landing = f"{settings.public_base_url}/o/{order.public_token}"
        form = {
            "mode": "payment",
            "success_url": landing,
            "cancel_url": landing,
            "client_reference_id": str(payment.id),
            "metadata[payment_id]": str(payment.id),
            "metadata[order_number]": order.number,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": payment.currency.lower(),
            "line_items[0][price_data][unit_amount]": str(amount),
            "line_items[0][price_data][product_data][name]": f"Order {order.number}",
        }
        if order.customer and order.customer.email:
            form["customer_email"] = order.customer.email
        try:
            response = self.client.post("/checkout/sessions", data=form)
            response.raise_for_status()
            session = response.json()
        except httpx.HTTPError as exc:
            raise PaymentError(f"Stripe request failed: {exc}") from exc
        return Checkout(provider_ref=session["id"], pay_url=session["url"])


def verify_webhook(payload: bytes, signature_header: str, secret: str) -> dict:
    """Check a Stripe-Signature header and return the decoded event."""
    pairs = [p.strip().split("=", 1) for p in signature_header.split(",") if "=" in p]
    timestamp = next((v for k, v in pairs if k == "t"), "")
    signatures = [v for k, v in pairs if k == "v1"]
    if not timestamp.isdigit() or not signatures:
        raise PaymentError("Malformed Stripe-Signature header")
    if abs(time.time() - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
        raise PaymentError("Stripe webhook timestamp outside tolerance")
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise PaymentError("Invalid Stripe webhook signature")
    return json.loads(payload)

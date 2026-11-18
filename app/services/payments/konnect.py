import httpx

from app.core.config import settings
from app.models import Order, Payment, PaymentMethod
from app.services.payments.base import Checkout, PaymentError, to_minor_units


class KonnectProvider:
    """Konnect (konnect.network), a Tunisian gateway: bank cards, e-DINAR and Konnect wallet.

    Flow: init-payment returns a payUrl. When the customer finishes, Konnect calls our webhook
    with ?payment_ref=..., and we ask Konnect for the real status (the webhook itself is unsigned,
    so it is only a hint).
    """

    method = PaymentMethod.konnect

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            base_url=settings.konnect_base_url,
            headers={"x-api-key": settings.konnect_api_key},
            timeout=15,
        )

    def create_checkout(self, payment: Payment, order: Order) -> Checkout:
        base = settings.public_base_url
        body = {
            "receiverWalletId": settings.konnect_wallet_id,
            "token": payment.currency,
            "amount": to_minor_units(payment.amount, payment.currency),
            "type": "immediate",
            "description": f"Order {order.number}",
            "acceptedPaymentMethods": ["wallet", "bank_card", "e-DINAR"],
            "lifespan": 30,
            "checkoutForm": False,
            "addPaymentFeesToAmount": False,
            "orderId": order.number,
            "webhook": f"{base}/api/v1/payments/webhooks/konnect",
            "successUrl": f"{base}/o/{order.public_token}",
            "failUrl": f"{base}/o/{order.public_token}",
            "theme": "light",
        }
        if order.customer:
            body["email"] = order.customer.email
            body["phoneNumber"] = order.customer.phone
        data = self._request("POST", "/payments/init-payment", json=body)
        try:
            return Checkout(provider_ref=data["paymentRef"], pay_url=data["payUrl"])
        except KeyError as exc:
            raise PaymentError(f"Unexpected Konnect response: {data}") from exc

    def is_completed(self, payment_ref: str) -> tuple[bool, dict]:
        data = self._request("GET", f"/payments/{payment_ref}")
        status = (data.get("payment") or {}).get("status")
        return status == "completed", {"konnect_status": status}

    def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = self.client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise PaymentError(f"Konnect request failed: {exc}") from exc

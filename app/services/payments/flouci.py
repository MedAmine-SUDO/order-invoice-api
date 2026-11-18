import httpx

from app.core.config import settings
from app.models import Order, Payment, PaymentMethod
from app.services.payments.base import Checkout, PaymentError, to_minor_units

# verify_payment statuses that will never turn into a success.
FINAL_FAILURES = {"FAILURE", "EXPIRED", "SYSTEM_FAILURE"}


class FlouciProvider:
    """Flouci (flouci.com), a Tunisian gateway: Flouci wallet and bank cards.

    Flow: generate_payment returns a checkout link. When the payment ends, Flouci POSTs a
    signed webhook (version 2), and we still confirm with verify_payment before trusting it.
    Test and live accounts use the same URLs; only the keys differ.
    """

    method = PaymentMethod.flouci

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            base_url=settings.flouci_base_url,
            headers={
                "Authorization": f"Bearer {settings.flouci_public_key}:{settings.flouci_private_key}"
            },
            timeout=15,
        )

    def create_checkout(self, payment: Payment, order: Order) -> Checkout:
        base = settings.public_base_url
        landing = f"{base}/o/{order.public_token}"
        body = {
            "amount": str(to_minor_units(payment.amount, payment.currency)),
            "accept_card": True,
            "session_timeout_secs": 1800,
            "success_link": landing,
            "fail_link": landing,
            "webhook": f"{base}/api/v1/payments/webhooks/flouci",
            "webhook_version": 2,
            "developer_tracking_id": str(payment.id),
        }
        data = self._request("POST", "/generate_payment", json=body)
        result = data.get("result") or {}
        if not result.get("success") or "link" not in result:
            raise PaymentError(f"Flouci refused the payment: {result}")
        return Checkout(provider_ref=result["payment_id"], pay_url=result["link"])

    def check(self, payment_id: str) -> tuple[bool | None, dict]:
        """True if paid, False if it failed for good, None if still pending."""
        data = self._request("GET", f"/verify_payment/{payment_id}")
        status = (data.get("result") or {}).get("status")
        details = {"flouci_status": status}
        if data.get("success") is True and status == "SUCCESS":
            return True, details
        return (False if status in FINAL_FAILURES else None), details

    def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = self.client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise PaymentError(f"Flouci request failed: {exc}") from exc

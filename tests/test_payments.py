import hashlib
import hmac
import json
import time

import httpx
import pytest

from app.main import app
from app.models import Payment
from app.services.payments import get_payment_provider
from app.services.payments.konnect import KonnectProvider
from app.services.payments.stripe import StripeProvider


def _pay(client, headers, order_id, method):
    return client.post(f"/api/v1/orders/{order_id}/payments", json={"method": method},
                       headers=headers)  # fmt: skip


def test_cash_payment_marks_order_paid(client, cashier_headers, make_order):
    order = make_order()
    r = _pay(client, cashier_headers, order["id"], "cash")
    assert r.status_code == 201
    assert r.json()["status"] == "succeeded"
    order = client.get(f"/api/v1/orders/{order['id']}", headers=cashier_headers).json()
    assert order["status"] == "paid" and order["paid_at"]
    assert _pay(client, cashier_headers, order["id"], "cash").status_code == 409


def test_paid_order_cannot_be_cancelled(client, cashier_headers, make_order):
    order = make_order()
    _pay(client, cashier_headers, order["id"], "cash")
    r = client.post(f"/api/v1/orders/{order['id']}/cancel", headers=cashier_headers)
    assert r.status_code == 409


def test_sandbox_checkout_flow(client, cashier_headers, make_order):
    order = make_order()
    payment = _pay(client, cashier_headers, order["id"], "online").json()
    assert payment["method"] == "mock"
    assert payment["status"] == "pending"

    # Asking again reuses the open checkout.
    again = _pay(client, cashier_headers, order["id"], "online").json()
    assert again["id"] == payment["id"]

    path = httpx.URL(payment["pay_url"]).path
    assert "Pay 4,570 TND" in client.get(path).text
    r = client.post(f"{path}/succeed", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/o/")

    order = client.get(f"/api/v1/orders/{order['id']}", headers=cashier_headers).json()
    assert order["status"] == "paid"


def test_declined_sandbox_payment_leaves_order_pending(client, cashier_headers, make_order):
    order = make_order()
    payment = _pay(client, cashier_headers, order["id"], "online").json()
    client.post(httpx.URL(payment["pay_url"]).path + "/fail")
    assert (
        client.get(f"/api/v1/payments/{payment['id']}", headers=cashier_headers).json()["status"]
        == "failed"
    )
    order = client.get(f"/api/v1/orders/{order['id']}", headers=cashier_headers).json()
    assert order["status"] == "pending"


def test_cancelling_order_closes_open_checkout(client, cashier_headers, make_order):
    order = make_order()
    payment = _pay(client, cashier_headers, order["id"], "online").json()
    client.post(f"/api/v1/orders/{order['id']}/cancel", headers=cashier_headers)
    r = client.get(f"/api/v1/payments/{payment['id']}", headers=cashier_headers)
    assert r.json()["status"] == "failed"


# --- Konnect --------------------------------------------------------------------------


@pytest.fixture
def konnect(request):
    """A Konnect provider talking to a fake Konnect API. Set `.status` to drive it."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path.endswith("/payments/init-payment"):
            return httpx.Response(
                200, json={"payUrl": "https://pay.konnect/abc", "paymentRef": "kref1"}
            )
        return httpx.Response(200, json={"payment": {"status": fake.status}})

    fake = KonnectProvider(
        httpx.Client(base_url="https://konnect.test/api/v2", transport=httpx.MockTransport(handler))
    )
    fake.status = "pending"
    fake.calls = calls
    app.dependency_overrides[get_payment_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_payment_provider, None)


def test_konnect_checkout_sends_millimes(client, cashier_headers, make_order, konnect):
    order = make_order()
    payment = _pay(client, cashier_headers, order["id"], "online").json()
    assert payment["method"] == "konnect"
    assert payment["pay_url"] == "https://pay.konnect/abc"
    body = json.loads(konnect.calls[0].content)
    assert body["amount"] == 4570
    assert body["token"] == "TND"
    assert body["webhook"] == "http://testserver/api/v1/payments/webhooks/konnect"


def test_konnect_webhook_checks_status_before_trusting_it(
    client, cashier_headers, make_order, konnect
):
    order = make_order()
    _pay(client, cashier_headers, order["id"], "online")

    client.get("/api/v1/payments/webhooks/konnect", params={"payment_ref": "kref1"})
    assert (
        client.get(f"/api/v1/orders/{order['id']}", headers=cashier_headers).json()["status"]
        == "pending"
    )

    konnect.status = "completed"
    r = client.get("/api/v1/payments/webhooks/konnect", params={"payment_ref": "kref1"})
    assert r.json()["status"] == "succeeded"
    assert (
        client.get(f"/api/v1/orders/{order['id']}", headers=cashier_headers).json()["status"]
        == "paid"
    )


def test_konnect_gateway_error_returns_502(client, cashier_headers, make_order):
    broken = KonnectProvider(
        httpx.Client(
            base_url="https://konnect.test",
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        )
    )
    app.dependency_overrides[get_payment_provider] = lambda: broken
    try:
        order = make_order()
        assert _pay(client, cashier_headers, order["id"], "online").status_code == 502
    finally:
        app.dependency_overrides.pop(get_payment_provider, None)


# --- Stripe ---------------------------------------------------------------------------


def _signed(payload: dict, secret="whsec_test", timestamp=None) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    t = str(timestamp or int(time.time()))
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={t},v1={sig}"


@pytest.fixture
def stripe_order(client, cashier_headers, make_order):
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json={"id": "cs_test_1", "url": "https://checkout.stripe/cs1"})

    provider = StripeProvider(
        httpx.Client(base_url="https://stripe.test/v1", transport=httpx.MockTransport(handler))
    )
    app.dependency_overrides[get_payment_provider] = lambda: provider
    order = make_order()
    payment = _pay(client, cashier_headers, order["id"], "online").json()
    app.dependency_overrides.pop(get_payment_provider, None)
    return order, payment, sent


def test_stripe_checkout_amount_in_millimes_rounded_to_ten(stripe_order):
    _, payment, sent = stripe_order
    form = dict(httpx.QueryParams(sent[0].content.decode()))
    assert form["line_items[0][price_data][unit_amount]"] == "4570"
    assert form["line_items[0][price_data][currency]"] == "tnd"
    assert form["metadata[payment_id]"] == str(payment["id"])


def test_stripe_webhook_with_valid_signature(client, cashier_headers, stripe_order):
    order, payment, _ = stripe_order
    event = {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {"payment_status": "paid", "metadata": {"payment_id": str(payment["id"])}}
        },
    }
    body, header = _signed(event)
    r = client.post("/api/v1/payments/webhooks/stripe", content=body,
                    headers={"Stripe-Signature": header})  # fmt: skip
    assert r.status_code == 200
    assert (
        client.get(f"/api/v1/orders/{order['id']}", headers=cashier_headers).json()["status"]
        == "paid"
    )


@pytest.mark.parametrize(
    "secret,age", [("whsec_wrong", 0), ("whsec_test", 3600)], ids=["bad-secret", "replayed"]
)
def test_stripe_webhook_rejects_bad_signatures(client, db, stripe_order, secret, age):
    _, payment, _ = stripe_order
    event = {"type": "checkout.session.completed",
             "data": {"object": {"payment_status": "paid",
                                 "metadata": {"payment_id": str(payment["id"])}}}}  # fmt: skip
    body, header = _signed(event, secret=secret, timestamp=int(time.time()) - age)
    r = client.post("/api/v1/payments/webhooks/stripe", content=body,
                    headers={"Stripe-Signature": header})  # fmt: skip
    assert r.status_code == 400
    assert db.get(Payment, payment["id"]).status == "pending"


def test_cash_payment_closes_open_online_checkout(client, cashier_headers, make_order, db):
    order = make_order()
    online = _pay(client, cashier_headers, order["id"], "online").json()
    _pay(client, cashier_headers, order["id"], "cash")
    assert db.get(Payment, online["id"]).status == "failed"


def test_late_gateway_success_on_paid_order_is_flagged_for_refund(
    client, cashier_headers, make_order, db
):
    order = make_order()
    online = _pay(client, cashier_headers, order["id"], "online").json()
    _pay(client, cashier_headers, order["id"], "cash")
    # The customer had the checkout open and paid anyway.
    client.post(httpx.URL(online["pay_url"]).path + "/succeed")
    db.expire_all()
    payment = db.get(Payment, online["id"])
    assert payment.status == "succeeded"
    assert payment.details["needs_refund"] is True

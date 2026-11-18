import csv
import io
from datetime import UTC, datetime, timedelta

import httpx

from app.models import Order
from app.services.reports import day_bounds, today


def _token(order):
    return httpx.URL(order["public_url"]).path.rsplit("/", 1)[-1]


def test_qr_landing_page_shows_status_without_login(client, cashier_headers, make_order):
    order = make_order()
    token = _token(order)
    page = client.get(f"/o/{token}")
    assert page.status_code == 200
    assert order["number"] in page.text
    assert "En attente de paiement" in page.text and "Payer en ligne" in page.text

    client.post(f"/api/v1/orders/{order['id']}/payments", json={"method": "cash"},
                headers=cashier_headers)  # fmt: skip
    page = client.get(f"/o/{token}")
    assert "Payée" in page.text and "Payer en ligne" not in page.text
    assert "Facture FAC-" in page.text

    r = client.get(f"/api/v1/public/orders/{token}")
    assert r.json()["status"] == "paid"
    assert "customer" not in r.json()  # nothing private on the public endpoint
    assert client.get(f"/o/{token}/document.pdf").content.startswith(b"%PDF")
    assert r.json()["invoice_number"].startswith("FAC-")


def test_unknown_token_is_404(client):
    assert client.get("/o/not-a-real-token").status_code == 404


def test_customer_can_pay_from_qr_page(client, cashier_headers, make_order):
    order = make_order()
    r = client.post(f"/o/{_token(order)}/pay", follow_redirects=False)
    assert r.status_code == 303
    checkout = httpx.URL(r.headers["location"]).path
    assert checkout.startswith("/sandbox/pay/")
    client.post(f"{checkout}/succeed")
    assert (
        client.get(f"/api/v1/orders/{order['id']}", headers=cashier_headers).json()["status"]
        == "paid"
    )


def test_day_bounds_follow_tunis_time():
    start, end = day_bounds(datetime(2026, 7, 1).date())
    # Tunis is UTC+1 all year: the local day starts at 23:00 UTC the day before.
    assert start == datetime(2026, 6, 30, 23, tzinfo=UTC)
    assert end - start == timedelta(days=1)


def test_daily_report(client, admin_headers, cashier_headers, make_order, products):
    paid_cash = make_order([{"product_id": products[0].id, "quantity": 2}])  # 4.570
    paid_card = make_order([{"product_id": products[1].id, "quantity": 1}])  # 2.498
    cancelled = make_order()
    make_order()  # stays pending
    client.post(f"/api/v1/orders/{paid_cash['id']}/payments", json={"method": "cash"},
                headers=cashier_headers)  # fmt: skip
    pay = client.post(f"/api/v1/orders/{paid_card['id']}/payments", json={"method": "online"},
                      headers=cashier_headers).json()  # fmt: skip
    client.post(httpx.URL(pay["pay_url"]).path + "/succeed")
    client.post(f"/api/v1/orders/{cancelled['id']}/cancel", headers=cashier_headers)

    r = client.get("/api/v1/reports/daily", headers=cashier_headers)
    assert r.status_code == 200
    report = r.json()
    assert report["date"] == today().isoformat()
    assert report["orders_created"] == 4
    assert report["orders_paid"] == 2
    assert report["orders_cancelled"] == 1
    assert report["revenue"] == "7.068"
    assert report["average_basket"] == "3.534"
    assert report["items_sold"] == 3
    assert {m["method"]: m["amount"] for m in report["payments_by_method"]} == {
        "cash": "4.570",
        "mock": "2.498",
    }
    assert report["top_products"][0]["name"] == "Café"

    empty = client.get("/api/v1/reports/daily", params={"day": "2020-01-01"},
                       headers=admin_headers).json()  # fmt: skip
    assert empty["orders_created"] == 0 and empty["revenue"] == "0.000"


def test_csv_export(client, admin_headers, cashier_headers, make_order, customer, db):
    order = make_order(customer_id=customer.id)
    old = make_order()
    db.get(Order, old["id"]).created_at = datetime(2025, 1, 15, 10, tzinfo=UTC)
    db.commit()

    day = today().isoformat()
    params = {"start": day, "end": day}
    assert client.get("/api/v1/reports/orders.csv", params=params,
                      headers=cashier_headers).status_code == 403  # fmt: skip
    r = client.get("/api/v1/reports/orders.csv", params=params, headers=admin_headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿")), delimiter=";"))
    assert len(rows) == 1
    assert rows[0]["number"] == order["number"]
    assert rows[0]["customer"] == "Société Test"
    assert rows[0]["total_ttc"] == "4,570"  # French Excel format by default

    bad = {"start": day, "end": "2000-01-01"}
    r = client.get("/api/v1/reports/orders.csv", params=bad, headers=admin_headers)
    assert r.status_code == 422


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_seed_is_idempotent(db):
    from app.seed import seed

    assert seed(db) is True
    assert seed(db) is False
    assert db.query(Order).count() > 20

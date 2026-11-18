from app.models import Product


def test_order_totals_tax_and_fiscal_stamp(make_order, products):
    order = make_order(
        [
            {"product_id": products[0].id, "quantity": 2},  # 3.000 HT, 19 %
            {"product_id": products[1].id, "quantity": 1},  # 1.400 HT, 7 %
        ]
    )
    assert order["number"].startswith("FAC-")
    assert order["status"] == "pending"
    assert order["subtotal"] == "4.400"
    assert order["tax_total"] == "0.668"  # 0.570 + 0.098
    assert order["stamp_duty"] == "1.000"
    assert order["total"] == "6.068"
    assert order["public_url"].startswith("http://testserver/o/")


def test_order_reserves_stock(make_order, products, db):
    make_order([{"product_id": products[0].id, "quantity": 5}])
    db.expire_all()
    assert db.get(Product, products[0].id).stock == 95


def test_duplicate_lines_are_merged(make_order, products):
    pid = products[0].id
    order = make_order([{"product_id": pid, "quantity": 1}, {"product_id": pid, "quantity": 2}])
    assert len(order["items"]) == 1
    assert order["items"][0]["quantity"] == 3


def test_not_enough_stock(client, cashier_headers, products):
    body = {"items": [{"product_id": products[1].id, "quantity": 4}]}
    r = client.post("/api/v1/orders", json=body, headers=cashier_headers)
    assert r.status_code == 409
    assert "3 left" in r.json()["detail"]


def test_unknown_product_or_customer(client, cashier_headers, products):
    r = client.post("/api/v1/orders", json={"items": [{"product_id": 999, "quantity": 1}]},
                    headers=cashier_headers)  # fmt: skip
    assert r.status_code == 404
    r = client.post(
        "/api/v1/orders",
        json={"customer_id": 999, "items": [{"product_id": products[0].id, "quantity": 1}]},
        headers=cashier_headers,
    )
    assert r.status_code == 404


def test_price_change_does_not_alter_existing_order(client, admin_headers, make_order, products):
    order = make_order()
    client.patch(f"/api/v1/products/{products[0].id}", json={"unit_price": "9.999"},
                 headers=admin_headers)  # fmt: skip
    again = client.get(f"/api/v1/orders/{order['id']}", headers=admin_headers).json()
    assert again["items"][0]["unit_price"] == "1.500"
    assert again["total"] == order["total"]


def test_cancel_restocks_and_only_once(client, cashier_headers, make_order, products, db):
    order = make_order([{"product_id": products[1].id, "quantity": 3}])
    r = client.post(f"/api/v1/orders/{order['id']}/cancel", headers=cashier_headers)
    assert r.json()["status"] == "cancelled"
    db.expire_all()
    assert db.get(Product, products[1].id).stock == 3
    r = client.post(f"/api/v1/orders/{order['id']}/cancel", headers=cashier_headers)
    assert r.status_code == 409


def test_list_filters(client, cashier_headers, make_order):
    first = make_order()
    make_order()
    client.post(f"/api/v1/orders/{first['id']}/cancel", headers=cashier_headers)
    r = client.get("/api/v1/orders", params={"status": "pending"}, headers=cashier_headers)
    assert r.json()["total"] == 1
    r = client.get("/api/v1/orders", params={"limit": 1}, headers=cashier_headers)
    assert r.json()["total"] == 2 and len(r.json()["items"]) == 1


def test_invoice_pdf_and_qr(client, cashier_headers, make_order, customer):
    order = make_order(customer_id=customer.id, note="Livraison 2e étage")
    r = client.get(f"/api/v1/orders/{order['id']}/invoice.pdf", headers=cashier_headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert order["number"] in r.headers["content-disposition"]

    r = client.get(f"/api/v1/orders/{order['id']}/qr.png", headers=cashier_headers)
    assert r.content.startswith(b"\x89PNG")


def test_invoice_requires_login(client, make_order):
    order = make_order()
    assert client.get(f"/api/v1/orders/{order['id']}/invoice.pdf").status_code == 401

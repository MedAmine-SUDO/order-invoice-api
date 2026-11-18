def test_admin_creates_product_with_price_including_tax(client, admin_headers):
    body = {"sku": "JUS", "name": "Jus d'orange", "unit_price": "3.361", "stock": 10}
    r = client.post("/api/v1/products", json=body, headers=admin_headers)
    assert r.status_code == 201
    product = r.json()
    assert product["tax_rate"] == "19"  # default VAT
    assert product["price_with_tax"] == "4.000"


def test_duplicate_sku_is_refused(client, admin_headers, products):
    body = {"sku": "CAF", "name": "Other", "unit_price": "1"}
    assert client.post("/api/v1/products", json=body, headers=admin_headers).status_code == 409


def test_cashier_reads_but_cannot_edit_products(client, cashier_headers, products):
    assert client.get("/api/v1/products", headers=cashier_headers).json()["total"] == 2
    r = client.patch(
        f"/api/v1/products/{products[0].id}", json={"name": "x"}, headers=cashier_headers
    )
    assert r.status_code == 403


def test_search_and_low_stock_filter(client, cashier_headers, products):
    r = client.get("/api/v1/products", params={"q": "crois"}, headers=cashier_headers)
    assert [p["sku"] for p in r.json()["items"]] == ["CRO"]
    r = client.get("/api/v1/products", params={"low_stock": 5}, headers=cashier_headers)
    assert [p["sku"] for p in r.json()["items"]] == ["CRO"]


def test_archived_products_are_hidden(client, admin_headers, products):
    client.delete(f"/api/v1/products/{products[1].id}", headers=admin_headers)
    assert client.get("/api/v1/products", headers=admin_headers).json()["total"] == 1
    r = client.get("/api/v1/products", params={"include_inactive": True}, headers=admin_headers)
    assert r.json()["total"] == 2


def test_stock_adjustment(client, admin_headers, products):
    url = f"/api/v1/products/{products[1].id}/stock"
    r = client.post(url, json={"delta": 24, "note": "delivery"}, headers=admin_headers)
    assert r.json()["stock"] == 27
    assert client.post(url, json={"delta": -100}, headers=admin_headers).status_code == 409


def test_customer_crud(client, cashier_headers, admin_headers):
    r = client.post(
        "/api/v1/customers",
        json={"name": "Hôtel Les Berges", "phone": "+216 71 860 200"},
        headers=cashier_headers,
    )
    assert r.status_code == 201
    cid = r.json()["id"]
    r = client.get("/api/v1/customers", params={"q": "berges"}, headers=cashier_headers)
    assert r.json()["total"] == 1
    r = client.patch(f"/api/v1/customers/{cid}", json={"tax_id": "0987654/A/P/000"},
                     headers=cashier_headers)  # fmt: skip
    assert r.json()["tax_id"] == "0987654/A/P/000"
    assert client.delete(f"/api/v1/customers/{cid}", headers=cashier_headers).status_code == 403
    assert client.delete(f"/api/v1/customers/{cid}", headers=admin_headers).status_code == 204


def test_customer_with_orders_cannot_be_deleted(client, admin_headers, customer, make_order):
    make_order(customer_id=customer.id)
    r = client.delete(f"/api/v1/customers/{customer.id}", headers=admin_headers)
    assert r.status_code == 409

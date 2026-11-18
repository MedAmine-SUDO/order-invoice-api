import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models import Customer, Order, Product
from app.services.documents import issue_invoice
from app.services.reports import today
from app.services.words import amount_in_words


def _pay(client, headers, order_id, method="cash"):
    r = client.post(f"/api/v1/orders/{order_id}/payments", json={"method": method}, headers=headers)
    assert r.status_code == 201, r.text
    return client.get(f"/api/v1/orders/{order_id}", headers=headers).json()


def _credit(client, headers, order_id, restock=True):
    return client.post(
        f"/api/v1/orders/{order_id}/credit-note",
        json={"reason": "Marchandise retournée", "restock": restock},
        headers=headers,
    )


def _pdf_title(content: bytes) -> bytes:
    start = content.index(b"/Title (") + 8
    return content[start : content.index(b")", start)]


# --- Numbering ------------------------------------------------------------------------


def test_invoice_is_issued_at_payment_with_gap_free_numbers(client, cashier_headers, make_order):
    year = today().year
    first, dropped, second = make_order(), make_order(), make_order()
    client.post(f"/api/v1/orders/{dropped['id']}/cancel", headers=cashier_headers)

    first = _pay(client, cashier_headers, first["id"])
    second = _pay(client, cashier_headers, second["id"], "cheque")
    # The cancelled order never got an invoice, so there's no hole in the sequence.
    assert first["invoice"]["number"] == f"FAC-{year}-00001"
    assert second["invoice"]["number"] == f"FAC-{year}-00002"
    assert second["payments"][0]["method"] == "cheque"


def test_numbering_restarts_each_year(db, users, products):
    def order():
        o = Order(created_by_id=users["admin"].id, subtotal=Decimal(1), tax_total=Decimal(0),
                  stamp_duty=Decimal(0), total=Decimal(1), currency="TND")  # fmt: skip
        db.add(o)
        db.flush()
        return o

    numbers = [
        issue_invoice(db, order(), at=datetime(2025, 12, 31, 12, tzinfo=UTC)).number,
        # 23:30 UTC on 31 Dec is already 1 Jan in Tunis.
        issue_invoice(db, order(), at=datetime(2025, 12, 31, 23, 30, tzinfo=UTC)).number,
        issue_invoice(db, order(), at=datetime(2026, 1, 2, tzinfo=UTC)).number,
    ]
    assert numbers == ["FAC-2025-00001", "FAC-2026-00001", "FAC-2026-00002"]


# --- Invoice before payment (account customers) -----------------------------------------


def test_invoice_now_pay_later(client, cashier_headers, make_order, customer):
    order = make_order(customer_id=customer.id)
    r = client.post(f"/api/v1/orders/{order['id']}/invoice", headers=cashier_headers)
    invoiced = r.json()
    assert invoiced["status"] == "pending" and invoiced["invoice"]["number"].startswith("FAC-")

    unpaid = client.get(
        "/api/v1/orders", params={"status": "pending", "invoiced": True}, headers=cashier_headers
    ).json()
    assert [o["id"] for o in unpaid["items"]] == [order["id"]]

    paid = _pay(client, cashier_headers, order["id"], "transfer")
    assert paid["status"] == "paid"
    assert paid["invoice"]["id"] == invoiced["invoice"]["id"]  # no second invoice


def test_invoiced_order_cannot_simply_be_cancelled(client, cashier_headers, make_order):
    order = make_order()
    client.post(f"/api/v1/orders/{order['id']}/invoice", headers=cashier_headers)
    r = client.post(f"/api/v1/orders/{order['id']}/cancel", headers=cashier_headers)
    assert r.status_code == 409
    assert "credit note" in r.json()["detail"]


# --- Credit notes ---------------------------------------------------------------------


def test_credit_note_on_paid_order(
    client, admin_headers, cashier_headers, make_order, products, db
):
    order = make_order([{"product_id": products[1].id, "quantity": 3}])
    _pay(client, cashier_headers, order["id"])

    assert _credit(client, cashier_headers, order["id"]).status_code == 403  # admins only
    r = _credit(client, admin_headers, order["id"])
    assert r.status_code == 200
    refunded = r.json()
    assert refunded["status"] == "refunded"
    assert refunded["credit_note"]["number"] == f"AV-{today().year}-00001"
    db.expire_all()
    assert db.get(Product, products[1].id).stock == 3  # back on the shelf

    note = client.get(f"/api/v1/invoices/{refunded['credit_note']['id']}", headers=admin_headers)
    note = note.json()
    assert note["kind"] == "credit_note"
    assert note["credited_invoice_id"] == refunded["invoice"]["id"]
    assert note["stamp_duty"] == "0.000"  # the stamp isn't credited by default
    assert note["total"] == "4.494"  # 3 × 1.400 HT + 7 % TVA

    assert _credit(client, admin_headers, order["id"]).status_code == 409  # only once


def test_credit_note_on_unpaid_invoice_cancels_the_order(
    client, admin_headers, cashier_headers, make_order, products, db
):
    order = make_order([{"product_id": products[1].id, "quantity": 2}])
    client.post(f"/api/v1/orders/{order['id']}/invoice", headers=cashier_headers)
    r = _credit(client, admin_headers, order["id"], restock=False)
    assert r.json()["status"] == "cancelled"
    db.expire_all()
    assert db.get(Product, products[1].id).stock == 1  # restock=False


def test_order_without_invoice_has_nothing_to_credit(client, admin_headers, make_order):
    order = make_order()
    assert _credit(client, admin_headers, order["id"]).status_code == 409


# --- Documents ------------------------------------------------------------------------


def test_invoice_keeps_the_customer_as_they_were(
    client, admin_headers, cashier_headers, make_order, customer
):
    order = _pay(client, cashier_headers, make_order(customer_id=customer.id)["id"])
    client.patch(f"/api/v1/customers/{customer.id}", json={"name": "Nouveau Nom SARL"},
                 headers=admin_headers)  # fmt: skip
    invoice = client.get(f"/api/v1/invoices/{order['invoice']['id']}", headers=admin_headers)
    assert invoice.json()["customer"]["name"] == "Société Test"


def test_each_document_renders(client, admin_headers, cashier_headers, make_order):
    order = make_order()
    pdf = client.get(f"/api/v1/orders/{order['id']}/document.pdf", headers=cashier_headers)
    assert _pdf_title(pdf.content) == f"Bon de commande {order['number']}".encode()

    order = _pay(client, cashier_headers, order["id"])
    pdf = client.get(f"/api/v1/orders/{order['id']}/document.pdf", headers=cashier_headers)
    assert _pdf_title(pdf.content) == f"Facture {order['invoice']['number']}".encode()

    order = _credit(client, admin_headers, order["id"]).json()
    pdf = client.get(order["credit_note"]["pdf_url"].replace("http://testserver", ""),
                     headers=admin_headers)  # fmt: skip
    assert _pdf_title(pdf.content) == f"Avoir {order['credit_note']['number']}".encode()


def test_markup_in_names_does_not_break_the_pdf(client, cashier_headers, db, products):
    db.add(Customer(name="Dupont & Fils <SARL>", address="Rue <b>"))
    db.commit()
    cid = db.query(Customer).one().id
    r = client.post("/api/v1/orders", headers=cashier_headers, json={
        "customer_id": cid, "note": "a < b & c",
        "items": [{"product_id": products[0].id, "quantity": 1}],
    })  # fmt: skip
    pdf = client.get(f"/api/v1/orders/{r.json()['id']}/document.pdf", headers=cashier_headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_invoice_list(client, admin_headers, cashier_headers, make_order):
    _pay(client, cashier_headers, make_order()["id"])
    paid = _pay(client, cashier_headers, make_order()["id"])
    _credit(client, admin_headers, paid["id"])
    everything = client.get("/api/v1/invoices", headers=cashier_headers).json()
    assert everything["total"] == 3
    notes = client.get("/api/v1/invoices", params={"kind": "credit_note"}, headers=cashier_headers)
    assert notes.json()["total"] == 1


# --- Reports --------------------------------------------------------------------------


def test_monthly_vat_by_rate_net_of_credit_notes(
    client, admin_headers, cashier_headers, make_order, products
):
    cafe, croissant = products[0].id, products[1].id
    _pay(client, cashier_headers, make_order([{"product_id": cafe, "quantity": 2}])["id"])
    both = make_order(
        [{"product_id": cafe, "quantity": 1}, {"product_id": croissant, "quantity": 1}]
    )
    _pay(client, cashier_headers, both["id"])
    credited = make_order([{"product_id": cafe, "quantity": 4}])
    _pay(client, cashier_headers, credited["id"])
    _credit(client, admin_headers, credited["id"])

    month = today().strftime("%Y-%m")
    assert client.get("/api/v1/reports/vat", headers=cashier_headers).status_code == 403
    vat = client.get("/api/v1/reports/vat", params={"month": month}, headers=admin_headers).json()
    assert vat["invoices"] == 3 and vat["credit_notes"] == 1
    lines = {line["rate"]: line for line in vat["by_rate"]}
    # 19 %: 3.000 + 1.500 + 6.000 invoiced, 6.000 credited
    assert lines["19"] == {"rate": "19", "base": "4.500", "vat": "0.855", "total": "5.355"}
    assert lines["7"] == {"rate": "7", "base": "1.400", "vat": "0.098", "total": "1.498"}
    assert vat["fiscal_stamps"] == "3.000"  # 3 invoices, stamp not credited
    assert vat["total"] == "9.853"

    r = client.get("/api/v1/reports/vat", params={"month": "2026-13"}, headers=admin_headers)
    assert r.status_code == 422


def test_sales_journal(client, admin_headers, cashier_headers, make_order, customer, products):
    a = _pay(client, cashier_headers, make_order(customer_id=customer.id)["id"])
    _credit(client, admin_headers, a["id"])
    day = today().isoformat()

    r = client.get("/api/v1/reports/sales-journal.csv", params={"start": day, "end": day},
                   headers=admin_headers)  # fmt: skip
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿")), delimiter=";"))
    assert [row["type"] for row in rows] == ["facture", "avoir"]
    invoice, note = rows
    assert invoice["customer_tax_id"] == "1234567/A/M/000"
    assert invoice["base_ht_19"] == "3,000" and invoice["tva_19"] == "0,570"
    assert invoice["total_ttc"] == "4,570"
    assert note["credited_invoice"] == invoice["number"]
    assert note["total_ttc"] == "-3,570" and note["fiscal_stamp"] == "0,000"

    r = client.get("/api/v1/reports/sales-journal.csv",
                   params={"start": day, "end": day, "format": "standard"}, headers=admin_headers)  # fmt: skip
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
    assert rows[0]["total_ttc"] == "4.570"


def test_daily_report_counts_refunds_separately(client, admin_headers, cashier_headers, make_order):
    order = _pay(client, cashier_headers, make_order()["id"])  # 4.570
    _credit(client, admin_headers, order["id"])  # credits 3.570 (no stamp)
    report = client.get("/api/v1/reports/daily", headers=cashier_headers).json()
    # The sale still happened today; the credit note is reported on its own.
    assert report["revenue"] == "4.570"
    assert report["refunds"] == "3.570"
    assert report["net_revenue"] == "1.000"


@pytest.mark.parametrize(
    "amount,words",
    [
        ("1.000", "un dinar"),
        ("21.000", "vingt et un dinars"),
        ("80.000", "quatre-vingts dinars"),
        ("200.500", "deux cents dinars et cinq cents millimes"),
        ("1280.001", "mille deux cent quatre-vingts dinars et un millime"),
        ("71971.000", "soixante et onze mille neuf cent soixante et onze dinars"),
    ],
)
def test_amount_in_words(amount, words):
    assert amount_in_words(Decimal(amount)) == words

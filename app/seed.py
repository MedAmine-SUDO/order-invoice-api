"""Fill an empty database with a demo business: an office-supplies distributor in Tunis.

Two logins, a catalogue, company customers and about six weeks of activity: counter sales paid on
the spot, company orders invoiced on delivery and paid later by cheque or transfer (some still
unpaid), a few cancellations and credit notes.

Run with `python -m app.seed`. Does nothing if the admin account already exists.
`python -m app.seed --reset` wipes every table first (what the nightly demo reset does).
"""

import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.db import Base, SessionLocal, utcnow
from app.models import (
    Customer,
    Order,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Product,
    Role,
    StockMovement,
    StockReason,
    User,
)
from app.schemas import OrderCreate, OrderItemIn
from app.services.documents import issue_credit_note, issue_invoice
from app.services.orders import create_order

# sku, name, price before tax, VAT % (illustrative), stock, max quantity per order.
# A distributor quotes prices before tax; with one decimal at most, VAT is always exact.
CATALOGUE = [
    ("PAP-A4-80", "Ramette papier A4 80 g (500 feuilles)", "11.800", "19", 3000, 25),
    ("PAP-A3-80", "Ramette papier A3 80 g (500 feuilles)", "25.200", "19", 400, 5),
    ("STY-BLE-50", "Stylos bille bleus (boîte de 50)", "16.800", "19", 600, 6),
    ("CLA-LEV-75", "Classeur à levier dos 75 mm", "4.200", "19", 1500, 20),
    ("TON-HP-85A", "Toner compatible HP 85A", "58.000", "19", 250, 3),
    ("CAR-EP-603", "Cartouche Epson 603 noir", "29.400", "19", 300, 4),
    ("CLE-USB-64", "Clé USB 64 Go", "21.000", "19", 400, 10),
    ("SOU-SF", "Souris sans fil", "25.200", "19", 250, 5),
    ("CAH-96-10", "Cahiers 96 pages (lot de 10)", "14.000", "7", 800, 10),
    ("AGE-2027", "Agenda 2027", "28.000", "7", 300, 15),
    ("POS-IT-12", "Post-it 76 × 76 mm (lot de 12)", "10.100", "19", 600, 10),
    ("CHA-ERG", "Chaise de bureau ergonomique", "295.000", "19", 90, 2),
    # Kept out of the sales so the low-stock filter has something to show.
    ("AGR-24-6", "Agrafeuse 24/6", "12.600", "19", 4, 0),
]

# name, email, phone, address, matricule fiscal (all fictional)
COMPANIES = [
    ("Atlas Logistique SARL", "achats@atlas-logistique.tn", "+216 71 940 112",
     "Zone industrielle, 2013 Ben Arous", "1523847/B/M/000"),
    ("Cabinet Horizon Expertise", "contact@horizon-expertise.tn", "+216 71 196 350",
     "Immeuble Les Berges, Lac 2, 1053 Tunis", "1678452/N/P/000"),
    ("École privée Les Jasmins", "economat@lesjasmins.tn", "+216 70 682 400",
     "Rue de l'Indépendance, 2080 Ariana", "1345298/A/M/000"),
    ("Centre Médical Les Oliviers", "admin@cm-oliviers.tn", "+216 74 299 810",
     "Route de Tunis km 3, 3027 Sfax", "1402765/M/A/000"),
    ("Studio Médina Digital", "hello@medina-digital.tn", "+216 29 870 455",
     "Rue d'Espagne, 1000 Tunis", "1789012/D/M/000"),
]  # fmt: skip

CREDIT_REASONS = ["Marchandise retournée", "Erreur de quantité", "Article défectueux"]
DAYS = 42


@dataclass(order=True)
class Event:
    at: datetime
    seq: int
    action: str = field(compare=False)
    order: Order = field(compare=False)
    method: PaymentMethod | None = field(default=None, compare=False)


def _business_time(rng: random.Random, day, tz: ZoneInfo) -> datetime:
    minutes = rng.randint(8 * 60, 17 * 60 + 30)
    return datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=tz).astimezone(
        ZoneInfo("UTC")
    )


def seed(db: Session, rng: random.Random | None = None) -> bool:
    if db.scalar(select(User).where(User.email == "admin@demo.tn")):
        return False
    rng = rng or random.Random(2026)
    tz = ZoneInfo(settings.timezone)

    password = hash_password(settings.demo_password)
    admin = User(email="admin@demo.tn", full_name="Amine (gérant)", role=Role.admin,
                 hashed_password=password)  # fmt: skip
    cashier = User(email="cashier@demo.tn", full_name="Salma (comptoir)", role=Role.cashier,
                   hashed_password=password)  # fmt: skip
    db.add_all([admin, cashier])

    products = [
        Product(sku=sku, name=name, unit_price=Decimal(price), tax_rate=Decimal(tax), stock=stock)
        for sku, name, price, tax, stock, _ in CATALOGUE
    ]
    max_qty = {sku: q for sku, *_, q in CATALOGUE}
    companies = [
        Customer(name=n, email=e, phone=p, address=a, tax_id=t) for n, e, p, a, t in COMPANIES
    ]
    walk_in_regular = Customer(name="Mohamed Gharbi", phone="+216 98 123 456")
    db.add_all([*products, *companies, walk_in_regular])
    db.flush()
    for product in products:
        db.add(StockMovement(product_id=product.id, delta=product.stock,
                             reason=StockReason.initial, user_id=admin.id))  # fmt: skip
    db.commit()

    # 1. Orders, oldest first, with what will happen to each of them.
    now = utcnow()
    today = now.astimezone(tz).date()
    sellable = [p for p in products if max_qty[p.sku]]
    events: list[Event] = []
    seq = 0

    def plan(at: datetime, action: str, order: Order, method: PaymentMethod | None = None):
        nonlocal seq
        if at <= now:
            seq += 1
            events.append(Event(at, seq, action, order, method))

    for days_ago in range(DAYS, -1, -1):
        day = today - timedelta(days=days_ago)
        if day.weekday() == 6:  # closed on Sunday
            continue
        count = rng.randint(1, 3) if day.weekday() == 5 else rng.randint(3, 6)
        times = sorted(_business_time(rng, day, tz) for _ in range(count))
        for created in (t for t in times if t <= now):
            company = rng.random() < 0.6
            customer = (
                rng.choice(companies) if company else rng.choice([None, None, walk_in_regular])
            )
            picks = rng.sample(sellable, rng.randint(2, 5) if company else rng.randint(1, 3))
            lines = [(p, rng.randint(1, max_qty[p.sku])) for p in picks]
            lines = [(p, q) for p, q in lines if p.stock >= q] or [(sellable[0], 1)]
            data = OrderCreate(
                customer_id=customer.id if customer else None,
                items=[OrderItemIn(product_id=p.id, quantity=q) for p, q in lines],
            )
            order = create_order(db, data, admin if company else cashier)
            order.created_at = created
            db.commit()

            if company:
                # Invoiced on delivery, paid later by cheque or transfer (or not yet).
                plan(created + timedelta(minutes=30), "invoice", order)
                if rng.random() < 0.8:
                    method = rng.choice([PaymentMethod.transfer, PaymentMethod.cheque])
                    plan(created + timedelta(days=rng.randint(3, 25)), "pay", order, method)
                if rng.random() < 0.04:
                    plan(created + timedelta(days=rng.randint(1, 2)), "credit", order)
            elif rng.random() < 0.06:
                plan(created + timedelta(minutes=10), "cancel", order)
            else:
                method = rng.choices(
                    [PaymentMethod.cash, PaymentMethod.mock, PaymentMethod.cheque], [6, 3, 1]
                )[0]
                plan(created + timedelta(minutes=rng.randint(2, 15)), "pay", order, method)
                if rng.random() < 0.03:
                    plan(created + timedelta(days=rng.randint(1, 4)), "credit", order)

    # 2. Replay in time order, so invoice and credit note numbers follow the calendar.
    for event in sorted(events):
        order = event.order
        if event.action == "invoice":
            issue_invoice(db, order, at=event.at)
        elif event.action == "pay" and order.status == OrderStatus.pending:
            issue_invoice(db, order, at=event.at)
            db.add(Payment(order=order, method=event.method, status=PaymentStatus.succeeded,
                           amount=order.total, currency=order.currency, created_at=event.at,
                           completed_at=event.at))  # fmt: skip
            order.status, order.paid_at = OrderStatus.paid, event.at
        elif event.action == "cancel":
            order.status, order.cancelled_at = OrderStatus.cancelled, event.at
            for item in order.items:
                db.get(Product, item.product_id).stock += item.quantity
        elif event.action == "credit" and order.invoice and order.status != OrderStatus.cancelled:
            issue_credit_note(db, order, rng.choice(CREDIT_REASONS), at=event.at)
            if order.status == OrderStatus.paid:
                order.status, order.refunded_at = OrderStatus.refunded, event.at
            else:
                order.status, order.cancelled_at = OrderStatus.cancelled, event.at
        db.commit()
    return True


def reset(db: Session) -> None:
    """Delete everything and seed again, so the public demo starts each day clean."""
    tables = Base.metadata.sorted_tables
    if db.bind.dialect.name == "postgresql":
        # Also restarts the id sequences, so invoice numbers start again from 1.
        names = ", ".join(t.name for t in tables)
        db.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    else:
        for table in reversed(tables):
            db.execute(table.delete())
    db.commit()
    seed(db)


def main() -> None:
    with SessionLocal() as db:
        if "--reset" in sys.argv:
            reset(db)
            print("Demo data reset.")
        else:
            print("Demo data created." if seed(db) else "Demo data already present.")


if __name__ == "__main__":
    main()

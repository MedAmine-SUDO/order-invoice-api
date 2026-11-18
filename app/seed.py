"""Fill an empty database with a demo café: two logins, a menu, customers and a week of sales.

Run with `python -m app.seed`. Does nothing if the admin account already exists.
`python -m app.seed --reset` wipes every table first (what the nightly demo reset does).
"""

import random
import sys
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.db import Base, SessionLocal, utcnow
from app.models import (
    Customer,
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
from app.services.orders import cancel_order, create_order

# sku, name, price before tax, VAT %, stock
MENU = [
    ("CAF-EXP", "Café express", "1.513", "19", 400),
    ("CAF-CAP", "Cappuccino", "2.941", "19", 300),
    ("CAF-DIR", "Café direct", "2.101", "19", 300),
    ("THE-MEN", "Thé à la menthe", "1.008", "19", 300),
    ("JUS-ORA", "Jus d'orange pressé", "3.361", "19", 120),
    ("EAU-150", "Eau minérale 1,5 L", "0.841", "7", 200),
    ("CRO-BEU", "Croissant au beurre", "1.402", "7", 150),
    ("PAI-CHO", "Pain au chocolat", "1.682", "7", 150),
    ("SAN-THO", "Sandwich thon", "4.202", "19", 60),
    ("PIZ-MAR", "Pizza margherita", "9.244", "19", 40),
    ("SAL-MEC", "Salade méchouia", "5.042", "19", 40),
    ("GAT-BAM", "Bambalouni", "0.935", "7", 4),
]

CUSTOMERS = [
    ("Société Jasmin Conseil", "compta@jasmin-conseil.tn", "+216 71 555 010", "1456789/B/M/000"),
    ("Yasmine Trabelsi", "yasmine.t@example.tn", "+216 22 345 678", None),
    ("Hôtel Les Berges", "achats@lesberges.tn", "+216 71 860 200", "0987654/A/P/000"),
]


def seed(db: Session, rng: random.Random | None = None) -> bool:
    if db.scalar(select(User).where(User.email == "admin@demo.tn")):
        return False
    rng = rng or random.Random(2026)

    password = hash_password(settings.demo_password)
    admin = User(email="admin@demo.tn", full_name="Amine (gérant)", role=Role.admin,
                 hashed_password=password)  # fmt: skip
    cashier = User(email="cashier@demo.tn", full_name="Salma (caisse)", role=Role.cashier,
                   hashed_password=password)  # fmt: skip
    db.add_all([admin, cashier])

    products = [
        Product(sku=sku, name=name, unit_price=Decimal(price), tax_rate=Decimal(tax), stock=stock)
        for sku, name, price, tax, stock in MENU
    ]
    customers = [Customer(name=n, email=e, phone=p, tax_id=t) for n, e, p, t in CUSTOMERS]
    db.add_all(products + customers)
    db.flush()
    for product in products:
        db.add(StockMovement(product_id=product.id, delta=product.stock,
                             reason=StockReason.initial, user_id=admin.id))  # fmt: skip
    db.commit()

    # A week of sales, oldest first, so invoice numbers follow the dates.
    sellable = products[:-1]  # leave the last item low on stock for the low_stock filter
    now = utcnow()
    for days_ago in range(6, -1, -1):
        for _ in range(rng.randint(6, 12)):
            picks = rng.sample(sellable, rng.randint(1, 4))
            data = OrderCreate(
                customer_id=rng.choice([None, None, None, *[c.id for c in customers]]),
                items=[OrderItemIn(product_id=p.id, quantity=rng.randint(1, 3)) for p in picks],
            )
            order = create_order(db, data, rng.choice([admin, cashier, cashier]))
            created = now - timedelta(
                days=days_ago, hours=rng.randint(0, 9), minutes=rng.randint(0, 59)
            )
            order.created_at = created

            roll = rng.random()
            if roll < 0.08:
                cancel_order(db, order, cashier)
                order.cancelled_at = created + timedelta(minutes=5)
            elif roll < 0.85 or days_ago > 0:
                method = PaymentMethod.cash if rng.random() < 0.6 else PaymentMethod.mock
                paid = min(created + timedelta(minutes=rng.randint(1, 40)), now)
                db.add(Payment(order=order, method=method, status=PaymentStatus.succeeded,
                               amount=order.total, currency=order.currency, completed_at=paid,
                               created_at=created))  # fmt: skip
                order.status, order.paid_at = OrderStatus.paid, paid
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

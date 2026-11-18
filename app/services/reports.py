import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db import as_utc
from app.models import Order, OrderItem, OrderStatus, Payment, PaymentStatus
from app.schemas import DailyReport, MethodTotal, TopProduct
from app.services.orders import money


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """A local calendar day (Africa/Tunis by default) as a UTC [start, end) range."""
    tz = ZoneInfo(settings.timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def today() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def daily_report(db: Session, day: date) -> DailyReport:
    start, end = day_bounds(day)
    created = (Order.created_at >= start) & (Order.created_at < end)
    paid_that_day = (
        (Order.status == OrderStatus.paid) & (Order.paid_at >= start) & (Order.paid_at < end)
    )

    counts = dict(
        db.execute(select(Order.status, func.count()).where(created).group_by(Order.status)).all()
    )
    orders_paid, revenue, tax = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Order.total), 0),
            func.coalesce(func.sum(Order.tax_total), 0),
        ).where(paid_that_day)
    ).one()
    revenue, tax = Decimal(revenue), Decimal(tax)

    by_method = db.execute(
        select(Payment.method, func.count(), func.sum(Payment.amount))
        .join(Order)
        .where(paid_that_day, Payment.status == PaymentStatus.succeeded)
        .group_by(Payment.method)
        .order_by(func.sum(Payment.amount).desc())
    ).all()

    qty = func.sum(OrderItem.quantity)
    top = db.execute(
        select(OrderItem.product_id, OrderItem.name, qty, func.sum(OrderItem.line_total))
        .join(Order)
        .where(paid_that_day)
        .group_by(OrderItem.product_id, OrderItem.name)
        .order_by(qty.desc(), OrderItem.product_id)
        .limit(5)
    ).all()
    items_sold = db.scalar(select(func.coalesce(qty, 0)).join(Order).where(paid_that_day))

    return DailyReport(
        date=day,
        timezone=settings.timezone,
        currency=settings.currency,
        orders_created=sum(counts.values()),
        orders_paid=orders_paid,
        orders_cancelled=counts.get(OrderStatus.cancelled, 0),
        revenue=money(revenue),
        tax_collected=money(tax),
        average_basket=money(revenue / orders_paid) if orders_paid else Decimal("0.000"),
        items_sold=items_sold or 0,
        payments_by_method=[
            MethodTotal(method=m, count=c, amount=money(Decimal(a))) for m, c, a in by_method
        ],
        top_products=[
            TopProduct(product_id=p, name=n, quantity=q, revenue=money(Decimal(r)))
            for p, n, q, r in top
        ],
    )


CSV_COLUMNS = [
    "number", "created_at", "paid_at", "status", "customer", "cashier",
    "items", "subtotal_ht", "tax", "fiscal_stamp", "total_ttc", "currency", "payment_method",
]  # fmt: skip


def orders_csv(db: Session, start_day: date, end_day: date) -> str:
    """Every order created between two local days (inclusive), one row per order.

    Starts with a UTF-8 BOM so Excel opens accents correctly.
    """
    tz = ZoneInfo(settings.timezone)
    start, _ = day_bounds(start_day)
    _, end = day_bounds(end_day)
    orders = db.scalars(
        select(Order)
        .where(Order.created_at >= start, Order.created_at < end)
        .options(
            selectinload(Order.items),
            selectinload(Order.payments),
            selectinload(Order.customer),
            selectinload(Order.created_by),
        )
        .order_by(Order.created_at)
    )

    def local(ts: datetime | None) -> str:
        return as_utc(ts).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if ts else ""

    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for o in orders:
        method = next((p.method for p in o.payments if p.status == PaymentStatus.succeeded), "")
        writer.writerow(
            [
                o.number, local(o.created_at), local(o.paid_at), o.status,
                o.customer.name if o.customer else "", o.created_by.full_name,
                sum(i.quantity for i in o.items), o.subtotal, o.tax_total, o.stamp_duty,
                o.total, o.currency, method,
            ]
        )  # fmt: skip
    return out.getvalue()

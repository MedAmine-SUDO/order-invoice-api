"""Invoices and credit notes: issued once, numbered without gaps, never changed afterwards."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import as_utc, utcnow
from app.models import DocumentSequence, Invoice, InvoiceKind, Order

PREFIXES = {InvoiceKind.invoice: "FAC", InvoiceKind.credit_note: "AV"}


def next_number(db: Session, prefix: str, year: int) -> str:
    """Take the next number in a yearly sequence, holding a row lock until the commit.

    Two tills paying at the same moment wait for each other instead of skipping or
    reusing a number, and a rolled-back transaction gives its number back.
    """
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    db.execute(
        insert(DocumentSequence)
        .values(prefix=prefix, year=year, last_value=0)
        .on_conflict_do_nothing()
    )
    sequence = db.scalars(
        select(DocumentSequence)
        .where(DocumentSequence.prefix == prefix, DocumentSequence.year == year)
        .with_for_update()
    ).one()
    sequence.last_value += 1
    return f"{prefix}-{year}-{sequence.last_value:05d}"


def _customer_snapshot(order: Order) -> dict | None:
    c = order.customer
    if c is None:
        return None
    return {
        "name": c.name,
        "address": c.address,
        "phone": c.phone,
        "email": c.email,
        "tax_id": c.tax_id,
    }


def _issue(
    db: Session,
    order: Order,
    kind: InvoiceKind,
    at: datetime | None,
    stamp: Decimal,
    **extra,
) -> Invoice:
    at = at or utcnow()
    year = as_utc(at).astimezone(ZoneInfo(settings.timezone)).year
    document = Invoice(
        number=next_number(db, PREFIXES[kind], year),
        kind=kind,
        order=order,
        issued_at=at,
        customer=_customer_snapshot(order),
        subtotal=order.subtotal,
        tax_total=order.tax_total,
        stamp_duty=stamp,
        total=order.subtotal + order.tax_total + stamp,
        currency=order.currency,
        **extra,
    )
    db.add(document)
    return document


def issue_invoice(db: Session, order: Order, at: datetime | None = None) -> Invoice:
    """The invoice for a paid order. Does nothing if it already has one."""
    return order.invoice or _issue(db, order, InvoiceKind.invoice, at, order.stamp_duty)


def issue_credit_note(
    db: Session, order: Order, reason: str, at: datetime | None = None
) -> Invoice:
    """An "avoir" cancelling the order's invoice in full."""
    invoice = order.invoice
    stamp = invoice.stamp_duty if settings.credit_note_includes_stamp else Decimal("0.000")
    return _issue(
        db,
        order,
        InvoiceKind.credit_note,
        at,
        stamp,
        credited_invoice=invoice,
        reason=reason,
    )

import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db import as_utc
from app.models import Invoice, InvoiceKind, Order, OrderItem, OrderStatus, Payment, PaymentStatus
from app.schemas import DailyReport, MethodTotal, TopProduct, VatLine, VatReport
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
    # Refunded orders still count on the day they were paid; the refund shows up on its own day.
    paid_that_day = (
        Order.status.in_([OrderStatus.paid, OrderStatus.refunded])
        & (Order.paid_at >= start)
        & (Order.paid_at < end)
    )
    refunds = Decimal(
        db.scalar(
            select(func.coalesce(func.sum(Invoice.total), 0)).where(
                Invoice.kind == InvoiceKind.credit_note,
                Invoice.issued_at >= start,
                Invoice.issued_at < end,
            )
        )
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
        refunds=money(refunds),
        net_revenue=money(revenue - refunds),
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


def month_bounds(month: str) -> tuple[datetime, datetime]:
    year, mon = (int(x) for x in month.split("-"))
    first = date(year, mon, 1)
    following = date(year + mon // 12, mon % 12 + 1, 1)
    return day_bounds(first)[0], day_bounds(following)[0]


def vat_report(db: Session, month: str) -> VatReport:
    """Invoices minus credit notes issued in a month, by VAT rate."""
    start, end = month_bounds(month)
    in_month = (Invoice.issued_at >= start) & (Invoice.issued_at < end)
    sign = {InvoiceKind.invoice: 1, InvoiceKind.credit_note: -1}

    lines: dict[Decimal, list[Decimal]] = {}
    for kind, rate, base, vat in db.execute(
        select(Invoice.kind, OrderItem.tax_rate, func.sum(OrderItem.line_subtotal),
               func.sum(OrderItem.line_tax))
        .join(OrderItem, OrderItem.order_id == Invoice.order_id)
        .where(in_month)
        .group_by(Invoice.kind, OrderItem.tax_rate)
    ):  # fmt: skip
        acc = lines.setdefault(Decimal(rate), [Decimal(0), Decimal(0)])
        acc[0] += sign[kind] * Decimal(base)
        acc[1] += sign[kind] * Decimal(vat)

    counts, stamps = {}, Decimal(0)
    for kind, count, stamp in db.execute(
        select(Invoice.kind, func.count(), func.sum(Invoice.stamp_duty))
        .where(in_month)
        .group_by(Invoice.kind)
    ):
        counts[kind] = count
        stamps += sign[kind] * Decimal(stamp)
    first, last = db.execute(
        select(func.min(Invoice.number), func.max(Invoice.number)).where(
            in_month, Invoice.kind == InvoiceKind.invoice
        )
    ).one()

    by_rate = [
        VatLine(rate=rate.normalize(), base=money(base), vat=money(vat), total=money(base + vat))
        for rate, (base, vat) in sorted(lines.items(), reverse=True)
    ]
    base = sum((line.base for line in by_rate), Decimal("0.000"))
    vat = sum((line.vat for line in by_rate), Decimal("0.000"))
    return VatReport(
        month=month,
        currency=settings.currency,
        invoices=counts.get(InvoiceKind.invoice, 0),
        credit_notes=counts.get(InvoiceKind.credit_note, 0),
        first_invoice=first,
        last_invoice=last,
        by_rate=by_rate,
        base=base,
        vat=vat,
        fiscal_stamps=money(stamps),
        total=money(base + vat + stamps),
    )


class CsvWriter:
    """CSV that opens cleanly in Excel: UTF-8 BOM, and in French format by default
    (";" between columns, "12,500" for amounts) since Excel in French reads "12.500" as 12500.
    """

    def __init__(self, french: bool = True):
        self.french = french
        self.out = io.StringIO()
        self.out.write("\ufeff")
        self.writer = csv.writer(self.out, delimiter=";" if french else ",")

    def row(self, values) -> None:
        values = [abs(v) if isinstance(v, Decimal) and v == 0 else v for v in values]  # no "-0"
        self.writer.writerow(
            [str(v).replace(".", ",") if self.french and isinstance(v, Decimal) else v
             for v in values]
        )  # fmt: skip

    def text(self) -> str:
        return self.out.getvalue()


def _local(ts: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return as_utc(ts).astimezone(ZoneInfo(settings.timezone)).strftime(fmt) if ts else ""


ORDER_COLUMNS = [
    "number", "created_at", "paid_at", "status", "invoice", "customer", "cashier",
    "items", "subtotal_ht", "tax", "fiscal_stamp", "total_ttc", "currency", "payment_method",
]  # fmt: skip


def orders_csv(db: Session, start_day: date, end_day: date, french: bool = True) -> str:
    """Every order created between two local days (inclusive), one row per order."""
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
            selectinload(Order.invoices),
        )
        .order_by(Order.created_at)
    )
    out = CsvWriter(french)
    out.row(ORDER_COLUMNS)
    for o in orders:
        method = next((p.method for p in o.payments if p.status == PaymentStatus.succeeded), "")
        out.row(
            [
                o.number, _local(o.created_at), _local(o.paid_at), o.status,
                o.invoice.number if o.invoice else "",
                o.customer.name if o.customer else "", o.created_by.full_name,
                sum(i.quantity for i in o.items), o.subtotal, o.tax_total, o.stamp_duty,
                o.total, o.currency, method,
            ]
        )  # fmt: skip
    return out.text()


def sales_journal_csv(db: Session, start_day: date, end_day: date, french: bool = True) -> str:
    """The sales journal ("journal des ventes"): one row per invoice or credit note, with the
    base and VAT split by rate. Credit notes are negative, so every column adds up.
    """
    start, _ = day_bounds(start_day)
    _, end = day_bounds(end_day)
    documents = list(
        db.scalars(
            select(Invoice)
            .where(Invoice.issued_at >= start, Invoice.issued_at < end)
            .options(
                selectinload(Invoice.order).selectinload(Order.items),
                selectinload(Invoice.order).selectinload(Order.payments),
                selectinload(Invoice.credited_invoice),
            )
            .order_by(Invoice.issued_at, Invoice.id)
        )
    )
    rates = sorted({i.tax_rate for d in documents for i in d.order.items}, reverse=True)
    out = CsvWriter(french)
    out.row(
        ["date", "type", "number", "credited_invoice", "order", "customer", "customer_tax_id"]
        + [col for r in rates for col in (f"base_ht_{r.normalize():f}", f"tva_{r.normalize():f}")]
        + ["fiscal_stamp", "total_ttc", "currency", "payment_method", "reason"]
    )
    for d in documents:
        sign = -1 if d.kind == InvoiceKind.credit_note else 1
        split = {r: [Decimal("0.000"), Decimal("0.000")] for r in rates}
        for item in d.order.items:
            split[item.tax_rate][0] += item.line_subtotal
            split[item.tax_rate][1] += item.line_tax
        payment = next(
            (p.method for p in d.order.payments if p.status == PaymentStatus.succeeded), ""
        )
        customer = d.customer or {}
        out.row(
            [
                _local(d.issued_at, "%Y-%m-%d"),
                "avoir" if d.kind == InvoiceKind.credit_note else "facture",
                d.number,
                d.credited_invoice.number if d.credited_invoice else "",
                d.order.number,
                customer.get("name", "Client de passage"),
                customer.get("tax_id") or "",
            ]
            + [sign * v for r in rates for v in split[r]]
            + [sign * d.stamp_duty, sign * d.total, d.currency, payment, d.reason or ""]
        )
    return out.text()

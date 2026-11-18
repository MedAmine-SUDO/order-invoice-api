import logging
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import utcnow
from app.models import (
    Customer,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Product,
    StockMovement,
    StockReason,
    User,
)
from app.schemas import OrderCreate
from app.services.payments.base import PaymentError, PaymentProvider

MILLIME = Decimal("0.001")
log = logging.getLogger(__name__)


def money(value: Decimal) -> Decimal:
    return value.quantize(MILLIME, rounding=ROUND_HALF_UP)


def create_order(db: Session, data: OrderCreate, user: User) -> Order:
    if data.customer_id is not None and db.get(Customer, data.customer_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")

    quantities = Counter()
    for item in data.items:
        quantities[item.product_id] += item.quantity

    # Lock the rows (sorted, to avoid deadlocks) so two cashiers can't sell the last unit twice.
    products = {
        p.id: p
        for p in db.scalars(
            select(Product).where(Product.id.in_(quantities)).order_by(Product.id).with_for_update()
        )
    }
    missing = sorted(set(quantities) - products.keys())
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Products not found: {missing}")

    order = Order(
        customer_id=data.customer_id,
        created_by_id=user.id,
        note=data.note,
        currency=settings.currency,
        status=OrderStatus.pending,
    )
    subtotal = tax_total = Decimal("0")
    for product_id, quantity in quantities.items():
        product = products[product_id]
        if not product.is_active:
            raise HTTPException(status.HTTP_409_CONFLICT, f"'{product.name}' is not for sale")
        if product.stock < quantity:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Not enough stock for '{product.name}': {product.stock} left, {quantity} asked",
            )
        line_subtotal = money(product.unit_price * quantity)
        line_tax = money(line_subtotal * product.tax_rate / 100)
        order.items.append(
            OrderItem(
                product_id=product.id,
                sku=product.sku,
                name=product.name,
                unit_price=product.unit_price,
                tax_rate=product.tax_rate,
                quantity=quantity,
                line_subtotal=line_subtotal,
                line_tax=line_tax,
                line_total=line_subtotal + line_tax,
            )
        )
        subtotal += line_subtotal
        tax_total += line_tax
        product.stock -= quantity

    order.subtotal = subtotal
    order.tax_total = tax_total
    order.stamp_duty = money(settings.fiscal_stamp)
    order.total = subtotal + tax_total + order.stamp_duty
    db.add(order)
    db.flush()
    order.number = f"FAC-{order.created_at.year}-{order.id:06d}"
    for item in order.items:
        db.add(
            StockMovement(
                product_id=item.product_id,
                delta=-item.quantity,
                reason=StockReason.sale,
                order_id=order.id,
                user_id=user.id,
            )
        )
    db.commit()
    return order


def cancel_order(db: Session, order: Order, user: User) -> Order:
    if order.status != OrderStatus.pending:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Only pending orders can be cancelled (this one is {order.status})",
        )
    order.status = OrderStatus.cancelled
    order.cancelled_at = utcnow()
    for item in order.items:
        product = db.get(Product, item.product_id, with_for_update=True)
        product.stock += item.quantity
        db.add(
            StockMovement(
                product_id=product.id,
                delta=item.quantity,
                reason=StockReason.cancellation,
                order_id=order.id,
                user_id=user.id,
            )
        )
    for payment in order.payments:
        if payment.status == PaymentStatus.pending:
            payment.status = PaymentStatus.failed
            payment.completed_at = utcnow()
    db.commit()
    return order


def start_payment(db: Session, order: Order, method: str, provider: PaymentProvider) -> Payment:
    """Record a cash payment, or open a checkout on the online gateway."""
    if order.status != OrderStatus.pending:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Order is already {order.status}")
    if method == "cash":
        payment = Payment(
            order=order,
            method=PaymentMethod.cash,
            amount=order.total,
            currency=order.currency,
            status=PaymentStatus.pending,
        )
        db.add(payment)
        db.flush()
        settle_payment(db, payment, succeeded=True)
        return payment

    # Reuse an open checkout instead of creating a new one every time someone scans the QR.
    for existing in order.payments:
        if existing.status == PaymentStatus.pending and existing.method == provider.method:
            return existing
    payment = Payment(
        order=order,
        method=provider.method,
        amount=order.total,
        currency=order.currency,
        status=PaymentStatus.pending,
    )
    db.add(payment)
    db.flush()
    try:
        checkout = provider.create_checkout(payment, order)
    except PaymentError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    payment.provider_ref = checkout.provider_ref
    payment.pay_url = checkout.pay_url
    db.commit()
    return payment


def settle_payment(db: Session, payment: Payment, succeeded: bool, details: dict | None = None):
    """Record a gateway's verdict. Safe to call several times for the same payment.

    A success always wins, even on a checkout we had closed: the money has moved. If the order
    was already paid or cancelled by then, the payment is flagged for a refund.
    """
    if payment.status == PaymentStatus.succeeded:
        return
    if payment.status == PaymentStatus.failed and not succeeded:
        return
    payment.status = PaymentStatus.succeeded if succeeded else PaymentStatus.failed
    payment.completed_at = utcnow()
    details = dict(details or {})
    order = payment.order
    if succeeded:
        if order.status == OrderStatus.pending:
            order.status = OrderStatus.paid
            order.paid_at = payment.completed_at
            # Close the other open checkouts so the customer can't pay twice.
            for other in order.payments:
                if other is not payment and other.status == PaymentStatus.pending:
                    other.status = PaymentStatus.failed
                    other.completed_at = payment.completed_at
        else:
            details["needs_refund"] = True
            log.warning("Payment %s succeeded on %s order %s", payment.id, order.status, order.id)
    if details:
        payment.details = {**(payment.details or {}), **details}
    db.commit()

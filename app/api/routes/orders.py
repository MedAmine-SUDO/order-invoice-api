from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import AdminUser, CurrentUser, DbSession, Paginate
from app.models import Invoice, InvoiceKind, Order, OrderStatus
from app.schemas import CreditNoteCreate, OrderCreate, OrderOut, Page, PaymentCreate, PaymentOut
from app.services import invoice, orders, reports
from app.services.payments import PaymentProvider, get_payment_provider

router = APIRouter(prefix="/orders", tags=["orders"])

_eager = (
    selectinload(Order.items),
    selectinload(Order.payments),
    selectinload(Order.customer),
    selectinload(Order.invoices),
)


def get_order(db, order_id: int) -> Order:
    order = db.get(Order, order_id, options=_eager)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


@router.get("", response_model=Page[OrderOut])
def list_orders(
    db: DbSession,
    _: CurrentUser,
    page: Paginate,
    status_: Annotated[OrderStatus | None, Query(alias="status")] = None,
    customer_id: int | None = None,
    invoiced: Annotated[
        bool | None, Query(description="`true` + `status=pending`: unpaid invoices.")
    ] = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    """Newest first. Dates are local calendar days (Africa/Tunis by default), inclusive."""
    query = select(Order)
    if status_:
        query = query.where(Order.status == status_)
    if customer_id:
        query = query.where(Order.customer_id == customer_id)
    if invoiced is not None:
        has_invoice = Order.invoices.any(Invoice.kind == InvoiceKind.invoice)
        query = query.where(has_invoice if invoiced else ~has_invoice)
    if date_from:
        query = query.where(Order.created_at >= reports.day_bounds(date_from)[0])
    if date_to:
        query = query.where(Order.created_at < reports.day_bounds(date_to)[1])
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.options(*_eager).order_by(Order.id.desc()).limit(page.limit).offset(page.offset)
    )
    return Page(items=list(items), total=total, limit=page.limit, offset=page.offset)


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(db: DbSession, user: CurrentUser, data: OrderCreate):
    """Create an order. Stock is reserved right away and given back if the order is cancelled."""
    order = orders.create_order(db, data, user)
    return get_order(db, order.id)


@router.get("/{order_id}", response_model=OrderOut)
def read_order(db: DbSession, _: CurrentUser, order_id: int):
    return get_order(db, order_id)


@router.post("/{order_id}/cancel", response_model=OrderOut)
def cancel_order(db: DbSession, user: CurrentUser, order_id: int):
    """Drop an order that was never invoiced, and give the stock back.

    Invoiced orders are cancelled with a credit note instead (POST /orders/{id}/credit-note).
    """
    orders.cancel_order(db, get_order(db, order_id), user)
    return get_order(db, order_id)


@router.post("/{order_id}/invoice", response_model=OrderOut)
def invoice_now(db: DbSession, _: CurrentUser, order_id: int):
    """Issue the invoice before payment, for account customers paying by cheque or transfer.

    Counter sales don't need this: the invoice is issued automatically at payment.
    """
    orders.invoice_order(db, get_order(db, order_id))
    return get_order(db, order_id)


@router.post("/{order_id}/credit-note", response_model=OrderOut)
def credit_note(db: DbSession, admin: AdminUser, order_id: int, data: CreditNoteCreate):
    """Cancel an invoiced order with a credit note ("avoir"). The invoice is never changed.

    Paid orders become `refunded`: paying the money back happens outside the API.
    Unpaid invoiced orders become `cancelled`.
    """
    orders.credit_order(db, get_order(db, order_id), admin, data.reason, data.restock)
    return get_order(db, order_id)


@router.post("/{order_id}/payments", response_model=PaymentOut, status_code=201)
def pay_order(
    db: DbSession,
    _: CurrentUser,
    order_id: int,
    data: PaymentCreate,
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
):
    """Take a payment. For `online`, send the customer to the returned `pay_url`."""
    return orders.start_payment(db, get_order(db, order_id), data.method, provider)


@router.get(
    "/{order_id}/document.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
def order_document(db: DbSession, _: CurrentUser, order_id: int, download: bool = False):
    """The order's current document: its invoice once paid, its order form before that."""
    order = get_order(db, order_id)
    document = order.invoice
    return pdf_response(
        invoice.render_pdf(order, document),
        document.number if document else order.number,
        download,
    )


@router.get(
    "/{order_id}/qr.png",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
def qr_code(db: DbSession, _: CurrentUser, order_id: int):
    """The QR code printed on the invoice, on its own (e.g. for a receipt printer)."""
    return Response(invoice.qr_png(get_order(db, order_id)), media_type="image/png")


def pdf_response(content: bytes, name: str, download: bool = False) -> Response:
    disposition = "attachment" if download else "inline"
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{name}.pdf"'},
    )

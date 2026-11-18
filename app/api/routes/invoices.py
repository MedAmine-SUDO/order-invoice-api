from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, Paginate
from app.api.routes.orders import pdf_response
from app.models import Invoice, InvoiceKind, Order
from app.schemas import InvoiceOut, Page
from app.services import invoice as pdf
from app.services.reports import day_bounds

router = APIRouter(prefix="/invoices", tags=["invoices & credit notes"])


@router.get("", response_model=Page[InvoiceOut])
def list_invoices(
    db: DbSession,
    _: CurrentUser,
    page: Paginate,
    kind: Annotated[InvoiceKind | None, Query()] = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    """Invoices and credit notes, newest first. Dates are local days, inclusive."""
    query = select(Invoice)
    if kind:
        query = query.where(Invoice.kind == kind)
    if date_from:
        query = query.where(Invoice.issued_at >= day_bounds(date_from)[0])
    if date_to:
        query = query.where(Invoice.issued_at < day_bounds(date_to)[1])
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Invoice.id.desc()).limit(page.limit).offset(page.offset))
    return Page(items=list(items), total=total, limit=page.limit, offset=page.offset)


def _get(db, invoice_id: int) -> Invoice:
    document = db.get(
        Invoice,
        invoice_id,
        options=[
            selectinload(Invoice.order).selectinload(Order.items),
            selectinload(Invoice.order).selectinload(Order.invoices),
        ],
    )
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return document


@router.get("/{invoice_id}", response_model=InvoiceOut)
def read_invoice(db: DbSession, _: CurrentUser, invoice_id: int):
    return _get(db, invoice_id)


@router.get(
    "/{invoice_id}/pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
def invoice_pdf(db: DbSession, _: CurrentUser, invoice_id: int, download: bool = False):
    document = _get(db, invoice_id)
    return pdf_response(pdf.render_pdf(document.order, document), document.number, download)

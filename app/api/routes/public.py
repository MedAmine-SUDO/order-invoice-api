"""Pages reached without logging in: the QR code landing page and the sandbox checkout."""

from html import escape
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession
from app.core.config import settings
from app.db import as_utc
from app.models import Order, OrderStatus, Payment, PaymentMethod
from app.schemas import PublicOrder
from app.services import invoice
from app.services.orders import settle_payment, start_payment
from app.services.payments import PaymentProvider, get_payment_provider

router = APIRouter(tags=["public (no login)"])

BADGES = {
    OrderStatus.pending: ("Awaiting payment", "#b45309", "#fef3c7"),
    OrderStatus.paid: ("Paid", "#047857", "#d1fae5"),
    OrderStatus.cancelled: ("Cancelled", "#b91c1c", "#fee2e2"),
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --ink:#111827; --muted:#6b7280; --line:#e5e7eb; --bg:#f6f7f9; --card:#fff; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --ink:#f3f4f6; --muted:#9ca3af; --line:#374151; --bg:#0b0f17; --card:#111827; }}
  }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif }}
  main {{ max-width:460px; margin:0 auto; padding:24px 16px }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px }}
  h1 {{ font-size:18px; margin:0 }} .muted {{ color:var(--muted); font-size:13px }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:99px; font-weight:600;
           font-size:13px }}
  table {{ width:100%; border-collapse:collapse; margin:14px 0 }}
  td {{ padding:7px 0; border-bottom:1px solid var(--line) }} td:last-child {{ text-align:right }}
  .total td {{ border:0; font-weight:700; font-size:17px; padding-top:12px }}
  .btn {{ display:block; width:100%; text-align:center; padding:12px; border-radius:10px;
         border:0; font:inherit; font-weight:600; cursor:pointer; text-decoration:none;
         margin-top:10px }}
  .primary {{ background:#111827; color:#fff }} .ghost {{ background:transparent;
         color:var(--ink); border:1px solid var(--line) }}
  @media (prefers-color-scheme: dark) {{ .primary {{ background:#f3f4f6; color:#111827 }} }}
  .row {{ display:flex; justify-content:space-between; align-items:center; gap:12px }}
</style></head><body><main>{body}</main></body></html>"""


def _money(value) -> str:
    return f"{invoice.fmt(value)} {settings.currency}"


def _get_order(db, token: str) -> Order:
    order = db.scalar(
        select(Order)
        .where(Order.public_token == token)
        .options(selectinload(Order.items), selectinload(Order.payments))
    )
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


@router.get("/o/{token}", response_class=HTMLResponse, summary="QR code landing page")
def order_page(db: DbSession, token: str):
    """What a customer sees after scanning the invoice QR code."""
    order = _get_order(db, token)
    label, fg, bg = BADGES[order.status]
    when = as_utc(order.created_at).astimezone(ZoneInfo(settings.timezone))
    rows = "".join(
        f"<tr><td>{item.quantity} × {escape(item.name)}</td><td>{_money(item.line_total)}</td></tr>"
        for item in order.items
    )
    if order.stamp_duty:
        rows += f"<tr><td>Timbre fiscal</td><td>{_money(order.stamp_duty)}</td></tr>"
    pay = ""
    if order.status == OrderStatus.pending:
        pay = (
            f'<form method="post" action="/o/{token}/pay">'
            '<button class="btn primary">Pay online</button></form>'
        )
    body = f"""
    <div class="card">
      <div class="row"><div><div class="muted">{escape(settings.business_name)}</div>
        <h1>Order {order.number}</h1></div>
        <span class="badge" style="color:{fg};background:{bg}">{label}</span></div>
      <p class="muted">{when:%d/%m/%Y %H:%M}</p>
      <table>{rows}<tr class="total"><td>Total</td><td>{_money(order.total)}</td></tr></table>
      {pay}
      <a class="btn ghost" href="/o/{token}/invoice.pdf">Download invoice (PDF)</a>
    </div>"""
    return PAGE.format(title=f"Order {order.number}", body=body)


@router.get("/api/v1/public/orders/{token}", response_model=PublicOrder)
def order_status(db: DbSession, token: str):
    """Same as the QR page, as JSON (for a customer app or a kitchen screen)."""
    return _get_order(db, token)


@router.get("/o/{token}/invoice.pdf", response_class=Response, include_in_schema=False)
def public_invoice(db: DbSession, token: str):
    order = _get_order(db, token)
    return Response(
        invoice.render_invoice_pdf(order),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{order.number}.pdf"'},
    )


@router.post("/o/{token}/pay", include_in_schema=False)
def public_pay(
    db: DbSession,
    token: str,
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
):
    order = _get_order(db, token)
    if order.status != OrderStatus.pending:
        return RedirectResponse(f"/o/{token}", status_code=303)
    payment = start_payment(db, order, "online", provider)
    return RedirectResponse(payment.pay_url, status_code=303)


# --- Sandbox gateway (PAYMENT_PROVIDER=mock) ----------------------------------------


def _mock_payment(db, ref: str) -> Payment:
    payment = db.scalar(
        select(Payment).where(Payment.provider_ref == ref, Payment.method == PaymentMethod.mock)
    )
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown sandbox payment")
    return payment


@router.get("/sandbox/pay/{ref}", response_class=HTMLResponse, include_in_schema=False)
def sandbox_checkout(db: DbSession, ref: str):
    payment = _mock_payment(db, ref)
    order = payment.order
    if payment.status != "pending":
        return RedirectResponse(f"/o/{order.public_token}", status_code=303)
    body = f"""
    <div class="card">
      <p class="muted">Sandbox payment gateway · no real money moves</p>
      <h1>Pay {_money(payment.amount)}</h1>
      <p class="muted">{escape(settings.business_name)} · Order {order.number}</p>
      <form method="post" action="/sandbox/pay/{ref}/succeed">
        <button class="btn primary">Pay with test card 4242 4242 4242 4242</button></form>
      <form method="post" action="/sandbox/pay/{ref}/fail">
        <button class="btn ghost">Decline the payment</button></form>
    </div>"""
    return PAGE.format(title="Sandbox checkout", body=body)


@router.post("/sandbox/pay/{ref}/{outcome}", include_in_schema=False)
def sandbox_complete(db: DbSession, ref: str, outcome: str):
    if outcome not in {"succeed", "fail"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    payment = _mock_payment(db, ref)
    settle_payment(db, payment, succeeded=outcome == "succeed", details={"sandbox": outcome})
    return RedirectResponse(f"/o/{payment.order.public_token}", status_code=303)

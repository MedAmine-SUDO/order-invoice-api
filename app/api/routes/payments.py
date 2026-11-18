import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.models import Payment, PaymentMethod
from app.schemas import PaymentOut
from app.services.orders import settle_payment
from app.services.payments import PaymentError, PaymentProvider, get_payment_provider
from app.services.payments.base import verify_signature
from app.services.payments.flouci import FlouciProvider
from app.services.payments.konnect import KonnectProvider
from app.services.payments.stripe import verify_webhook

log = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])


def _by_ref(db, ref: str) -> Payment:
    payment = db.scalar(select(Payment).where(Payment.provider_ref == ref))
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown payment")
    return payment


@router.get("/{payment_id}", response_model=PaymentOut)
def read_payment(db: DbSession, _: CurrentUser, payment_id: int):
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    return payment


@router.post("/webhooks/flouci", include_in_schema=False)
async def flouci_webhook(
    request: Request,
    db: DbSession,
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
    x_flouci_signature: Annotated[str, Header()] = "",
):
    """Flouci's signed webhook (version 2). Signature first, then Flouci confirms the status."""
    payload = await request.body()
    try:
        verify_signature(payload, x_flouci_signature, settings.flouci_private_key)
    except PaymentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    event = json.loads(payload)
    payment = db.scalar(
        select(Payment).where(
            Payment.provider_ref == str(event.get("payment_id")),
            Payment.method == PaymentMethod.flouci,
        )
    )
    if payment is None:
        return {"received": True}  # not ours: acknowledge so Flouci stops retrying
    if not isinstance(provider, FlouciProvider):
        raise HTTPException(status.HTTP_409_CONFLICT, "Flouci is not the active provider")
    try:
        outcome, details = provider.check(payment.provider_ref)
    except PaymentError as exc:
        # A non-2xx answer makes Flouci retry later, which is what we want here.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not verify payment") from exc
    if outcome is not None:
        settle_payment(db, payment, outcome, {**details, "flouci_event": event.get("event_id")})
    return {"received": True}


@router.get("/webhooks/konnect", include_in_schema=False)
def konnect_webhook(
    db: DbSession,
    payment_ref: str,
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
):
    """Konnect calls this with ?payment_ref=. We never trust it: we ask Konnect for the status."""
    payment = _by_ref(db, payment_ref)
    if not isinstance(provider, KonnectProvider):
        raise HTTPException(status.HTTP_409_CONFLICT, "Konnect is not the active provider")
    try:
        completed, details = provider.is_completed(payment_ref)
    except PaymentError as exc:
        log.warning("Konnect status check failed for %s: %s", payment_ref, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not verify payment") from exc
    if completed:
        settle_payment(db, payment, succeeded=True, details=details)
    return {"status": payment.status}


@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(
    request: Request,
    db: DbSession,
    stripe_signature: Annotated[str, Header()],
):
    payload = await request.body()
    try:
        event = verify_webhook(payload, stripe_signature, settings.stripe_webhook_secret)
    except PaymentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    session = event.get("data", {}).get("object", {})
    payment_id = (session.get("metadata") or {}).get("payment_id")
    payment = db.get(Payment, int(payment_id)) if payment_id else None
    if payment is None or payment.method != PaymentMethod.stripe:
        # Not one of ours (Stripe sends every event of the account): acknowledge and move on.
        return {"received": True}
    details = {"stripe_event": event.get("id")}
    if (
        event.get("type") == "checkout.session.completed"
        and session.get("payment_status") == "paid"
    ):
        settle_payment(db, payment, succeeded=True, details=details)
    elif event.get("type") == "checkout.session.expired":
        settle_payment(db, payment, succeeded=False, details=details)
    return {"received": True}

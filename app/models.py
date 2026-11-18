import secrets
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow

# Three decimals: Tunisian dinars are counted in millimes.
Money = Numeric(12, 3)


def _enum(cls: type[StrEnum]) -> Enum:
    # Stored as plain strings so adding a value never needs a database type migration.
    return Enum(cls, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e])


def _timestamp(**kwargs) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kwargs)


class Role(StrEnum):
    admin = "admin"
    cashier = "cashier"


class OrderStatus(StrEnum):
    pending = "pending"
    paid = "paid"
    cancelled = "cancelled"
    refunded = "refunded"


class PaymentMethod(StrEnum):
    cash = "cash"
    cheque = "cheque"
    transfer = "transfer"
    mock = "mock"
    flouci = "flouci"
    konnect = "konnect"
    stripe = "stripe"


class PaymentStatus(StrEnum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"


class StockReason(StrEnum):
    initial = "initial"
    sale = "sale"
    cancellation = "cancellation"
    refund = "refund"
    adjustment = "adjustment"


class InvoiceKind(StrEnum):
    invoice = "invoice"
    credit_note = "credit_note"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(_enum(Role))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _timestamp(default=utcnow)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(String(255))
    # Matricule fiscal, for business customers who need it on the invoice.
    tax_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = _timestamp(default=utcnow)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    # Price before tax (HT).
    unit_price: Mapped[Decimal] = mapped_column(Money)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    stock: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _timestamp(default=utcnow)
    updated_at: Mapped[datetime] = _timestamp(default=utcnow, onupdate=utcnow)


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    delta: Mapped[int]
    reason: Mapped[StockReason] = mapped_column(_enum(StockReason))
    note: Mapped[str | None] = mapped_column(String(255))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _timestamp(default=utcnow)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Order form ("bon de commande") number, assigned right after insert: BC-2026-000042.
    # The invoice gets its own gap-free number when the order is paid (see Invoice).
    number: Mapped[str | None] = mapped_column(String(32), unique=True)
    # Unguessable token behind the QR code, so order ids are never exposed publicly.
    public_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, default=lambda: secrets.token_urlsafe(16)
    )
    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus), default=OrderStatus.pending, index=True
    )
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    subtotal: Mapped[Decimal] = mapped_column(Money)
    tax_total: Mapped[Decimal] = mapped_column(Money)
    stamp_duty: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _timestamp(default=utcnow, index=True)
    paid_at: Mapped[datetime | None] = _timestamp(index=True)
    cancelled_at: Mapped[datetime | None] = _timestamp()
    refunded_at: Mapped[datetime | None] = _timestamp()

    customer: Mapped[Customer | None] = relationship()
    created_by: Mapped[User] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )
    payments: Mapped[list["Payment"]] = relationship(back_populates="order", order_by="Payment.id")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="order", order_by="Invoice.id")

    def _document(self, kind: "InvoiceKind") -> "Invoice | None":
        return next((i for i in self.invoices if i.kind == kind), None)

    @property
    def invoice(self) -> "Invoice | None":
        return self._document(InvoiceKind.invoice)

    @property
    def credit_note(self) -> "Invoice | None":
        return self._document(InvoiceKind.credit_note)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    # Snapshot of the product at sale time, so later price edits never change old invoices.
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160))
    unit_price: Mapped[Decimal] = mapped_column(Money)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    quantity: Mapped[int]
    line_subtotal: Mapped[Decimal] = mapped_column(Money)
    line_tax: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)

    order: Mapped[Order] = relationship(back_populates="items")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    method: Mapped[PaymentMethod] = mapped_column(_enum(PaymentMethod))
    status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus), default=PaymentStatus.pending
    )
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    # The gateway's id for this payment (Flouci payment_id, Stripe session id, ...).
    provider_ref: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    pay_url: Mapped[str | None] = mapped_column(String(1024))
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _timestamp(default=utcnow)
    completed_at: Mapped[datetime | None] = _timestamp()

    order: Mapped[Order] = relationship(back_populates="payments")


class Invoice(Base):
    """An invoice ("facture") or a credit note ("avoir"). Never edited or deleted once issued.

    Amounts are stored positive for both kinds; a credit note cancels the invoice it points to.
    """

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    # FAC-2026-00001 / AV-2026-00001: one gap-free sequence per kind and year.
    number: Mapped[str] = mapped_column(String(32), unique=True)
    kind: Mapped[InvoiceKind] = mapped_column(_enum(InvoiceKind), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    credited_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    issued_at: Mapped[datetime] = _timestamp(default=utcnow, index=True)
    # The customer as they were when the document was issued.
    customer: Mapped[dict | None] = mapped_column(JSON)
    subtotal: Mapped[Decimal] = mapped_column(Money)
    tax_total: Mapped[Decimal] = mapped_column(Money)
    stamp_duty: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    reason: Mapped[str | None] = mapped_column(String(255))

    order: Mapped[Order] = relationship(back_populates="invoices")
    credited_invoice: Mapped["Invoice | None"] = relationship(remote_side=[id])


class DocumentSequence(Base):
    """Last number used per prefix and year, so invoice numbers have no gaps."""

    __tablename__ = "document_sequences"

    prefix: Mapped[str] = mapped_column(String(8), primary_key=True)
    year: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    last_value: Mapped[int] = mapped_column(default=0)

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field

from app.core.config import settings
from app.models import InvoiceKind, OrderStatus, PaymentMethod, PaymentStatus, Role

Price = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=3, examples=["4.500"])]
TaxRate = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2, examples=["19"])]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


# --- Auth & users ---------------------------------------------------------------


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: Role
    full_name: str


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=72)
    role: Role = Role.cashier


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=8, max_length=72)
    role: Role | None = None
    is_active: bool | None = None


class UserOut(ORM):
    id: int
    email: str
    full_name: str
    role: Role
    is_active: bool
    created_at: datetime


# --- Customers --------------------------------------------------------------------


class CustomerBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=255)
    tax_id: str | None = Field(default=None, max_length=40)


class CustomerCreate(CustomerBase):
    pass


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=255)
    tax_id: str | None = Field(default=None, max_length=40)


class CustomerOut(ORM, CustomerBase):
    id: int
    email: str | None = None
    created_at: datetime


# --- Products & stock -------------------------------------------------------------


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=64, examples=["CAFE-EXP"])
    name: str = Field(min_length=1, max_length=160, examples=["Café express"])
    description: str | None = None
    unit_price: Price
    tax_rate: TaxRate = settings.default_tax_rate
    stock: int = Field(default=0, ge=0)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    unit_price: Price | None = None
    tax_rate: TaxRate | None = None
    is_active: bool | None = None


class ProductOut(ORM):
    id: int
    sku: str
    name: str
    description: str | None
    unit_price: Decimal
    tax_rate: Decimal
    stock: int
    is_active: bool

    @computed_field
    @property
    def price_with_tax(self) -> Decimal:
        return (self.unit_price * (1 + self.tax_rate / 100)).quantize(Decimal("0.001"))


class StockAdjust(BaseModel):
    delta: int = Field(description="Positive to add stock, negative to remove it.", examples=[24])
    note: str | None = Field(default=None, max_length=255, examples=["Delivery from supplier"])


# --- Orders & payments ------------------------------------------------------------


class OrderItemIn(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=10_000)


class OrderCreate(BaseModel):
    customer_id: int | None = Field(default=None, description="Leave empty for a walk-in sale.")
    items: list[OrderItemIn] = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class OrderItemOut(ORM):
    product_id: int
    sku: str
    name: str
    unit_price: Decimal
    tax_rate: Decimal
    quantity: int
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal


class PaymentOut(ORM):
    id: int
    order_id: int
    method: PaymentMethod
    status: PaymentStatus
    amount: Decimal
    currency: str
    pay_url: str | None
    created_at: datetime
    completed_at: datetime | None
    details: dict | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def needs_refund(self) -> bool:
        """Paid after the order was already settled or cancelled: give the money back."""
        return bool((self.details or {}).get("needs_refund"))


class PaymentCreate(BaseModel):
    method: Literal["cash", "cheque", "transfer", "online"] = Field(
        description="`cash`, `cheque` and `transfer` are recorded as paid immediately. `online` "
        "returns a `pay_url` on the configured gateway (mock sandbox, Konnect or Stripe)."
    )


class InvoiceRef(ORM):
    id: int
    number: str
    issued_at: datetime
    total: Decimal

    @computed_field
    @property
    def pdf_url(self) -> str:
        return f"{settings.public_base_url}/api/v1/invoices/{self.id}/pdf"


class InvoiceOut(InvoiceRef):
    kind: InvoiceKind
    order_id: int
    credited_invoice_id: int | None
    customer: dict | None = Field(description="The customer as they were at issue time.")
    subtotal: Decimal
    tax_total: Decimal
    stamp_duty: Decimal
    currency: str
    reason: str | None


class OrderOut(ORM):
    id: int
    number: str = Field(description="Order form (bon de commande) number. Not an invoice number.")
    status: OrderStatus
    customer: CustomerOut | None
    created_by_id: int
    items: list[OrderItemOut]
    subtotal: Decimal
    tax_total: Decimal
    stamp_duty: Decimal
    total: Decimal
    currency: str
    note: str | None
    created_at: datetime
    paid_at: datetime | None
    cancelled_at: datetime | None
    refunded_at: datetime | None
    invoice: InvoiceRef | None = Field(
        description="Issued at payment, or earlier with POST /orders/{id}/invoice."
    )
    credit_note: InvoiceRef | None = Field(description="Issued when an invoiced order is undone.")
    payments: list[PaymentOut]
    public_token: str = Field(exclude=True)

    @computed_field
    @property
    def public_url(self) -> str:
        """The page the QR code on the documents points to."""
        return f"{settings.public_base_url}/o/{self.public_token}"

    @computed_field
    @property
    def document_url(self) -> str:
        """The order's current PDF: the invoice once paid, the order form before that."""
        return f"{settings.public_base_url}/api/v1/orders/{self.id}/document.pdf"


class CreditNoteCreate(BaseModel):
    reason: str = Field(min_length=3, max_length=255, examples=["Marchandise retournée"])
    restock: bool = Field(default=True, description="Put the items back in stock.")


class PublicOrderItem(ORM):
    name: str
    quantity: int
    line_total: Decimal


class PublicOrder(ORM):
    number: str
    invoice: InvoiceRef | None = Field(exclude=True)
    status: OrderStatus
    total: Decimal
    currency: str
    created_at: datetime
    paid_at: datetime | None
    items: list[PublicOrderItem]

    @computed_field
    @property
    def invoice_number(self) -> str | None:
        return self.invoice.number if self.invoice else None


# --- Reports ----------------------------------------------------------------------


class MethodTotal(BaseModel):
    method: PaymentMethod
    count: int
    amount: Decimal


class TopProduct(BaseModel):
    product_id: int
    name: str
    quantity: int
    revenue: Decimal


class DailyReport(BaseModel):
    date: date
    timezone: str
    currency: str
    orders_created: int
    orders_paid: int
    orders_cancelled: int
    revenue: Decimal = Field(description="Total collected (TTC) on orders paid that day.")
    refunds: Decimal = Field(description="Total of the credit notes issued that day.")
    net_revenue: Decimal
    tax_collected: Decimal
    average_basket: Decimal
    items_sold: int
    payments_by_method: list[MethodTotal]
    top_products: list[TopProduct]


class VatLine(BaseModel):
    rate: Decimal
    base: Decimal = Field(
        description="Taxable amount before tax (HT), invoices minus credit notes."
    )
    vat: Decimal
    total: Decimal


class VatReport(BaseModel):
    """What the accountant needs for the monthly TVA return."""

    month: str = Field(examples=["2026-11"])
    currency: str
    invoices: int
    credit_notes: int
    first_invoice: str | None
    last_invoice: str | None
    by_rate: list[VatLine]
    base: Decimal
    vat: Decimal
    fiscal_stamps: Decimal
    total: Decimal

"""PDF invoice (labels in French, as Tunisian invoices usually are) and QR code rendering."""

import io
from decimal import Decimal
from zoneinfo import ZoneInfo

import segno
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.config import settings
from app.db import as_utc
from app.models import Order, OrderStatus

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")
LINE = colors.HexColor("#e5e7eb")
STATUS_LABELS = {
    OrderStatus.pending: ("EN ATTENTE DE PAIEMENT", colors.HexColor("#b45309")),
    OrderStatus.paid: ("PAYÉE", colors.HexColor("#047857")),
    OrderStatus.cancelled: ("ANNULÉE", colors.HexColor("#b91c1c")),
}


def public_url(order: Order) -> str:
    return f"{settings.public_base_url}/o/{order.public_token}"


def qr_png(order: Order, scale: int = 8) -> bytes:
    buffer = io.BytesIO()
    segno.make(public_url(order), error="m").save(buffer, kind="png", scale=scale, border=2)
    return buffer.getvalue()


def fmt(amount: Decimal) -> str:
    # 1234.5 -> "1 234,500" (French grouping, millimes).
    whole, _, frac = f"{amount:,.3f}".partition(".")
    return f"{whole.replace(',', ' ')},{frac}"


def _qr_drawing(url: str, size: float) -> Drawing:
    widget = QrCodeWidget(url, barLevel="M")
    x1, y1, x2, y2 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    drawing.add(widget)
    return drawing


def render_invoice_pdf(order: Order) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Facture {order.number}",
        author=settings.business_name,
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5, leading=13, textColor=INK)
    muted = ParagraphStyle("muted", parent=body, textColor=MUTED, fontSize=8.5)
    right = ParagraphStyle("right", parent=body, alignment=TA_RIGHT)
    title = ParagraphStyle("title", parent=body, fontSize=20, leading=24, fontName="Helvetica-Bold")
    tz = ZoneInfo(settings.timezone)
    cur = order.currency

    issued = as_utc(order.created_at).astimezone(tz).strftime("%d/%m/%Y %H:%M")
    status_text, status_color = STATUS_LABELS[order.status]
    header = Table(
        [
            [
                Paragraph(f"<b>{settings.business_name}</b>", title),
                Paragraph(f"<b>FACTURE</b><br/>{order.number}", right),
            ],
            [
                Paragraph(
                    f"{settings.business_address}<br/>Tél. {settings.business_phone}<br/>"
                    f"MF : {settings.business_tax_id}",
                    muted,
                ),
                Paragraph(
                    f"Date : {issued}<br/>"
                    f"<font color='{status_color.hexval().replace('0x', '#')}'><b>{status_text}</b>"
                    "</font>",
                    right,
                ),
            ],
        ],
        colWidths=["60%", "40%"],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    customer = order.customer
    if customer:
        lines = [f"<b>{customer.name}</b>"]
        lines += [v for v in (customer.address, customer.phone, customer.email) if v]
        if customer.tax_id:
            lines.append(f"MF : {customer.tax_id}")
        bill_to = "<br/>".join(lines)
    else:
        bill_to = "<b>Client de passage</b>"

    rows = [["Désignation", "Qté", "P.U. HT", "TVA", "Total HT"]]
    for item in order.items:
        rows.append(
            [
                Paragraph(
                    f"{item.name}<br/><font size=7.5 color='#6b7280'>{item.sku}</font>", body
                ),
                str(item.quantity),
                fmt(item.unit_price),
                f"{item.tax_rate.normalize():f} %",
                fmt(item.line_subtotal),
            ]
        )
    items = Table(rows, colWidths=["46%", "10%", "16%", "11%", "17%"], repeatRows=1)
    items.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 9.5),
                ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
                ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    totals_rows = [
        ["Total HT", f"{fmt(order.subtotal)} {cur}"],
        ["TVA", f"{fmt(order.tax_total)} {cur}"],
    ]
    if order.stamp_duty:
        totals_rows.append(["Timbre fiscal", f"{fmt(order.stamp_duty)} {cur}"])
    totals_rows.append(["Net à payer TTC", f"{fmt(order.total)} {cur}"])
    totals = Table(totals_rows, colWidths=[38 * mm, 40 * mm])
    totals.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 9.5),
                ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 11),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK),
                ("TOPPADDING", (0, -1), (-1, -1), 6),
            ]
        )
    )

    action = "suivre ou payer" if order.status == OrderStatus.pending else "vérifier"
    qr_caption = Paragraph(
        f"Scannez pour {action} cette facture<br/><font size=7>{public_url(order)}</font>",
        muted,
    )
    footer = Table(
        [[_qr_drawing(public_url(order), 32 * mm), qr_caption, totals]],
        colWidths=[36 * mm, None, 80 * mm],
    )
    footer.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    story = [
        header,
        Spacer(1, 10 * mm),
        Paragraph("FACTURÉ À", muted),
        Spacer(1, 1.5 * mm),
        Paragraph(bill_to, body),
        Spacer(1, 8 * mm),
        items,
        Spacer(1, 6 * mm),
        footer,
    ]
    if order.note:
        story += [Spacer(1, 6 * mm), Paragraph(f"<b>Note :</b> {order.note}", muted)]
    doc.build(story)
    return buffer.getvalue()

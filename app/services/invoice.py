"""PDF documents (in French, as Tunisian invoices usually are) and QR code rendering.

One layout, three documents: the order form before payment, the invoice, and the credit note.
"""

import io
from decimal import Decimal
from xml.sax.saxutils import escape
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
from app.models import Invoice, InvoiceKind, Order, OrderStatus
from app.services.words import amount_in_words

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")
LINE = colors.HexColor("#e5e7eb")
AMBER, GREEN, RED = (colors.HexColor(c) for c in ("#b45309", "#047857", "#b91c1c"))


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


def _customer_lines(customer: dict | None) -> str:
    if not customer:
        return "<b>Client de passage</b>"
    lines = [f"<b>{escape(customer['name'])}</b>"]
    lines += [escape(customer[k]) for k in ("address", "phone", "email") if customer.get(k)]
    if customer.get("tax_id"):
        lines.append(f"MF : {escape(customer['tax_id'])}")
    return "<br/>".join(lines)


def _live_customer(order: Order) -> dict | None:
    c = order.customer
    if c is None:
        return None
    return {"name": c.name, "address": c.address, "phone": c.phone, "email": c.email,
            "tax_id": c.tax_id}  # fmt: skip


def _describe(order: Order, document: Invoice | None, tz: ZoneInfo):
    """Title, number, date, status stamp, reference lines and customer for one document."""
    when = document.issued_at if document else order.created_at
    date = as_utc(when).astimezone(tz).strftime("%d/%m/%Y %H:%M")
    if document is None:
        stamp = {
            OrderStatus.pending: ("EN ATTENTE DE PAIEMENT", AMBER),
            OrderStatus.cancelled: ("ANNULÉ", RED),
        }.get(order.status, ("", INK))
        refs = ["Ce document n'est pas une facture. La facture est émise au paiement."]
        return "BON DE COMMANDE", order.number, date, stamp, refs, _live_customer(order)
    refs = [f"Bon de commande {order.number}"]
    if document.kind == InvoiceKind.credit_note:
        refs.insert(0, f"Annule la facture {document.credited_invoice.number}")
        if document.reason:
            refs.append(f"Motif : {escape(document.reason)}")
        return "AVOIR", document.number, date, ("AVOIR", RED), refs, document.customer
    credit = order.credit_note
    if credit:
        stamp = (f"ANNULÉE PAR {credit.number}", RED)
    elif order.status == OrderStatus.paid:
        stamp = ("PAYÉE", GREEN)
    else:
        stamp = ("À RÉGLER", AMBER)
    return "FACTURE", document.number, date, stamp, refs, document.customer


def render_pdf(order: Order, document: Invoice | None = None) -> bytes:
    """The order form (no document), an invoice or a credit note."""
    tz = ZoneInfo(settings.timezone)
    title_text, number, date, (stamp_text, stamp_color), refs, customer = _describe(
        order, document, tz
    )
    amounts = document or order
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"{title_text.capitalize()} {number}",
        author=settings.business_name,
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5, leading=13, textColor=INK)
    muted = ParagraphStyle("muted", parent=body, textColor=MUTED, fontSize=8.5)
    right = ParagraphStyle("right", parent=body, alignment=TA_RIGHT)
    title = ParagraphStyle("title", parent=body, fontSize=20, leading=24, fontName="Helvetica-Bold")
    cur = order.currency

    stamp_hex = stamp_color.hexval().replace("0x", "#")
    header = Table(
        [
            [
                Paragraph(f"<b>{escape(settings.business_name)}</b>", title),
                Paragraph(f"<b>{title_text}</b><br/>{number}", right),
            ],
            [
                Paragraph(
                    f"{escape(settings.business_address)}<br/>Tél. {escape(settings.business_phone)}"
                    f"<br/>MF : {escape(settings.business_tax_id)}",
                    muted,
                ),
                Paragraph(
                    f"Date : {date}<br/><font color='{stamp_hex}'><b>{stamp_text}</b></font>",
                    right,
                ),
            ],
        ],
        colWidths=["60%", "40%"],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    rows = [["Désignation", "Qté", "P.U. HT", "TVA", "Total HT"]]
    for item in order.items:
        rows.append(
            [
                Paragraph(
                    f"{escape(item.name)}<br/><font size=7.5 color='#6b7280'>"
                    f"{escape(item.sku)}</font>",
                    body,
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

    # VAT broken down by rate, as Tunisian invoices show it.
    by_rate: dict[Decimal, list[Decimal]] = {}
    for item in order.items:
        base, vat = by_rate.setdefault(item.tax_rate, [Decimal(0), Decimal(0)])
        by_rate[item.tax_rate] = [base + item.line_subtotal, vat + item.line_tax]
    vat_rows = [["Taux", "Base HT", "TVA"]] + [
        [f"{rate.normalize():f} %", fmt(base), fmt(vat)]
        for rate, (base, vat) in sorted(by_rate.items())
    ]
    vat_table = Table(vat_rows, colWidths=[16 * mm, 24 * mm, 22 * mm])
    vat_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
                ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
                ("LINEBELOW", (0, 0), (-1, 0), 0.4, LINE),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ]
        )
    )

    total_label = {"AVOIR": "Total avoir TTC", "FACTURE": "Net à payer TTC"}.get(
        title_text, "Total TTC"
    )
    totals_rows = [
        ["Total HT", f"{fmt(amounts.subtotal)} {cur}"],
        ["TVA", f"{fmt(amounts.tax_total)} {cur}"],
    ]
    if amounts.stamp_duty:
        totals_rows.append(["Timbre fiscal", f"{fmt(amounts.stamp_duty)} {cur}"])
    totals_rows.append([total_label, f"{fmt(amounts.total)} {cur}"])
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
    summary = Table([[vat_table, totals]], colWidths=[None, 80 * mm])
    summary.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LEFTPADDING", (0, 0), (0, 0), 0)]))  # fmt: skip

    action = (
        "suivre ou payer cette commande"
        if order.status == OrderStatus.pending
        else ("vérifier ce document")
    )
    qr = Table(
        [[
            _qr_drawing(public_url(order), 26 * mm),
            Paragraph(f"Scannez pour {action}<br/><font size=7>{public_url(order)}</font>", muted),
        ]],
        colWidths=[30 * mm, None],
    )  # fmt: skip
    qr.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                            ("LEFTPADDING", (0, 0), (0, 0), 0)]))  # fmt: skip

    story = [
        header,
        Spacer(1, 9 * mm),
        Paragraph("CLIENT", muted),
        Spacer(1, 1.5 * mm),
        Paragraph(_customer_lines(customer), body),
        Spacer(1, 3 * mm),
        Paragraph("<br/>".join(refs), muted),
        Spacer(1, 6 * mm),
        items,
        Spacer(1, 5 * mm),
        summary,
    ]
    if document is not None:
        verb = "le présent avoir" if title_text == "AVOIR" else "la présente facture"
        story += [
            Spacer(1, 5 * mm),
            Paragraph(
                f"Arrêté{'' if title_text == 'AVOIR' else 'e'} {verb} à la somme de : "
                f"<b>{amount_in_words(amounts.total)}</b>.",
                body,
            ),
        ]
    if order.note:
        story += [Spacer(1, 4 * mm), Paragraph(f"<b>Note :</b> {escape(order.note)}", muted)]
    story += [Spacer(1, 8 * mm), qr]
    doc.build(story)
    return buffer.getvalue()

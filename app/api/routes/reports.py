from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.schemas import DailyReport, VatReport
from app.services import reports

router = APIRouter(prefix="/reports", tags=["reports"])

MAX_EXPORT_DAYS = 366
CsvFormat = Annotated[
    Literal["fr", "standard"],
    Query(description='`fr`: ";" and decimal comma, for Excel in French. `standard`: "," and "."'),
]


@router.get("/daily", response_model=DailyReport)
def daily_summary(db: DbSession, _: CurrentUser, day: date | None = None):
    """Sales for one local day (default: today). Revenue counts orders paid that day."""
    return reports.daily_report(db, day or reports.today())


@router.get("/vat", response_model=VatReport)
def monthly_vat(
    db: DbSession,
    _: AdminUser,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
):
    """The month's TVA by rate (invoices minus credit notes), for the monthly return.

    Defaults to the current month.
    """
    return reports.vat_report(db, month or reports.today().strftime("%Y-%m"))


def _check_range(start: date, end: date) -> None:
    if end < start:
        raise HTTPException(422, "end is before start")
    if (end - start).days >= MAX_EXPORT_DAYS:
        raise HTTPException(422, "Export at most one year")


def _csv(content: str, name: str) -> Response:
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


@router.get(
    "/sales-journal.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
def sales_journal(db: DbSession, _: AdminUser, start: date, end: date, format: CsvFormat = "fr"):
    """The sales journal for the accountant: every invoice and credit note from `start` to
    `end` (inclusive), base HT and TVA split by rate, credit notes negative.
    """
    _check_range(start, end)
    return _csv(
        reports.sales_journal_csv(db, start, end, french=format == "fr"),
        f"journal_ventes_{start}_{end}",
    )


@router.get(
    "/orders.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
def export_orders(db: DbSession, _: AdminUser, start: date, end: date, format: CsvFormat = "fr"):
    """Every order created from `start` to `end` (inclusive), paid or not."""
    _check_range(start, end)
    return _csv(reports.orders_csv(db, start, end, french=format == "fr"), f"orders_{start}_{end}")

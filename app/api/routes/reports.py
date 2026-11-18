from datetime import date

from fastapi import APIRouter, HTTPException, Response

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.schemas import DailyReport
from app.services import reports

router = APIRouter(prefix="/reports", tags=["reports"])

MAX_EXPORT_DAYS = 366


@router.get("/daily", response_model=DailyReport)
def daily_summary(db: DbSession, _: CurrentUser, day: date | None = None):
    """Sales for one local day (default: today). Revenue counts orders paid that day."""
    return reports.daily_report(db, day or reports.today())


@router.get(
    "/orders.csv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
def export_orders(db: DbSession, _: AdminUser, start: date, end: date):
    """Every order created from `start` to `end` (inclusive), ready for Excel or an accountant."""
    if end < start:
        raise HTTPException(422, "end is before start")
    if (end - start).days >= MAX_EXPORT_DAYS:
        raise HTTPException(422, "Export at most one year")
    return Response(
        reports.orders_csv(db, start, end),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="orders_{start}_{end}.csv"'},
    )

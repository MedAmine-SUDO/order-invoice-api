import hmac
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status

from app.api.deps import DbSession
from app.core.config import settings
from app.seed import reset

router = APIRouter(prefix="/internal", include_in_schema=False)


@router.api_route("/reset-demo", methods=["GET", "POST"])
def reset_demo(db: DbSession, authorization: Annotated[str, Header()] = ""):
    """Called daily by a Vercel Schedule, which sends `Authorization: Bearer $CRON_SECRET`.

    Accepts GET and POST: Schedules default to POST, vercel.json asks for GET.
    """
    if not settings.cron_secret:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not hmac.compare_digest(authorization, f"Bearer {settings.cron_secret}"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    reset(db)
    return {"status": "reset"}

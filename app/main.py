from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.deps import DbSession
from app.api.routes import (
    auth,
    customers,
    internal,
    invoices,
    orders,
    payments,
    products,
    public,
    reports,
    users,
)
from app.core.config import settings

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

DESCRIPTION = f"""
Orders, invoices and credit notes with gap-free numbering, QR payments, the monthly TVA and a
sales journal: the backend a Tunisian shop or distributor needs. The demo is an office-supplies
distributor; it resets every day.

### Try it in 30 seconds
1. Click **Authorize** and log in with one of the demo accounts:
   - `admin@demo.tn` / `{settings.demo_password}` (manager: everything)
   - `cashier@demo.tn` / `{settings.demo_password}` (counter: sales only)
2. `GET /api/v1/products`, then `POST /api/v1/orders`: you get an order form (`BC-…`).
3. `POST /api/v1/orders/{{id}}/payments` with `{{"method": "cash"}}`: the invoice (`FAC-…`) is issued.
   Or `{{"method": "online"}}` and open the `pay_url`.
4. Open the order's `document_url` (the PDF) or its `public_url` (what the QR code points to).
5. `GET /api/v1/reports/vat` for the month's TVA by rate.

Money is returned as strings with 3 decimals (millimes), so no amount is ever rounded by a float.
Online payments currently go through: **{settings.payment_provider}**.
"""

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=DESCRIPTION,
    swagger_ui_parameters={"persistAuthorization": True, "docExpansion": "none"},
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

for module in (auth, users, customers, products, orders, invoices, payments, reports):
    app.include_router(module.router, prefix="/api/v1")
app.include_router(public.router)
app.include_router(internal.router)


@app.get("/health", tags=["meta"])
def health(db: DbSession):
    db.execute(text("select 1"))
    return {"status": "ok", "payment_provider": settings.payment_provider}


if FRONTEND.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/", include_in_schema=False)
    def frontend():
        return FileResponse(FRONTEND / "index.html")

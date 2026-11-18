from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET = "dev-only-secret-change-me-in-production"


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Order & Invoice API"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://orders:orders@localhost:5432/orders"
    secret_key: str = DEFAULT_SECRET
    access_token_expire_minutes: int = 8 * 60
    # Absolute URL the API is reachable at. Used in QR codes and payment redirects.
    # Empty means: the Vercel production URL when deployed there, else localhost.
    public_base_url: str = ""
    # Comma-separated list of allowed origins, or "*".
    cors_origins: str = "*"

    # What gets printed on invoices.
    business_name: str = "Café El Medina"
    business_address: str = "12 Rue de Marseille, 1000 Tunis"
    business_phone: str = "+216 71 000 000"
    business_tax_id: str = "1234567/A/M/000"
    currency: str = "TND"
    timezone: str = "Africa/Tunis"
    default_tax_rate: Decimal = Decimal("19")
    # Tunisian "timbre fiscal" added to every invoice. Set to 0 to disable.
    fiscal_stamp: Decimal = Decimal("1.000")

    # Which gateway handles online payments.
    payment_provider: Literal["mock", "konnect", "stripe"] = "mock"
    konnect_api_key: str = ""
    konnect_wallet_id: str = ""
    konnect_base_url: str = "https://api.sandbox.konnect.network/api/v2"
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    seed_demo: bool = False
    demo_password: str = "demo1234"
    # Set to enable GET /internal/reset-demo (called nightly by Vercel Cron with this as Bearer).
    cron_secret: str = ""

    # Set by Vercel on every deployment. Serverless instances shouldn't hold a connection pool.
    vercel: str = ""
    vercel_project_production_url: str = ""

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, value: str) -> str:
        # Neon, Render and Railway hand out postgres:// URLs; SQLAlchemy needs the driver name.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @model_validator(mode="after")
    def _resolve_public_base_url(self) -> "Settings":
        if not self.public_base_url:
            if self.vercel_project_production_url:
                self.public_base_url = f"https://{self.vercel_project_production_url}"
            else:
                self.public_base_url = "http://localhost:8000"
        self.public_base_url = self.public_base_url.rstrip("/")
        return self

    @model_validator(mode="after")
    def _refuse_default_secret_in_production(self) -> "Settings":
        if self.environment == "production" and self.secret_key == DEFAULT_SECRET:
            raise ValueError("SECRET_KEY must be set in production")
        return self

    @property
    def serverless(self) -> bool:
        return self.vercel == "1"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

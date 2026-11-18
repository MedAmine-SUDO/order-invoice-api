import pytest
from sqlalchemy.pool import NullPool

from app.core.config import Settings, settings
from app.db import make_engine
from app.models import Order, User


@pytest.fixture
def cron_secret(monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "s3cret")
    return "s3cret"


def test_reset_endpoint_is_off_without_a_secret(client):
    assert client.get("/internal/reset-demo").status_code == 404


def test_reset_endpoint_needs_the_secret(client, cron_secret):
    assert client.get("/internal/reset-demo").status_code == 401
    r = client.get("/internal/reset-demo", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_reset_wipes_and_reseeds(client, cron_secret, make_order, db):
    make_order(note="left by a visitor")
    r = client.get("/internal/reset-demo", headers={"Authorization": f"Bearer {cron_secret}"})
    assert r.status_code == 200
    db.expire_all()
    assert db.query(Order).filter(Order.note == "left by a visitor").count() == 0
    assert {u.email for u in db.query(User)} == {"admin@demo.tn", "cashier@demo.tn"}


def test_public_url_defaults_to_the_vercel_production_url():
    on_vercel = Settings(_env_file=None, public_base_url="",
                         vercel_project_production_url="shop.vercel.app")  # fmt: skip
    assert on_vercel.public_base_url == "https://shop.vercel.app"
    assert Settings(_env_file=None, public_base_url="").public_base_url == "http://localhost:8000"
    explicit = Settings(_env_file=None, public_base_url="https://api.shop.tn/",
                        vercel_project_production_url="shop.vercel.app")  # fmt: skip
    assert explicit.public_base_url == "https://api.shop.tn"


def test_database_url_from_neon_gets_the_driver():
    s = Settings(
        _env_file=None, database_url="postgres://u:p@ep-x-pooler.neon.tech/db?sslmode=require"
    )
    assert s.database_url.startswith("postgresql+psycopg://")


def test_serverless_engine_does_not_pool():
    assert isinstance(make_engine("sqlite://", serverless=True).pool, NullPool)
    assert not isinstance(make_engine("sqlite://").pool, NullPool)

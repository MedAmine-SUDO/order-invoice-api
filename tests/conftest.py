import os

# Configure before the app is imported.
os.environ["ENVIRONMENT"] = "test"
os.environ["SECRET_KEY"] = "test-secret-that-is-long-enough-for-hs256"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["PAYMENT_PROVIDER"] = "mock"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, Product, Role, User  # noqa: E402

# CI runs the suite against PostgreSQL; locally it falls back to in-memory SQLite.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
PASSWORD = "secret123"


@pytest.fixture
def engine():
    if TEST_DATABASE_URL:
        engine = create_engine(TEST_DATABASE_URL)
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db(engine):
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        yield session


@pytest.fixture
def client(engine):
    Session = sessionmaker(engine, expire_on_commit=False)

    def override():
        with Session() as session:
            yield session

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def users(db):
    admin = User(email="admin@test.tn", full_name="Admin", role=Role.admin,
                 hashed_password=hash_password(PASSWORD))  # fmt: skip
    cashier = User(email="cashier@test.tn", full_name="Cashier", role=Role.cashier,
                   hashed_password=hash_password(PASSWORD))  # fmt: skip
    db.add_all([admin, cashier])
    db.commit()
    return {"admin": admin, "cashier": cashier}


def _login(client, email: str) -> dict:
    r = client.post("/api/v1/auth/login", data={"username": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin_headers(client, users):
    return _login(client, "admin@test.tn")


@pytest.fixture
def cashier_headers(client, users):
    return _login(client, "cashier@test.tn")


@pytest.fixture
def products(db):
    items = [
        Product(
            sku="CAF", name="Café", unit_price=Decimal("1.500"), tax_rate=Decimal("19"), stock=100
        ),  # fmt: skip
        Product(
            sku="CRO", name="Croissant", unit_price=Decimal("1.400"), tax_rate=Decimal("7"), stock=3
        ),  # fmt: skip
    ]
    db.add_all(items)
    db.commit()
    return items


@pytest.fixture
def customer(db):
    c = Customer(name="Société Test", email="compta@test.tn", tax_id="1234567/A/M/000")
    db.add(c)
    db.commit()
    return c


@pytest.fixture
def make_order(client, cashier_headers, products):
    def _make(items=None, **extra):
        items = items or [{"product_id": products[0].id, "quantity": 2}]
        r = client.post("/api/v1/orders", json={"items": items, **extra}, headers=cashier_headers)
        assert r.status_code == 201, r.text
        return r.json()

    return _make

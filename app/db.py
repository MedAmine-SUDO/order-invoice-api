from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings


def make_engine(url: str, serverless: bool = False):
    kwargs = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        # Poolers in transaction mode (Neon, Supabase) can't keep prepared statements.
        kwargs["connect_args"] = {"prepare_threshold": None}
    if serverless:
        # Each function instance is short-lived; the provider's pooler does the pooling.
        kwargs["poolclass"] = NullPool
    return create_engine(url, **kwargs)


engine = make_engine(settings.database_url, serverless=settings.serverless)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(ts: datetime) -> datetime:
    """Timestamps are stored in UTC; SQLite (used in tests) hands them back without an offset."""
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session

"""Sync SQLAlchemy engine — Lambda functions are not async."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def sync_database_url(url: str) -> str:
    """
    The worker is sync and ships psycopg2 only, but DATABASE_URL in SSM may be
    written for the async services ("postgresql+psycopg://", "+asyncpg") or in
    Heroku style ("postgres://"). Any of those made create_engine try to import
    a driver that isn't installed, and every job failed before touching the DB.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    if scheme in ("postgres", "postgresql") or scheme.startswith("postgresql+"):
        return f"postgresql+psycopg2://{rest}"
    return url


def get_engine():
    return create_engine(sync_database_url(get_settings().database_url), pool_pre_ping=True, pool_size=2)


_SessionFactory = None


def get_session() -> Session:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory()

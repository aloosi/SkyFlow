"""Async SQLite engine, session management and declarative base."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import Enum as SAEnum
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no timezone support)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def str_enum(enum_cls: type[enum.Enum], length: int) -> SAEnum:
    """Store an Enum by *value* in a VARCHAR column guarded by a CHECK constraint."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


def build_engine(url: str, echo: bool = False) -> AsyncEngine:
    """Create an async engine; SQLite connections get FK enforcement + WAL."""
    engine = create_async_engine(url, echo=echo)

    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


engine: AsyncEngine = build_engine(get_settings().database_url)
SessionLocal: async_sessionmaker[AsyncSession] = build_session_factory(engine)


async def init_db(target: AsyncEngine | None = None) -> None:
    """Create all tables (idempotent)."""
    import app.models  # noqa: F401  (register models on Base.metadata)

    async with (target or engine).begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency: factory used by background workers (overridable in tests)."""
    return SessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with SessionLocal() as session:
        yield session

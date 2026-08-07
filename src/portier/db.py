"""Асинхронный движок SQLAlchemy и фабрика сессий."""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str) -> None:
    """Создать движок и фабрику сессий по URL базы данных."""
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
    if database_url.startswith("sqlite"):
        # WAL + busy_timeout: почтовый цикл и панель пишут в одну SQLite,
        # без этого возможны "database is locked"
        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Движок БД не инициализирован: вызовите init_engine()")
    return _session_factory


async def init_db() -> None:
    """Создать таблицы, если их ещё нет."""
    if _engine is None:
        raise RuntimeError("Движок БД не инициализирован: вызовите init_engine()")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

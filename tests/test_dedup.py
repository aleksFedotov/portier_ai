"""Тесты дедупликации писем (in-memory SQLite)."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portier.gmail_client import get_last_uid, is_processed
from portier.models import Base, ProcessedEmail


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_email(message_id: str, uid: int) -> ProcessedEmail:
    return ProcessedEmail(
        message_id=message_id, uid=uid, sender="a@b.c", subject="Тема",
        raw_payload="", status="SUCCESS",
    )


async def test_duplicate_by_message_id_skipped(session_factory):
    async with session_factory() as session:
        session.add(_make_email("<msg-1@x>", 100))
        await session.commit()

    async with session_factory() as session:
        assert await is_processed(session, "<msg-1@x>") is True
        assert await is_processed(session, "<msg-2@x>") is False


async def test_pending_email_is_reprocessed(session_factory):
    """Зависшее PENDING-письмо не считается обработанным — его обработают повторно."""
    async with session_factory() as session:
        record = _make_email("<msg-1@x>", 100)
        record.status = "PENDING"
        session.add(record)
        await session.commit()

    async with session_factory() as session:
        assert await is_processed(session, "<msg-1@x>") is False


async def test_get_last_uid(session_factory):
    async with session_factory() as session:
        assert await get_last_uid(session) is None
        session.add(_make_email("<m1@x>", 50))
        session.add(_make_email("<m2@x>", 80))
        await session.commit()
        assert await get_last_uid(session) == 80

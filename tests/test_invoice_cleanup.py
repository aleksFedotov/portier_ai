"""Тесты автоудаления закрытых карточек счетов (тикет 18)."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import portier.invoice_cleanup as cleanup
from portier.models import Base, EmailAction, ProcessedEmail

CHAT_ID = -100777


@pytest.fixture
async def _db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(cleanup, "get_session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _email(tag: str, message_id=101) -> ProcessedEmail:
    return ProcessedEmail(
        message_id=f"<m{tag}@x>", uid=1, sender="a@b.c", subject="Счёт",
        raw_payload="", status="SUCCESS", email_type="invoice_required",
        invoice_message_id=message_id, invoice_chat_id=CHAT_ID,
    )


def _action(email: ProcessedEmail, action: str, hours_old: float) -> EmailAction:
    return EmailAction(
        email_id=email.id, action=action, admin_name="admin",
        done_at=datetime.utcnow() - timedelta(hours=hours_old),
    )


async def test_closed_card_deleted(_db):
    factory = _db
    async with factory() as session:
        email = _email("a")
        session.add(email)
        await session.flush()
        session.add(_action(email, "invoice_sent", hours_old=30))
        session.add(_action(email, "invoice_paid", hours_old=25))
        await session.commit()
        email_id = email.id

    bot = AsyncMock()
    deleted = await cleanup.cleanup_closed_cards(bot, ttl_hours=24)

    assert deleted == 1
    bot.delete_message.assert_awaited_once_with(chat_id=CHAT_ID, message_id=101)
    async with factory() as session:
        assert (await session.get(ProcessedEmail, email_id)).invoice_message_id is None


async def test_unclosed_card_kept(_db):
    factory = _db
    async with factory() as session:
        only_sent = _email("a", message_id=101)
        no_actions = _email("b", message_id=102)
        session.add_all([only_sent, no_actions])
        await session.flush()
        session.add(_action(only_sent, "invoice_sent", hours_old=100))
        await session.commit()

    bot = AsyncMock()
    deleted = await cleanup.cleanup_closed_cards(bot, ttl_hours=24)

    assert deleted == 0
    bot.delete_message.assert_not_awaited()


async def test_fresh_closed_card_kept(_db):
    factory = _db
    async with factory() as session:
        email = _email("a")
        session.add(email)
        await session.flush()
        session.add(_action(email, "invoice_sent", hours_old=5))
        session.add(_action(email, "invoice_paid", hours_old=1))  # свежее TTL
        await session.commit()

    bot = AsyncMock()
    deleted = await cleanup.cleanup_closed_cards(bot, ttl_hours=24)

    assert deleted == 0
    bot.delete_message.assert_not_awaited()


async def test_ttl_counts_from_last_action(_db):
    factory = _db
    async with factory() as session:
        email = _email("a")
        session.add(email)
        await session.flush()
        session.add(_action(email, "invoice_sent", hours_old=100))
        session.add(_action(email, "invoice_paid", hours_old=2))  # закрыта 2 ч назад
        await session.commit()

    bot = AsyncMock()
    deleted = await cleanup.cleanup_closed_cards(bot, ttl_hours=24)

    assert deleted == 0
    bot.delete_message.assert_not_awaited()


async def test_missing_message_untracked(_db):
    """Сообщение уже удалено вручную — снимаем с учёта без ошибок."""
    factory = _db
    async with factory() as session:
        email = _email("a")
        session.add(email)
        await session.flush()
        session.add(_action(email, "invoice_sent", hours_old=30))
        session.add(_action(email, "invoice_paid", hours_old=25))
        await session.commit()
        email_id = email.id

    bot = AsyncMock()
    bot.delete_message.side_effect = TelegramBadRequest(
        method=AsyncMock(), message="message to delete not found"
    )
    deleted = await cleanup.cleanup_closed_cards(bot, ttl_hours=24)

    assert deleted == 1  # снята с учёта
    async with factory() as session:
        assert (await session.get(ProcessedEmail, email_id)).invoice_message_id is None

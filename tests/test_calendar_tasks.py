"""Напоминания из Google Календаря: отправка в чат + кнопка «✅ Выполнено»."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portier import calendar_tasks
from portier.calendar_tasks import check_calendar_once
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.models import Base, CalendarTask


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    yield factory
    await engine.dispose()


def _settings() -> Settings:
    return Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )


def _service(events):
    """Мок Calendar API: events().list().execute() → {"items": events}."""
    return SimpleNamespace(
        events=lambda: SimpleNamespace(
            list=lambda **kwargs: SimpleNamespace(
                execute=lambda: {"items": events}
            )
        )
    )


def _event(event_id, title, start: datetime):
    return {
        "id": event_id,
        "summary": title,
        "status": "confirmed",
        "start": {"dateTime": start.astimezone().isoformat()},
    }


async def _tasks():
    async with get_session_factory()() as session:
        return (await session.execute(select(CalendarTask))).scalars().all()


async def test_due_event_sends_notification_once(db):
    now = datetime.now()
    service = _service([_event("ev1", "Помыть кулер", now - timedelta(minutes=5))])
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=555)

    sent = await check_calendar_once(bot, _settings(), service)
    assert sent == 1
    bot.send_message.assert_awaited_once()
    args = bot.send_message.await_args
    assert args.args[0] == 111
    assert "Помыть кулер" in args.args[1]
    button = args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "cal:ev1"

    tasks = await _tasks()
    assert len(tasks) == 1
    assert tasks[0].event_id == "ev1"
    assert tasks[0].notified_at is not None

    # Повторный проход — дубля нет
    sent = await check_calendar_once(bot, _settings(), service)
    assert sent == 0
    assert bot.send_message.await_count == 1


async def test_future_and_allday_events_silent(db):
    now = datetime.now()
    future = _event("ev2", "Будущая задача", now + timedelta(hours=3))
    allday = {"id": "ev3", "summary": "На весь день", "status": "confirmed",
              "start": {"date": "2026-08-16"}}
    # API сам фильтрует по timeMax; здесь проверяем только all-day молчок
    service = _service([allday])
    bot = AsyncMock()

    sent = await check_calendar_once(bot, _settings(), service)
    assert sent == 0
    bot.send_message.assert_not_called()
    assert await _tasks() == []


def test_parse_start_allday_returns_none():
    assert calendar_tasks._parse_start({"start": {"date": "2026-08-16"}}) is None


def test_parse_start_with_timezone():
    dt = calendar_tasks._parse_start(
        {"start": {"dateTime": "2026-08-15T14:00:00+03:00"}}
    )
    assert dt is not None and dt.tzinfo is None


async def test_callback_marks_done_once(db):
    from portier.callbacks import handle_calendar_done

    now = datetime.now()
    service = _service([_event("ev9", "Сменить воду", now - timedelta(minutes=1))])
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=77)
    await check_calendar_once(bot, _settings(), service)

    user = SimpleNamespace(full_name="Иван Петров", username="ivan")
    callback = AsyncMock()
    callback.data = "cal:ev9"
    callback.from_user = user
    callback.message = AsyncMock()

    await handle_calendar_done(callback)
    text = callback.message.edit_text.await_args.args[0]
    assert "✅ Выполнено: Сменить воду" in text
    assert "Иван Петров (@ivan)" in text

    task = (await _tasks())[0]
    assert task.done_at is not None
    assert task.done_by == "Иван Петров (@ivan)"

    # Повторное нажатие — «Уже отмечено», текст не трогаем
    callback2 = AsyncMock()
    callback2.data = "cal:ev9"
    callback2.from_user = user
    callback2.message = AsyncMock()
    await handle_calendar_done(callback2)
    callback2.answer.assert_awaited_with("Уже отмечено")
    callback2.message.edit_text.assert_not_called()

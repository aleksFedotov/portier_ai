"""Напоминания о задачах из Google Календаря в основной чат.

Сотрудники создают события-задачи в календаре рабочего аккаунта. Когда время
события наступает, бот шлёт в TELEGRAM_CHAT_ID напоминание с кнопкой
«✅ Выполнено»; колбэк живёт в callbacks.py (префикс cal:). Уже отправленные
события хранятся в таблице CalendarTask — дублей нет.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from .config import Settings
from .db import get_session_factory
from .gmail_client import get_credentials, in_quiet_hours
from .models import CalendarTask

logger = logging.getLogger(__name__)

# Окно выборки: события, начавшиеся за последние сутки (чтобы дослать
# напоминания, пропущенные в тихие часы или при простое бота).
_LOOKBACK = timedelta(hours=24)


def _build_service(settings: Settings):
    from googleapiclient.discovery import build

    creds = get_credentials(settings)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_start(event: dict) -> datetime | None:
    """Локальное naive-время начала события; None для all-day событий."""
    start = event.get("start") or {}
    raw = start.get("dateTime")
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    # Храним и сравниваем в локальном naive-времени сервера
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


async def _list_due_events(service, calendar_id: str, now: datetime) -> list[dict]:
    """События с наступившим временем начала (не all-day)."""
    time_min = (now - _LOOKBACK).astimezone().isoformat()
    time_max = now.astimezone().isoformat()

    def _call():
        return service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()

    result = await asyncio.to_thread(_call)
    events = []
    for event in result.get("items", []):
        if event.get("status") == "cancelled":
            continue
        start = _parse_start(event)
        if start is None:
            logger.info("All-day событие календаря пропущено: %s", event.get("summary"))
            continue
        events.append({"id": event["id"], "title": event.get("summary") or "Задача", "start": start})
    return events


async def check_calendar_once(bot, settings: Settings, service=None) -> int:
    """Один проход: дослать напоминания по наступившим задачам.

    Возвращает число отправленных напоминаний.
    """
    if service is None:
        service = await asyncio.to_thread(_build_service, settings)
    now = datetime.now()
    events = await _list_due_events(service, settings.CALENDAR_ID, now)
    if not events:
        return 0

    session_factory = get_session_factory()
    sent = 0
    async with session_factory() as session:
        known = set((await session.execute(
            select(CalendarTask.event_id).where(
                CalendarTask.event_id.in_([e["id"] for e in events])
            )
        )).scalars().all())

        for event in events:
            if event["id"] in known:
                continue
            text = (
                f"⏰ Задача из календаря: {event['title']}\n"
                f"🕐 {event['start']:%d.%m.%Y %H:%M}"
            )
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ Выполнено", callback_data=f"cal:{event['id']}"
                )
            ]])
            message = await bot.send_message(
                settings.TELEGRAM_CHAT_ID, text, reply_markup=markup
            )
            session.add(CalendarTask(
                event_id=event["id"], title=event["title"], due_at=event["start"],
                notified_at=now, tg_message_id=message.message_id,
            ))
            sent += 1
        await session.commit()

    if sent:
        logger.info("Календарь: отправлено напоминаний: %d", sent)
    return sent


async def calendar_loop(bot, settings: Settings) -> None:
    """Периодический опрос Google Календаря."""
    service = await asyncio.to_thread(_build_service, settings)
    logger.info("Google Calendar API: авторизация успешна (календарь %s)", settings.CALENDAR_ID)
    while True:
        try:
            if in_quiet_hours(
                datetime.now(), settings.QUIET_HOURS_START, settings.QUIET_HOURS_END
            ):
                logger.info("Тихие часы — опрос календаря пропущен")
            else:
                await check_calendar_once(bot, settings, service)
        except Exception as exc:
            # Частый случай — 403 до переавторизации со scope calendar:
            # без traceback, чтобы не мусорить в логе каждую минуту.
            logger.warning(
                "Опрос календаря не удался (%s) — повторим на следующем цикле. "
                "Если это 403/401 — выполните python -m portier.gmail_auth заново",
                exc,
            )
        await asyncio.sleep(settings.CALENDAR_POLL_SECONDS)

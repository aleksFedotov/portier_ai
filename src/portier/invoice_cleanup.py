"""Тикет 18: автоудаление закрытых карточек счетов из чата счетов.

Карточка счёта (текстовое сообщение с кнопками) удаляется, когда по ней
нажаты обе кнопки — «Счёт отправлен» и «Оплачен», — и с момента закрытия
прошло INVOICE_CARD_TTL_HOURS часов. PDF-документы и незакрытые карточки
не трогаем: неоплаченные счета должны оставаться на виду.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .config import Settings
from .db import get_session_factory
from .models import ProcessedEmail

logger = logging.getLogger(__name__)

CLOSED_ACTIONS = frozenset({"invoice_sent", "invoice_paid"})
# Как часто подметаем чат счетов
_CLEANUP_INTERVAL_SECONDS = 3600


async def cleanup_closed_cards(bot, ttl_hours: int) -> int:
    """Удалить закрытые карточки старше ttl_hours. Возвращает число удалённых."""
    session_factory = get_session_factory()
    now = datetime.utcnow()
    deleted = 0

    async with session_factory() as session:
        emails = (
            await session.execute(
                select(ProcessedEmail)
                .options(selectinload(ProcessedEmail.actions))
                .where(ProcessedEmail.invoice_message_id.isnot(None))
            )
        ).scalars().all()

        for email in emails:
            done_at = {a.action: a.done_at for a in email.actions}
            if not CLOSED_ACTIONS.issubset(done_at):
                continue
            closed_at = max(done_at[a] for a in CLOSED_ACTIONS)
            if now - closed_at < timedelta(hours=ttl_hours):
                continue
            try:
                await bot.delete_message(
                    chat_id=email.invoice_chat_id,
                    message_id=email.invoice_message_id,
                )
            except TelegramBadRequest as exc:
                # Уже удалено вручную или слишком старое — больше не пытаемся
                logger.info(
                    "Карточка счёта %s недоступна для удаления (%s) — снимаем с учёта",
                    email.invoice_message_id, exc,
                )
            except Exception:
                # Сетевой сбой — попробуем на следующем проходе
                logger.warning(
                    "Не удалось удалить карточку счёта %s",
                    email.invoice_message_id, exc_info=True,
                )
                continue
            email.invoice_message_id = None
            deleted += 1

        await session.commit()

    if deleted:
        logger.info("Подметка чата счетов: удалено закрытых карточек: %d", deleted)
    return deleted


async def invoice_cleanup_loop(bot, settings: Settings) -> None:
    """Периодическая подметка закрытых карточек счетов (раз в час)."""
    if getattr(settings, "INVOICE_CHAT_ID", None) is None:
        logger.info("INVOICE_CHAT_ID не задан — подметка карточек счетов выключена")
        return
    ttl = getattr(settings, "INVOICE_CARD_TTL_HOURS", 24)
    while True:
        try:
            await cleanup_closed_cards(bot, ttl)
        except Exception:
            logger.exception("Сбой подметки карточек счетов")
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)

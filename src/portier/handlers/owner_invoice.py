"""Пересылка счёта от владельца в группу входящих счетов.

Владелец отправляет или пересылает боту документ (PDF счёта) в личку, при
желании с подписью — бот пересылает документ в группу входящих счетов
(INCOMING_INVOICES_CHAT_ID, та же группа, куда тикет 10 складывает счета
из почты автоматически). Подпись владельца сохраняется под стандартным
префиксом. В других чатах хендлер молчит.
"""

import logging
from html import escape as esc

from aiogram import F, Router
from aiogram.types import Message

logger = logging.getLogger(__name__)

router = Router()


def _owner_chat_id() -> int | None:
    """Личка владельца; fallback — основной чат (как в gmail_client)."""
    from ..config import get_settings

    s = get_settings()
    return s.OWNER_CHAT_ID or s.TELEGRAM_CHAT_ID


def _invoices_chat_id() -> int | None:
    """Группа входящих счетов; fallback — владелец → основной чат."""
    from ..config import get_settings

    s = get_settings()
    return s.INCOMING_INVOICES_CHAT_ID or s.OWNER_CHAT_ID or s.TELEGRAM_CHAT_ID


@router.message(F.document)
async def forward_owner_invoice(message: Message) -> None:
    owner = _owner_chat_id()
    if owner is None or message.chat.id != owner:
        return
    target = _invoices_chat_id()
    if target is None:
        return

    caption = "🧾 Счёт от участника"
    if message.caption:
        caption += f"\n📝 {esc(message.caption)}"
    await message.bot.send_document(
        chat_id=target,
        document=message.document.file_id,
        caption=caption,
    )
    await message.answer("✅ Отправлено в группу счетов")
    logger.info(
        "Счёт от владельца переслан в группу %s (file: %s)",
        target, message.document.file_name,
    )

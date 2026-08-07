"""Диспетчер уведомлений: тип письма → сообщение в Telegram."""

import logging

from ..bot import send_notification
from ..schemas import EmailAnalysisResult
from .templates import build_notification

logger = logging.getLogger(__name__)


async def route_notification(
    bot,
    chat_id: int,
    result: EmailAnalysisResult,
    *,
    email_id: int,
    sender: str,
    subject: str,
    body_text: str,
    invoice_note: str | None = None,
) -> None:
    """Собрать уведомление по типу письма и отправить администраторам."""
    text, buttons = build_notification(
        result, email_id=email_id, sender=sender, subject=subject,
        body_text=body_text, invoice_note=invoice_note,
    )
    await send_notification(bot, chat_id, text, buttons)
    logger.info("Уведомление отправлено: email_id=%s, тип=%s", email_id, result.type)

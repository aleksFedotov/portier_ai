"""Пересылка счёта от владельца в группу входящих счетов.

Владелец отправляет или пересылает боту документ (PDF счёта) в личку, при
желании с подписью — бот пересылает документ в группу входящих счетов
(INCOMING_INVOICES_CHAT_ID, та же группа, куда тикет 10 складывает счета
из почты автоматически). Подпись владельца сохраняется под стандартным
префиксом. В других чатах хендлер молчит.

Счёт сохраняется на диск (INVOICES_DIR) и записывается в реестр как
неоплаченный (email_type="owner_invoice"): команда /invoices выдаёт его
наравне со счетами из почты. На пересланном документе — кнопка
«💰 Оплачен» (EmailAction invoice_paid), чтобы снять счёт с учёта.
"""

import logging
from datetime import datetime
from html import escape as esc
from pathlib import Path

from aiogram import F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

router = Router()

EMAIL_TYPE = "owner_invoice"


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


async def _save_pdf(message: Message) -> Path:
    """Скачать документ в INVOICES_DIR, имя уникально по file_unique_id."""
    from ..config import get_settings

    file_name = message.document.file_name or "invoice.pdf"
    safe_name = "".join(
        c if (c.isalnum() or c in "._- ") else "_" for c in file_name
    )
    dest = (
        Path(get_settings().INVOICES_DIR)
        / f"owner_{message.document.file_unique_id}_{safe_name}"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    buf = await message.bot.download(message.document.file_id)
    dest.write_bytes(buf.read())
    return dest


async def _register_unpaid(message: Message, pdf_path: Path) -> int:
    """Записать счёт в реестр как неоплаченный; вернуть id записи."""
    from ..db import get_session_factory
    from ..models import EmailStatus, ProcessedEmail

    file_name = message.document.file_name or "invoice.pdf"
    title = message.caption or file_name
    session_factory = get_session_factory()
    async with session_factory() as session:
        record = ProcessedEmail(
            message_id=f"owner-invoice:{message.chat.id}:{message.message_id}",
            uid=0,
            gmail_id="",
            sender="owner",
            subject=title,
            processed_at=datetime.utcnow(),
            email_type=EMAIL_TYPE,
            raw_payload="",
            llm_result={"invoice": {"company_name": title, "amount": None}},
            status=EmailStatus.SUCCESS.value,
            invoice_pdf=str(pdf_path),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record.id


def _paid_keyboard(email_id: int) -> InlineKeyboardMarkup:
    from .templates import ACTION_LABELS

    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=ACTION_LABELS["invoice_paid"],
                callback_data=f"action:invoice_paid:{email_id}",
            )
        ]]
    )


@router.message(F.document)
async def forward_owner_invoice(message: Message) -> None:
    owner = _owner_chat_id()
    if owner is None or message.chat.id != owner:
        return
    target = _invoices_chat_id()
    if target is None:
        return

    pdf_path = await _save_pdf(message)
    email_id = await _register_unpaid(message, pdf_path)

    caption = "🧾 Счёт от участника"
    if message.caption:
        caption += f"\n📝 {esc(message.caption)}"
    await message.bot.send_document(
        chat_id=target,
        document=message.document.file_id,
        caption=caption,
        reply_markup=_paid_keyboard(email_id),
    )
    await message.answer("✅ Отправлено в группу счетов и записано как неоплаченный")
    logger.info(
        "Счёт от владельца переслан в группу %s и записан как неоплаченный "
        "(id=%s, file: %s)",
        target, email_id, message.document.file_name,
    )

"""Команда /invoices в чате счетов (тикет 06): выдача PDF за период.

Работает только в чате INVOICE_CHAT_ID — в основном чате молчит, чтобы не
путать администраторов. Поток: /invoices → «Все» / «Только неоплаченные» →
период (сегодня / 7 / 30 дней) → PDF файлами. Оплата отмечается кнопкой
«💰 Оплачен» в уведомлении о счёте (EmailAction invoice_paid).
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from ..db import get_session_factory
from ..models import EmailAction, ProcessedEmail

logger = logging.getLogger(__name__)

router = Router()

PERIODS = [("Сегодня", 1), ("7 дней", 7), ("30 дней", 30)]
SCOPES = [("Все счета", "all"), ("Только неоплаченные", "due")]
LIMIT = 20

PAID_ACTION = "invoice_paid"


def _invoice_chat_id() -> int | None:
    """Чат счетов из настроек; None — фича отключена, команда молчит."""
    from ..config import get_settings

    return get_settings().INVOICE_CHAT_ID


def scope_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"inv-scope:{scope}")]
            for label, scope in SCOPES
        ]
    )


def period_keyboard(scope: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"inv:{scope}:{days}")]
            for label, days in PERIODS
        ]
    )


def parse_period(data: str | None) -> tuple[str, int] | None:
    """Разобрать callback_data вида inv:<all|due>:<дней>."""
    parts = (data or "").split(":")
    if len(parts) == 3 and parts[0] == "inv" and parts[1] in {"all", "due"}:
        try:
            return parts[1], int(parts[2])
        except ValueError:
            return None
    return None


def _invoice_caption(record: ProcessedEmail, paid: bool) -> str:
    """Подпись к PDF: компания, сумма, дата выставления, отметка об оплате."""
    invoice = (record.llm_result or {}).get("invoice") or {}
    parts = ["✅ Оплачен" if paid else "⏳ Не оплачен"]
    parts.append(invoice.get("company_name") or "счёт")
    if invoice.get("amount"):
        parts.append(str(invoice["amount"]))
    if record.processed_at:
        parts.append(record.processed_at.strftime("%d.%m.%Y"))
    return " — ".join(parts)


@router.message(Command("invoices"))
async def cmd_invoices(message: Message) -> None:
    chat_id = _invoice_chat_id()
    if chat_id is None or message.chat.id != chat_id:
        return
    await message.answer("Какие счета прислать?", reply_markup=scope_keyboard())


@router.callback_query(F.data.startswith("inv-scope:"))
async def handle_scope(callback: CallbackQuery) -> None:
    scope = (callback.data or "").rsplit(":", 1)[-1]
    if scope not in {"all", "due"}:
        await callback.answer("Неизвестный фильтр")
        return
    chat_id = _invoice_chat_id()
    if chat_id is None or callback.message.chat.id != chat_id:
        await callback.answer()
        return
    await callback.message.answer(
        "За какой период?", reply_markup=period_keyboard(scope)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("inv:"))
async def handle_period(callback: CallbackQuery) -> None:
    parsed = parse_period(callback.data)
    if parsed is None:
        await callback.answer("Неизвестный период")
        return
    scope, days = parsed
    chat_id = _invoice_chat_id()
    if chat_id is None or callback.message.chat.id != chat_id:
        await callback.answer()
        return

    since = datetime.utcnow() - timedelta(days=days)
    paid_ids_q = select(EmailAction.email_id).where(EmailAction.action == PAID_ACTION)
    session_factory = get_session_factory()
    async with session_factory() as session:
        query = (
            select(ProcessedEmail)
            .where(
                ProcessedEmail.email_type == "invoice_required",
                ProcessedEmail.invoice_pdf.is_not(None),
                ProcessedEmail.processed_at >= since,
            )
            .order_by(ProcessedEmail.processed_at.desc())
            .limit(LIMIT)
        )
        if scope == "due":  # только без отметки «Оплачен»
            query = query.where(ProcessedEmail.id.not_in(paid_ids_q))
        records = (await session.execute(query)).scalars().all()
        paid_ids = set((await session.execute(paid_ids_q)).scalars().all())

    if not records:
        await callback.message.answer(
            "Неоплаченных счетов за период нет."
            if scope == "due" else "Счетов за период нет."
        )
        await callback.answer()
        return

    from ..bot import send_document

    sent = 0
    for record in reversed(records):  # от старых к новым
        path = Path(record.invoice_pdf)
        if not path.exists():
            logger.warning("PDF счёта не найден на диске: %s", path)
            continue
        await send_document(
            callback.bot,
            callback.message.chat.id,
            filename=path.name,
            data=path.read_bytes(),
            caption=_invoice_caption(record, paid=record.id in paid_ids),
        )
        sent += 1
    await callback.answer(f"Отправлено счетов: {sent}")
    logger.info("/invoices: выдано %d счетов (%s) за %d дн.", sent, scope, days)

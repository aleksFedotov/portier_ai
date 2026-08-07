"""LLM-классификация письма через OpenAI Structured Outputs."""

import logging

from openai import AsyncOpenAI, BadRequestError
from tenacity import (
    retry,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .schemas import EmailAnalysisResult

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — помощник администратора мини-отеля. Тебе приходит текст письма из почты отеля. "
    "Классифицируй письмо и извлеки ключевые данные.\n\n"
    "Типы писем:\n"
    "- booking_comment — комментарий или пожелание гостя к бронированию (поздний заезд, детская кроватка и т.п.)\n"
    "- guest_message — сообщение гостя из экстранета канала бронирования\n"
    "- invoice_required — запрос счёта на оплату от гостя или компании\n"
    "- booking_modified — изменение существующего бронирования (даты, число гостей, категория номера)\n"
    "- booking_cancelled — отмена бронирования\n"
    "- payment_received — подтверждение получения оплаты\n"
    "- payment_failed — ошибка или отказ оплаты\n"
    "- review_notification — уведомление о новом отзыве\n"
    "- unknown — если письмо не относится к работе отеля или тип неясен\n\n"
    "Письма приходят из экстранетов каналов бронирования (Островок, Яндекс.Путешествия и др.), "
    "от гостей и от платёжных систем. Указывай номер брони, имя гостя, даты заезда и выезда "
    "в формате ISO (ГГГГ-ММ-ДД), если они есть в письме. Если уверенности нет — выбирай unknown.\n"
    "Поле action_required — краткая инструкция администратору, что нужно сделать.\n"
    "Для типа invoice_required дополнительно заполни блок invoice: название компании "
    "(company_name), ИНН (inn), сумму (amount, как в письме), описание услуги (description), "
    "даты проживания (arrival_date/departure_date). Для остальных типов оставляй invoice=null.\n\n"
    "В тексте письма персональные данные замаскированы токенами: [GUEST_N] — имена гостей, "
    "[PHONE_N] — телефоны, [EMAIL_N] — адреса почты. Сохраняй эти токены в полях ответа "
    "как есть, не пытайся их восстановить или заменить."
)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    # 400 (BadRequestError) не ретраим: запрос невалиден, повтор не поможет
    retry=retry_if_exception_type(Exception) & retry_if_not_exception_type(BadRequestError),
    reraise=True,
)
async def analyze_email(
    client: AsyncOpenAI,
    model: str,
    *,
    sender: str,
    subject: str,
    body: str,
) -> EmailAnalysisResult:
    """Классифицировать письмо. Бросает исключение после 3 неудачных попыток."""
    user_content = f"Отправитель: {sender}\nТема: {subject}\n\nТекст письма:\n{body}"
    response = await client.beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format=EmailAnalysisResult,
    )
    result = response.choices[0].message.parsed
    if result is None:
        raise ValueError("OpenAI вернул пустой результат разбора")
    return result

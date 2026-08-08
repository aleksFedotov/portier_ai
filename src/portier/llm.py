"""LLM-классификация письма: OpenAI (Structured Outputs) или DeepSeek (JSON mode)."""

import json
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
    "- booking_comment — письмо содержит комментарий или пожелание гостя к бронированию "
    "(поздний заезд, детская кроватка, парковка и т.п.). Если в подтверждении брони есть "
    "комментарий гостя — выбирай этот тип, а не booking_confirmed\n"
    "- booking_confirmed — подтверждение нового бронирования БЕЗ комментариев и пожеланий гостя\n"
    "- guest_message — сообщение гостя из экстранета канала бронирования\n"
    "- invoice_required — запрос счёта на оплату от гостя или компании\n"
    "- booking_modified — изменение существующего бронирования (даты, число гостей, категория номера)\n"
    "- booking_cancelled — отмена бронирования\n"
    "- payment_received — письмо именно о получении оплаты (платёжное поручение, "
    "подтверждение платежа). Подтверждение бронирования — это НЕ оплата\n"
    "- payment_failed — ошибка или отказ оплаты\n"
    "- review_notification — уведомление о новом отзыве\n"
    "- unknown — если письмо не относится к работе отеля или тип неясен\n\n"
    "Письма приходят из экстранетов каналов бронирования (Островок, Яндекс.Путешествия и др.), "
    "от гостей и от платёжных систем. Указывай номер брони, имя гостя, даты заезда и выезда "
    "в формате ISO (ГГГГ-ММ-ДД), если они есть в письме. Если уверенности нет — выбирай unknown.\n"
    "Поле action_required — краткая инструкция администратору, что нужно сделать.\n"
    "Для типа invoice_required дополнительно заполни блок invoice: название компании "
    "(company_name), ИНН (inn), КПП (kpp), юридический адрес (legal_address), "
    "сумму (amount, как в письме), описание услуги (description), "
    "даты проживания (arrival_date/departure_date). Для остальных типов оставляй invoice=null.\n\n"
    "В тексте письма персональные данные замаскированы токенами: [GUEST_N] — имена гостей, "
    "[PHONE_N] — телефоны, [EMAIL_N] — адреса почты. Сохраняй эти токены в полях ответа "
    "как есть, не пытайся их восстановить или заменить."
)


# DeepSeek требует слово "json" в промпте при response_format=json_object.
# Схему передаём текстом: без strict-режима модель должна знать точные имена полей.
_JSON_MODE_SUFFIX = (
    "\n\nОтветь строго одним JSON-объектом без пояснений и без markdown-обёртки, "
    "соответствующим этой JSON Schema:\n"
    + json.dumps(EmailAnalysisResult.model_json_schema(), ensure_ascii=False)
)


def create_llm_client(settings) -> AsyncOpenAI:
    """Создать клиента активного LLM-провайдера (настройка LLM_PROVIDER)."""
    if settings.LLM_PROVIDER == "deepseek":
        if not settings.DEEPSEEK_API_KEY:
            raise ValueError("LLM_PROVIDER=deepseek, но DEEPSEEK_API_KEY не задан")
        return AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY, base_url=settings.DEEPSEEK_BASE_URL
        )
    if not settings.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY не задан")
    return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


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
    json_mode: bool = False,
) -> EmailAnalysisResult:
    """Классифицировать письмо. Бросает исключение после 3 неудачных попыток.

    json_mode=True — для провайдеров без Structured Outputs (DeepSeek):
    обычный chat.completions + response_format=json_object, валидация pydantic.
    """
    user_content = f"Отправитель: {sender}\nТема: {subject}\n\nТекст письма:\n{body}"
    if json_mode:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT + _JSON_MODE_SUFFIX},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("LLM вернул пустой ответ")
        return EmailAnalysisResult.model_validate_json(content)
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

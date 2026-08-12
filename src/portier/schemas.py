"""Схема результата LLM-классификации письма (OpenAI Structured Outputs)."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class InvoiceDetails(BaseModel):
    """Данные для счёта (заполняются только для invoice_required)."""

    company_name: Optional[str] = None
    inn: Optional[str] = None
    kpp: Optional[str] = None
    legal_address: Optional[str] = None
    amount: Optional[str] = None
    description: Optional[str] = None
    arrival_date: Optional[str] = None
    departure_date: Optional[str] = None


class EmailAnalysisResult(BaseModel):
    """Результат анализа письма мини-отеля."""

    type: Literal[
        "booking_comment",
        "booking_confirmed",
        "guest_message",
        "invoice_required",
        "booking_modified",
        "booking_cancelled",
        "payment_received",
        "payment_failed",
        "review_notification",
        "unknown",
    ]
    priority: Literal["low", "normal", "high", "urgent"]
    guest_name: Optional[str] = None
    arrival_date: Optional[str] = None
    departure_date: Optional[str] = None
    guests_count: Optional[int] = Field(
        default=None,
        description="Число гостей по брони (для booking_confirmed); "
        "целое число, null если в письме не указано",
    )
    booking_number: Optional[str] = None
    channel_name: Optional[str] = None
    internal_booking_id: Optional[str] = Field(
        default=None,
        description="Внутренний ID TravelLine (20260830-7348-456721519); "
        "заполняется кодом из тела письма, не LLM — основа номера счёта",
    )
    comment_details: Optional[str] = None
    action_required: str = Field(description="Что нужно сделать администратору")
    invoice: Optional[InvoiceDetails] = Field(
        default=None, description="Данные для счёта; заполнять только для типа invoice_required"
    )

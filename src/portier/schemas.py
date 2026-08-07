"""Схема результата LLM-классификации письма (OpenAI Structured Outputs)."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class InvoiceDetails(BaseModel):
    """Данные для счёта (заполняются только для invoice_required)."""

    company_name: Optional[str] = None
    inn: Optional[str] = None
    amount: Optional[str] = None
    description: Optional[str] = None
    arrival_date: Optional[str] = None
    departure_date: Optional[str] = None


class EmailAnalysisResult(BaseModel):
    """Результат анализа письма мини-отеля."""

    type: Literal[
        "booking_comment",
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
    booking_number: Optional[str] = None
    channel_name: Optional[str] = None
    comment_details: Optional[str] = None
    action_required: str = Field(description="Что нужно сделать администратору")
    invoice: Optional[InvoiceDetails] = Field(
        default=None, description="Данные для счёта; заполнять только для типа invoice_required"
    )

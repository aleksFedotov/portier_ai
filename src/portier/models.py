"""Модели базы данных Portier AI."""

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class EmailStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"  # чёрный список (тикет 08): не обрабатываем, не шлём


class ProcessedEmail(Base):
    """Письмо, прошедшее через конвейер обработки."""

    __tablename__ = "processed_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    uid: Mapped[int] = mapped_column(Integer, index=True)
    sender: Mapped[str] = mapped_column(String, default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    email_type: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_payload: Mapped[str] = mapped_column(Text, default="")
    pii_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    llm_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default=EmailStatus.PENDING.value)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)

    actions: Mapped[list["EmailAction"]] = relationship(back_populates="email")


class EmailAction(Base):
    """Отметка администратора о выполненном действии по письму."""

    __tablename__ = "email_actions"
    __table_args__ = (
        UniqueConstraint("email_id", "action", name="uq_email_action"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("processed_emails.id"))
    action: Mapped[str] = mapped_column(String)  # recorded_in_pms / replied_to_guest / invoice_sent
    admin_name: Mapped[str] = mapped_column(String)
    done_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    email: Mapped[ProcessedEmail] = relationship(back_populates="actions")


class Company(Base):
    """Компания-заказчик: реквизиты для счетов и шаблон темы письма."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    inn: Mapped[str] = mapped_column(String, default="")
    kpp: Mapped[str] = mapped_column(String, default="")
    legal_address: Mapped[str] = mapped_column(String, default="")
    details: Mapped[str] = mapped_column(Text, default="")
    email: Mapped[str] = mapped_column(String, default="")
    subject_template: Mapped[str] = mapped_column(String, default="")


class Agent(Base):
    """Канал-агент (тикет 13): правило «подтверждение брони = счёт» и реквизиты.

    Матчится по алиасам в теме письма / названии канала. Правило работает от
    справочника, а не от догадок LLM.
    """

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    # Алиасы через «;» — как канал выглядит в подтверждениях TravelLine
    aliases: Mapped[str] = mapped_column(Text, default="")
    invoice_on_booking: Mapped[bool] = mapped_column(default=True)
    payer_name: Mapped[str] = mapped_column(String, default="")  # на кого счёт
    invoice_email: Mapped[str] = mapped_column(String, default="")  # куда слать счёт
    price_note: Mapped[str] = mapped_column(String, default="")  # «-18% ко всем дням»
    note: Mapped[str] = mapped_column(Text, default="")

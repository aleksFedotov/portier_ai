"""Черновики счетов в Gmail: мэтчинг заказчика по реестру и сборка MIME."""

import base64
import email.utils
import logging
from dataclasses import dataclass
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from sqlalchemy import select

from .models import Company
from .schemas import EmailAnalysisResult, InvoiceDetails

logger = logging.getLogger(__name__)

DEFAULT_SUBJECT = "Счёт на оплату проживания"

# Подпись в письмах со счетами (решение владельца): фиксированная, без HOTEL_NAME.
SIGNATURE = (
    "С уважением, ЛиКи Лофт Отель / LiKi LOFT HOTEL\n"
    "+ 7 950 003 50 30\n"
    "likihotel.com\n"
    "Санкт-Петербург, ул. Кирочная, 11"
)


def parse_sender_email(sender: str) -> str:
    """Вытащить адрес из заголовка From вида «Имя <a@b.c>»."""
    return email.utils.parseaddr(sender)[1]


@dataclass
class InvoiceDraftData:
    """Сведённые данные для черновика: реестр в приоритете над LLM-извлечением."""

    to: str
    subject: str
    company: Company | None
    invoice: InvoiceDetails
    booking_number: str | None = None


def merge_invoice_data(
    result: EmailAnalysisResult, sender: str, company: Company | None
) -> InvoiceDraftData:
    """Свести данные счёта: поля реестра компании важнее LLM-извлечённых."""
    llm_invoice = result.invoice or InvoiceDetails()
    if company is not None:
        invoice = InvoiceDetails(
            company_name=company.name or llm_invoice.company_name,
            inn=company.inn or llm_invoice.inn,
            kpp=company.kpp or llm_invoice.kpp,
            legal_address=company.legal_address or llm_invoice.legal_address,
            amount=llm_invoice.amount,
            description=llm_invoice.description,
            arrival_date=llm_invoice.arrival_date,
            departure_date=llm_invoice.departure_date,
        )
        to = company.email or parse_sender_email(sender)
        subject = company.subject_template or DEFAULT_SUBJECT
    else:
        invoice = llm_invoice
        to = parse_sender_email(sender)
        subject = DEFAULT_SUBJECT
    return InvoiceDraftData(
        to=to, subject=subject, company=company, invoice=invoice,
        booking_number=result.booking_number,
    )


async def find_company(
    session, sender: str, invoice: InvoiceDetails | None
) -> Company | None:
    """Найти заказчика в реестре: по email-домену отправителя, ИНН или названию."""
    result = await session.execute(select(Company))
    companies = result.scalars().all()

    sender_domain = parse_sender_email(sender).split("@")[-1].lower().strip()
    inn = (invoice.inn or "").strip() if invoice else ""
    name = (invoice.company_name or "").lower() if invoice else ""

    # 1. Совпадение ИНН — самое надёжное
    if inn:
        for company in companies:
            if company.inn and company.inn.strip() == inn:
                return company
    # 2. Домен email компании совпадает с доменом отправителя
    if sender_domain:
        for company in companies:
            company_domain = company.email.split("@")[-1].lower().strip()
            if company_domain and company_domain == sender_domain:
                return company
    # 3. Название (подстрока, без регистра)
    if name:
        for company in companies:
            if company.name and (
                company.name.lower() in name or name in company.name.lower()
            ):
                return company
    return None


def build_draft_mime(to: str, subject: str, body_text: str, pdf_path: Path) -> str:
    """Собрать MIME-письмо с PDF-вложением, вернуть raw (base64url) для Gmail API."""
    message = MIMEMultipart()
    message["To"] = to
    message["Subject"] = subject
    message.attach(MIMEText(body_text, "plain", "utf-8"))
    with open(pdf_path, "rb") as fh:
        attachment = MIMEApplication(fh.read(), _subtype="pdf")
    attachment.add_header(
        "Content-Disposition", "attachment", filename=pdf_path.name
    )
    message.attach(attachment)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def build_reply_mime(
    to: str,
    subject: str,
    in_reply_to: str,
    body_text: str,
    attachments: list[tuple[str, bytes]],
) -> str:
    """MIME-ответ на письмо (тикет 31): In-Reply-To/References + вложения.

    in_reply_to — Message-ID исходного письма (record.message_id).
    """
    message = MIMEMultipart()
    message["To"] = to
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.attach(MIMEText(body_text, "plain", "utf-8"))
    for filename, data in attachments:
        attachment = MIMEApplication(data, _subtype="pdf")
        attachment.add_header("Content-Disposition", "attachment", filename=filename)
        message.attach(attachment)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def build_draft_body(data: InvoiceDraftData, hotel_name: str = "") -> str:
    """Текст письма-черновика со счётом. hotel_name не используется: подпись
    фиксированная (SIGNATURE)."""
    inv = data.invoice
    lines = ["Добрый день!", ""]
    period = ""
    if inv.arrival_date or inv.departure_date:
        period = f" ({inv.arrival_date or '—'} — {inv.departure_date or '—'})"
    lines.append(f"Выставляем счёт на оплату проживания{period}.")
    if data.booking_number:
        lines.append(f"Номер бронирования: #{data.booking_number}.")
    if inv.amount:
        lines.append(f"Сумма к оплате: {inv.amount}.")
    lines += [
        "",
        "Счёт во вложении (PDF).",
        "",
        SIGNATURE,
    ]
    return "\n".join(lines)

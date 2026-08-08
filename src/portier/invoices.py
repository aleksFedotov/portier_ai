"""Генерация PDF-счетов для писем invoice_required (reportlab).

WeasyPrint заменён на reportlab: на Windows-окружении разработки WeasyPrint
не находит системные библиотеки pango/gobject.
"""

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from .config import Settings
from .schemas import EmailAnalysisResult, InvoiceDetails

logger = logging.getLogger(__name__)

# Кандидаты TTF с поддержкой кириллицы (Linux-контейнер → Windows)
_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans"),
    (r"C:\Windows\Fonts\arial.ttf", "Arial"),
]

_FONT_NAME = "Helvetica"  # fallback без кириллицы
_font_registered = False

# Внутренний ID TravelLine в теле письма: «Подтверждение бронирования\nID 20260830-7348-456721519»
_TL_ID_RE = re.compile(r"ID\s*(\d{8}-\d{4}-\d{9})")
_TL_ID_FALLBACK_RE = re.compile(r"\b(\d{8}-\d{4}-\d{9})\b")


def extract_travelline_id(body: str) -> str | None:
    """Внутренний ID брони TravelLine из тела письма (детерминированно, без LLM)."""
    m = _TL_ID_RE.search(body) or _TL_ID_FALLBACK_RE.search(body)
    return m.group(1) if m else None


def invoice_number_from_id(internal_id: str | None) -> str | None:
    """Номер счёта: последняя часть ID + порядковый суффикс (обычно 1).

    «Бронь: 20260815-7348-447005164» → «447005164-1».
    """
    if not internal_id:
        return None
    return f"{internal_id.rsplit('-', 1)[-1]}-1"


def _register_font() -> str:
    """Зарегистрировать первый доступный Unicode-шрифт, вернуть его имя."""
    global _FONT_NAME, _font_registered
    if _font_registered:
        return _FONT_NAME
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path, name in _FONT_CANDIDATES:
        if Path(path).exists():
            pdfmetrics.registerFont(TTFont(name, path))
            _FONT_NAME = name
            break
    _font_registered = True
    return _FONT_NAME


def invoice_missing_fields(invoice: InvoiceDetails | None) -> list[str]:
    """Список незаполненных ключевых полей счёта (для пометки «проверьте данные»)."""
    if invoice is None:
        return ["компания", "ИНН", "сумма"]
    missing = []
    if not invoice.company_name:
        missing.append("компания")
    if not invoice.inn:
        missing.append("ИНН")
    if not invoice.amount:
        missing.append("сумма")
    return missing


def build_invoice_lines(result: EmailAnalysisResult, settings: Settings) -> list[str]:
    """Текстовые строки счёта (источник истины для содержимого PDF)."""
    inv = result.invoice or InvoiceDetails()
    dash = "—"
    today = datetime.now().strftime("%d.%m.%Y")
    inv_no = invoice_number_from_id(result.internal_booking_id)
    lines = [
        f"СЧЁТ № {inv_no} от {today}" if inv_no else f"СЧЁТ НА ОПЛАТУ от {today}",
    ]
    if result.internal_booking_id:
        lines.append(f"Бронь: {result.internal_booking_id}")
    lines += [
        "",
        f"Поставщик: {settings.HOTEL_NAME or dash}",
        f"ИНН поставщика: {settings.HOTEL_INN or dash}",
    ]
    if settings.HOTEL_DETAILS:
        lines.append(f"Реквизиты: {settings.HOTEL_DETAILS}")
    lines += [
        "",
        f"Покупатель: {inv.company_name or dash}",
        f"ИНН покупателя: {inv.inn or dash}",
        "",
        f"Описание: {inv.description or result.comment_details or 'Проживание в отеле'}",
        f"Период проживания: {inv.arrival_date or result.arrival_date or dash} — "
        f"{inv.departure_date or result.departure_date or dash}",
        f"Номер бронирования: {result.booking_number or dash}",
        f"Сумма к оплате: {inv.amount or dash}",
        "",
        "",
        "Подпись: ____________________        М.П.",
    ]
    return lines


def _safe_filename(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_") or "invoice"


def generate_invoice_pdf(result: EmailAnalysisResult, settings: Settings) -> Path:
    """Сформировать PDF-счёт в INVOICES_DIR. Недостающие поля → «—», не падаем."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    out_dir = Path(settings.INVOICES_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    company = _safe_filename((result.invoice and result.invoice.company_name) or "")
    # uuid-суффикс: два счёта в одну секунду не перезапишут друг друга
    path = out_dir / f"invoice_{stamp}_{uuid.uuid4().hex[:8]}_{company}.pdf"

    font = _register_font()
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setTitle("Счёт на оплату")
    width, height = A4
    y = height - 60
    for i, line in enumerate(build_invoice_lines(result, settings)):
        pdf.setFont(font, 16 if i == 0 else 11)
        pdf.drawString(60, y, line)
        y -= 26 if i == 0 else 18
        if y < 60:
            pdf.showPage()
            y = height - 60
    pdf.save()
    logger.info("PDF-счёт сформирован: %s", path)
    return path

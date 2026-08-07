"""Реестры бронирований Яндекс Путешествий: детект письма и разбор xlsx-вложения.

Письма вида «Реестр бронирований по платежному поручению N от <дата>» от
hotels@travel.yandex.ru содержат xlsx-реестр: номера броней и суммы
перечислений отелю. Обрабатываются детерминированно, без LLM.
Пустой реестр (нет броней) — молча пропускаем.
"""

import io
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

YANDEX_SENDER = "hotels@travel.yandex.ru"
_SUBJECT_MARKER = "реестр бронирований"
_PP_RE = re.compile(r"платежному поручению\s+(\d+)\s+от\s+([\d.]+)", re.IGNORECASE)


@dataclass
class RegistryBooking:
    """Одна строка реестра."""

    number: str
    guest: str
    amount: float


@dataclass
class RegistryReport:
    """Результат разбора реестра."""

    payment_order: str  # номер ПП из темы письма
    payment_date: str  # дата ПП из темы письма
    bookings: list[RegistryBooking] = field(default_factory=list)
    commission: float = 0.0  # суммарное вознаграждение Яндекса

    @property
    def total(self) -> float:
        return sum(b.amount for b in self.bookings)


def is_yandex_registry(sender: str, subject: str) -> bool:
    """Детект письма-реестра Яндекс Путешествий по отправителю и теме."""
    return YANDEX_SENDER in (sender or "") and _SUBJECT_MARKER in (subject or "").lower()


def parse_payment_order(subject: str) -> tuple[str, str]:
    """Номер и дата платёжного поручения из темы письма."""
    m = _PP_RE.search(subject or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def parse_registry(xlsx_bytes: bytes, *, payment_order: str = "", payment_date: str = "") -> RegistryReport:
    """Разобрать xlsx-реестр: брони, вознаграждение Яндекса, итог перечислено отелю."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    report = RegistryReport(payment_order=payment_order, payment_date=payment_date)

    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        header_idx = next(
            (i for i, row in enumerate(rows) if any("бронирования" in str(c).lower() for c in row if c)),
            None,
        )
        if header_idx is None:
            continue
        header = [str(c).lower() if c else "" for c in rows[header_idx]]
        col_number = next((i for i, h in enumerate(header) if "бронирования" in h), 1)
        col_guest = next((i for i, h in enumerate(header) if "имя гостя" in h), None)
        col_amount = next((i for i, h in enumerate(header) if "перечислено отелю" in h), None)
        col_commission = next((i for i, h in enumerate(header) if "вознаграждение яндекса" in h), None)
        if col_amount is None:
            logger.warning("Реестр: колонка «Перечислено отелю» не найдена")
            continue

        for row in rows[header_idx + 1:]:
            number = row[col_number] if col_number < len(row) else None
            if not number:  # пустая строка или итоговая — конец данных
                break
            amount = row[col_amount] if col_amount < len(row) else None
            guest = (str(row[col_guest]).strip() if col_guest is not None and row[col_guest] else "")
            try:
                amount_f = float(amount) if amount is not None else 0.0
            except (TypeError, ValueError):
                amount_f = 0.0
            report.bookings.append(RegistryBooking(number=str(number).strip(), guest=guest, amount=amount_f))
            if col_commission is not None and col_commission < len(row):
                try:
                    report.commission += float(row[col_commission] or 0)
                except (TypeError, ValueError):
                    pass
    wb.close()
    return report


def _fmt_money(value: float) -> str:
    """12 340 ₽ / 12 340,50 ₽."""
    if value == int(value):
        return f"{int(value):,}".replace(",", " ") + " ₽"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def build_registry_notification(report: RegistryReport) -> str:
    """Текст карточки для Telegram (HTML)."""
    from .handlers.templates import esc

    head = f"💳 <b>Реестр Яндекс Путешествий</b> (ПП №{esc(report.payment_order)} от {esc(report.payment_date)})"
    lines = [head, ""]
    for b in report.bookings:
        guest = f" — {esc(b.guest)}" if b.guest else ""
        lines.append(f"🛎 {esc(b.number)}{guest}: {_fmt_money(b.amount)}")
    lines.append("")
    if report.commission:
        lines.append(f"💸 Вознаграждение Яндекса: {_fmt_money(report.commission)}")
    lines.append(f"💰 <b>Перечислено отелю: {_fmt_money(report.total)}</b>")
    return "\n".join(lines)

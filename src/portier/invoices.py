"""Генерация PDF-счетов для писем invoice_required (reportlab).

Тикет 11: шаблон приведён к реальному образцу ООО «ОРОН» (`invocie.pdf` /
`HotelInvoiceData-_447005164-01__1.doc`): логотип, банковский блок-рамка,
центрированный заголовок «Счет № …», таблица позиций с №, рамка итогов,
сумма прописью курсивом, печать и факсимиле. Без ПДн гостей — вместо имён
внутренний номер брони.

WeasyPrint заменён на reportlab: на Windows-окружении разработки WeasyPrint
не находит системные библиотеки pango/gobject.
"""

import logging
import re
import textwrap
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from .config import Settings
from .schemas import EmailAnalysisResult, InvoiceDetails

logger = logging.getLogger(__name__)

# Кандидаты TTF с поддержкой кириллицы (Windows → Linux-контейнер).
# Основной шрифт счёта — Arial (как в invocie.pdf); в Docker fallback — DejaVu Sans.
_FONT_CANDIDATES = {
    "regular": [
        (r"C:\Windows\Fonts\arial.ttf", "Arial"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans"),
    ],
    "bold": [
        (r"C:\Windows\Fonts\arialbd.ttf", "Arial-Bold"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold"),
    ],
    "italic": [
        (r"C:\Windows\Fonts\ariali.ttf", "Arial-Italic"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", "DejaVuSans-Oblique"),
    ],
}

_FONTS = {"regular": "Helvetica", "bold": "Helvetica-Bold", "italic": "Helvetica-Oblique"}
_fonts_registered = False

# Внутренний ID TravelLine в теле письма: «Подтверждение бронирования\nID 20260830-7348-456721519»
_TL_ID_RE = re.compile(r"ID\s*(\d{8}-\d{4}-\d{9})")
_TL_ID_FALLBACK_RE = re.compile(r"\b(\d{8}-\d{4}-\d{9})\b")

_RU_MONTHS = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _register_fonts() -> dict:
    """Зарегистрировать первый доступный Unicode-шрифт (regular/bold/italic)."""
    global _fonts_registered
    if _fonts_registered:
        return _FONTS
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    registered = set()
    for kind, candidates in _FONT_CANDIDATES.items():
        for path, name in candidates:
            if Path(path).exists():
                pdfmetrics.registerFont(TTFont(name, path))
                _FONTS[kind] = name
                registered.add(kind)
                break
    # TTF для курсива/болда не нашёлся (напр., на сервере нет *-Oblique) —
    # подставляем обычный Unicode-шрифт: базовая Helvetica кириллицу не
    # рисует, и сумма прописью в PDF выходит «квадратами».
    if "regular" in registered:
        for kind in ("bold", "italic"):
            if kind not in registered:
                logger.warning(
                    "TTF для %s не найден — в PDF используем %s",
                    kind, _FONTS["regular"],
                )
                _FONTS[kind] = _FONTS["regular"]
    _fonts_registered = True
    return _FONTS


def extract_travelline_id(body: str) -> str | None:
    """Внутренний ID брони TravelLine из тела письма (детерминированно, без LLM)."""
    m = _TL_ID_RE.search(body) or _TL_ID_FALLBACK_RE.search(body)
    return m.group(1) if m else None


def invoice_number_from_id(internal_id: str | None) -> str | None:
    """Номер счёта: последняя часть ID + порядковый суффикс NN (обычно 01).

    «Бронь: 20260815-7348-447005164» → «447005164-01» (формат как в образце).
    """
    if not internal_id:
        return None
    return f"{internal_id.rsplit('-', 1)[-1]}-01"


def ru_date(value: str | None) -> str:
    """'2026-08-15' → '15 августа 2026 г.'. Не ISO — вернуть как есть (или тире)."""
    if not value:
        return "—"
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return value
    return f"{d.day} {_RU_MONTHS[d.month]} {d.year} г."


def parse_amount(raw: str | None) -> Decimal | None:
    """'18 140 RUB' / '14022' / '18870,50' → Decimal; не распарсилось — None."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.,]", "", raw.replace(" ", " ").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    # несколько точек (тысячные разделители) — оставляем последнюю как десятичную
    if cleaned.count(".") > 1:
        head, _, tail = cleaned.rpartition(".")
        cleaned = head.replace(".", "") + "." + tail
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def format_money(value: Decimal | None) -> str:
    """Decimal('18870') → '18 870,00' (пробел — тысячи, запятая — копейки)."""
    if value is None:
        return "—"
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    rub, kop = divmod(abs(q) * 100, 100)
    grouped = f"{int(rub):,}".replace(",", " ")
    sign = "-" if q < 0 else ""
    return f"{sign}{grouped},{int(kop):02d}"


# --- Сумма прописью (рус.) -------------------------------------------------

_UNITS = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_UNITS_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = [
    "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
    "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def _triad(n: int, feminine: bool = False) -> str:
    parts = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append(_HUNDREDS[hundreds])
    tens, units = divmod(rest, 10)
    if tens == 1:
        parts.append(_TEENS[units])
    else:
        if tens:
            parts.append(_TENS[tens])
        if units:
            parts.append((_UNITS_F if feminine else _UNITS)[units])
    return " ".join(parts)


def _declension(n: int, one: str, few: str, many: str) -> str:
    n %= 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def int_in_words(n: int) -> str:
    """18870 → 'восемнадцать тысяч восемьсот семьдесят' (до миллиардов)."""
    if n == 0:
        return "ноль"
    parts = []
    billions, rest = divmod(n, 1_000_000_000)
    millions, rest = divmod(rest, 1_000_000)
    thousands, rest = divmod(rest, 1_000)
    if billions:
        parts += [_triad(billions), _declension(billions, "миллиард", "миллиарда", "миллиардов")]
    if millions:
        parts += [_triad(millions), _declension(millions, "миллион", "миллиона", "миллионов")]
    if thousands:
        parts += [_triad(thousands, feminine=True), _declension(thousands, "тысяча", "тысячи", "тысяч")]
    if rest:
        parts.append(_triad(rest))
    return " ".join(p for p in parts if p)


def amount_in_words(total: Decimal | None) -> str:
    """'18870.00' → 'Восемнадцать тысяч восемьсот семьдесят рублей 00 копеек'."""
    if total is None:
        return ""
    q = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    rub = int(q)
    kop = int((q - rub) * 100)
    words = int_in_words(rub).capitalize()
    return (
        f"{words} {_declension(rub, 'рубль', 'рубля', 'рублей')} "
        f"{kop:02d} {_declension(kop, 'копейка', 'копейки', 'копеек')}"
    )


# --- Модель данных счёта ----------------------------------------------------


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


def _nights(arrival: str | None, departure: str | None) -> int | None:
    try:
        days = (date.fromisoformat(departure[:10]) - date.fromisoformat(arrival[:10])).days
        return days if days > 0 else None
    except (TypeError, ValueError):
        return None


def _hotel(settings: Settings, field: str) -> str:
    """Реквизит отеля из конфига; тестовые стабы могут не иметь новых полей."""
    return getattr(settings, field, "") or ""


@dataclass
class InvoiceData:
    """Всё, что попадает в PDF (источник истины для содержимого счёта)."""

    number: str | None
    date_str: str
    booking_ref: str | None
    # поставщик (отель)
    supplier_name: str
    supplier_address: str
    supplier_phone: str
    supplier_email: str
    supplier_inn: str
    supplier_kpp: str
    supplier_rs: str
    supplier_bank: str
    supplier_bik: str
    supplier_ks: str
    # плательщик (агент)
    payer_name: str
    payer_inn: str
    payer_kpp: str
    payer_address: str
    # позиция
    item_name: str
    qty: int | None
    price: Decimal | None
    amount: Decimal | None

    @property
    def total_words(self) -> str:
        return amount_in_words(self.amount)


def build_invoice_data(result: EmailAnalysisResult, settings: Settings) -> InvoiceData:
    """Собрать модель счёта из результата анализа и реквизитов отеля."""
    inv = result.invoice or InvoiceDetails()
    # «Бронь:» и позиция в таблице — номер из «Подтверждение №…» (booking_number);
    # внутренний TravelLine ID — только для номера счёта (invoice_number_from_id)
    booking_ref = result.booking_number or result.internal_booking_id

    parts = []
    arrival = inv.arrival_date or result.arrival_date
    departure = inv.departure_date or result.departure_date
    if arrival or departure:
        parts.append(f"Проживание с {ru_date(arrival)} по {ru_date(departure)}")
    if inv.description:
        parts.append(inv.description.strip().rstrip("."))
    if booking_ref:
        # решение пользователя: вместо имён гостей — номер брони (Подтверждение №…)
        parts.append(f"Бронь № {booking_ref}")
    item_name = ". ".join(parts) or "Проживание в отеле"

    amount = parse_amount(inv.amount)
    qty = _nights(arrival, departure)
    price = (
        (amount / qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount is not None and qty
        else amount
    )

    return InvoiceData(
        number=invoice_number_from_id(result.internal_booking_id),
        date_str=ru_date(datetime.now().date().isoformat()),
        booking_ref=booking_ref,
        supplier_name=_hotel(settings, "HOTEL_NAME"),
        supplier_address=_hotel(settings, "HOTEL_ADDRESS"),
        supplier_phone=_hotel(settings, "HOTEL_PHONE"),
        supplier_email=_hotel(settings, "HOTEL_EMAIL"),
        supplier_inn=_hotel(settings, "HOTEL_INN"),
        supplier_kpp=_hotel(settings, "HOTEL_KPP"),
        supplier_rs=_hotel(settings, "HOTEL_RS"),
        supplier_bank=_hotel(settings, "HOTEL_BANK"),
        supplier_bik=_hotel(settings, "HOTEL_BIK"),
        supplier_ks=_hotel(settings, "HOTEL_KS"),
        payer_name=inv.company_name or "",
        payer_inn=inv.inn or "",
        payer_kpp=inv.kpp or "",
        payer_address=inv.legal_address or "",
        item_name=item_name,
        qty=qty,
        price=price,
        amount=amount,
    )


# --- Рендер PDF --------------------------------------------------------------


def _safe_filename(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_") or "invoice"


def _draw_wrapped(pdf, font: str, size: float, x: float, y: float, text: str, width_chars: int, leading: float) -> float:
    """Многострочный вывод слева, возвращает y следующей строки."""
    pdf.setFont(font, size)
    for line in textwrap.wrap(text, width=width_chars) or [""]:
        pdf.drawString(x, y, line)
        y -= leading
    return y


def _draw_centered_wrapped(pdf, font: str, size: float, center: float, y: float, text: str, width_chars: int, leading: float) -> float:
    """Многострочный центрированный вывод, возвращает y следующей строки."""
    pdf.setFont(font, size)
    for line in textwrap.wrap(text, width=width_chars) or [""]:
        pdf.drawCentredString(center, y, line)
        y -= leading
    return y


def _draw_image_if_exists(pdf, path: str, x: float, y: float, w: float, h: float) -> bool:
    p = Path(path) if path else None
    if not p or not p.exists():
        logger.warning("Файл картинки не найден: %s — рисуем без него", path)
        return False
    pdf.drawImage(str(p), x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")
    return True


def generate_invoice_pdf(result: EmailAnalysisResult, settings: Settings) -> Path:
    """Сформировать PDF-счёт в INVOICES_DIR по образцу ООО «ОРОН» (тикет 11)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    data = build_invoice_data(result, settings)
    dash = "—"

    out_dir = Path(settings.INVOICES_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    company = _safe_filename(data.payer_name)
    # uuid-суффикс: два счёта в одну секунду не перезапишут друг друга
    path = out_dir / f"invoice_{stamp}_{uuid.uuid4().hex[:8]}_{company}.pdf"

    fonts = _register_fonts()
    reg, bold, italic = fonts["regular"], fonts["bold"], fonts["italic"]
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setTitle(f"Счёт {data.number or 'на оплату'}")
    width, height = A4
    left, right = 45, width - 45
    pad = 4

    y = height - 40

    # --- Логотип + реквизиты поставщика ---
    logo_h = 66
    _draw_image_if_exists(
        pdf, _hotel(settings, "INVOICE_LOGO_PATH"), left, y - logo_h, 100, logo_h
    )
    tx = left + 112
    pdf.setFont(bold, 11)
    pdf.drawString(tx, y - 10, data.supplier_name or dash)
    pdf.setFont(reg, 8.5)
    pdf.drawString(tx, y - 22, data.supplier_address or dash)
    contacts = " ".join(filter(None, [data.supplier_phone, data.supplier_email]))
    pdf.drawString(tx, y - 33, contacts or dash)
    y -= logo_h + 12

    # --- Банковский блок (рамка, 4 равные строки) ---
    colB, colC, colD = left + 195, left + 330, left + 395
    row_h = 22
    top = y
    y1, y2, y3, y4 = top - row_h, top - 2 * row_h, top - 3 * row_h, top - 4 * row_h
    pdf.rect(left, y4, right - left, 4 * row_h, stroke=1, fill=0)
    pdf.line(colB, top, colB, y1)          # ИНН | КПП — только первая строка
    pdf.line(colC, top, colC, y4)
    pdf.line(colD, top, colD, y4)
    pdf.line(left, y1, colC, y1)           # под ИНН/КПП
    pdf.line(left, y2, right, y2)          # под «Получатель» / над БИК
    pdf.line(left, y3, colC, y3)           # под «Банк получателя»
    pdf.line(colC, y3, right, y3)          # между БИК и к/с

    pdf.setFont(reg, 9)
    pdf.drawString(left + pad, top - 15, f"ИНН {data.supplier_inn or dash}")
    pdf.drawString(colB + pad, top - 15, f"КПП {data.supplier_kpp or dash}")
    pdf.setFont(reg, 8)
    pdf.drawString(colC + pad, (top + y2) / 2 - 3, "р/с №")
    pdf.setFont(reg, 9)
    pdf.drawString(colD + pad, (top + y2) / 2 - 3, data.supplier_rs or dash)

    pdf.setFont(reg, 7)
    pdf.drawString(left + pad, y1 - 8, "Получатель")
    pdf.setFont(reg, 9)
    pdf.drawString(left + pad, y1 - 17, data.supplier_name or dash)

    pdf.setFont(reg, 9)
    pdf.drawString(left + pad, y2 - 15, "Банк получателя")
    pdf.setFont(reg, 8)
    pdf.drawString(colC + pad, y2 - 15, "БИК")
    pdf.setFont(reg, 9)
    pdf.drawString(colD + pad, y2 - 15, data.supplier_bik or dash)
    pdf.drawString(left + pad, y3 - 15, data.supplier_bank or dash)
    pdf.setFont(reg, 8)
    pdf.drawString(colC + pad, y3 - 14, "к/с №")
    pdf.setFont(reg, 9)
    pdf.drawString(colD + pad, y3 - 15, data.supplier_ks or dash)

    y = y4 - 32

    # --- Заголовок ---
    pdf.setFont(bold, 14)
    pdf.drawCentredString(width / 2, y, f"Счет № {data.number}" if data.number else "Счет")
    y -= 17
    pdf.drawCentredString(width / 2, y, f"от {data.date_str}")
    y -= 24

    # --- Бронь / Плательщик ---
    value_x = left + 95
    pdf.setFont(bold, 9)
    pdf.drawString(left, y, "Бронь:")
    pdf.setFont(reg, 9)
    pdf.drawString(value_x, y, data.booking_ref or dash)
    y -= 12
    pdf.setFont(bold, 9)
    pdf.drawString(left, y, "Плательщик:")
    pdf.setFont(reg, 9)
    pdf.drawString(value_x, y, data.payer_name or dash)
    y -= 12
    payer_details = ", ".join(
        filter(None, [
            f"ИНН: {data.payer_inn}" if data.payer_inn else "",
            f"КПП: {data.payer_kpp}" if data.payer_kpp else "",
            f"юр. адрес: {data.payer_address}" if data.payer_address else "",
        ])
    )
    if payer_details:
        y = _draw_wrapped(pdf, reg, 9, left, y, payer_details, 100, 11)
    y -= 14

    # --- Таблица позиций ---
    c_num, c_name, c_unit, c_qty, c_price, c_sum = (
        left, left + 30, left + 270, left + 325, left + 380, left + 445)
    head_h = 26
    top = y
    pdf.rect(left, top - head_h, right - left, head_h, stroke=1, fill=0)
    headers = [
        (c_num, c_name, "№"),
        (c_name, c_unit, "Наименование товара (работы, услуги)"),
        (c_unit, c_qty, "Единица измерения"),
        (c_qty, c_price, "Количество"),
        (c_price, c_sum, "Цена, RUB"),
        (c_sum, right, "Сумма, RUB"),
    ]
    pdf.setFont(reg, 8)
    for x0, x1, text in headers:
        lines = textwrap.wrap(text, width=max(8, int((x1 - x0) / 4.2)))
        # вертикальное центрирование блока из n строк внутри ячейки
        ty = top - head_h / 2 - 3 + (len(lines) - 1) * 4.5
        for ln in lines:
            pdf.drawCentredString((x0 + x1) / 2, ty, ln)
            ty -= 9
    for cx in (c_name, c_unit, c_qty, c_price, c_sum):
        pdf.line(cx, top, cx, top - head_h)
    y = top - head_h

    item_lines = textwrap.wrap(data.item_name, width=52) or [dash]
    row_h = max(22, 10 * len(item_lines) + 8)
    pdf.rect(left, y - row_h, right - left, row_h, stroke=1, fill=0)
    for cx in (c_name, c_unit, c_qty, c_price, c_sum):
        pdf.line(cx, y, cx, y - row_h)
    mid_y = y - row_h / 2 - 3
    pdf.setFont(reg, 8)
    pdf.drawCentredString((c_num + c_name) / 2, mid_y, "1")
    ty = y - 11
    for ln in item_lines:
        pdf.drawString(c_name + 3, ty, ln)
        ty -= 10
    pdf.drawCentredString((c_unit + c_qty) / 2, mid_y, "сут.")
    pdf.drawCentredString((c_qty + c_price) / 2, mid_y, str(data.qty) if data.qty else dash)
    pdf.drawCentredString((c_price + c_sum) / 2, mid_y, format_money(data.price))
    pdf.drawCentredString((c_sum + right) / 2, mid_y, format_money(data.amount))
    y -= row_h

    # --- Итоги: рамка продолжает колонку «Сумма, RUB», вплотную к таблице ---
    tot_left = c_sum
    row = 14
    totals = [
        ("Итого:", format_money(data.amount), reg),
        ("В том числе НДС 0%:", "0,00", reg),
        ("Стоимость:", format_money(data.amount), reg),
        ("Оплачено:", "0,00", reg),
        ("Итого к оплате:", format_money(data.amount), bold),
    ]
    tot_top = y
    pdf.rect(tot_left, tot_top - row * len(totals), right - tot_left, row * len(totals), stroke=1, fill=0)
    for i, (label, value, fnt) in enumerate(totals):
        ry = tot_top - row * (i + 1)
        pdf.line(tot_left, ry, right, ry)
        pdf.setFont(fnt, 9)
        pdf.drawRightString(tot_left - 6, ry + 4, label)
        pdf.drawRightString(right - pad, ry + 4, value)
    y = tot_top - row * len(totals) - 16

    # --- Сумма прописью (курсив, по центру) ---
    if data.total_words:
        y = _draw_centered_wrapped(
            pdf, italic, 9, width / 2, y,
            f"Итого к оплате: {data.total_words}, в т.ч. НДС 0 рублей 00 копеек",
            95, 12,
        )
    y -= 30

    # --- Подпись + факсимиле/печать (тикет 11: сразу с печатью, директор разрешил) ---
    pdf.setFont(reg, 10)
    pdf.drawString(left, y, "Администратор")
    pdf.line(left + 160, y - 2, left + 330, y - 2)
    sig_path = _hotel(settings, "INVOICE_SIGNATURE_PATH")
    stamp_path = _hotel(settings, "INVOICE_STAMP_PATH")
    has_stamp = False
    if sig_path:
        _draw_image_if_exists(pdf, sig_path, left + 175, y - 25, 90, 76)
    if stamp_path:
        has_stamp = _draw_image_if_exists(pdf, stamp_path, left + 265, y - 50, 110, 88)
    if not has_stamp:
        pdf.setFont(reg, 9)
        pdf.drawString(left + 265, y, "М.П.")

    pdf.save()
    logger.info("PDF-счёт сформирован: %s", path)
    return path

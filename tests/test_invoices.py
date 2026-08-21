"""Тесты счетов: схема invoice-блока, модель данных, генерация PDF, PII."""

from decimal import Decimal
from pathlib import Path

import pytest

from portier.gmail_client import _unmask_result
from portier.invoices import (
    amount_in_words,
    build_invoice_data,
    extract_travelline_id,
    format_money,
    generate_invoice_pdf,
    int_in_words,
    invoice_missing_fields,
    invoice_number_from_id,
    parse_amount,
    ru_date,
)
from portier.schemas import EmailAnalysisResult, InvoiceDetails


class _Settings:
    HOTEL_NAME = "ООО «Тест»"
    HOTEL_INN = "7701234567"
    HOTEL_KPP = "770101001"
    HOTEL_ADDRESS = "191014, СПб, ул. Тестовая, д.1"
    HOTEL_PHONE = "+7 900 000 00 00"
    HOTEL_EMAIL = "test@example.com"
    HOTEL_RS = "40702810003000012501"
    HOTEL_BANK = "Филиал «Тест» АО «Банк»"
    HOTEL_BIK = "044030723"
    HOTEL_KS = "30101810100000000723"
    INVOICE_STAMP_PATH = "нет/такого/файла.png"
    INVOICE_SIGNATURE_PATH = "нет/такого/файла.png"

    def __init__(self, invoices_dir: str):
        self.INVOICES_DIR = invoices_dir


def _result(**kwargs) -> EmailAnalysisResult:
    return EmailAnalysisResult(
        type="invoice_required", priority="normal",
        action_required="Выставить счёт", **kwargs,
    )


def test_invoice_block_optional():
    result = EmailAnalysisResult(type="unknown", priority="low", action_required="—")
    assert result.invoice is None


def test_invoice_block_fields():
    inv = InvoiceDetails(
        company_name='ООО «Ромашка»', inn="7712345678", kpp="771201001",
        legal_address="г. Москва, ул. Полевая, 1", amount="15000 руб.",
        description="Проживание 2 гостей", arrival_date="2026-09-01",
        departure_date="2026-09-05",
    )
    result = _result(invoice=inv)
    assert result.invoice.company_name == 'ООО «Ромашка»'
    assert result.invoice.inn == "7712345678"
    assert result.invoice.kpp == "771201001"


def test_invoice_block_partial():
    inv = InvoiceDetails(company_name="ООО «Альфа»")
    assert inv.inn is None and inv.amount is None


def test_missing_fields_detection():
    assert invoice_missing_fields(None) == ["компания", "ИНН", "сумма"]
    assert invoice_missing_fields(InvoiceDetails()) == ["компания", "ИНН", "сумма"]
    full = InvoiceDetails(company_name="ООО", inn="123", amount="100 руб.")
    assert invoice_missing_fields(full) == []


def test_extract_travelline_id():
    body = "Подтверждение бронирования \nID 20260830-7348-456721519\nTRAVELLINE"
    assert extract_travelline_id(body) == "20260830-7348-456721519"
    # без префикса ID — fallback на голый паттерн
    assert extract_travelline_id("бронь 20260815-7348-447005164 ok") == "20260815-7348-447005164"
    assert extract_travelline_id("нет идентификатора") is None


def test_invoice_number_from_id():
    assert invoice_number_from_id("20260815-7348-447005164") == "447005164-01"
    assert invoice_number_from_id(None) is None


def test_ru_date():
    assert ru_date("2026-08-15") == "15 августа 2026 г."
    assert ru_date("2026-01-01") == "1 января 2026 г."
    assert ru_date(None) == "—"
    assert ru_date("не дата") == "не дата"


def test_parse_amount():
    assert parse_amount("18 140 RUB") == Decimal("18140.00")
    assert parse_amount("14022") == Decimal("14022.00")
    assert parse_amount("18870,50") == Decimal("18870.50")
    assert parse_amount(None) is None
    assert parse_amount("без цифр") is None


def test_format_money():
    assert format_money(Decimal("18870")) == "18 870,00"
    assert format_money(Decimal("870.5")) == "870,50"
    assert format_money(None) == "—"


def test_int_in_words():
    assert int_in_words(0) == "ноль"
    assert int_in_words(21) == "двадцать один"
    assert int_in_words(1001) == "одна тысяча один"
    assert int_in_words(22000) == "двадцать две тысячи"
    assert int_in_words(18870) == "восемнадцать тысяч восемьсот семьдесят"


def test_amount_in_words():
    assert (
        amount_in_words(Decimal("18870"))
        == "Восемнадцать тысяч восемьсот семьдесят рублей 00 копеек"
    )
    assert amount_in_words(Decimal("1")) == "Один рубль 00 копеек"
    assert amount_in_words(Decimal("21001.55")) == "Двадцать одна тысяча один рубль 55 копеек"
    assert amount_in_words(Decimal("22022")) == "Двадцать две тысячи двадцать два рубля 00 копеек"
    assert amount_in_words(None) == ""


def test_invoice_data_full(tmp_path):
    settings = _Settings(str(tmp_path))
    inv = InvoiceDetails(
        company_name='ООО «ПЕГАС СПб»', inn="7840483966", kpp="784001001",
        legal_address="191040, СПб, Пушкинская, 10", amount="18 140 RUB",
        description="Номер стандарт с завтраком",
        arrival_date="2026-08-30", departure_date="2026-09-03",
    )
    result = _result(
        invoice=inv,
        booking_number="1208478229",
        internal_booking_id="20260830-7348-456721519",
    )
    data = build_invoice_data(result, settings)
    assert data.number == "456721519-01"  # из внутреннего TravelLine ID
    assert data.booking_ref == "1208478229"  # «Бронь:» — номер из «Подтверждение №…»
    assert data.qty == 4
    assert data.price == Decimal("4535.00")
    assert data.amount == Decimal("18140.00")
    assert "Проживание с 30 августа 2026 г. по 3 сентября 2026 г." in data.item_name
    assert "Бронь № 1208478229" in data.item_name
    assert "AUKHADEEV" not in data.item_name  # без ПДн
    assert data.payer_kpp == "784001001"
    assert data.supplier_rs == "40702810003000012501"


def test_invoice_data_booking_ref_fallback(tmp_path):
    """Нет «Подтверждение №» — в «Бронь:» уходит внутренний TravelLine ID."""
    settings = _Settings(str(tmp_path))
    result = _result(invoice=None, internal_booking_id="20260830-7348-456721519")
    assert build_invoice_data(result, settings).booking_ref == "20260830-7348-456721519"


def test_invoice_data_missing_no_crash(tmp_path):
    settings = _Settings(str(tmp_path))
    data = build_invoice_data(_result(invoice=None), settings)
    assert data.number is None
    assert data.qty is None
    assert data.amount is None
    assert data.item_name == "Проживание в отеле"
    assert format_money(data.amount) == "—"


def test_pdf_generated(tmp_path):
    settings = _Settings(str(tmp_path))
    inv = InvoiceDetails(company_name='ООО «Ромашка»', inn="7712345678", amount="15000")
    path = generate_invoice_pdf(_result(invoice=inv), settings)
    assert path.exists()
    assert path.parent == tmp_path
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(data) > 1000


def test_pdf_missing_data_no_crash(tmp_path):
    settings = _Settings(str(tmp_path))
    path = generate_invoice_pdf(_result(invoice=None), settings)
    assert path.exists()


def test_pii_unmask_invoice_fields():
    result = _result(
        invoice=InvoiceDetails(company_name="ООО «Ромашка»", description="Контакт: [GUEST_1]"),
        comment_details="Просит [GUEST_1]",
    )
    _unmask_result(result, {"[GUEST_1]": "Иван Петров"})
    assert result.invoice.description == "Контакт: Иван Петров"
    assert result.comment_details == "Просит Иван Петров"


def test_font_fallback_to_regular(monkeypatch):
    """Нет TTF для курсива/болда — подставляем обычный Unicode-шрифт,
    иначе базовая Helvetica рисует кириллицу «квадратами» (сумма прописью)."""
    from portier import invoices

    class _FakePath:
        def __init__(self, path):
            self.path = str(path)

        def exists(self):
            return self.path == "/fake/regular.ttf"

    monkeypatch.setattr(invoices, "_FONT_CANDIDATES", {
        "regular": [("/fake/regular.ttf", "FakeRegular")],
        "bold": [("/fake/bold.ttf", "FakeBold")],
        "italic": [("/fake/italic.ttf", "FakeItalic")],
    })
    monkeypatch.setattr(invoices, "_FONTS", {
        "regular": "Helvetica", "bold": "Helvetica-Bold", "italic": "Helvetica-Oblique",
    })
    monkeypatch.setattr(invoices, "_fonts_registered", False)
    monkeypatch.setattr(invoices, "Path", _FakePath)
    from reportlab.pdfbase import pdfmetrics

    monkeypatch.setattr(pdfmetrics, "registerFont", lambda font: None)
    monkeypatch.setattr("reportlab.pdfbase.ttfonts.TTFont", lambda name, path: object())

    fonts = invoices._register_fonts()
    assert fonts["regular"] == "FakeRegular"
    assert fonts["bold"] == "FakeRegular"
    assert fonts["italic"] == "FakeRegular"

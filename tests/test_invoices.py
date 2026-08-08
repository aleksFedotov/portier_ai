"""Тесты счетов: схема invoice-блока, генерация PDF, PII на invoice-полях."""

from pathlib import Path

import pytest

from portier.gmail_client import _unmask_result
from portier.invoices import (
    build_invoice_lines,
    extract_travelline_id,
    generate_invoice_pdf,
    invoice_missing_fields,
    invoice_number_from_id,
)
from portier.schemas import EmailAnalysisResult, InvoiceDetails


class _Settings:
    HOTEL_NAME = "Мини-отель «Тест»"
    HOTEL_INN = "7701234567"
    HOTEL_DETAILS = "р/с 4070281..."

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
        company_name='ООО «Ромашка»', inn="7712345678", amount="15000 руб.",
        description="Проживание 2 гостей", arrival_date="2026-09-01",
        departure_date="2026-09-05",
    )
    result = _result(invoice=inv)
    assert result.invoice.company_name == 'ООО «Ромашка»'
    assert result.invoice.inn == "7712345678"


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
    assert invoice_number_from_id("20260815-7348-447005164") == "447005164-1"
    assert invoice_number_from_id(None) is None


def test_invoice_lines_with_internal_id(tmp_path):
    settings = _Settings(str(tmp_path))
    inv = InvoiceDetails(company_name='ООО «Ромашка»', amount="15000 руб.")
    result = _result(invoice=inv, internal_booking_id="20260830-7348-456721519")
    text = "\n".join(build_invoice_lines(result, settings))
    assert "СЧЁТ № 456721519-1 от" in text
    assert "Бронь: 20260830-7348-456721519" in text


def test_invoice_lines_without_internal_id(tmp_path):
    settings = _Settings(str(tmp_path))
    text = "\n".join(build_invoice_lines(_result(invoice=None), settings))
    assert "СЧЁТ НА ОПЛАТУ от" in text
    assert "Бронь:" not in text


def test_invoice_lines_content(tmp_path):
    settings = _Settings(str(tmp_path))
    inv = InvoiceDetails(
        company_name='ООО «Ромашка»', inn="7712345678", amount="15000 руб.",
        arrival_date="2026-09-01", departure_date="2026-09-05",
    )
    text = "\n".join(
        build_invoice_lines(_result(invoice=inv, booking_number="1208478229"), settings)
    )
    assert "Номер бронирования: 1208478229" in text
    assert "7701234567" in text  # ИНН отеля
    assert "7712345678" in text  # ИНН заказчика
    assert "15000 руб." in text
    assert "2026-09-01" in text and "2026-09-05" in text
    assert "М.П." in text


def test_invoice_lines_missing_data_no_crash(tmp_path):
    settings = _Settings(str(tmp_path))
    text = "\n".join(build_invoice_lines(_result(invoice=None), settings))
    assert "—" in text  # пустые поля заменены прочерком


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

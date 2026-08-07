"""Тесты разбора реестров Яндекс Путешествий."""

import io

import openpyxl
import pytest

from portier.yandex_registry import (
    build_registry_notification,
    is_yandex_registry,
    parse_payment_order,
    parse_registry,
)

SENDER = '"Яндекс Путешествия" <hotels@travel.yandex.ru>'
SUBJECT = 'Реестр бронирований по платежному поручению 25732 от 22.06.2026 для ООО "ОРОН"'


def _make_xlsx(bookings: list[tuple[str, str, float, float]]) -> bytes:
    """Собрать xlsx в формате реестра Яндекса: (номер брони, гость, перечислено, комиссия)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Реестр бронирований по ПП"])
    ws.append(["Договор №826102/20 от 27."])
    ws.append([])
    ws.append(["П/п №25732 от 22.06.2026"])
    ws.append([])
    ws.append([
        "Объект размещения", "№ бронирования в системе", "№ бронирования в системе отеля",
        "Имя гостя", "Количество номеров", "Дата бронирования", "Дата заезда", "Дата выезда",
        "Тип перечисления", "Начислено по тарифу", "Скидка", "Оплатил гость",
        "Вознаграждение Яндекса с НДС", "Перечислено отелю",
    ])
    for number, guest, amount, commission in bookings:
        ws.append(["ЛиКи Лофт Отель", number, "", guest, 1, "2026-05-27", "2026-06-06",
                   "2026-06-08", "Оплата", amount, 0, amount, commission, amount - commission])
    ws.append([])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_is_yandex_registry():
    assert is_yandex_registry(SENDER, SUBJECT) is True
    assert is_yandex_registry(SENDER, "Подтверждение бронирования №1") is False
    assert is_yandex_registry("noreply@raiffeisen.ru", SUBJECT) is False
    assert is_yandex_registry("", "") is False


def test_parse_payment_order():
    assert parse_payment_order(SUBJECT) == ("25732", "22.06.2026")
    assert parse_payment_order("любая другая тема") == ("", "")


def test_parse_registry_one_booking():
    data = _make_xlsx([("YA-3309-9145-0970", "Медведева Наталия", 18144.0, 2457.0)])
    report = parse_registry(data, payment_order="25732", payment_date="22.06.2026")
    assert len(report.bookings) == 1
    b = report.bookings[0]
    assert b.number == "YA-3309-9145-0970"
    assert b.guest == "Медведева Наталия"
    assert b.amount == pytest.approx(15687.0)
    assert report.total == pytest.approx(15687.0)
    assert report.commission == pytest.approx(2457.0)


def test_parse_registry_several_bookings():
    data = _make_xlsx([
        ("YA-1", "Иванов", 10000.0, 1500.0),
        ("YA-2", "Петров", 5000.0, 750.0),
    ])
    report = parse_registry(data)
    assert [b.number for b in report.bookings] == ["YA-1", "YA-2"]
    assert report.total == pytest.approx(12750.0)
    assert report.commission == pytest.approx(2250.0)


def test_parse_registry_empty():
    report = parse_registry(_make_xlsx([]))
    assert report.bookings == []
    assert report.total == 0.0


def test_notification_text():
    data = _make_xlsx([("YA-1", "Иванов & Ко", 10000.0, 1500.0)])
    report = parse_registry(data, payment_order="25732", payment_date="22.06.2026")
    text = build_registry_notification(report)
    assert "ПП №25732 от 22.06.2026" in text
    assert "YA-1" in text
    assert "Иванов &amp; Ко" in text  # HTML-экранирование
    assert "8 500 ₽" in text
    assert "Вознаграждение Яндекса: 1 500 ₽" in text

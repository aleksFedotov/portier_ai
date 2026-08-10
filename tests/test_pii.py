"""Тесты маскирования PII."""

from portier.pii import mask_pii, unmask_pii


def test_phone_masked():
    masked, mapping = mask_pii("Телефон гостя: +7 916 123-45-67")
    assert "+7 916 123-45-67" not in masked
    assert "[PHONE_1]" in masked
    assert mapping["[PHONE_1]"] == "+7 916 123-45-67"


def test_phone_formats():
    for phone in ("89161234567", "8(916)123-45-67", "+7(916)1234567", "+7 916 123 45 67"):
        masked, _ = mask_pii(f"звоните {phone}")
        assert phone not in masked, phone


def test_email_masked():
    masked, mapping = mask_pii("Почта: ivan.petrov@example.com, пишите.")
    assert "ivan.petrov@example.com" not in masked
    assert "[EMAIL_1]" in masked
    # точка в конце предложения не входит в токен
    assert mapping["[EMAIL_1]"] == "ivan.petrov@example.com"


def test_name_masked():
    masked, mapping = mask_pii("Бронь для Ивана Петрова на двоих.")
    assert "Ивана Петрова" not in masked
    assert any(t.startswith("[GUEST_") for t in mapping)


def test_no_pii_left_in_masked():
    text = "Гость Мария Сидорова, +7 925 111-22-33, maria@mail.ru"
    masked, _ = mask_pii(text)
    for fragment in ("Мария Сидорова", "925 111-22-33", "maria@mail.ru"):
        assert fragment not in masked


def test_unmask_roundtrip():
    text = "Гость Иван Петров, тел 89161234567, почта ivan@mail.ru."
    masked, mapping = mask_pii(text)
    assert unmask_pii(masked, mapping) == text


def test_same_value_same_token():
    masked, mapping = mask_pii("Иван Петров приедет поздно. С уважением, Иван Петров.")
    tokens = [t for t in mapping if t.startswith("[GUEST_")]
    assert tokens == ["[GUEST_1]"]
    assert masked.count("[GUEST_1]") == 2


def test_unmask_partial_fields():
    mapping = {"[GUEST_1]": "Иван Петров"}
    assert unmask_pii("Гость [GUEST_1] просит счёт", mapping) == "Гость Иван Петров просит счёт"
    assert unmask_pii(None, mapping) is None
    assert unmask_pii("", mapping) == ""


# ---------- даты рождения (тикет 22) ----------


def test_birthdates_masked():
    text = (
        "Даты рождения проживающих: NIGMATULLINA ALBINA 15.03.1976, "
        "TYRYSHKINA VERONIKA 04.02.1976"
    )
    masked, mapping = mask_pii(text)
    assert "15.03.1976" not in masked
    assert "04.02.1976" not in masked
    bdates = [t for t in mapping if t.startswith("[BDATE_")]
    assert bdates == ["[BDATE_1]", "[BDATE_2]"]
    assert unmask_pii(masked, mapping) == text


def test_stay_dates_not_masked():
    """Даты проживания — не ПДн, нужны для счёта: не маскируем."""
    text = "Проживание с 15.08.2026 по 19.08.2026, заезд 15.08.2026."
    masked, mapping = mask_pii(text)
    assert "15.08.2026" in masked
    assert "19.08.2026" in masked
    assert not any(t.startswith("[BDATE_") for t in mapping)

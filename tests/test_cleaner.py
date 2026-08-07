"""Тесты очистки HTML-писем."""

from portier.cleaner import clean_email


def test_removes_style_and_script():
    html = (
        "<html><head><style>body{color:red}</style>"
        "<script>alert(1)</script></head>"
        "<body><p>Здравствуйте, бронь 123</p></body></html>"
    )
    text = clean_email(html)
    assert "Здравствуйте, бронь 123" in text
    assert "alert" not in text
    assert "color:red" not in text


def test_extracts_text_from_html():
    html = "<div>Гость: <b>Иван</b><br>Заезд 01.09</div>"
    text = clean_email(html)
    assert "Гость:" in text
    assert "Иван" in text
    assert "Заезд 01.09" in text
    assert "<" not in text


def test_plain_text_passthrough():
    plain = "Добрый день! Прошу счёт на оплату брони 456."
    assert clean_email(plain) == plain


def test_footer_removed():
    text = "Важное сообщение\n\nОтправлено с iPhone"
    assert clean_email(text) == "Важное сообщение"


def test_empty():
    assert clean_email("") == ""

"""Тикет 31: кнопки «Понятно»/«🖋 Печать» на уведомлениях владельцу."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import portier.callbacks as cb
from portier import gmail_client
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.models import Base, EmailAction, EmailStatus, ProcessedEmail
from portier.stamp import stamp_pdf


async def _pipeline(monkeypatch, tmp_path, sender, subject):
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    analyze = AsyncMock()
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>", "sender": sender,
            "subject": subject, "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Текст письма."),
        fetch_attachments=AsyncMock(return_value=[]),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111, OWNER_CHAT_ID=999,
        DATABASE_URL="sqlite+aiosqlite:///:memory:", INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")
    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    return bot, record


async def test_owner_notice_card_has_both_buttons(monkeypatch, tmp_path):
    bot, record = await _pipeline(
        monkeypatch, tmp_path, "Отелло <otello@2gis.ru>", "Акт сверки"
    )
    assert record.email_type == "owner_notice"
    assert record.gmail_id == "gmail-id-1"  # тикет 31: храним для кнопки «Печать»
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    data = {btn.callback_data for row in markup.inline_keyboard for btn in row}
    assert f"action:notice_ok:{record.id}" in data
    assert f"action:notice_stamp:{record.id}" in data


# ---------- stamp_and_draft ----------

@pytest.fixture
def _email():
    return ProcessedEmail(
        message_id="<m1@x>", uid=1, gmail_id="gmail-1",
        sender="Бухгалтерия <buh@x.ru>", subject="Акт сверки",
        raw_payload="", status=EmailStatus.SUCCESS.value,
    )


async def test_stamp_and_draft_success(monkeypatch, _email, tmp_path):
    gmail = SimpleNamespace(
        fetch_attachments=AsyncMock(return_value=[("akt.pdf", b"PDF-BYTES")]),
        fetch_thread_id=AsyncMock(return_value="thread-1"),
        create_draft=AsyncMock(return_value="draft-1"),
    )
    monkeypatch.setattr(cb, "_get_gmail", lambda: gmail)
    monkeypatch.setattr(cb, "get_settings" if hasattr(cb, "get_settings") else "get_settings", None, raising=False)
    # get_settings импортируется внутри функции — мокаем на уровне модулей
    import portier.drafts, portier.stamp
    monkeypatch.setattr("portier.stamp.stamp_pdf", lambda data, settings: data + b"-STAMPED")
    monkeypatch.setattr("portier.config.get_settings", lambda: SimpleNamespace(SIGNATURE_CAPTION=""))

    note = await cb.stamp_and_draft(_email)

    assert "Подписано (1 шт.)" in note
    gmail.fetch_attachments.assert_awaited_once_with("gmail-1", ".pdf")
    raw = gmail.create_draft.await_args.args[0]
    assert gmail.create_draft.await_args.kwargs["thread_id"] == "thread-1"
    import base64, email as em
    msg = em.message_from_bytes(base64.urlsafe_b64decode(raw), policy=em.policy.default)
    assert msg["To"] == "buh@x.ru"
    assert msg["Subject"].startswith("Re: Акт сверки")
    assert msg["In-Reply-To"] == "<m1@x>"


async def test_stamp_and_draft_no_pdf_raises(monkeypatch, _email):
    gmail = SimpleNamespace(fetch_attachments=AsyncMock(return_value=[]))
    monkeypatch.setattr(cb, "_get_gmail", lambda: gmail)
    monkeypatch.setattr("portier.config.get_settings", lambda: SimpleNamespace())

    with pytest.raises(ValueError, match="PDF"):
        await cb.stamp_and_draft(_email)


# ---------- handle_action ----------

def _callback(data: str, with_buttons=True):
    markup = None
    if with_buttons:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Понятно", callback_data="action:notice_ok:1"),
            InlineKeyboardButton(text="🖋 Печать", callback_data="action:notice_stamp:1"),
        ]])
    callback = AsyncMock()
    callback.data = data
    callback.from_user.full_name = "Владелец"
    callback.from_user.username = "owner"
    callback.message.text = "📄 Документ"
    callback.message.html_text = "📄 Документ"
    callback.message.reply_markup = markup
    return callback


@pytest.fixture
async def _db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    cb.get_session_factory.__wrapped__ if hasattr(cb.get_session_factory, "__wrapped__") else None
    import portier.callbacks
    portier.callbacks.get_session_factory = lambda: factory
    async with factory() as session:
        session.add(ProcessedEmail(
            id=1, message_id="<m1@x>", uid=1, gmail_id="gmail-1",
            sender="a@b.c", subject="Акт", raw_payload="",
            status=EmailStatus.SUCCESS.value,
        ))
        await session.commit()
    yield factory
    await engine.dispose()


async def test_notice_ok_records_and_clears_buttons(_db):
    callback = _callback("action:notice_ok:1")
    await cb.handle_action(callback)

    async with _db() as session:
        action = (await session.execute(select(EmailAction))).scalars().one()
    assert action.action == "notice_ok"
    # обе кнопки сняты (взаимоисключающие)
    assert callback.message.edit_text.await_args.kwargs["reply_markup"] is None
    assert "Понятно" in callback.message.edit_text.await_args.args[0]


async def test_notice_stamp_success(monkeypatch, _db):
    monkeypatch.setattr(
        cb, "stamp_and_draft", AsyncMock(return_value="🖋 Подписано (1 шт.)")
    )
    callback = _callback("action:notice_stamp:1")
    await cb.handle_action(callback)

    async with _db() as session:
        action = (await session.execute(select(EmailAction))).scalars().one()
    assert action.action == "notice_stamp"
    text = callback.message.edit_text.await_args.args[0]
    assert "Подписано" in text


async def test_notice_stamp_failure_keeps_button(monkeypatch, _db):
    monkeypatch.setattr(
        cb, "stamp_and_draft", AsyncMock(side_effect=ValueError("нет PDF"))
    )
    callback = _callback("action:notice_stamp:1")
    await cb.handle_action(callback)

    async with _db() as session:
        actions = (await session.execute(select(EmailAction))).scalars().all()
    assert actions == []  # действие не записано — кнопка остаётся живой
    callback.message.edit_text.assert_not_called()


# ---------- stamp_pdf smoke ----------

def test_stamp_pdf_smoke(tmp_path):
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader

    src = tmp_path / "src.pdf"
    pdf = canvas.Canvas(str(src))
    pdf.drawString(100, 700, "page 1")
    pdf.showPage()
    pdf.drawString(100, 700, "page 2")
    pdf.save()

    settings = SimpleNamespace(
        INVOICE_STAMP_PATH="data/печать 2-Photoroom.png",
        INVOICE_SIGNATURE_PATH="data/подпись 2-Photoroom.png",
        SIGNATURE_CAPTION="",
    )
    out = stamp_pdf(src.read_bytes(), settings)
    reader = PdfReader(io.BytesIO(out))
    assert len(reader.pages) == 2


# ---------- find_signature_line ----------

def _make_pdf(path, draw):
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path))
    draw(pdf)
    pdf.save()
    return path.read_bytes()


def test_find_signature_line_underscores(tmp_path):
    """Текстовая черта «____» справа внизу находится, берётся самая правая."""
    from pypdf import PdfReader
    from portier.stamp import find_signature_line

    data = _make_pdf(tmp_path / "u.pdf", lambda pdf: (
        pdf.drawString(50, 300, "____________________"),
        pdf.drawString(400, 250, "____________________"),
    ))
    reader = PdfReader(io.BytesIO(data))
    line = find_signature_line(reader, reader.pages[-1])
    assert line is not None
    x, y, w = line
    assert x == 400
    assert y == 250
    assert w > 50


def test_find_signature_line_vector(tmp_path):
    """Векторная черта (pdf.line) в нижней половине находится."""
    from pypdf import PdfReader
    from portier.stamp import find_signature_line

    def draw(pdf):
        pdf.setLineWidth(1)
        pdf.line(300, 200, 450, 200)

    data = _make_pdf(tmp_path / "v.pdf", draw)
    reader = PdfReader(io.BytesIO(data))
    line = find_signature_line(reader, reader.pages[-1])
    assert line is not None
    x, y, w = line
    assert x == 300
    assert y == 200
    assert w == 150


def test_find_signature_line_none(tmp_path):
    """Пустая страница — черта не находится (fallback в вызывающем коде)."""
    from pypdf import PdfReader
    from portier.stamp import find_signature_line

    data = _make_pdf(tmp_path / "e.pdf", lambda pdf: pdf.drawString(100, 700, "hi"))
    reader = PdfReader(io.BytesIO(data))
    assert find_signature_line(reader, reader.pages[-1]) is None


def test_stamp_pdf_uses_detected_line(tmp_path, monkeypatch):
    """stamp_pdf передаёт найденную черту в overlay (подпись над ней)."""
    import portier.stamp as stamp_mod
    from reportlab.pdfgen import canvas

    src = tmp_path / "src.pdf"
    pdf = canvas.Canvas(str(src))
    pdf.drawString(400, 250, "____________________")
    pdf.save()

    captured = {}
    real_overlay = stamp_mod._overlay_page

    def spy(page_box, stamp_path, signature_path, caption, line, anchor=None,
            placements=None):
        captured["line"] = line
        captured["anchor"] = anchor
        captured["placements"] = placements
        return real_overlay(page_box, stamp_path, signature_path, caption, line,
                            anchor, placements)

    monkeypatch.setattr(stamp_mod, "_overlay_page", spy)
    settings = SimpleNamespace(
        INVOICE_STAMP_PATH="data/печать 2-Photoroom.png",
        INVOICE_SIGNATURE_PATH="data/подпись 2-Photoroom.png",
        SIGNATURE_CAPTION="",
    )
    stamp_pdf(src.read_bytes(), settings)
    assert captured["line"] is not None
    assert captured["line"][0] == 400


# ---------- find_anchor ----------
# Генерировать кириллицу reportlab'ом нельзя (Helvetica без глифов),
# поэтому якорные тесты идут по реальным образцам из test_data.

def _anchor_of(name: str):
    from pathlib import Path

    from pypdf import PdfReader

    from portier.stamp import find_anchor

    data = Path("test_data", name).read_bytes()
    reader = PdfReader(io.BytesIO(data))
    page = reader.pages[-1]
    return find_anchor(data, len(reader.pages) - 1, float(page.mediabox.height))


def test_find_anchor_company_column_right():
    """Акт сверки 50351: наша колонка справа — «М.П.» правого блока."""
    anchor = _anchor_of("Акт сверки взаиморасчетов № 50351 от 30 июля 2026 г (1).pdf")
    assert anchor is not None
    x, y, word = anchor
    assert word == "М.П."
    assert x > 250  # правая половина страницы (левый «М.П.» — у контрагента)


def test_find_anchor_company_column_left():
    """Отчёт комитенту: ООО «Орон» слева — берём ЛЕВЫЙ «М.П.»."""
    anchor = _anchor_of("Отчет комитенту(о продажах) ХП00260531-00173К от 31.05.2026.pdf")
    assert anchor is not None
    x, y, word = anchor
    assert word == "М.П."
    assert x < 100  # левая колонка — наша сторона в этом шаблоне


def test_find_anchor_company_fallback():
    """Отчёт о выполнении поручения: якорные слова заняты печатью Броневика —
    привязка к названию нашей компании (левая колонка «Принципал»)."""
    anchor = _anchor_of("Отчет агента о выполнении поручения (5).pdf")
    assert anchor is not None
    x, y, word = anchor
    assert word == "ООО «ОРОН»"
    assert x < 200


def test_find_anchor_none_on_empty(tmp_path):
    """Пустая страница (и скан) — якоря нет, сработает следующий fallback."""
    from portier.stamp import find_anchor

    data = _make_pdf(tmp_path / "n.pdf", lambda pdf: pdf.drawString(100, 700, "hi"))
    assert find_anchor(data, 0, 792) is None


def test_stamp_pdf_real_samples(tmp_path):
    """Все образцы из test_data проходят stamp_pdf без ошибок, страничность та же."""
    from pathlib import Path

    from pypdf import PdfReader

    samples = sorted(Path("test_data").glob("*.pdf"))
    assert samples, "test_data пуст — нечего проверять"
    settings = SimpleNamespace(
        INVOICE_STAMP_PATH="data/печать 2-Photoroom.png",
        INVOICE_SIGNATURE_PATH="data/подпись 2-Photoroom.png",
        SIGNATURE_CAPTION="",
    )
    for sample in samples:
        src = sample.read_bytes()
        out = stamp_pdf(src, settings)
        assert len(PdfReader(io.BytesIO(out)).pages) == len(PdfReader(io.BytesIO(src)).pages), sample.name

"""Тикет 13: справочник агентов — матчинг, сид, веб-панель, конвейер счетов."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portier import gmail_client
from portier.agents import match_agent_in_list, seed_agents
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.models import Agent, Base, EmailStatus, ProcessedEmail
from portier.schemas import EmailAnalysisResult, InvoiceDetails
from portier.web import create_app


def _agent(**kw) -> Agent:
    defaults = dict(
        name="Pegas", aliases="Pegas;Пегас", invoice_on_booking=True,
        payer_name="ООО «ПЕГАС СПб»", invoice_email="priemspb@pegast.ru",
        price_note="цену не меняем", note="",
    )
    return Agent(**{**defaults, **kw})


# ---------- юнит: матчинг ----------


def test_match_by_subject_and_channel():
    agents = [_agent(), _agent(name="Alean", aliases="Alean;Алеан",
                               payer_name="ООО «Система Алеан»")]
    assert match_agent_in_list(
        agents, "Подтверждение бронирования №1206313115. Pegas", None
    ).name == "Pegas"
    assert match_agent_in_list(agents, "Какая-то тема", "Alean.ru").name == "Alean"
    assert match_agent_in_list(agents, "Какая-то тема", "Ostrovok") is None
    # регистр не важен
    assert match_agent_in_list(agents, "пегас touristic", None).name == "Pegas"


# ---------- сид ----------


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_seed_agents(session_factory, tmp_path):
    seed_file = tmp_path / "agents.yaml"
    seed_file.write_text(yaml.safe_dump([
        {"name": "Pegas", "aliases": "Pegas", "payer_name": "ООО «ПЕГАС СПб»"},
        {"name": "Tvil", "aliases": "Tvil", "invoice_on_booking": False},
    ], allow_unicode=True), encoding="utf-8")

    assert await seed_agents(session_factory, str(seed_file)) == 2
    async with session_factory() as session:
        agents = (await session.execute(select(Agent))).scalars().all()
    by_name = {a.name: a for a in agents}
    assert by_name["Pegas"].invoice_on_booking is True  # дефолт вкл
    assert by_name["Tvil"].invoice_on_booking is False

    # повторный сид не дублирует
    assert await seed_agents(session_factory, str(seed_file)) == 0


async def test_seed_agents_missing_file(session_factory):
    assert await seed_agents(session_factory, "нет_такого_файла.yaml") == 0


# ---------- веб-панель ----------


@pytest.fixture
def client(session_factory):
    with TestClient(create_app(session_factory)) as c:
        yield c


def test_agents_crud(client):
    assert "Пока нет ни одного агента" in client.get("/agents").text

    resp = client.post("/agents/new", follow_redirects=False, data={
        "name": "Pegas", "aliases": "Pegas;Пегас", "invoice_on_booking": "on",
        "payer_name": "ООО «ПЕГАС СПб»", "invoice_email": "priemspb@pegast.ru",
        "price_note": "цену не меняем", "note": "",
    })
    assert resp.status_code == 303
    html = client.get("/agents").text
    assert "Pegas" in html and "ООО «ПЕГАС СПб»" in html

    resp = client.post("/agents/1/edit", follow_redirects=False, data={
        "name": "Pegas", "aliases": "Pegas",  # чекбокс не передан → выкл
        "payer_name": "", "invoice_email": "", "price_note": "", "note": "",
    })
    assert resp.status_code == 303
    assert "<td>нет</td>" in client.get("/agents").text

    assert client.post("/agents/1/delete", follow_redirects=False).status_code == 303
    assert "Pegas" not in client.get("/agents").text


# ---------- конвейер: подтверждение от агента → счёт ----------


async def test_agent_booking_becomes_invoice(monkeypatch, tmp_path):
    """booking_confirmed от Pegas → invoice_required: PDF в чат, email агента в пометке."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    async with get_session_factory()() as session:
        session.add(_agent())
        await session.commit()

    analyze = AsyncMock(return_value=EmailAnalysisResult(
        type="booking_confirmed", priority="normal",
        booking_number="1206313115", channel_name="Pegas",
        arrival_date="2026-08-30", departure_date="2026-09-03",
        action_required="—",
        invoice=InvoiceDetails(amount="18 140,00", description="Стандарт, 2 взрослых"),
    ))
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>",
            "sender": "TravelLine <noreply@travellinemail.com>",
            "subject": "Подтверждение бронирования №1206313115. Pegas",
            "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Подтверждение бронирования."),
        fetch_attachments=AsyncMock(return_value=[]),
        create_draft=AsyncMock(return_value="draft-1"),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        OWNER_CHAT_ID=999,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")

    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    assert record.status == EmailStatus.SUCCESS.value
    assert record.email_type == "invoice_required"
    # Тикет 22: карточка и PDF счёта — в основную группу, не владельцу и не
    # в группу счетов
    assert bot.send_document.await_args.kwargs["chat_id"] == 111
    assert bot.send_message.await_args.kwargs["chat_id"] == 111
    text = bot.send_message.await_args.kwargs.get("text", "")
    assert "priemspb@pegast.ru" in text  # email агента, а не travellinemail
    assert "цену не меняем" in text  # price_note


async def test_agent_without_invoice_stays_silent(monkeypatch, tmp_path):
    """Агент с invoice_on_booking=false: бронь молча в БД, счёт не выставляем."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    async with get_session_factory()() as session:
        session.add(_agent(name="Tvil", aliases="Tvil", invoice_on_booking=False,
                           payer_name="", invoice_email="", price_note=""))
        await session.commit()

    analyze = AsyncMock(return_value=EmailAnalysisResult(
        type="booking_confirmed", priority="normal",
        channel_name="Tvil.ru", action_required="—",
    ))
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>",
            "sender": "TravelLine <noreply@travellinemail.com>",
            "subject": "Подтверждение бронирования №555. Tvil.ru",
            "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Подтверждение бронирования."),
        fetch_attachments=AsyncMock(return_value=[]),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")

    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    assert record.status == EmailStatus.SUCCESS.value
    assert record.email_type == "booking_confirmed"
    bot.send_message.assert_not_called()
    bot.send_document.assert_not_called()


# ---------- скидка/комиссия из price_note ----------

from decimal import Decimal

from portier.agents import apply_price_percent, parse_price_percent


def test_parse_price_percent():
    assert parse_price_percent("-15% ко всем дням") == Decimal("-15")
    assert parse_price_percent("-18% ко всем дням") == Decimal("-18")
    assert parse_price_percent("+10%") == Decimal("10")
    assert parse_price_percent("цену не меняем") is None
    assert parse_price_percent("") is None
    assert parse_price_percent(None) is None


def test_apply_price_percent_discount():
    assert apply_price_percent("18 870", Decimal("-15")) == "16 039,50"


def test_apply_price_percent_no_percent_keeps_amount():
    assert apply_price_percent("18 870", None) == "18 870"


def test_apply_price_percent_unparsed_amount_kept():
    assert apply_price_percent("договорная", Decimal("-15")) == "договорная"


# ---------- напоминание «отредактируйте бронирование» (тикет 30) ----------

async def test_agent_edit_notice_sent(monkeypatch, tmp_path):
    """Агент с edit_note + booking_confirmed → напоминание в основную группу."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    async with get_session_factory()() as session:
        session.add(_agent(
            name="Tvil", aliases="Tvil", invoice_on_booking=False,
            payer_name="", invoice_email="", price_note="",
            edit_note="Цена -20% ко всем дням. Гость оплачивает в отеле.",
        ))
        await session.commit()

    analyze = AsyncMock(return_value=EmailAnalysisResult(
        type="booking_confirmed", priority="normal",
        booking_number="555", channel_name="Tvil.ru",
        arrival_date="2026-09-01", departure_date="2026-09-03",
        action_required="—",
    ))
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>",
            "sender": "TravelLine <noreply@travellinemail.com>",
            "subject": "Подтверждение бронирования №555. Tvil.ru",
            "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Подтверждение бронирования."),
        fetch_attachments=AsyncMock(return_value=[]),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")

    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    assert record.status == EmailStatus.SUCCESS.value
    bot.send_message.assert_awaited_once()
    call = bot.send_message.await_args
    assert call.kwargs["chat_id"] == 111
    text = call.kwargs["text"]
    assert "Отредактируйте бронирование" in text
    assert "Бронь № 555" in text
    assert "-20% ко всем дням" in text
    bot.send_document.assert_not_called()


async def test_yandex_modified_edit_notice_sent(monkeypatch, tmp_path):
    """Яндекс Путешествия: бронь приходит как booking_modified — напоминание
    «отредактируйте бронь» всё равно уходит в основную группу."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    async with get_session_factory()() as session:
        session.add(_agent(
            name="Яндекс Путешествия", aliases="Яндекс Путешествия;Yandex Travel",
            invoice_on_booking=False, payer_name="", invoice_email="", price_note="",
            edit_note="Убрать скидку на совместную акцию, цена -20% ко всем дням.",
        ))
        await session.commit()

    analyze = AsyncMock(return_value=EmailAnalysisResult(
        type="booking_modified", priority="normal",
        booking_number="777", channel_name="Яндекс Путешествия",
        arrival_date="2026-09-01", departure_date="2026-09-03",
        action_required="—",
    ))
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>",
            "sender": "TravelLine <noreply@travellinemail.com>",
            "subject": "Изменение бронирования №777. Яндекс Путешествия",
            "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Изменение бронирования."),
        fetch_attachments=AsyncMock(return_value=[]),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")

    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    assert record.status == EmailStatus.SUCCESS.value
    assert record.email_type == "booking_modified"
    bot.send_message.assert_awaited_once()
    call = bot.send_message.await_args
    assert call.kwargs["chat_id"] == 111
    assert "Отредактируйте бронирование" in call.kwargs["text"]


async def test_other_agent_modified_stays_silent(monkeypatch, tmp_path):
    """Другой агент с edit_note + booking_modified — напоминание не уходит."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    async with get_session_factory()() as session:
        session.add(_agent(
            name="Tvil", aliases="Tvil", invoice_on_booking=False,
            payer_name="", invoice_email="", price_note="",
            edit_note="Цена -20% ко всем дням.",
        ))
        await session.commit()

    analyze = AsyncMock(return_value=EmailAnalysisResult(
        type="booking_modified", priority="normal",
        booking_number="888", channel_name="Tvil.ru", action_required="—",
    ))
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>",
            "sender": "TravelLine <noreply@travellinemail.com>",
            "subject": "Изменение бронирования №888. Tvil.ru",
            "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Изменение бронирования."),
        fetch_attachments=AsyncMock(return_value=[]),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")
    bot.send_message.assert_not_called()


async def test_agent_without_edit_notice_stays_silent(monkeypatch, tmp_path):
    """Агент без edit_note: напоминание не уходит (поведение тикета 13)."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    async with get_session_factory()() as session:
        session.add(_agent(name="Tvil", aliases="Tvil", invoice_on_booking=False,
                           payer_name="", invoice_email="", price_note=""))
        await session.commit()

    analyze = AsyncMock(return_value=EmailAnalysisResult(
        type="booking_confirmed", priority="normal",
        channel_name="Tvil.ru", action_required="—",
    ))
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>",
            "sender": "TravelLine <noreply@travellinemail.com>",
            "subject": "Подтверждение бронирования №555. Tvil.ru",
            "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Подтверждение бронирования."),
        fetch_attachments=AsyncMock(return_value=[]),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")

    bot.send_message.assert_not_called()


# ---------- тема/тело черновика Броневика ----------

async def test_bronevik_draft_subject_and_body(monkeypatch, tmp_path):
    """Броневику тема черновика — только «#<номер брони>»; в теле всех счетов
    дублируется номер брони, подпись — фиксированный блок отеля."""
    import base64
    import email
    from email.header import decode_header

    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    async with get_session_factory()() as session:
        session.add(_agent(
            name="Bronevik", aliases="Bronevik;Броневик",
            payer_name="ООО «Компания Броневик»", invoice_email="",
        ))
        await session.commit()

    analyze = AsyncMock(return_value=EmailAnalysisResult(
        type="booking_confirmed", priority="normal",
        booking_number="62158429", channel_name="Bronevik",
        arrival_date="2026-09-01", departure_date="2026-09-03",
        action_required="—",
        invoice=InvoiceDetails(amount="9 000,00", description="Стандарт"),
    ))
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>",
            "sender": "TravelLine <noreply@travellinemail.com>",
            "subject": "Подтверждение бронирования №62158429. Bronevik",
            "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Подтверждение бронирования."),
        fetch_attachments=AsyncMock(return_value=[]),
        create_draft=AsyncMock(return_value="draft-1"),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")

    gmail.create_draft.assert_awaited_once()
    raw = gmail.create_draft.await_args.args[0]
    message = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    raw_subject = decode_header(message["Subject"])[0][0]
    subject = raw_subject if isinstance(raw_subject, str) else str(raw_subject, "utf-8")
    assert subject == "#62158429"  # без префикса «Счёт на оплату проживания»

    body = message.get_payload(0).get_payload(decode=True).decode("utf-8")
    assert "Номер бронирования: #62158429." in body
    assert body.rstrip().endswith(
        "С уважением, ЛиКи Лофт Отель / LiKi LOFT HOTEL\n"
        "+ 7 950 003 50 30\n"
        "likihotel.com\n"
        "Санкт-Петербург, ул. Кирочная, 11"
    )

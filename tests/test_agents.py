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

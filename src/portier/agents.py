"""Справочник агентов (тикет 13): матчинг канала брони и сид из agents.yaml.

Правило «подтверждение брони от агента = счёт» работает от справочника,
а не от догадок LLM: если канал найден и invoice_on_booking включён —
по booking_confirmed выставляем счёт на payer_name.
"""

import logging
import re
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from .models import Agent

logger = logging.getLogger(__name__)

DEFAULT_SEED_FILE = "agents.yaml"


def _aliases(agent: Agent) -> list[str]:
    return [a.strip().lower() for a in agent.aliases.split(";") if a.strip()]


def match_agent_in_list(
    agents: list[Agent], subject: str, channel_name: str | None
) -> Agent | None:
    """Найти агента по алиасу в теме письма или названии канала (без регистра)."""
    haystacks = [(subject or "").lower(), (channel_name or "").lower()]
    for agent in agents:
        for alias in _aliases(agent):
            if any(alias in h for h in haystacks if h):
                return agent
    return None


async def match_agent(
    session, subject: str, channel_name: str | None
) -> Agent | None:
    """Найти агента в справочнике по теме письма / названию канала."""
    result = await session.execute(select(Agent))
    return match_agent_in_list(result.scalars().all(), subject, channel_name)


_PERCENT_RE = re.compile(r"([+-]?\d+(?:[.,]\d+)?)\s*%")


def parse_price_percent(price_note: str | None) -> Decimal | None:
    """Процент скидки/комиссии из price_note: '-15% ко всем дням' → Decimal('-15').

    Процента нет («цену не меняем») — None, сумма счёта не корректируется.
    """
    m = _PERCENT_RE.search(price_note or "")
    if not m:
        return None
    return Decimal(m.group(1).replace(",", "."))


def apply_price_percent(amount_str: str | None, percent: Decimal | None) -> str | None:
    """Применить процент к сумме счёта ('18 870' + -15% → '16 039,50').

    Сумма не распарсилась или процента нет — вернуть исходную строку.
    """
    if percent is None:
        return amount_str
    from .invoices import format_money, parse_amount

    raw = parse_amount(amount_str)
    if raw is None:
        return amount_str
    adjusted = raw * (1 + percent / 100)
    return format_money(adjusted)


async def seed_agents(session_factory, path: str = DEFAULT_SEED_FILE) -> int:
    """Сид справочника из agents.yaml (только если таблица пуста). Возвращает число добавленных."""
    if not Path(path).exists():
        return 0
    import yaml

    with open(path, encoding="utf-8") as fh:
        items = yaml.safe_load(fh) or []

    async with session_factory() as session:
        count = await session.execute(select(func.count(Agent.id)))
        if count.scalar_one() > 0:
            logger.info("Справочник агентов не пуст, сид из %s пропущен", path)
            return 0
        for item in items:
            session.add(Agent(
                name=item.get("name", ""),
                aliases=item.get("aliases", ""),
                invoice_on_booking=bool(item.get("invoice_on_booking", True)),
                payer_name=item.get("payer_name", ""),
                invoice_email=item.get("invoice_email", ""),
                price_note=item.get("price_note", ""),
                note=item.get("note", ""),
            ))
        await session.commit()
    logger.info("Сид агентов из %s: добавлено %d", path, len(items))
    return len(items)

"""Точка входа Portier AI: Gmail-цикл и Telegram-бот в одном процессе."""

import asyncio
import logging
import sys

from .bot import create_bot, create_dispatcher, polling_with_restart
from .config import get_settings
from .db import init_db, init_engine
from .gmail_client import GmailAuthError, gmail_loop, set_llm_client
from .invoice_cleanup import invoice_cleanup_loop
from .llm import create_llm_client


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    settings = get_settings()

    if not settings.TELEGRAM_BOT_TOKEN or settings.TELEGRAM_CHAT_ID is None:
        raise SystemExit(
            "Ошибка конфигурации: для боевого режима задайте TELEGRAM_BOT_TOKEN "
            "и TELEGRAM_CHAT_ID в .env (для бэктеста они не нужны: python -m portier.backtest)"
        )

    init_engine(settings.DATABASE_URL)
    await init_db()

    from .agents import seed_agents
    from .db import get_session_factory
    from .web import create_app, seed_companies

    session_factory = get_session_factory()
    seeded = await seed_companies(session_factory, settings.COMPANIES_SEED_FILE)
    if seeded:
        logging.getLogger(__name__).info("Сид компаний: %d записей", seeded)
    seeded = await seed_agents(session_factory, settings.AGENTS_SEED_FILE)
    if seeded:
        logging.getLogger(__name__).info("Сид агентов: %d записей", seeded)

    set_llm_client(create_llm_client(settings))

    bot = create_bot(
        settings.TELEGRAM_BOT_TOKEN,
        proxy=settings.TELEGRAM_PROXY,
        timeout=settings.TELEGRAM_TIMEOUT,
        retry_attempts=settings.TELEGRAM_RETRY_ATTEMPTS,
        retry_base_delay=settings.TELEGRAM_RETRY_BASE_DELAY,
    )
    dp = create_dispatcher()

    import uvicorn

    web_server = uvicorn.Server(
        uvicorn.Config(
            create_app(session_factory),
            host=settings.WEB_HOST,
            port=settings.WEB_PORT,
            log_level="warning",
        )
    )

    try:
        await asyncio.gather(
            gmail_loop(settings, bot),
            polling_with_restart(dp, bot),
            web_server.serve(),
            invoice_cleanup_loop(bot, settings),  # тикет 18
        )
    except GmailAuthError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    asyncio.run(main())

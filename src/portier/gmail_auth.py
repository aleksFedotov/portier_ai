"""Первичная OAuth-авторизация Gmail: python -m portier.gmail_auth

Открывает браузер со ссылкой авторизации Google, после подтверждения сохраняет
токен в GOOGLE_TOKEN_FILE (по умолчанию data/token.json) для переиспользования.
"""

import logging
import sys

from .config import get_settings
from .gmail_client import SCOPES, _save_credentials

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    from google_auth_oauthlib.flow import InstalledAppFlow

    settings = get_settings()
    flow = InstalledAppFlow.from_client_secrets_file(
        settings.GOOGLE_CREDENTIALS_FILE, SCOPES
    )
    print(
        "Сейчас откроется браузер со ссылкой авторизации Google.\n"
        "Войдите в почтовый аккаунт отеля и подтвердите доступ.\n"
        f"Токен будет сохранён в: {settings.GOOGLE_TOKEN_FILE}\n"
    )
    creds = flow.run_local_server(port=0)
    _save_credentials(creds, settings.GOOGLE_TOKEN_FILE)
    print(f"Готово: токен сохранён в {settings.GOOGLE_TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

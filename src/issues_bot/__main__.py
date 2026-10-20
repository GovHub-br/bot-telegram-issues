"""Ponto de entrada: `uv run issues-bot` ou `python -m issues_bot`."""

from __future__ import annotations

import logging
import os
import sys

from telegram import Update

from .app import build_application
from .config import ConfigError, Settings


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    # O httpx registra as URLs das requisições, e a URL da API do Telegram contém o token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logging.getLogger("issues_bot").error("%s", exc)
        sys.exit(1)

    app = build_application(settings)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

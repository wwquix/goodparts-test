"""Configuration for the production Ozon access layer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOTENV_PATH = PROJECT_ROOT / ".env"


class OzonConfigError(RuntimeError):
    """Raised when required Ozon configuration is missing."""


class TelegramConfigError(RuntimeError):
    """Raised when required Telegram configuration is invalid or missing."""


@dataclass(frozen=True, slots=True)
class OzonConfig:
    client_id: str
    api_key: str


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str
    chat_id: str
    low_stock_threshold: int


def load_ozon_config(
    dotenv_path: str | PathLike[str] = DEFAULT_DOTENV_PATH,
) -> OzonConfig:
    """Load and validate Ozon credentials without exposing their values."""
    load_dotenv(dotenv_path)

    client_id = os.getenv("OZON_CLIENT_ID", "").strip()
    api_key = os.getenv("OZON_API_KEY", "").strip()
    missing = [
        name
        for name, value in (
            ("OZON_CLIENT_ID", client_id),
            ("OZON_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise OzonConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )

    return OzonConfig(client_id=client_id, api_key=api_key)


def load_telegram_config(
    dotenv_path: str | PathLike[str] = DEFAULT_DOTENV_PATH,
) -> TelegramConfig:
    """Load Telegram settings without exposing credential values."""
    load_dotenv(dotenv_path)

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", bot_token),
            ("TELEGRAM_CHAT_ID", chat_id),
        )
        if not value
    ]
    if missing:
        raise TelegramConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )

    raw_threshold = os.getenv("LOW_STOCK_THRESHOLD", "5").strip()
    try:
        threshold = int(raw_threshold)
    except ValueError:
        threshold = None
    if threshold is None or threshold < 0:
        raise TelegramConfigError("LOW_STOCK_THRESHOLD must be an integer >= 0")

    return TelegramConfig(
        bot_token=bot_token,
        chat_id=chat_id,
        low_stock_threshold=threshold,
    )

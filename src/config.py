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


@dataclass(frozen=True, slots=True)
class OzonConfig:
    client_id: str
    api_key: str


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

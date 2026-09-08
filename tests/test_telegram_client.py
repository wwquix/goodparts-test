from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.config import (
    TelegramConfig,
    TelegramConfigError,
    load_telegram_config,
)
from src.telegram_client import (
    MAX_ATTEMPTS,
    TelegramClient,
    TelegramError,
    TelegramHTTPError,
    TelegramResponseError,
)


Handler = Callable[[httpx.Request], httpx.Response]
MISSING_DOTENV_PATH = Path(__file__).with_name(".missing-telegram-env")
FAKE_TOKEN = "123456:fake-token"
FAKE_CHAT_ID = "-100123"
FAKE_TEXT = "outbound message"


def make_client(
    handler: Handler,
    *,
    sleep: Callable[[float], None] = lambda _: None,
) -> TelegramClient:
    return TelegramClient(
        TelegramConfig(
            bot_token=FAKE_TOKEN,
            chat_id=FAKE_CHAT_ID,
            low_stock_threshold=5,
        ),
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )


def request_json(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def test_success_posts_plain_json_without_parse_mode() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    with make_client(handler) as client:
        payload = client.send_message("Привет")

    assert payload["ok"] is True
    assert len(requests) == 1
    assert requests[0].url.path == f"/bot{FAKE_TOKEN}/sendMessage"
    assert request_json(requests[0]) == {"chat_id": FAKE_CHAT_ID, "text": "Привет"}


def test_http_200_ok_false_is_a_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": 400, "description": "bad text"},
        )

    with make_client(handler) as client, pytest.raises(
        TelegramResponseError, match="ok=false"
    ):
        client.send_message("text")


def test_invalid_json_error_has_no_parse_context_or_echoed_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=f"not-json {FAKE_TOKEN} {FAKE_CHAT_ID} {FAKE_TEXT}",
        )

    with caplog.at_level(logging.INFO, logger="httpx"):
        with make_client(handler) as client, pytest.raises(
            TelegramResponseError
        ) as error:
            client.send_message(FAKE_TEXT)

    assert "endpoint=sendMessage" in str(error.value)
    assert FAKE_TOKEN not in str(error.value)
    assert FAKE_CHAT_ID not in str(error.value)
    assert FAKE_TEXT not in str(error.value)
    assert FAKE_TOKEN not in caplog.text
    assert FAKE_CHAT_ID not in caplog.text
    assert FAKE_TEXT not in caplog.text
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_dependency_info_logs_redact_token(caplog: pytest.LogCaptureFixture) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with caplog.at_level(logging.INFO, logger="httpx"):
        with make_client(handler) as client:
            client.send_message(FAKE_TEXT)

    assert "HTTP Request" in caplog.text
    assert FAKE_TOKEN not in caplog.text
    assert FAKE_CHAT_ID not in caplog.text
    assert FAKE_TEXT not in caplog.text


def test_http_error_is_metadata_only_and_redacts_echoed_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            401,
            text=json.dumps(
                {
                    "error_code": 401,
                    "description": (
                        f"{FAKE_TOKEN} {FAKE_CHAT_ID} {FAKE_TEXT}"
                    ),
                }
            ),
        )

    with caplog.at_level(logging.INFO, logger="httpx"):
        with make_client(handler) as client, pytest.raises(
            TelegramHTTPError
        ) as error:
            client.send_message(FAKE_TEXT)

    assert attempts == 1
    assert "status=401" in str(error.value)
    assert "error_code=401" in str(error.value)
    assert FAKE_TOKEN not in str(error.value)
    assert FAKE_CHAT_ID not in str(error.value)
    assert FAKE_TEXT not in str(error.value)
    assert FAKE_TOKEN not in caplog.text
    assert FAKE_CHAT_ID not in caplog.text
    assert FAKE_TEXT not in caplog.text


def test_ok_false_description_is_metadata_only_and_redacts_echoed_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error_code": 400,
                "description": f"{FAKE_TOKEN} {FAKE_CHAT_ID} {FAKE_TEXT}",
            },
        )

    with caplog.at_level(logging.INFO, logger="httpx"):
        with make_client(handler) as client, pytest.raises(
            TelegramResponseError
        ) as error:
            client.send_message(FAKE_TEXT)

    assert "endpoint=sendMessage" in str(error.value)
    assert "error_code=400" in str(error.value)
    assert FAKE_TOKEN not in str(error.value)
    assert FAKE_CHAT_ID not in str(error.value)
    assert FAKE_TEXT not in str(error.value)
    assert FAKE_TOKEN not in caplog.text
    assert FAKE_CHAT_ID not in caplog.text
    assert FAKE_TEXT not in caplog.text


def test_http_500_retries_then_succeeds() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(200, json={"ok": True})

    with make_client(handler, sleep=delays.append) as client:
        client.send_message("text")

    assert attempts == 2
    assert delays == [1.0]


def test_http_429_uses_numeric_retry_after() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "parameters": {"retry_after": 2},
                },
            )
        return httpx.Response(200, json={"ok": True})

    with make_client(handler, sleep=delays.append) as client:
        client.send_message("text")

    assert attempts == 2
    assert delays == [2.0]


def test_http_429_huge_retry_after_is_capped() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "parameters": {"retry_after": 10**400},
                },
            )
        return httpx.Response(200, json={"ok": True})

    with make_client(handler, sleep=delays.append) as client:
        client.send_message("text")

    assert attempts == 2
    assert delays == [60.0]


def test_network_failure_retries_without_token_in_error_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError(
            f"cannot reach {request.url} {FAKE_CHAT_ID} {FAKE_TEXT}"
        )

    with make_client(handler) as client, pytest.raises(TelegramError) as error:
        client.send_message("text")

    assert attempts == MAX_ATTEMPTS
    assert FAKE_TOKEN not in str(error.value)
    assert FAKE_CHAT_ID not in str(error.value)
    assert FAKE_TEXT not in str(error.value)
    assert FAKE_TOKEN not in caplog.text
    assert FAKE_CHAT_ID not in caplog.text
    assert FAKE_TEXT not in caplog.text
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    ("token", "chat_id", "threshold", "message"),
    [
        (None, "chat", "5", "TELEGRAM_BOT_TOKEN"),
        ("secret-token", None, "5", "TELEGRAM_CHAT_ID"),
        ("secret-token", "chat", "not-an-int", "LOW_STOCK_THRESHOLD"),
        ("secret-token", "chat", "-1", "LOW_STOCK_THRESHOLD"),
    ],
)
def test_telegram_config_validation_never_exposes_token(
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    chat_id: str | None,
    threshold: str,
    message: str,
) -> None:
    values = {
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_CHAT_ID": chat_id,
        "LOW_STOCK_THRESHOLD": threshold,
    }
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    with pytest.raises(TelegramConfigError) as error:
        load_telegram_config(MISSING_DOTENV_PATH)

    assert message in str(error.value)
    assert "secret-token" not in str(error.value)


def test_telegram_config_trims_values_and_defaults_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  token-value  ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "  chat-value  ")
    monkeypatch.delenv("LOW_STOCK_THRESHOLD", raising=False)

    config = load_telegram_config(MISSING_DOTENV_PATH)

    assert config == TelegramConfig(
        bot_token="token-value",
        chat_id="chat-value",
        low_stock_threshold=5,
    )

"""Small synchronous Telegram Bot API client used by Task 2."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from typing import Any, TypeAlias

import httpx

from .config import TelegramConfig


TELEGRAM_API_BASE_URL = "https://api.telegram.org"
SEND_MESSAGE_ENDPOINT = "/sendMessage"
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER_SECONDS = 60.0

JsonObject: TypeAlias = dict[str, Any]
Sleep: TypeAlias = Callable[[float], None]
_INVALID_JSON = object()

logger = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    """Base error for Telegram delivery failures."""


class TelegramHTTPError(TelegramError):
    """Raised when Telegram returns an HTTP status that cannot be accepted."""

    def __init__(self, status_code: int, error_code: int | None = None) -> None:
        self.status_code = status_code
        self.error_code = error_code
        message = (
            "Telegram request failed: endpoint=sendMessage, "
            f"status={status_code}"
        )
        if error_code is not None:
            message += f", error_code={error_code}"
        super().__init__(message)


class TelegramResponseError(TelegramError):
    """Raised when Telegram returns invalid or unsuccessful JSON."""


class _DependencyLogRedactor(logging.Filter):
    """Redact request data from dependency records while one request runs."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        redacted = rendered
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


class TelegramClient:
    """Synchronous, bounded-retry client for Telegram ``sendMessage``."""

    def __init__(
        self,
        config: TelegramConfig,
        *,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=f"{TELEGRAM_API_BASE_URL}/bot{config.bot_token}",
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _dependency_logger_names() -> set[str]:
        names = {
            "httpx",
            "httpcore",
            "httpcore.connection",
            "httpcore.http11",
            "httpcore.http2",
            "httpcore.proxy",
            "httpcore.socks",
        }
        for name, value in logging.Logger.manager.loggerDict.items():
            if not isinstance(value, logging.Logger):
                continue
            if name.startswith("httpx.") or name.startswith("httpcore."):
                names.add(name)
        return names

    def _install_dependency_log_redaction(
        self,
        text: str,
    ) -> list[tuple[logging.Logger, _DependencyLogRedactor]]:
        secrets = tuple(
            dict.fromkeys(
                value
                for value in (self._config.bot_token, self._config.chat_id, text)
                if value
            )
        )
        redactor = _DependencyLogRedactor(secrets)
        installed: list[tuple[logging.Logger, _DependencyLogRedactor]] = []
        for name in self._dependency_logger_names():
            dependency_logger = logging.getLogger(name)
            dependency_logger.addFilter(redactor)
            installed.append((dependency_logger, redactor))
        return installed

    @staticmethod
    def _remove_dependency_log_redaction(
        installed: list[tuple[logging.Logger, _DependencyLogRedactor]],
    ) -> None:
        for dependency_logger, redactor in installed:
            dependency_logger.removeFilter(redactor)

    @staticmethod
    def _error_code(payload: object) -> int | None:
        if not isinstance(payload, dict):
            return None
        value = payload.get("error_code")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    @classmethod
    def _response_error_code(cls, response: httpx.Response) -> int | None:
        try:
            return cls._error_code(response.json())
        except (ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _parse_response_json(response: httpx.Response) -> object:
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            return _INVALID_JSON

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        return BACKOFF_SECONDS[attempt - 1]

    @classmethod
    def _retry_after_seconds(cls, response: httpx.Response) -> float | None:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            return None
        retry_after = parameters.get("retry_after")
        if isinstance(retry_after, bool):
            return None
        if isinstance(retry_after, int):
            if retry_after < 0:
                return None
            return float(min(retry_after, int(MAX_RETRY_AFTER_SECONDS)))
        if not isinstance(retry_after, float):
            return None
        if not math.isfinite(retry_after) or retry_after < 0:
            return None
        return min(retry_after, MAX_RETRY_AFTER_SECONDS)

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code <= 599

    def _request(self, body: JsonObject) -> JsonObject:
        final_network_error: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            request_error_type: str | None = None
            try:
                response = self._client.post(SEND_MESSAGE_ENDPOINT, json=body)
            except httpx.RequestError as exc:
                request_error_type = type(exc).__name__

            if request_error_type is not None:
                if attempt == MAX_ATTEMPTS:
                    final_network_error = request_error_type
                    break
                delay = self._backoff_seconds(attempt)
                logger.warning(
                    "Telegram sendMessage retry: attempt=%d error=%s delay=%.2fs",
                    attempt,
                    request_error_type,
                    delay,
                )
                self._sleep(delay)
                continue

            if self._is_retryable_status(response.status_code) and (
                attempt < MAX_ATTEMPTS
            ):
                delay = self._retry_after_seconds(response)
                if delay is None:
                    delay = self._backoff_seconds(attempt)
                logger.warning(
                    "Telegram sendMessage retry: attempt=%d status=%d delay=%.2fs",
                    attempt,
                    response.status_code,
                    delay,
                )
                self._sleep(delay)
                continue

            if not 200 <= response.status_code < 300:
                raise TelegramHTTPError(
                    response.status_code,
                    self._response_error_code(response),
                )

            payload = self._parse_response_json(response)
            if payload is _INVALID_JSON:
                raise TelegramResponseError(
                    "Telegram response is not valid JSON: endpoint=sendMessage"
                )
            if not isinstance(payload, dict):
                raise TelegramResponseError(
                    "Telegram response top level must be an object: "
                    "endpoint=sendMessage"
                )
            if payload.get("ok") is not True:
                error_code = self._error_code(payload)
                message = "Telegram API returned ok=false: endpoint=sendMessage"
                if error_code is not None:
                    message += f", error_code={error_code}"
                raise TelegramResponseError(message)
            return payload

        if final_network_error is not None:
            raise TelegramError(
                "Telegram request failed after "
                f"{MAX_ATTEMPTS} attempts: endpoint=sendMessage, "
                f"error={final_network_error}"
            )
        raise AssertionError("Retry loop exited unexpectedly")

    def send_message(self, text: str) -> JsonObject:
        """Send one plain-text message and return Telegram's validated JSON."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        installed = self._install_dependency_log_redaction(text)
        try:
            return self._request(
                {"chat_id": self._config.chat_id, "text": text}
            )
        finally:
            self._remove_dependency_log_redaction(installed)


__all__ = [
    "BACKOFF_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_RETRY_AFTER_SECONDS",
    "REQUEST_TIMEOUT_SECONDS",
    "SEND_MESSAGE_ENDPOINT",
    "TelegramClient",
    "TelegramError",
    "TelegramHTTPError",
    "TelegramResponseError",
]

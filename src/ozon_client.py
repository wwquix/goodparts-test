"""Small synchronous access layer for raw Ozon Seller API data."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeAlias

import httpx

from .config import OzonConfig


BASE_URL = "https://api-seller.ozon.ru"
REQUEST_TIMEOUT_SECONDS = 20.0

# Configured operational page and batch limits; they are not claims that every
# Ozon API method is permanently capped at 1000.
PRODUCT_PAGE_LIMIT = 1000
PRICE_PAGE_LIMIT = 1000
STOCK_PAGE_LIMIT = 1000
INFO_BATCH_SIZE = 1000

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
JITTER_MAX_SECONDS = 0.25
MAX_RETRY_AFTER_SECONDS = 60.0
SAFE_RESPONSE_EXCERPT_LENGTH = 500

JsonObject: TypeAlias = dict[str, Any]
Sleep: TypeAlias = Callable[[float], None]

logger = logging.getLogger(__name__)
_INVALID_JSON = object()


class OzonError(RuntimeError):
    """Base error for Ozon access failures."""


class OzonHTTPError(OzonError):
    """Raised for an HTTP response that cannot be retried successfully."""

    def __init__(self, endpoint: str, status_code: int, excerpt: str) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        super().__init__(
            f"Ozon request failed: endpoint={endpoint}, status={status_code}, "
            f"response={excerpt}"
        )


class OzonResponseError(OzonError):
    """Raised when Ozon returns JSON that violates the confirmed schema."""


class OzonClient:
    """Synchronous client that returns strict, unnormalized Ozon dictionaries."""

    def __init__(
        self,
        config: OzonConfig,
        *,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Client-Id": config.client_id,
                "Api-Key": config.api_key,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def __enter__(self) -> OzonClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _redact(self, text: str) -> str:
        redacted = text
        for secret in (self._config.client_id, self._config.api_key):
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted

    def _response_excerpt(self, response: httpx.Response) -> str:
        return self._redact(response.text)[:SAFE_RESPONSE_EXCERPT_LENGTH]

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            return None
        if seconds < 0 or not seconds < float("inf"):
            return None
        return min(seconds, MAX_RETRY_AFTER_SECONDS)

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        return BACKOFF_SECONDS[attempt - 1] + random.uniform(
            0.0, JITTER_MAX_SECONDS
        )

    def _request(self, endpoint: str, json_body: JsonObject) -> JsonObject:
        final_network_error: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.post(endpoint, json=json_body)
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                if attempt == MAX_ATTEMPTS:
                    final_network_error = type(exc).__name__
                    break
                delay = self._backoff_seconds(attempt)
                logger.warning(
                    "Ozon request retry: endpoint=%s attempt=%d error=%s delay=%.2fs",
                    endpoint,
                    attempt,
                    type(exc).__name__,
                    delay,
                )
                self._sleep(delay)
                continue

            is_transient_status = response.status_code == 429 or (
                500 <= response.status_code <= 599
            )
            if is_transient_status and attempt < MAX_ATTEMPTS:
                delay = self._retry_after_seconds(response)
                if delay is None:
                    delay = self._backoff_seconds(attempt)
                logger.warning(
                    "Ozon request retry: endpoint=%s attempt=%d status=%d delay=%.2fs",
                    endpoint,
                    attempt,
                    response.status_code,
                    delay,
                )
                self._sleep(delay)
                continue

            if response.is_error:
                raise OzonHTTPError(
                    endpoint,
                    response.status_code,
                    self._response_excerpt(response),
                )

            try:
                payload = response.json()
            except ValueError:
                payload = _INVALID_JSON
            if payload is _INVALID_JSON:
                raise OzonResponseError(
                    f"Unexpected response from {endpoint}: body is not valid JSON"
                )
            if not isinstance(payload, dict):
                raise OzonResponseError(
                    f"Unexpected response from {endpoint}: top level must be an object"
                )
            return payload

        if final_network_error is not None:
            raise OzonError(
                f"Ozon request failed after {MAX_ATTEMPTS} attempts: "
                f"endpoint={endpoint}, error={final_network_error}"
            )
        raise AssertionError("Retry loop exited unexpectedly")

    @staticmethod
    def _require_object(payload: JsonObject, key: str, endpoint: str) -> JsonObject:
        value = payload.get(key)
        if not isinstance(value, dict):
            raise OzonResponseError(
                f"Unexpected response from {endpoint}: $.{key} must be an object"
            )
        return value

    @staticmethod
    def _require_items(
        payload: JsonObject,
        key: str,
        endpoint: str,
        path: str,
    ) -> list[JsonObject]:
        value = payload.get(key)
        if not isinstance(value, list):
            raise OzonResponseError(
                f"Unexpected response from {endpoint}: {path} must be an array"
            )
        if any(not isinstance(item, dict) for item in value):
            raise OzonResponseError(
                f"Unexpected response from {endpoint}: every item in {path} "
                "must be an object"
            )
        return value

    @staticmethod
    def _require_token(
        payload: JsonObject,
        key: str,
        endpoint: str,
        path: str,
    ) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise OzonResponseError(
                f"Unexpected response from {endpoint}: {path} must be a string"
            )
        return value

    @staticmethod
    def _guard_continuation_progress(
        token: str,
        seen_tokens: set[str],
        endpoint: str,
    ) -> None:
        # Tokens are opaque: a live diagnostic returned one after a short page, so
        # only empty terminates; a seen non-empty token is a cycle/broken progress.
        if token in seen_tokens:
            raise OzonResponseError(
                f"Pagination repeated a continuation token for {endpoint}: "
                "the same non-empty continuation token was returned again"
            )
        seen_tokens.add(token)

    def list_all_products(self) -> list[JsonObject]:
        endpoint = "/v3/product/list"
        collected: list[JsonObject] = []
        last_id = ""
        seen_tokens: set[str] = set()
        page_number = 1

        while True:
            body: JsonObject = {
                "filter": {"visibility": "ALL"},
                "limit": PRODUCT_PAGE_LIMIT,
            }
            if last_id:
                body["last_id"] = last_id

            payload = self._request(endpoint, body)
            result = self._require_object(payload, "result", endpoint)
            items = self._require_items(
                result, "items", endpoint, "$.result.items"
            )
            next_last_id = self._require_token(
                result, "last_id", endpoint, "$.result.last_id"
            )
            collected.extend(items)
            logger.info(
                "Ozon product page: page=%d items=%d collected=%d",
                page_number,
                len(items),
                len(collected),
            )

            if not next_last_id:
                break
            self._guard_continuation_progress(next_last_id, seen_tokens, endpoint)
            last_id = next_last_id
            page_number += 1

        return collected

    def get_products_info(
        self, offer_ids: Sequence[str]
    ) -> list[JsonObject]:
        endpoint = "/v3/product/info/list"
        identifiers = list(offer_ids)
        if not identifiers:
            return []
        if any(
            not isinstance(offer_id, str) or not offer_id.strip()
            for offer_id in identifiers
        ):
            raise ValueError("offer_ids must contain only non-empty strings")

        collected: list[JsonObject] = []
        for start in range(0, len(identifiers), INFO_BATCH_SIZE):
            batch = identifiers[start : start + INFO_BATCH_SIZE]
            payload = self._request(endpoint, {"offer_id": batch})
            items = self._require_items(payload, "items", endpoint, "$.items")
            collected.extend(items)
            logger.info(
                "Ozon product-info batch: batch=%d size=%d items=%d collected=%d",
                start // INFO_BATCH_SIZE + 1,
                len(batch),
                len(items),
                len(collected),
            )
        return collected

    def _list_cursor_items(
        self,
        endpoint: str,
        limit: int,
        dataset_name: str,
    ) -> list[JsonObject]:
        collected: list[JsonObject] = []
        cursor = ""
        seen_tokens: set[str] = set()
        page_number = 1

        while True:
            payload = self._request(
                endpoint,
                {
                    "cursor": cursor,
                    "filter": {"visibility": "ALL"},
                    "limit": limit,
                },
            )
            items = self._require_items(payload, "items", endpoint, "$.items")
            next_cursor = self._require_token(
                payload, "cursor", endpoint, "$.cursor"
            )
            collected.extend(items)
            logger.info(
                "Ozon %s page: page=%d items=%d collected=%d",
                dataset_name,
                page_number,
                len(items),
                len(collected),
            )

            if not next_cursor:
                break
            self._guard_continuation_progress(next_cursor, seen_tokens, endpoint)
            cursor = next_cursor
            page_number += 1

        return collected

    def list_all_prices(self) -> list[JsonObject]:
        return self._list_cursor_items(
            "/v5/product/info/prices", PRICE_PAGE_LIMIT, "price"
        )

    def list_all_stocks(self) -> list[JsonObject]:
        return self._list_cursor_items(
            "/v4/product/info/stocks", STOCK_PAGE_LIMIT, "stock"
        )

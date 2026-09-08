from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.config import OzonConfig, OzonConfigError, load_ozon_config
from src.ozon_client import (
    INFO_BATCH_SIZE,
    MAX_ATTEMPTS,
    OzonClient,
    OzonHTTPError,
    OzonResponseError,
    PRODUCT_PAGE_LIMIT,
)


Handler = Callable[[httpx.Request], httpx.Response]
MISSING_DOTENV_PATH = Path(__file__).with_name(".missing-env-for-test")


def make_client(
    handler: Handler,
    *,
    sleep: Callable[[float], None] = lambda _: None,
) -> OzonClient:
    return OzonClient(
        OzonConfig(client_id="client-value", api_key="api-secret-value"),
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )


def request_json(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def test_load_config_trims_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OZON_CLIENT_ID", "  client-id  ")
    monkeypatch.setenv("OZON_API_KEY", "  api-key  ")

    config = load_ozon_config(MISSING_DOTENV_PATH)

    assert config == OzonConfig(client_id="client-id", api_key="api-key")


@pytest.mark.parametrize(
    ("client_id", "api_key", "missing_names"),
    [
        (None, "secret", "OZON_CLIENT_ID"),
        ("client", None, "OZON_API_KEY"),
        (None, None, "OZON_CLIENT_ID, OZON_API_KEY"),
    ],
)
def test_load_config_reports_missing_names_without_values(
    monkeypatch: pytest.MonkeyPatch,
    client_id: str | None,
    api_key: str | None,
    missing_names: str,
) -> None:
    for name, value in (("OZON_CLIENT_ID", client_id), ("OZON_API_KEY", api_key)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    with pytest.raises(OzonConfigError) as error:
        load_ozon_config(MISSING_DOTENV_PATH)

    assert missing_names in str(error.value)
    assert "secret" not in str(error.value)


def test_product_pagination_uses_nonempty_last_id_even_for_short_page() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request_json(request))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "result": {
                        "items": [
                            {"product_id": 1, "offer_id": "A", "archived": False}
                        ],
                        "total": 1,
                        "last_id": "TOKEN",
                    }
                },
            )
        return httpx.Response(
            200,
            json={"result": {"items": [], "total": 1, "last_id": ""}},
        )

    with make_client(handler) as client:
        products = client.list_all_products()

    assert products == [{"product_id": 1, "offer_id": "A", "archived": False}]
    assert len(requests) == 2
    assert requests[0] == {
        "filter": {"visibility": "ALL"},
        "limit": PRODUCT_PAGE_LIMIT,
    }
    assert requests[1] == {
        "filter": {"visibility": "ALL"},
        "last_id": "TOKEN",
        "limit": PRODUCT_PAGE_LIMIT,
    }


def test_product_info_batches_1001_offer_ids() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request_json(request))
        return httpx.Response(200, json={"items": []})

    offer_ids = [f"offer-{index}" for index in range(INFO_BATCH_SIZE + 1)]
    with make_client(handler) as client:
        assert client.get_products_info(offer_ids) == []

    assert len(requests) == 2
    assert requests[0] == {"offer_id": offer_ids[:INFO_BATCH_SIZE]}
    assert requests[1] == {"offer_id": offer_ids[INFO_BATCH_SIZE:]}


def test_empty_product_info_input_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request: {request.url}")

    with make_client(handler) as client:
        assert client.get_products_info([]) == []


@pytest.mark.parametrize(
    ("method_name", "endpoint", "first_item"),
    [
        (
            "list_all_prices",
            "/v5/product/info/prices",
            {"product_id": 1, "offer_id": "A", "price": {"price": 100}},
        ),
        (
            "list_all_stocks",
            "/v4/product/info/stocks",
            {"product_id": 1, "offer_id": "A", "stocks": []},
        ),
    ],
)
def test_cursor_pagination_uses_exact_token(
    method_name: str,
    endpoint: str,
    first_item: dict[str, Any],
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request_json(request)))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"items": [first_item], "cursor": "NEXT", "total": 1},
            )
        return httpx.Response(200, json={"items": [], "cursor": "", "total": 1})

    with make_client(handler) as client:
        items = getattr(client, method_name)()

    assert items == [first_item]
    assert [path for path, _ in requests] == [endpoint, endpoint]
    assert requests[0][1]["cursor"] == ""
    assert requests[1][1]["cursor"] == "NEXT"


def test_retries_500_then_succeeds() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(200, json={"items": [], "cursor": ""})

    with make_client(handler, sleep=delays.append) as client:
        assert client.list_all_prices() == []

    assert attempts == 2
    assert len(delays) == 1


def test_retries_429_and_honors_numeric_retry_after() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"items": [], "cursor": ""})

    with make_client(handler, sleep=delays.append) as client:
        assert client.list_all_prices() == []

    assert attempts == 2
    assert delays == [0.0]


def test_401_is_not_retried_and_secrets_are_redacted() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            401,
            text="client-value api-secret-value unauthorized",
        )

    with make_client(handler) as client, pytest.raises(OzonHTTPError) as error:
        client.list_all_prices()

    assert attempts == 1
    assert "client-value" not in str(error.value)
    assert "api-secret-value" not in str(error.value)
    assert str(error.value).count("[REDACTED]") == 2


def test_exhausted_500_retries_are_bounded() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, text="still temporary")

    with make_client(handler) as client, pytest.raises(OzonHTTPError):
        client.list_all_prices()

    assert attempts == MAX_ATTEMPTS


def test_product_list_schema_error_is_explicit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with make_client(handler) as client, pytest.raises(
        OzonResponseError, match=r"\$\.result must be an object"
    ):
        client.list_all_products()


def test_price_cursor_schema_error_is_explicit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    with make_client(handler) as client, pytest.raises(
        OzonResponseError, match=r"\$\.cursor must be a string"
    ):
        client.list_all_prices()


@pytest.mark.parametrize(
    ("method_name", "first_payload", "second_payload", "continuation_key"),
    [
        (
            "list_all_products",
            {
                "result": {
                    "items": [
                        {"product_id": 1, "offer_id": "A", "archived": False}
                    ],
                    "last_id": "SAME",
                }
            },
            {
                "result": {
                    "items": [
                        {"product_id": 2, "offer_id": "B", "archived": False}
                    ],
                    "last_id": "SAME",
                }
            },
            "last_id",
        ),
        (
            "list_all_stocks",
            {
                "items": [
                    {"product_id": 1, "offer_id": "A", "stocks": []}
                ],
                "cursor": "SAME",
            },
            {
                "items": [
                    {"product_id": 2, "offer_id": "B", "stocks": []}
                ],
                "cursor": "SAME",
            },
            "cursor",
        ),
    ],
)
def test_repeated_continuation_token_with_items_fails(
    method_name: str,
    first_payload: dict[str, Any],
    second_payload: dict[str, Any],
    continuation_key: str,
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request_json(request))
        payload = first_payload if len(requests) == 1 else second_payload
        return httpx.Response(200, json=payload)

    with make_client(handler) as client, pytest.raises(
        OzonResponseError, match="same non-empty continuation token"
    ):
        getattr(client, method_name)()

    assert len(requests) == 2
    assert requests[1][continuation_key] == "SAME"

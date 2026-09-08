from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import httpx
import pytest


Handler = Callable[[httpx.Request], httpx.Response]
FAKE_CLIENT_ID = "OZON_TEST_CLIENT_SECRET"
FAKE_API_KEY = "OZON_TEST_SUPER_SECRET"
FAKE_SECRETS = (FAKE_CLIENT_ID, FAKE_API_KEY)


def _load_diagnostic_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "diagnose_ozon.py"
    spec = importlib.util.spec_from_file_location("diagnose_ozon_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client(handler: Handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(handler),
    )


def _assert_safe_exception(
    error: BaseException,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for secret in FAKE_SECRETS:
        assert secret not in str(error)
        assert secret not in repr(error)
        assert secret not in caplog.text
    assert error.__cause__ is None
    assert error.__context__ is None


def test_network_error_redacts_fake_credentials_without_exception_chain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostic = _load_diagnostic_module()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"network detail {FAKE_CLIENT_ID} {FAKE_API_KEY}",
            request=request,
        )

    with caplog.at_level(logging.INFO, logger="httpx"):
        with _client(handler) as client, pytest.raises(diagnostic.DiagnosticError) as raised:
            diagnostic.post_json(client, "/v3/product/list", {}, FAKE_SECRETS)

    _assert_safe_exception(raised.value, caplog)


def test_invalid_json_redacts_fake_body_without_exception_chain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostic = _load_diagnostic_module()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"not-json {FAKE_CLIENT_ID} {FAKE_API_KEY}")

    with caplog.at_level(logging.INFO, logger="httpx"):
        with _client(handler) as client, pytest.raises(diagnostic.DiagnosticError) as raised:
            diagnostic.post_json(client, "/v3/product/list", {}, FAKE_SECRETS)

    _assert_safe_exception(raised.value, caplog)


def test_http_error_uses_bounded_redacted_body_excerpt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostic = _load_diagnostic_module()
    body = (
        "x" * (diagnostic.SAFE_RESPONSE_EXCERPT_LENGTH - 3)
        + FAKE_API_KEY
        + "y" * 1_000_000
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=body)

    with caplog.at_level(logging.INFO, logger="httpx"):
        with _client(handler) as client, pytest.raises(diagnostic.DiagnosticError) as raised:
            diagnostic.post_json(client, "/v3/product/list", {}, FAKE_SECRETS)

    error_text = str(raised.value)
    excerpt = error_text.split("Response body:\n", maxsplit=1)[1]
    assert len(excerpt) <= diagnostic.SAFE_RESPONSE_EXCERPT_LENGTH
    assert FAKE_API_KEY not in error_text
    assert FAKE_API_KEY[:3] not in error_text
    assert FAKE_API_KEY[3:] not in error_text
    _assert_safe_exception(raised.value, caplog)

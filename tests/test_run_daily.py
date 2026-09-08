from __future__ import annotations

import csv
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.config import OzonConfig, TelegramConfig
from src.run_daily import DailyRunError, DailyRunResult, main, run_daily
from src.task1_export import ExportResult, export_ozon_products
from src.task2_summary import SummaryResult, send_summary


FIXED_NOW = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def temp_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
        yield Path(directory)


class Resource(AbstractContextManager[Any]):
    def __init__(self, value: Any, events: list[str], closed_event: str) -> None:
        self.value = value
        self.events = events
        self.closed_event = closed_event

    def __enter__(self) -> Any:
        return self.value

    def __exit__(self, *exc_info: object) -> None:
        self.events.append(self.closed_event)


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, text: str) -> None:
        self.messages.append(text)


class FakeOzonSource:
    """Ozon-shaped raw data used by the integration-style daily-pipeline test."""

    def __init__(self, product_count: int = 1) -> None:
        self.products = [
            {
                "product_id": product_id,
                "offer_id": f"SKU-{product_id:03d}",
                "archived": False,
            }
            for product_id in range(1, product_count + 1)
        ]
        self.info = [
            {
                "id": product_id,
                "offer_id": f"SKU-{product_id:03d}",
                "name": f"Товар {product_id} " + "я" * 100,
                "is_archived": False,
            }
            for product_id in range(1, product_count + 1)
        ]
        self.prices = [
            {
                "product_id": product_id,
                "offer_id": f"SKU-{product_id:03d}",
                "price": {"currency_code": "RUB", "price": 1000},
            }
            for product_id in range(1, product_count + 1)
        ]
        self.stocks = [
            {
                "product_id": product_id,
                "offer_id": f"SKU-{product_id:03d}",
                "stocks": [{"present": 2, "reserved": 0}],
            }
            for product_id in range(1, product_count + 1)
        ]

    def list_all_products(self) -> list[dict[str, Any]]:
        return self.products

    def get_products_info(self, offer_ids: list[str]) -> list[dict[str, Any]]:
        assert offer_ids == [item["offer_id"] for item in self.products]
        return self.info

    def list_all_prices(self) -> list[dict[str, Any]]:
        return self.prices

    def list_all_stocks(self) -> list[dict[str, Any]]:
        return self.stocks


def _export_result(path: Path, *, products: int = 1) -> ExportResult:
    return ExportResult(
        path=path,
        products=products,
        archived_excluded=0,
        info_archived_excluded=0,
        missing_info=0,
        missing_prices=0,
        missing_stocks=0,
    )


def _ozon_config() -> OzonConfig:
    return OzonConfig(client_id="test-client", api_key="test-key")


def _telegram_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="test-token",
        chat_id="test-chat",
        low_stock_threshold=5,
    )


def test_happy_path_hands_the_exact_export_path_to_task_2(
    temp_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    export_result = _export_result(temp_dir / "today.csv", products=3)
    source = object()
    telegram = FakeTelegram()
    handed_off: list[Path] = []

    def export(client: object) -> ExportResult:
        assert client is source
        events.append("export")
        return export_result

    def send(path: Path, client: FakeTelegram, threshold: int) -> SummaryResult:
        assert client is telegram
        assert threshold == 5
        handed_off.append(path)
        events.append("send")
        return SummaryResult(products=3, low_stock=2, messages=1)

    result = run_daily(
        load_ozon=lambda: (events.append("load_ozon"), _ozon_config())[1],
        ozon_client_factory=lambda config: (
            events.append("make_ozon"), Resource(source, events, "close_ozon")
        )[1],
        exporter=export,
        load_telegram=lambda: (events.append("load_telegram"), _telegram_config())[1],
        telegram_client_factory=lambda config: (
            events.append("make_telegram"), Resource(telegram, events, "close_telegram")
        )[1],
        summary_sender=send,
    )

    assert result.export is export_result
    assert handed_off == [export_result.path]
    assert handed_off[0] is export_result.path
    assert events == [
        "load_ozon",
        "make_ozon",
        "export",
        "close_ozon",
        "load_telegram",
        "make_telegram",
        "send",
        "close_telegram",
    ]
    assert main(lambda: result) == 0
    captured = capsys.readouterr()
    assert "Ozon export completed" in captured.out
    assert f"CSV: {export_result.path}" in captured.out
    assert "Products: 3" in captured.out
    assert "Telegram summary sent" in captured.out
    assert "Low stock: 2" in captured.out
    assert "Messages: 1" in captured.out


def test_task_1_failure_never_touches_telegram_and_cli_is_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def invoke() -> DailyRunResult:
        return run_daily(
            load_ozon=lambda: (_ for _ in ()).throw(RuntimeError("secret URL")),
            ozon_client_factory=lambda config: (_ for _ in ()).throw(AssertionError()),
            exporter=lambda client: (_ for _ in ()).throw(AssertionError()),
            load_telegram=lambda: calls.append("load_telegram"),
            telegram_client_factory=lambda config: calls.append("make_telegram"),
            summary_sender=lambda path, client, threshold: calls.append("send"),
        )

    with pytest.raises(DailyRunError, match="Ozon export failed") as raised:
        invoke()
    assert raised.value.detail == "unexpected error; details omitted for credential safety"
    assert calls == []

    assert main(invoke) == 1
    captured = capsys.readouterr()
    assert "Ozon export failed" in captured.err
    assert "secret URL" not in captured.err


def test_task_2_failure_keeps_successful_csv_and_cli_is_nonzero(
    temp_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    csv_path = temp_dir / "export.csv"
    csv_path.write_text("completed export", encoding="utf-8")
    export_result = _export_result(csv_path)
    telegram_calls: list[Path] = []

    def invoke() -> DailyRunResult:
        return run_daily(
            load_ozon=_ozon_config,
            ozon_client_factory=lambda config: Resource(object(), [], "unused"),
            exporter=lambda client: export_result,
            load_telegram=_telegram_config,
            telegram_client_factory=lambda config: Resource(FakeTelegram(), [], "unused"),
            summary_sender=lambda path, client, threshold: (
                telegram_calls.append(path),
                (_ for _ in ()).throw(RuntimeError("tokenized URL")),
            )[1],
        )

    with pytest.raises(DailyRunError, match="Telegram summary failed") as raised:
        invoke()
    assert raised.value.export_result is export_result
    assert telegram_calls == [csv_path]
    assert csv_path.read_text(encoding="utf-8") == "completed export"

    assert main(invoke) == 1
    captured = capsys.readouterr()
    assert "Telegram summary failed" in captured.err
    assert f"CSV remains: {csv_path}" in captured.err
    assert "tokenized URL" not in captured.err


def test_old_csv_files_are_never_selected_instead_of_current_export(
    temp_dir: Path,
) -> None:
    old_csv = temp_dir / "ozon_products_yesterday.csv"
    current_csv = temp_dir / "ozon_products_today.csv"
    old_csv.write_text("old", encoding="utf-8")
    current_csv.write_text("current", encoding="utf-8")
    export_result = _export_result(current_csv)
    received: list[Path] = []

    result = run_daily(
        load_ozon=_ozon_config,
        ozon_client_factory=lambda config: Resource(object(), [], "unused"),
        exporter=lambda client: export_result,
        load_telegram=_telegram_config,
        telegram_client_factory=lambda config: Resource(FakeTelegram(), [], "unused"),
        summary_sender=lambda path, client, threshold: (
            received.append(path),
            SummaryResult(products=1, low_stock=0, messages=1),
        )[1],
    )

    assert old_csv.exists()
    assert result.export.path is current_csv
    assert received == [current_csv]
    assert received[0] is current_csv


def test_real_task_1_and_task_2_run_together_with_fake_boundaries(
    temp_dir: Path,
) -> None:
    source = FakeOzonSource(product_count=60)
    telegram = FakeTelegram()
    sleeps: list[float] = []

    result = run_daily(
        load_ozon=_ozon_config,
        ozon_client_factory=lambda config: Resource(source, [], "unused"),
        exporter=lambda client: export_ozon_products(
            client,
            output_dir=temp_dir / "output",
            now=lambda: FIXED_NOW,
        ),
        load_telegram=_telegram_config,
        telegram_client_factory=lambda config: Resource(telegram, [], "unused"),
        summary_sender=lambda path, client, threshold: send_summary(
            path,
            client,
            threshold,
            sleep=sleeps.append,
        ),
    )

    assert result.export.path.exists()
    assert result.export.path.read_bytes().startswith(b"\xef\xbb\xbf")
    with result.export.path.open(encoding="utf-8-sig", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    assert len(rows) == 60
    assert result.summary.products == 60
    assert result.summary.low_stock == 60
    assert result.summary.messages == len(telegram.messages)
    assert len(telegram.messages) > 1
    assert len(sleeps) == len(telegram.messages) - 1
    assert sum("Артикул: SKU-001" in message for message in telegram.messages) == 1
    assert sum("Артикул: SKU-060" in message for message in telegram.messages) == 1

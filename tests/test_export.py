from __future__ import annotations

import csv
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.task1_export import (
    CSV_COLUMNS,
    Task1ConsistencyError,
    Task1SchemaError,
    export_ozon_products,
)


FIXED_NOW = datetime(2026, 9, 7, 22, 15, 32, tzinfo=timezone.utc)


def _product(product_id: int = 1, offer_id: str = "A") -> dict[str, Any]:
    return {"product_id": product_id, "offer_id": offer_id, "archived": False}


def _info(product_id: int = 1, offer_id: str = "A") -> dict[str, Any]:
    return {
        "id": product_id,
        "offer_id": offer_id,
        "name": "Полотенце",
        "is_archived": False,
    }


def _price(product_id: int = 1, offer_id: str = "A") -> dict[str, Any]:
    return {
        "product_id": product_id,
        "offer_id": offer_id,
        "price": {
            "currency_code": "RUB",
            "price": 1000,
            "marketing_seller_price": 700,
            "old_price": 1500,
        },
    }


def _stock(
    records: list[dict[str, Any]] | None = None,
    product_id: int = 1,
    offer_id: str = "A",
) -> dict[str, Any]:
    if records is None:
        records = [{"present": 10, "reserved": 3, "type": "fbs"}]
    return {
        "product_id": product_id,
        "offer_id": offer_id,
        "stocks": records,
    }


@dataclass
class FakeOzonClient:
    products: list[dict[str, Any]] = field(default_factory=lambda: [_product()])
    info: list[dict[str, Any]] = field(default_factory=lambda: [_info()])
    prices: list[dict[str, Any]] = field(default_factory=lambda: [_price()])
    stocks: list[dict[str, Any]] = field(default_factory=lambda: [_stock()])
    info_requests: list[list[str]] = field(default_factory=list)

    def list_all_products(self) -> list[dict[str, Any]]:
        return self.products

    def get_products_info(self, offer_ids: list[str]) -> list[dict[str, Any]]:
        self.info_requests.append(list(offer_ids))
        return self.info

    def list_all_prices(self) -> list[dict[str, Any]]:
        return self.prices

    def list_all_stocks(self) -> list[dict[str, Any]]:
        return self.stocks


@pytest.fixture
def output_dir() -> Iterator[Path]:
    tests_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(dir=tests_dir) as directory:
        yield Path(directory)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        return list(reader.fieldnames or []), list(reader)


def test_happy_path_uses_base_price_and_available_stock(output_dir: Path) -> None:
    client = FakeOzonClient()

    result = export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)

    columns, rows = _read_csv(result.path)
    assert columns == list(CSV_COLUMNS)
    assert rows == [
        {
            "offer_id": "A",
            "product_id": "1",
            "name": "Полотенце",
            "price": "1000.00",
            "currency": "RUB",
            "stock": "7",
            "exported_at": "2026-09-07T22:15:32+00:00",
        }
    ]
    assert result.products == 1
    assert result.missing_prices == 0
    assert result.missing_stocks == 0


def test_stock_aggregation_sums_available_records(output_dir: Path) -> None:
    client = FakeOzonClient(
        stocks=[
            _stock(
                [
                    {"present": 10, "reserved": 3},
                    {"present": 5, "reserved": 1},
                ]
            )
        ]
    )

    result = export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)

    _, rows = _read_csv(result.path)
    assert rows[0]["stock"] == "11"


def test_reserved_greater_than_present_is_clamped_and_warned(
    output_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeOzonClient(stocks=[_stock([{"present": 2, "reserved": 5}])])

    result = export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)

    _, rows = _read_csv(result.path)
    assert rows[0]["stock"] == "0"
    assert "reserved exceeds present" in caplog.text


def test_empty_stock_array_is_known_zero(output_dir: Path) -> None:
    client = FakeOzonClient(stocks=[_stock([])])

    result = export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)

    _, rows = _read_csv(result.path)
    assert rows[0]["stock"] == "0"
    assert result.missing_stocks == 0


def test_missing_stock_item_is_unknown(output_dir: Path) -> None:
    client = FakeOzonClient(stocks=[])

    result = export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)

    _, rows = _read_csv(result.path)
    assert rows[0]["stock"] == ""
    assert result.missing_stocks == 1


def test_archived_products_are_excluded_before_info_request(
    output_dir: Path,
) -> None:
    client = FakeOzonClient(
        products=[
            _product(1, "A"),
            {"product_id": 2, "offer_id": "B", "archived": True},
        ]
    )

    result = export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)

    _, rows = _read_csv(result.path)
    assert [row["offer_id"] for row in rows] == ["A"]
    assert client.info_requests == [["A"]]
    assert result.archived_excluded == 1


def test_product_info_archived_signal_excludes_product(
    output_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    info = _info()
    info["is_archived"] = True
    client = FakeOzonClient(info=[info])

    result = export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)

    _, rows = _read_csv(result.path)
    assert rows == []
    assert result.info_archived_excluded == 1
    assert "product-info reports archived" in caplog.text


def test_duplicate_product_id_in_price_source_fails(output_dir: Path) -> None:
    client = FakeOzonClient(prices=[_price(), _price()])

    with pytest.raises(Task1ConsistencyError, match="Duplicate product_id in price"):
        export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)


def test_offer_id_mismatch_fails(output_dir: Path) -> None:
    client = FakeOzonClient(prices=[_price(offer_id="B")])

    with pytest.raises(Task1ConsistencyError, match="offer_id mismatch in price"):
        export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)


@pytest.mark.parametrize(
    "bad_product",
    [
        {"product_id": True, "offer_id": "A", "archived": False},
        {"product_id": 1, "offer_id": "", "archived": False},
        {"product_id": 1, "offer_id": "A"},
        {"product_id": 1, "offer_id": "A", "archived": "false"},
    ],
)
def test_invalid_canonical_product_fails(
    output_dir: Path,
    bad_product: dict[str, Any],
) -> None:
    client = FakeOzonClient(products=[bad_product])

    with pytest.raises(Task1SchemaError):
        export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)


def test_missing_secondary_items_keep_canonical_product(output_dir: Path) -> None:
    client = FakeOzonClient(info=[], prices=[], stocks=[])

    result = export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)

    _, rows = _read_csv(result.path)
    assert rows[0]["name"] == ""
    assert rows[0]["price"] == ""
    assert rows[0]["currency"] == ""
    assert rows[0]["stock"] == ""
    assert result.missing_info == 1
    assert result.missing_prices == 1
    assert result.missing_stocks == 1


def test_invalid_price_keeps_product_with_empty_price(output_dir: Path) -> None:
    price = _price()
    price["price"]["price"] = "not-a-number"
    client = FakeOzonClient(prices=[price])

    result = export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)

    _, rows = _read_csv(result.path)
    assert rows[0]["price"] == ""
    assert rows[0]["currency"] == ""
    assert result.missing_prices == 1


@pytest.mark.parametrize(
    "record",
    [
        {"present": -1, "reserved": 0},
        {"present": 1, "reserved": -1},
        {"present": 1.0, "reserved": 0},
        {"present": 1, "reserved": False},
    ],
)
def test_malformed_stock_record_fails(
    output_dir: Path,
    record: dict[str, Any],
) -> None:
    client = FakeOzonClient(stocks=[_stock([record])])

    with pytest.raises(Task1SchemaError, match="Invalid stock record"):
        export_ozon_products(client, output_dir, now=lambda: FIXED_NOW)


def test_atomic_write_replaces_same_day_file_and_leaves_no_temp(
    output_dir: Path,
) -> None:
    final_path = output_dir / "ozon_products_2026-09-07.csv"
    final_path.write_text("old content", encoding="utf-8")

    result = export_ozon_products(
        FakeOzonClient(), output_dir, now=lambda: FIXED_NOW
    )

    assert result.path == final_path
    assert final_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "old content" not in final_path.read_text(encoding="utf-8-sig")
    assert list(output_dir.glob("*.tmp")) == []
    assert list(output_dir.glob(".*.tmp")) == []


def test_failed_atomic_replace_preserves_existing_file_and_removes_temp(
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_path = output_dir / "ozon_products_2026-09-07.csv"
    final_path.write_text("complete previous export", encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("src.task1_export.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        export_ozon_products(FakeOzonClient(), output_dir, now=lambda: FIXED_NOW)

    assert final_path.read_text(encoding="utf-8") == "complete previous export"
    assert list(output_dir.glob("*.tmp")) == []
    assert list(output_dir.glob(".*.tmp")) == []


def test_utf8_sig_and_one_utc_timestamp_are_used_for_all_rows(
    output_dir: Path,
) -> None:
    client = FakeOzonClient(
        products=[_product(1, "A"), _product(2, "B")],
        info=[_info(1, "A"), _info(2, "B")],
        prices=[_price(1, "A"), _price(2, "B")],
        stocks=[_stock(product_id=1, offer_id="A"), _stock(product_id=2, offer_id="B")],
    )
    non_utc_now = FIXED_NOW.astimezone(timezone(timedelta(hours=3)))

    result = export_ozon_products(
        client,
        output_dir,
        now=lambda: non_utc_now,
    )

    assert result.path.name == "ozon_products_2026-09-07.csv"
    assert result.path.read_bytes().startswith(b"\xef\xbb\xbf")
    _, rows = _read_csv(result.path)
    assert {row["name"] for row in rows} == {"Полотенце"}
    assert {row["exported_at"] for row in rows} == {
        "2026-09-07T22:15:32+00:00"
    }


def test_naive_export_timestamp_fails(output_dir: Path) -> None:
    client = FakeOzonClient()

    with pytest.raises(Task1SchemaError, match="timezone-aware"):
        export_ozon_products(
            client,
            output_dir,
            now=lambda: datetime(2026, 9, 7, 22, 15, 32),
        )

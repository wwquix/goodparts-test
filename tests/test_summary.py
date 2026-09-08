from __future__ import annotations

import csv
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.task2_summary import (
    CSV_COLUMNS,
    MAX_MESSAGE_CHARS,
    MESSAGE_INTERVAL_SECONDS,
    Task2CSVError,
    Task2SummaryError,
    build_summary_chunks,
    format_product_block,
    parse_stock,
    read_export_csv,
    send_summary,
)


@pytest.fixture
def temp_dir() -> Iterator[Path]:
    tests_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(dir=tests_dir) as directory:
        yield Path(directory)


def row(
    *,
    offer_id: str = "A-1",
    product_id: str = "1",
    name: str = "Полотенце",
    price: str = "100.00",
    currency: str = "RUB",
    stock: str = "4",
    exported_at: str = "2026-09-07T22:15:32+00:00",
) -> dict[str, str]:
    return {
        "offer_id": offer_id,
        "product_id": product_id,
        "name": name,
        "price": price,
        "currency": currency,
        "stock": stock,
        "exported_at": exported_at,
    }


def write_csv(path: Path, rows: list[dict[str, str]], *, columns=CSV_COLUMNS) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=columns,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def test_threshold_boundary_zero_and_unknown() -> None:
    rows = [
        row(offer_id="four", stock="4"),
        row(offer_id="five", stock="5"),
        row(offer_id="zero", stock="0"),
        row(offer_id="unknown", stock=""),
    ]

    chunks = build_summary_chunks(rows, threshold=5)
    first = chunks[0]

    assert "Товаров: 4" in first
    assert "Заканчиваются: 2" in first
    assert "Неизвестный остаток: 1" in first
    assert "Остаток: 4 — ЗАКАНЧИВАЕТСЯ" in first
    assert "Остаток: 5\n" in first
    assert "Остаток: 0 — ЗАКАНЧИВАЕТСЯ" in first
    assert "Остаток: неизвестно" in first

    assert "ЗАКАНЧИВАЕТСЯ" not in format_product_block(row(stock="5"), 5)
    assert "ЗАКАНЧИВАЕТСЯ" not in format_product_block(row(stock="1"), 0)
    assert "ЗАКАНЧИВАЕТСЯ" not in format_product_block(row(stock=""), 5)


def test_missing_price_and_name_fallback_are_russian_plain_text() -> None:
    block = format_product_block(
        row(offer_id="SKU-7", name="", price="", currency="", stock="5"),
        5,
    )

    assert block == (
        "Товар SKU-7\n"
        "Артикул: SKU-7\n"
        "Цена: неизвестно\n"
        "Остаток: 5"
    )


def test_multichunk_messages_are_bounded_and_each_product_appears_once() -> None:
    rows = [
        row(offer_id=f"SKU-{index:03d}", name="Товар " + "я" * 120)
        for index in range(60)
    ]

    chunks = build_summary_chunks(rows, 5)

    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_MESSAGE_CHARS for chunk in chunks)
    assert chunks[1].startswith("Утренняя сводка Ozon — продолжение")
    for product in rows:
        marker = f"Артикул: {product['offer_id']}"
        assert sum(chunk.count(marker) for chunk in chunks) == 1


def test_oversized_name_is_truncated_but_fixed_fields_are_retained() -> None:
    product = row(
        offer_id="FIXED-ID",
        name="Очень длинное имя " + "я" * 10000,
        price="123.45",
        currency="RUB",
        stock="4",
    )

    block = format_product_block(product, 5)

    assert len(block) <= MAX_MESSAGE_CHARS
    assert block.endswith("ЗАКАНЧИВАЕТСЯ")
    assert "Артикул: FIXED-ID" in block
    assert "Цена: 123.45 RUB" in block
    assert "…" in block


def test_read_csv_rejects_missing_column_invalid_stock_and_empty_file(
    temp_dir: Path,
) -> None:
    missing_column_path = temp_dir / "missing.csv"
    write_csv(missing_column_path, [row()], columns=CSV_COLUMNS[:-1])
    with pytest.raises(Task2CSVError, match="exported_at"):
        read_export_csv(missing_column_path)

    invalid_stock_path = temp_dir / "invalid-stock.csv"
    write_csv(invalid_stock_path, [row(stock="-1")])
    with pytest.raises(Task2CSVError, match="stock"):
        read_export_csv(invalid_stock_path)

    empty_path = temp_dir / "empty.csv"
    empty_path.touch()
    with pytest.raises(Task2CSVError, match="physically empty"):
        read_export_csv(empty_path)


def test_short_row_fails_but_explicit_empty_stock_is_unknown(
    temp_dir: Path,
) -> None:
    short_path = temp_dir / "short-row.csv"
    short_path.write_text(
        "offer_id,product_id,name,price,currency,stock,exported_at\n"
        "A-1,1,Полотенце,100.00,RUB,4\n",
        encoding="utf-8-sig",
    )
    with pytest.raises(Task2CSVError, match=r"row 2.*exported_at"):
        read_export_csv(short_path)

    explicit_empty_path = temp_dir / "explicit-empty-stock.csv"
    write_csv(explicit_empty_path, [row(stock="")])
    rows = read_export_csv(explicit_empty_path)
    assert rows[0]["stock"] == ""
    assert parse_stock(rows[0]["stock"]) is None


def test_read_csv_rejects_extra_and_reordered_columns(temp_dir: Path) -> None:
    extra_path = temp_dir / "extra.csv"
    write_csv(extra_path, [row()], columns=CSV_COLUMNS + ("extra",))
    with pytest.raises(Task2CSVError, match=r"unexpected column\(s\): extra"):
        read_export_csv(extra_path)

    reordered_path = temp_dir / "reordered.csv"
    reordered_columns = (CSV_COLUMNS[1], CSV_COLUMNS[0], *CSV_COLUMNS[2:])
    write_csv(reordered_path, [row()], columns=reordered_columns)
    with pytest.raises(Task2CSVError, match="reordered"):
        read_export_csv(reordered_path)


def test_headers_only_and_utf8_sig_are_supported(temp_dir: Path) -> None:
    headers_only = temp_dir / "headers.csv"
    write_csv(headers_only, [])
    assert read_export_csv(headers_only) == []

    utf8_path = temp_dir / "utf8.csv"
    write_csv(utf8_path, [row(name="Ёлка", stock="")])
    rows = read_export_csv(utf8_path)
    assert rows[0]["name"] == "Ёлка"
    assert parse_stock(rows[0]["stock"]) is None
    assert utf8_path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_date_header_requires_one_shared_reasonable_iso_date() -> None:
    dated = build_summary_chunks([row()], 5)
    assert dated[0].startswith("Утренняя сводка Ozon — 2026-09-07")

    mixed = build_summary_chunks(
        [row(exported_at="2026-09-07"), row(offer_id="B", exported_at="not-date")],
        5,
    )
    assert mixed[0].startswith("Утренняя сводка Ozon\n")


def test_headers_only_summary_is_one_message() -> None:
    assert build_summary_chunks([], 5) == [
        "Утренняя сводка Ozon\nТоваров: 0\nВ выгрузке нет товаров."
    ]


def test_send_summary_sleeps_only_between_successful_messages(temp_dir: Path) -> None:
    path = temp_dir / "many.csv"
    rows = [
        row(offer_id=f"SKU-{index}", name="x" * 140)
        for index in range(60)
    ]
    write_csv(path, rows)

    sent: list[str] = []
    sleeps: list[float] = []

    class FakeTelegram:
        def send_message(self, text: str) -> None:
            sent.append(text)

    result = send_summary(path, FakeTelegram(), 5, sleep=sleeps.append)

    assert result == {
        "products": 60,
        "low_stock": 60,
        "messages": len(sent),
    }
    assert len(sleeps) == len(sent) - 1
    assert sleeps == [MESSAGE_INTERVAL_SECONDS] * (len(sent) - 1)


def test_fixed_fields_that_cannot_fit_fail_clearly() -> None:
    with pytest.raises(Task2SummaryError, match="fixed fields"):
        format_product_block(
            row(offer_id="x" * 5000, name="short"),
            5,
        )

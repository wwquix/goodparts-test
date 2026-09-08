"""Task 2: format the Task 1 CSV and deliver a Telegram summary."""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from os import PathLike
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from .config import TelegramConfigError, load_telegram_config
from .telegram_client import TelegramClient, TelegramError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CSV_COLUMNS = (
    "offer_id",
    "product_id",
    "name",
    "price",
    "currency",
    "stock",
    "exported_at",
)
MAX_MESSAGE_CHARS = 4000
MESSAGE_INTERVAL_SECONDS = 1.05

CsvRow: TypeAlias = dict[str, str]
ProductRow: TypeAlias = Mapping[str, object]
Sleep: TypeAlias = Callable[[float], None]


class Task2Error(ValueError):
    """Base error for Task 2 CSV and summary failures."""


class Task2CSVError(Task2Error):
    """Raised when the Task 1 CSV is empty, malformed, or invalid."""


class Task2SummaryError(Task2Error):
    """Raised when a product or summary cannot fit the Telegram contract."""


class TelegramMessageSender(Protocol):
    def send_message(self, text: str) -> object: ...


class SummaryResult(dict[str, int]):
    """Safe delivery counters, usable both as a mapping and via attributes."""

    _ALIASES = {
        "low_stock_count": "low_stock",
        "message_count": "messages",
    }

    def __init__(self, *, products: int, low_stock: int, messages: int) -> None:
        super().__init__(
            products=products,
            low_stock=low_stock,
            messages=messages,
        )

    def __getitem__(self, key: str) -> int:
        return super().__getitem__(self._ALIASES.get(key, key))

    def get(self, key: str, default: int | None = None) -> int | None:
        return super().get(self._ALIASES.get(key, key), default)

    @property
    def products(self) -> int:
        return self["products"]

    @property
    def low_stock(self) -> int:
        return self["low_stock"]

    @property
    def low_stock_count(self) -> int:
        return self["low_stock"]

    @property
    def messages(self) -> int:
        return self["messages"]

    @property
    def message_count(self) -> int:
        return self["messages"]


def _validate_threshold(threshold: int) -> int:
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        raise Task2SummaryError("low_stock_threshold must be an integer >= 0")
    if threshold < 0:
        raise Task2SummaryError("low_stock_threshold must be an integer >= 0")
    return threshold


def parse_stock(value: object) -> int | None:
    """Parse Task 1 stock text, retaining an empty value as unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise Task2CSVError("stock must be an integer >= 0 or empty")
    if isinstance(value, int):
        if value < 0:
            raise Task2CSVError("stock must be an integer >= 0 or empty")
        return value
    if not isinstance(value, str):
        raise Task2CSVError("stock must be an integer >= 0 or empty")

    text = value.strip()
    if not text:
        return None
    if not re.fullmatch(r"[+-]?[0-9]+", text):
        raise Task2CSVError("stock must be an integer >= 0 or empty")
    stock = int(text)
    if stock < 0:
        raise Task2CSVError("stock must be an integer >= 0 or empty")
    return stock


def read_export_csv(path: str | PathLike[str]) -> list[CsvRow]:
    """Read and validate the Task 1 CSV while preserving row order."""
    csv_path = Path(path)
    try:
        csv_file = csv_path.open("r", encoding="utf-8-sig", newline="")
    except OSError:
        raise

    with csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = reader.fieldnames
        if fieldnames is None:
            raise Task2CSVError("CSV file is physically empty")
        missing = [column for column in CSV_COLUMNS if column not in fieldnames]
        if missing:
            raise Task2CSVError(
                "CSV is missing required column(s): " + ", ".join(missing)
            )
        unexpected = [column for column in fieldnames if column not in CSV_COLUMNS]
        if unexpected:
            raise Task2CSVError(
                "CSV contains unexpected column(s): " + ", ".join(unexpected)
            )
        duplicate_columns = {
            field for field in fieldnames if fieldnames.count(field) > 1
        }
        if duplicate_columns:
            names = ", ".join(sorted(str(name) for name in duplicate_columns))
            raise Task2CSVError(f"CSV contains duplicate column(s): {names}")
        if tuple(fieldnames) != CSV_COLUMNS:
            raise Task2CSVError(
                "CSV columns are reordered; expected exact order: "
                + ", ".join(CSV_COLUMNS)
            )

        rows: list[CsvRow] = []
        for row_number, raw_row in enumerate(reader, start=2):
            if None in raw_row:
                raise Task2CSVError(
                    f"CSV row {row_number} has more values than its header"
                )
            missing_values = [
                column for column in CSV_COLUMNS if raw_row.get(column) is None
            ]
            if missing_values:
                raise Task2CSVError(
                    f"CSV row {row_number} is missing value(s) for required "
                    "column(s): "
                    + ", ".join(missing_values)
                )
            row = {
                str(key): "" if value is None else value
                for key, value in raw_row.items()
                if key is not None
            }
            try:
                parse_stock(row.get("stock", ""))
            except Task2CSVError as exc:
                raise Task2CSVError(
                    f"Invalid stock at CSV row {row_number}: {exc}"
                ) from exc
            rows.append(row)
        return rows


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _parse_exported_date(value: object) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        if len(text) == 10:
            parsed = date.fromisoformat(text)
        else:
            timestamp = text
            if timestamp.endswith(("Z", "z")):
                timestamp = timestamp[:-1] + "+00:00"
            parsed = datetime.fromisoformat(timestamp).date()
    except ValueError:
        return None
    if not 2000 <= parsed.year <= 2100:
        return None
    return parsed


def _summary_header(rows: Sequence[ProductRow]) -> str:
    dates = [_parse_exported_date(row.get("exported_at")) for row in rows]
    if dates and all(value is not None for value in dates):
        first_date = dates[0]
        if first_date is not None and all(value == first_date for value in dates):
            return f"Утренняя сводка Ozon — {first_date:%Y-%m-%d}"
    return "Утренняя сводка Ozon"


def _row_stock(row: ProductRow) -> int | None:
    try:
        return parse_stock(row.get("stock", ""))
    except Task2CSVError:
        raise


def _product_counts(
    rows: Sequence[ProductRow],
    threshold: int,
) -> tuple[int, int]:
    low_stock = 0
    unknown_stock = 0
    for row in rows:
        stock = _row_stock(row)
        if stock is None:
            unknown_stock += 1
        elif stock < threshold:
            low_stock += 1
    return low_stock, unknown_stock


def _format_product_block_with_limit(
    row: ProductRow,
    threshold: int,
    max_chars: int,
) -> str:
    if max_chars <= 0:
        raise Task2SummaryError(
            "Product block cannot fit within the Telegram message limit"
        )
    offer_id = _text(row.get("offer_id"))
    if not offer_id:
        raise Task2SummaryError("offer_id must be a non-empty value")

    display_name = _text(row.get("name")) or f"Товар {offer_id}"
    price = _text(row.get("price"))
    currency = _text(row.get("currency"))
    stock = _row_stock(row)

    if price:
        price_text = f"{price} {currency}" if currency else price
        price_line = f"Цена: {price_text}"
    else:
        price_line = "Цена: неизвестно"

    if stock is None:
        stock_line = "Остаток: неизвестно"
    else:
        stock_line = f"Остаток: {stock}"
        if stock < threshold:
            stock_line += " — ЗАКАНЧИВАЕТСЯ"

    fixed_lines = (f"Артикул: {offer_id}", price_line, stock_line)
    fixed_text = "\n".join(fixed_lines)
    available_name_chars = max_chars - len(fixed_text) - 1
    if available_name_chars < 1:
        raise Task2SummaryError(
            "Product fixed fields cannot fit within the Telegram message limit"
        )
    if len(display_name) > available_name_chars:
        display_name = display_name[: available_name_chars - 1] + "…"

    block = "\n".join((display_name, *fixed_lines))
    if len(block) > max_chars:
        raise Task2SummaryError(
            "Product block cannot fit within the Telegram message limit"
        )
    return block


def format_product_block(row: ProductRow, threshold: int) -> str:
    """Format one product as compact Russian plain text."""
    checked_threshold = _validate_threshold(threshold)
    return _format_product_block_with_limit(
        row,
        checked_threshold,
        MAX_MESSAGE_CHARS,
    )


def build_summary_chunks(
    rows: Iterable[ProductRow],
    threshold: int,
) -> list[str]:
    """Build complete, lossless Telegram messages at product-block boundaries."""
    checked_threshold = _validate_threshold(threshold)
    row_list = list(rows)
    if not row_list:
        return ["Утренняя сводка Ozon\nТоваров: 0\nВ выгрузке нет товаров."]
    if any(not isinstance(row, Mapping) for row in row_list):
        raise Task2SummaryError("Every product row must be a mapping")

    low_stock, unknown_stock = _product_counts(row_list, checked_threshold)
    header = _summary_header(row_list)
    counts = (
        f"Товаров: {len(row_list)}\n"
        f"Заканчиваются: {low_stock}\n"
        f"Неизвестный остаток: {unknown_stock}"
    )
    first_prefix = f"{header}\n\n{counts}\n\n"
    continuation_prefix = "Утренняя сводка Ozon — продолжение\n\n"
    if len(first_prefix) > MAX_MESSAGE_CHARS:
        raise Task2SummaryError(
            "Summary header cannot fit within the Telegram message limit"
        )

    block_limit = MAX_MESSAGE_CHARS - max(
        len(first_prefix), len(continuation_prefix)
    )
    if block_limit < 1:
        raise Task2SummaryError(
            "Summary header cannot leave room for a product block"
        )

    blocks = [
        _format_product_block_with_limit(row, checked_threshold, block_limit)
        for row in row_list
    ]

    chunks: list[str] = []
    current = first_prefix
    for block in blocks:
        separator = "" if current == first_prefix else "\n\n"
        if len(current) + len(separator) + len(block) <= MAX_MESSAGE_CHARS:
            current += separator + block
            continue

        chunks.append(current)
        current = continuation_prefix + block
        if len(current) > MAX_MESSAGE_CHARS:
            raise Task2SummaryError(
                "Product block cannot fit within the Telegram message limit"
            )
    chunks.append(current)
    return chunks


def send_summary(
    csv_path: str | PathLike[str],
    telegram_client: TelegramMessageSender,
    threshold: int,
    *,
    sleep: Sleep = time.sleep,
) -> SummaryResult:
    """Read, format, and send a CSV summary in order."""
    rows = read_export_csv(csv_path)
    chunks = build_summary_chunks(rows, threshold)
    low_stock, _ = _product_counts(rows, _validate_threshold(threshold))

    for index, chunk in enumerate(chunks):
        telegram_client.send_message(chunk)
        if index < len(chunks) - 1:
            sleep(MESSAGE_INTERVAL_SECONDS)

    return SummaryResult(
        products=len(rows),
        low_stock=low_stock,
        messages=len(chunks),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a Task 1 CSV summary")
    parser.add_argument("csv_path", help="Path to the Task 1 CSV export")
    args = parser.parse_args(argv)

    try:
        config = load_telegram_config()
        with TelegramClient(config) as telegram_client:
            result = send_summary(
                args.csv_path,
                telegram_client,
                config.low_stock_threshold,
            )
    except (
        OSError,
        TelegramConfigError,
        TelegramError,
        Task2Error,
    ) as exc:
        print(f"Task 2 failed: {exc}", file=sys.stderr)
        return 1

    print("Sent Telegram summary")
    print(f"Products: {result.products}")
    print(f"Low stock: {result.low_stock}")
    print(f"Messages: {result.messages}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CSV_COLUMNS",
    "MAX_MESSAGE_CHARS",
    "MESSAGE_INTERVAL_SECONDS",
    "SummaryResult",
    "Task2CSVError",
    "Task2Error",
    "Task2SummaryError",
    "build_summary_chunks",
    "format_product_block",
    "main",
    "parse_stock",
    "read_export_csv",
    "send_summary",
]

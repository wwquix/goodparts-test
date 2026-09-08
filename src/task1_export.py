"""Task 1: export validated Ozon product data to an atomic CSV file."""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from .config import OzonConfigError, load_ozon_config
from .ozon_client import OzonClient, OzonError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "output"
CSV_COLUMNS = (
    "offer_id",
    "product_id",
    "name",
    "price",
    "currency",
    "stock",
    "exported_at",
)

JsonObject: TypeAlias = dict[str, Any]
CsvRow: TypeAlias = dict[str, str | int]

logger = logging.getLogger(__name__)


class Task1ExportError(RuntimeError):
    """Base error for Task 1 normalization and export failures."""


class Task1SchemaError(Task1ExportError):
    """Raised when Ozon business data has an unexpected shape or value."""


class Task1ConsistencyError(Task1ExportError):
    """Raised when sources contradict each other for the same product."""


class OzonProductSource(Protocol):
    """The raw-data methods Task 1 needs from the production Ozon client."""

    def list_all_products(self) -> list[JsonObject]: ...

    def get_products_info(
        self, offer_ids: Sequence[str]
    ) -> list[JsonObject]: ...

    def list_all_prices(self) -> list[JsonObject]: ...

    def list_all_stocks(self) -> list[JsonObject]: ...


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Safe operational summary for one completed Task 1 export."""

    path: Path
    products: int
    archived_excluded: int
    info_archived_excluded: int
    missing_info: int
    missing_prices: int
    missing_stocks: int


@dataclass(slots=True)
class _ExportCounts:
    archived_excluded: int = 0
    info_archived_excluded: int = 0
    missing_info: int = 0
    missing_prices: int = 0
    missing_stocks: int = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_product_id(
    item: JsonObject,
    key: str,
    source: str,
) -> int:
    product_id = item.get(key)
    if isinstance(product_id, bool) or not isinstance(product_id, int):
        raise Task1SchemaError(
            f"Invalid {source} item: {key} must be an integer (not bool)"
        )
    return product_id


def _require_offer_id(item: JsonObject, source: str) -> str:
    offer_id = item.get("offer_id")
    if not isinstance(offer_id, str) or not offer_id.strip():
        raise Task1SchemaError(
            f"Invalid {source} item: offer_id must be a non-empty string"
        )
    return offer_id


def _canonical_products(
    products: list[JsonObject],
) -> tuple[dict[int, JsonObject], int]:
    all_products: dict[int, JsonObject] = {}
    archived_excluded = 0

    for item in products:
        if not isinstance(item, dict):
            raise Task1SchemaError(
                "Invalid product-list item: every item must be an object"
            )
        product_id = _require_product_id(item, "product_id", "product-list")
        _require_offer_id(item, "product-list")
        archived = item.get("archived")
        if not isinstance(archived, bool):
            raise Task1SchemaError(
                "Invalid product-list item: archived must be a boolean "
                f"for product_id={product_id}"
            )
        if product_id in all_products:
            raise Task1ConsistencyError(
                "Duplicate product_id in product-list: " f"product_id={product_id}"
            )
        all_products[product_id] = item
        if archived:
            archived_excluded += 1

    active_products = {
        product_id: item
        for product_id, item in all_products.items()
        if item["archived"] is False
    }
    return active_products, archived_excluded


def _index_source(
    items: list[JsonObject],
    *,
    source: str,
    product_id_key: str,
) -> dict[int, JsonObject]:
    indexed: dict[int, JsonObject] = {}
    for item in items:
        if not isinstance(item, dict):
            raise Task1SchemaError(
                f"Invalid {source} item: every item must be an object"
            )
        product_id = _require_product_id(item, product_id_key, source)
        _require_offer_id(item, source)
        if product_id in indexed:
            raise Task1ConsistencyError(
                f"Duplicate product_id in {source}: product_id={product_id}"
            )
        indexed[product_id] = item
    return indexed


def _check_offer_id(
    canonical_item: JsonObject,
    secondary_item: JsonObject,
    *,
    source: str,
    product_id: int,
) -> None:
    canonical_offer_id = _require_offer_id(canonical_item, "product-list")
    secondary_offer_id = _require_offer_id(secondary_item, source)
    if canonical_offer_id != secondary_offer_id:
        raise Task1ConsistencyError(
            f"offer_id mismatch in {source} for product_id={product_id}: "
            f"canonical={canonical_offer_id!r}, source={secondary_offer_id!r}"
        )


def _normalize_name(info_item: JsonObject, *, product_id: int) -> str:
    name = info_item.get("name")
    if not isinstance(name, str):
        logger.warning(
            "Missing or invalid product name: product_id=%d; exporting empty name",
            product_id,
        )
        return ""
    return name


def _normalize_price(
    price_item: JsonObject,
    *,
    product_id: int,
) -> tuple[str, str, bool]:
    price_fields = price_item.get("price")
    if not isinstance(price_fields, dict):
        logger.warning(
            "Missing or invalid price object: product_id=%d; exporting empty price",
            product_id,
        )
        return "", "", True

    raw_price = price_fields.get("price")
    try:
        if isinstance(raw_price, bool):
            raise InvalidOperation
        amount = Decimal(str(raw_price))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        logger.warning(
            "Missing or invalid price.price: product_id=%d; exporting empty price",
            product_id,
        )
        return "", "", True

    currency_value = price_fields.get("currency_code")
    if isinstance(currency_value, str) and currency_value.strip():
        currency = currency_value
    else:
        currency = ""
        logger.warning(
            "Missing or invalid price currency: product_id=%d",
            product_id,
        )

    try:
        if currency == "RUB":
            normalized_price = format(amount.quantize(Decimal("0.01")), ".2f")
        else:
            normalized_price = format(amount, "f")
    except InvalidOperation:
        logger.warning(
            "Invalid price precision: product_id=%d; exporting empty price",
            product_id,
        )
        return "", "", True
    return normalized_price, currency, False


def _normalize_stock(stock_item: JsonObject, *, product_id: int) -> int:
    stock_records = stock_item.get("stocks")
    if not isinstance(stock_records, list):
        raise Task1SchemaError(
            "Invalid stock item: stocks must be an array "
            f"for product_id={product_id}"
        )

    available = 0
    for index, record in enumerate(stock_records):
        if not isinstance(record, dict):
            raise Task1SchemaError(
                "Invalid stock record: record must be an object "
                f"for product_id={product_id}, index={index}"
            )
        present = record.get("present")
        reserved = record.get("reserved")
        if (
            isinstance(present, bool)
            or not isinstance(present, int)
            or present < 0
        ):
            raise Task1SchemaError(
                "Invalid stock record: present must be an integer >= 0 "
                f"for product_id={product_id}, index={index}"
            )
        if (
            isinstance(reserved, bool)
            or not isinstance(reserved, int)
            or reserved < 0
        ):
            raise Task1SchemaError(
                "Invalid stock record: reserved must be an integer >= 0 "
                f"for product_id={product_id}, index={index}"
            )
        if reserved > present:
            logger.warning(
                "reserved exceeds present: product_id=%d index=%d; contribution=0",
                product_id,
                index,
            )
        available += max(present - reserved, 0)
    return available


def _validate_export_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise Task1SchemaError("Export timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _build_rows(
    canonical_by_product_id: dict[int, JsonObject],
    info_by_product_id: dict[int, JsonObject],
    price_by_product_id: dict[int, JsonObject],
    stock_by_product_id: dict[int, JsonObject],
    *,
    exported_at: datetime,
    counts: _ExportCounts,
) -> list[CsvRow]:
    timestamp_text = exported_at.isoformat()
    rows: list[CsvRow] = []

    for product_id, canonical_item in canonical_by_product_id.items():
        offer_id = _require_offer_id(canonical_item, "product-list")
        info_item = info_by_product_id.get(product_id)
        if info_item is None:
            counts.missing_info += 1
            name = ""
            logger.warning(
                "Missing product-info item: product_id=%d; exporting empty name",
                product_id,
            )
        else:
            _check_offer_id(
                canonical_item,
                info_item,
                source="product-info",
                product_id=product_id,
            )
            info_archived = info_item.get("is_archived")
            if "is_archived" in info_item and not isinstance(info_archived, bool):
                raise Task1SchemaError(
                    "Invalid product-info item: is_archived must be a boolean "
                    f"for product_id={product_id}"
                )
            if info_archived is True:
                counts.info_archived_excluded += 1
                logger.warning(
                    "Product excluded because product-info reports archived: "
                    "product_id=%d offer_id=%s",
                    product_id,
                    offer_id,
                )
                continue
            name = _normalize_name(info_item, product_id=product_id)

        price_item = price_by_product_id.get(product_id)
        if price_item is None:
            counts.missing_prices += 1
            price = ""
            currency = ""
            logger.warning(
                "Missing price item: product_id=%d; exporting empty price",
                product_id,
            )
        else:
            _check_offer_id(
                canonical_item,
                price_item,
                source="price",
                product_id=product_id,
            )
            price, currency, price_missing = _normalize_price(
                price_item,
                product_id=product_id,
            )
            if price_missing:
                counts.missing_prices += 1

        stock_item = stock_by_product_id.get(product_id)
        if stock_item is None:
            counts.missing_stocks += 1
            stock: int | str = ""
            logger.warning(
                "Missing stock item: product_id=%d; exporting unknown stock",
                product_id,
            )
        else:
            _check_offer_id(
                canonical_item,
                stock_item,
                source="stock",
                product_id=product_id,
            )
            stock = _normalize_stock(stock_item, product_id=product_id)

        rows.append(
            {
                "offer_id": offer_id,
                "product_id": product_id,
                "name": name,
                "price": price,
                "currency": currency,
                "stock": stock,
                "exported_at": timestamp_text,
            }
        )
    return rows


def _write_csv_atomically(
    rows: list[CsvRow],
    *,
    output_dir: Path,
    exported_at: datetime,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = output_dir / f"ozon_products_{exported_at:%Y-%m-%d}.csv"
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=output_dir,
        prefix=f".{final_path.stem}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(
            file_descriptor,
            mode="w",
            encoding="utf-8-sig",
            newline="",
        ) as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
            csv_file.flush()
            os.fsync(csv_file.fileno())
        os.replace(temporary_path, final_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return final_path


def export_ozon_products(
    client: OzonProductSource,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> ExportResult:
    """Fetch, validate, merge, normalize, and atomically publish Task 1 CSV."""
    exported_at = _validate_export_timestamp(now())
    counts = _ExportCounts()

    canonical_items = client.list_all_products()
    canonical_by_product_id, counts.archived_excluded = _canonical_products(
        canonical_items
    )
    offer_ids = [
        _require_offer_id(item, "product-list")
        for item in canonical_by_product_id.values()
    ]
    logger.info(
        "Task 1 canonical products: total=%d active=%d archived_excluded=%d",
        len(canonical_items),
        len(canonical_by_product_id),
        counts.archived_excluded,
    )

    info_items = client.get_products_info(offer_ids)
    price_items = client.list_all_prices()
    stock_items = client.list_all_stocks()
    info_by_product_id = _index_source(
        info_items,
        source="product-info",
        product_id_key="id",
    )
    price_by_product_id = _index_source(
        price_items,
        source="price",
        product_id_key="product_id",
    )
    stock_by_product_id = _index_source(
        stock_items,
        source="stock",
        product_id_key="product_id",
    )
    logger.info(
        "Task 1 source counts: info=%d prices=%d stocks=%d",
        len(info_by_product_id),
        len(price_by_product_id),
        len(stock_by_product_id),
    )

    rows = _build_rows(
        canonical_by_product_id,
        info_by_product_id,
        price_by_product_id,
        stock_by_product_id,
        exported_at=exported_at,
        counts=counts,
    )
    final_path = _write_csv_atomically(
        rows,
        output_dir=Path(output_dir),
        exported_at=exported_at,
    )
    logger.info(
        "Task 1 export complete: products=%d missing_info=%d "
        "missing_prices=%d missing_stocks=%d output=%s",
        len(rows),
        counts.missing_info,
        counts.missing_prices,
        counts.missing_stocks,
        final_path,
    )
    return ExportResult(
        path=final_path,
        products=len(rows),
        archived_excluded=counts.archived_excluded,
        info_archived_excluded=counts.info_archived_excluded,
        missing_info=counts.missing_info,
        missing_prices=counts.missing_prices,
        missing_stocks=counts.missing_stocks,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = load_ozon_config()
        with OzonClient(config) as client:
            result = export_ozon_products(client)
    except (OzonConfigError, OzonError, Task1ExportError, OSError) as exc:
        logger.error("Task 1 export failed: %s", exc)
        return 1

    try:
        display_path = result.path.relative_to(PROJECT_ROOT)
    except ValueError:
        display_path = result.path
    print(f"Exported: {display_path}")
    print(f"Products: {result.products}")
    print(f"Missing info: {result.missing_info}")
    print(f"Missing prices: {result.missing_prices}")
    print(f"Missing stocks: {result.missing_stocks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

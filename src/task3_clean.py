"""Task 3: clean the employer-provided auto-parts catalog safely."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "catalog_raw.csv"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "output" / "catalog_clean.csv"
SOURCE_COLUMNS = ("offer_id", "name", "price", "stock")
OUTPUT_COLUMNS = ("offer_id", "name", "price", "stock", "brand", "oem", "quantity")
KNOWN_BRANDS = ("Mavico", "DBA", "Деталиус")

_BRAND_BY_NORMALIZED = {brand.casefold(): brand for brand in KNOWN_BRANDS}
_BRAND_PATTERNS = tuple(
    (
        brand,
        re.compile(
            rf"(?<!\w){re.escape(brand).replace(r'\ ', r'\s+')}(?!\w)",
            re.IGNORECASE,
        ),
    )
    for brand in KNOWN_BRANDS
)
_PRICE_QUALIFIER = re.compile(r"^от\s+", re.IGNORECASE)
_PRICE_SUFFIX = re.compile(r"\s*(?:руб\.?|р|rub)\s*$", re.IGNORECASE)
_GROUPED_PRICE = re.compile(r"\d{1,3}(?:\s\d{3})+(?:[.,]\d{1,2})?")
_SIMPLE_PRICE = re.compile(r"\d+(?:[.,]\d{1,2})?")
_OEM_PATTERN = re.compile(
    r"(?<!\w)OEM\s+(?P<code>\d{10}|\d{4}-\d{7})(?![\w-])",
    re.IGNORECASE,
)
_QUANTITY_PATTERNS = (
    re.compile(r"(?:набор\s+)?(?P<count>\d+)\s*шт\.?\s*$", re.IGNORECASE),
    re.compile(r"(?P<count>\d+)\s*(?:комплект|компл\.?|к-т)\.?\s*$", re.IGNORECASE),
    re.compile(r"(?:комплект|компл\.?|к-т)\s*(?P<count>\d+)\s*$", re.IGNORECASE),
)
_PAIR_PATTERN = re.compile(r"(?<!\w)пара\.?\s*$", re.IGNORECASE)


class CatalogCleaningError(ValueError):
    """Base error for Task 3 catalog cleaning."""


class CatalogSchemaError(CatalogCleaningError):
    """Raised when the source CSV does not have the required schema."""


@dataclass(frozen=True, slots=True)
class CleaningResult:
    """Safe counts and destination for a completed catalog-cleaning run."""

    path: Path
    input_rows: int
    removed_empty: int
    removed_exact_duplicates: int
    business_duplicates_removed: int
    unparsed_prices: int
    missing_brands: int
    missing_oem: int
    missing_quantity: int
    output_rows: int


def _as_text(raw: object) -> str | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (int, Decimal)):
        return str(raw)
    return None


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_description(raw: object) -> str:
    text = _as_text(raw)
    return _collapse_whitespace(text) if text is not None else ""


def _normalize_price_source(raw: object) -> str:
    text = _as_text(raw)
    return text.strip() if text is not None else ""


def parse_price(raw: object) -> Decimal | None:
    """Parse only the price spellings observed in the employer CSV.

    ``от`` means a lower bound in the source. The required output schema has no
    qualifier column, so its numeric lower bound is emitted as the price.
    """
    text = _as_text(raw)
    if text is None:
        return None
    numeric = _collapse_whitespace(text)
    if not numeric:
        return None
    numeric = _PRICE_QUALIFIER.sub("", numeric)
    numeric = _PRICE_SUFFIX.sub("", numeric).strip()
    if _GROUPED_PRICE.fullmatch(numeric):
        normalized = numeric.replace(" ", "").replace(",", ".")
    elif _SIMPLE_PRICE.fullmatch(numeric):
        normalized = numeric.replace(",", ".")
    else:
        return None
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01"))


def normalize_brand(raw: object) -> str | None:
    """Return a canonical employer-dataset brand, or ``None`` when unknown."""
    text = _as_text(raw)
    if text is None:
        return None
    return _BRAND_BY_NORMALIZED.get(_collapse_whitespace(text).casefold())


def extract_brand(description: object) -> str | None:
    """Extract one observed brand with case-insensitive token boundaries."""
    text = _as_text(description)
    if text is None:
        return None
    for brand, pattern in _BRAND_PATTERNS:
        if pattern.search(text):
            return brand
    return None


def extract_quantity(description: object) -> int | None:
    """Extract only observed terminal package expressions, never dimensions."""
    text = _as_text(description)
    if text is None:
        return None
    normalized = _collapse_whitespace(text)
    if _PAIR_PATTERN.search(normalized):
        return 2
    for pattern in _QUANTITY_PATTERNS:
        match = pattern.search(normalized)
        if match is not None:
            return int(match.group("count"))
    return None


def extract_oem(description: object) -> str | None:
    """Extract only the two high-confidence OEM shapes seen after ``OEM``."""
    text = _as_text(description)
    if text is None:
        return None
    match = _OEM_PATTERN.search(_collapse_whitespace(text))
    return match.group("code") if match is not None else None


def _format_price(price: Decimal | None) -> str:
    return format(price, ".2f") if price is not None else ""


def _read_source(path: Path) -> pd.DataFrame:
    dataframe = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    if tuple(dataframe.columns) != SOURCE_COLUMNS:
        actual = ",".join(str(column) for column in dataframe.columns)
        expected = ",".join(SOURCE_COLUMNS)
        raise CatalogSchemaError(
            f"Expected source columns in order {expected}; received {actual}"
        )
    return dataframe


def _write_csv_atomically(dataframe: pd.DataFrame, final_path: Path) -> Path:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=final_path.parent,
            prefix=f".{final_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            dataframe.to_csv(
                temporary_file,
                index=False,
                columns=OUTPUT_COLUMNS,
                lineterminator="\n",
            )
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, final_path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return final_path


def _canonical_offer_id(raw: str) -> str:
    return raw.strip()


def _business_deduplicate(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        offer_id = _canonical_offer_id(row["offer_id"])
        if offer_id:
            groups[offer_id.casefold()].append(index)

    keep = [True] * len(rows)
    removed = 0
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        group = [rows[index] for index in indexes]
        prices = [parse_price(row["price"]) for row in group]
        if any(price is None for price in prices) or len(set(prices)) != 1:
            continue
        nonempty_stocks = {
            row["stock"].strip() for row in group if row["stock"].strip()
        }
        if len(nonempty_stocks) > 1:
            continue
        first = group[0]
        if not first["stock"].strip() and nonempty_stocks:
            first["stock"] = next(iter(nonempty_stocks))
        for index in indexes[1:]:
            keep[index] = False
            removed += 1
    return [row for row, include in zip(rows, keep, strict=True) if include], removed


def clean_catalog(
    input_path: str | Path = DEFAULT_INPUT_PATH,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
) -> CleaningResult:
    """Clean the employer catalog and atomically publish its derived CSV."""
    source = _read_source(Path(input_path))
    input_rows = len(source)
    seen: set[tuple[str, ...]] = set()
    rows: list[dict[str, str]] = []
    removed_empty = 0
    removed_exact_duplicates = 0
    for raw_row in source.itertuples(index=False, name=None):
        row = {
            column: (_as_text(value) or "")
            for column, value in zip(SOURCE_COLUMNS, raw_row, strict=True)
        }
        if all(not value.strip() for value in row.values()):
            removed_empty += 1
            continue
        exact_key = tuple(row[column] for column in SOURCE_COLUMNS)
        if exact_key in seen:
            removed_exact_duplicates += 1
            continue
        seen.add(exact_key)
        rows.append(row)

    rows, business_duplicates_removed = _business_deduplicate(rows)
    output_rows: list[dict[str, str | int]] = []
    unparsed_prices = missing_brands = missing_oem = missing_quantity = 0
    for row in rows:
        price = parse_price(row["price"])
        brand = extract_brand(row["name"])
        oem = extract_oem(row["name"])
        quantity = extract_quantity(row["name"])
        unparsed_prices += price is None
        missing_brands += brand is None
        missing_oem += oem is None
        missing_quantity += quantity is None
        output_rows.append(
            {
                "offer_id": _canonical_offer_id(row["offer_id"]),
                "name": row["name"],
                "price": _format_price(price),
                "stock": row["stock"].strip(),
                "brand": brand or "",
                "oem": oem or "",
                "quantity": quantity if quantity is not None else "",
            }
        )

    output = pd.DataFrame(output_rows, columns=OUTPUT_COLUMNS)
    final_path = _write_csv_atomically(output, Path(output_path))
    return CleaningResult(
        path=final_path,
        input_rows=input_rows,
        removed_empty=removed_empty,
        removed_exact_duplicates=removed_exact_duplicates,
        business_duplicates_removed=business_duplicates_removed,
        unparsed_prices=unparsed_prices,
        missing_brands=missing_brands,
        missing_oem=missing_oem,
        missing_quantity=missing_quantity,
        output_rows=len(output_rows),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean the Task 3 employer catalog")
    parser.add_argument("input_path", nargs="?", default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)
    try:
        result = clean_catalog(args.input_path, args.output)
    except (CatalogCleaningError, OSError, UnicodeError, pd.errors.ParserError) as exc:
        print(f"Task 3 cleaning failed: {exc}", file=sys.stderr)
        return 1
    try:
        display_path = result.path.relative_to(PROJECT_ROOT)
    except ValueError:
        display_path = result.path
    print(f"Input rows: {result.input_rows}")
    print(f"Removed empty: {result.removed_empty}")
    print(f"Removed exact duplicates: {result.removed_exact_duplicates}")
    print(f"Business duplicates removed: {result.business_duplicates_removed}")
    print(f"Unparsed prices: {result.unparsed_prices}")
    print(f"Missing brands: {result.missing_brands}")
    print(f"Missing OEM: {result.missing_oem}")
    print(f"Missing quantity: {result.missing_quantity}")
    print(f"Output rows: {result.output_rows}")
    print(f"Output: {display_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CatalogCleaningError",
    "CatalogSchemaError",
    "CleaningResult",
    "DEFAULT_INPUT_PATH",
    "DEFAULT_OUTPUT_PATH",
    "KNOWN_BRANDS",
    "OUTPUT_COLUMNS",
    "SOURCE_COLUMNS",
    "clean_catalog",
    "extract_brand",
    "extract_oem",
    "extract_quantity",
    "main",
    "normalize_brand",
    "parse_price",
]

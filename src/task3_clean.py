"""Task 3: clean a controlled representative auto-parts catalog safely."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "catalog_raw.csv"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "output" / "catalog_clean.csv"
SOURCE_COLUMNS = ("description", "price")
OUTPUT_COLUMNS = ("description", "price", "brand", "oem", "quantity")
KNOWN_BRANDS = (
    "BOSCH",
    "MANN-FILTER",
    "MAHLE",
    "SAKURA",
    "KNECHT",
    "NGK",
    "GATES",
    "SKF",
    "MOBIL 1",
    "FILTRON",
    "VALEO",
    "ELRING",
)

_QUANTITY_PATTERN = re.compile(
    r"(?:"
    r"(?P<units>\d+)\s*шт\.?"
    r"|(?P<pcs>\d+)\s+pcs"
    r"|комплект\s+(?P<set_after>\d+)"
    r"|(?P<set_before>\d+)\s+комплект"
    r")\s*$",
    re.IGNORECASE,
)
_OEM_MARKER = re.compile(r"(?<!\w)OEM(?!\w)", re.IGNORECASE)


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
    """Parse a documented catalog price without using binary floating point."""
    text = _as_text(raw)
    if text is None:
        return None
    text = _collapse_whitespace(text)
    if not text or text.casefold() == "по запросу":
        return None

    numeric = re.sub(r"\s*(?:руб|р|rub)\s*$", "", text, flags=re.IGNORECASE)
    numeric = numeric.strip()
    if not numeric:
        return None

    normalized: str
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", numeric):
        # The fixture explicitly documents this European thousands/decimal form.
        normalized = numeric.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?: \d{3})+(?:,\d{1,2})?", numeric):
        normalized = numeric.replace(" ", "").replace(",", ".")
    elif re.fullmatch(r"\d+(?:[.,]\d{1,2})?", numeric):
        normalized = numeric.replace(",", ".")
    else:
        return None

    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01"))


def normalize_brand(raw: object) -> str | None:
    """Return a canonical known brand label, or ``None`` for an unknown label."""
    text = _as_text(raw)
    if text is None:
        return None
    normalized = _collapse_whitespace(text).upper()
    return normalized if normalized in KNOWN_BRANDS else None


def _brand_pattern(brand: str) -> re.Pattern[str]:
    escaped = re.escape(brand).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


_BRAND_PATTERNS = tuple((brand, _brand_pattern(brand)) for brand in KNOWN_BRANDS)


def extract_brand(description: object) -> str | None:
    """Extract one known brand using case-insensitive token boundaries."""
    text = _as_text(description)
    if text is None:
        return None
    for brand, pattern in _BRAND_PATTERNS:
        if pattern.search(text):
            return brand
    return None


def _terminal_quantity(description: str) -> tuple[int, int] | None:
    match = _QUANTITY_PATTERN.search(description)
    if match is None:
        return None
    raw_quantity = next(value for value in match.groupdict().values() if value is not None)
    return match.start(), int(raw_quantity)


def extract_quantity(description: object) -> int | None:
    """Extract only a documented terminal quantity expression."""
    text = _as_text(description)
    if text is None:
        return None
    terminal = _terminal_quantity(_collapse_whitespace(text))
    return terminal[1] if terminal is not None else None


def extract_oem(description: object) -> str | None:
    """Extract a syntactically valid OEM code after an explicit marker."""
    text = _as_text(description)
    if text is None:
        return None
    normalized = _collapse_whitespace(text)
    marker = _OEM_MARKER.search(normalized)
    terminal = _terminal_quantity(normalized)
    if marker is None:
        return None

    if terminal is None:
        candidate = normalized[marker.end() :].strip()
    elif marker.end() < terminal[0]:
        candidate = normalized[marker.end() : terminal[0]].strip()
    else:
        return None
    canonical = re.sub(r"[\s.]", "", candidate).upper()
    if (
        not canonical
        or not re.fullmatch(r"[A-Z0-9/-]+", canonical)
        or not re.search(r"\d", canonical)
    ):
        return None
    return canonical


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


def clean_catalog(
    input_path: str | Path = DEFAULT_INPUT_PATH,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
) -> CleaningResult:
    """Clean the constrained source catalog and atomically publish its CSV."""
    source = _read_source(Path(input_path))
    input_rows = len(source)
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, str | int]] = []
    removed_empty = 0
    removed_exact_duplicates = 0
    unparsed_prices = 0
    missing_brands = 0
    missing_oem = 0
    missing_quantity = 0

    for raw_row in source.itertuples(index=False, name=None):
        raw_description = _as_text(raw_row[0]) or ""
        raw_price = _as_text(raw_row[1]) or ""
        description = _normalize_description(raw_description)
        price_source = _normalize_price_source(raw_price)
        if not description and not price_source:
            removed_empty += 1
            continue

        # Duplicates are exact only: do not make whitespace/case/format variants
        # disappear merely because their cleaned display values happen to match.
        exact_key = (raw_description, raw_price)
        if exact_key in seen:
            removed_exact_duplicates += 1
            continue
        seen.add(exact_key)

        parsed_price = parse_price(price_source)
        brand = extract_brand(description)
        oem = extract_oem(description)
        quantity = extract_quantity(description)
        if parsed_price is None:
            unparsed_prices += 1
        if brand is None:
            missing_brands += 1
        if oem is None:
            missing_oem += 1
        if quantity is None:
            missing_quantity += 1
        rows.append(
            {
                "description": description,
                "price": _format_price(parsed_price),
                "brand": brand or "",
                "oem": oem or "",
                "quantity": quantity if quantity is not None else "",
            }
        )

    output = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    final_path = _write_csv_atomically(output, Path(output_path))
    return CleaningResult(
        path=final_path,
        input_rows=input_rows,
        removed_empty=removed_empty,
        removed_exact_duplicates=removed_exact_duplicates,
        business_duplicates_removed=0,
        unparsed_prices=unparsed_prices,
        missing_brands=missing_brands,
        missing_oem=missing_oem,
        missing_quantity=missing_quantity,
        output_rows=len(rows),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean the Task 3 catalog fixture")
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

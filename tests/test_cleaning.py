from __future__ import annotations

import csv
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import pytest
import src.task3_clean as task3_clean

from src.task3_clean import (
    CatalogSchemaError,
    OUTPUT_COLUMNS,
    clean_catalog,
    extract_brand,
    extract_oem,
    extract_quantity,
    main,
    normalize_brand,
    parse_price,
)


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "data" / "catalog_raw.csv"


@pytest.fixture
def work_dir() -> Iterator[Path]:
    """Use a project-local temporary directory; shared OS pytest temp is unavailable."""
    with TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
        yield Path(directory)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 500 руб", "1500.00"),
        ("1500.00", "1500.00"),
        ("1500р", "1500.00"),
        ("2 350,50 руб", "2350.50"),
        ("4 200 rub", "4200.00"),
        ("3 600 RUB", "3600.00"),
        ("2750", "2750.00"),
        ("1.500,50 руб", "1500.50"),
        ("980р", "980.00"),
        ("1799.99", "1799.99"),
        ("1 250 руб", "1250.00"),
        ("890 руб", "890.00"),
        ("1 120,00 руб", "1120.00"),
        ("5 900 руб", "5900.00"),
        ("2100 руб", "2100.00"),
        ("1 650 руб", "1650.00"),
        ("1 700 руб", "1700.00"),
    ],
)
def test_parse_price_supports_every_documented_format(raw: str, expected: str) -> None:
    assert parse_price(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", "   ", "по запросу", "1,500", "1.500", "price 1500"])
def test_parse_price_rejects_empty_query_and_ambiguous_values(raw: str) -> None:
    assert parse_price(raw) is None


@pytest.mark.parametrize(
    "brand",
    [
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
    ],
)
def test_normalize_brand_returns_known_canonical_label(brand: str) -> None:
    assert normalize_brand(brand.lower()) == brand


def test_brand_variants_and_token_boundaries() -> None:
    assert normalize_brand("Bosch") == "BOSCH"
    assert normalize_brand("unknown") is None
    assert extract_brand("Фильтр bosch OEM 1 1 шт") == "BOSCH"
    assert extract_brand("Фильтр BOSCHY OEM 1 1 шт") is None
    assert extract_brand("Масло Mobil 1 ESP") == "MOBIL 1"


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Фильтр OEM 0 451 103 316 1 шт", "0451103316"),
        ("Фильтр OEM HU 719/7 X 1шт.", "HU719/7X"),
        ("Фильтр OEM C-1104 комплект 4", "C-1104"),
        ("Свеча OEM BKR6E-11 4 pcs", "BKR6E-11"),
        ("Фильтр OEM PP 836/1 1 комплект", "PP836/1"),
        ("Фильтр OEM 0451103316", "0451103316"),
    ],
)
def test_extract_oem_canonicalizes_spaces_dots_hyphens_and_slashes(
    description: str, expected: str
) -> None:
    assert extract_oem(description) == expected


@pytest.mark.parametrize(
    "description",
    [
        "BOSCH DOT 4 1 л",
        "Mobil 1 ESP 5W-30 4 л",
        "Фильтр 0451103316 1 шт",
        "Фильтр OEM ABC",
    ],
)
def test_extract_oem_requires_marker_and_valid_code(
    description: str,
) -> None:
    assert extract_oem(description) is None


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Фильтр 1шт.", 1),
        ("Фильтр 2 шт", 2),
        ("Свеча 4 pcs", 4),
        ("Набор комплект 4", 4),
        ("Набор 1 комплект", 1),
        ("BOSCH DOT 4 1 л", None),
        ("Mobil 1 ESP 5W-30 4 л", None),
        ("Набор 2 шт запас", None),
    ],
)
def test_extract_quantity_only_accepts_documented_terminal_patterns(
    description: str, expected: int | None
) -> None:
    assert extract_quantity(description) == expected


def _write_source(path: Path, rows: list[tuple[str, str]], columns: tuple[str, ...] = ("description", "price")) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(rows)


def _read_output(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def test_clean_catalog_fixture_preserves_russian_text_and_conflicting_rows(work_dir: Path) -> None:
    output = work_dir / "catalog_clean.csv"
    result = clean_catalog(FIXTURE_PATH, output)
    rows = _read_output(output)

    assert result.input_rows == 22
    assert result.removed_empty == 2
    assert result.removed_exact_duplicates == 1
    assert result.business_duplicates_removed == 0
    assert result.output_rows == 19
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    assert [*rows[0]] == list(OUTPUT_COLUMNS)
    assert any(row["description"].startswith("Фильтр масляный BOSCH") for row in rows)
    assert any(row["brand"] == "MANN-FILTER" and row["oem"] == "HU719/7X" for row in rows)
    assert any(row["brand"] == "MOBIL 1" and not row["oem"] and not row["quantity"] for row in rows)
    assert any(row["brand"] == "FILTRON" and row["price"] == "" for row in rows)
    assert any(row["brand"] == "VALEO" and row["price"] == "" for row in rows)
    conflicts = [row for row in rows if row["oem"] == "1457429261"]
    assert [row["price"] for row in conflicts] == ["1650.00", "1700.00"]


def test_empty_rows_and_only_exact_duplicates_are_removed(work_dir: Path) -> None:
    source = work_dir / "source.csv"
    output = work_dir / "output.csv"
    _write_source(
        source,
        [
            ("  ", " "),
            (" BOSCH OEM 123 1 шт ", " 1500 руб "),
            ("BOSCH OEM 123 1 шт", "1500 руб"),
            ("BOSCH OEM 123 1 шт", "1500 руб"),
            ("BOSCH OEM 123 1 шт", "1600 руб"),
            ("Описание без цены", ""),
        ],
    )

    result = clean_catalog(source, output)
    rows = _read_output(output)

    assert result.removed_empty == 1
    assert result.removed_exact_duplicates == 1
    assert result.business_duplicates_removed == 0
    assert result.output_rows == 4
    assert [row["price"] for row in rows[:3]] == ["1500.00", "1500.00", "1600.00"]
    assert [row["description"] for row in rows[:2]] == [
        "BOSCH OEM 123 1 шт",
        "BOSCH OEM 123 1 шт",
    ]
    assert rows[-1]["description"] == "Описание без цены"
    assert rows[-1]["price"] == ""


def test_schema_must_be_exact_and_ordered(work_dir: Path) -> None:
    source = work_dir / "wrong.csv"
    _write_source(source, [("x", "1")], columns=("price", "description"))
    with pytest.raises(CatalogSchemaError, match="Expected source columns in order"):
        clean_catalog(source, work_dir / "output.csv")


def test_atomic_failure_preserves_existing_final_and_removes_temp(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = work_dir / "source.csv"
    output = work_dir / "catalog_clean.csv"
    _write_source(source, [("BOSCH OEM 123 1 шт", "1500 руб")])
    output.write_bytes(b"existing final output")

    def broken_to_csv(*args: object, **kwargs: object) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(pd.DataFrame, "to_csv", broken_to_csv)
    with pytest.raises(OSError, match="simulated write failure"):
        clean_catalog(source, output)
    assert output.read_bytes() == b"existing final output"
    assert not list(work_dir.glob(".catalog_clean.csv.*.tmp"))


def test_atomic_replace_failure_preserves_existing_final_and_removes_temp(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = work_dir / "source.csv"
    output = work_dir / "catalog_clean.csv"
    _write_source(source, [("BOSCH OEM 123 1 шт", "1500 руб")])
    output.write_bytes(b"existing final output")

    def broken_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(task3_clean.os, "replace", broken_replace)
    with pytest.raises(OSError, match="simulated replacement failure"):
        clean_catalog(source, output)
    assert output.read_bytes() == b"existing final output"
    assert not list(work_dir.glob(".catalog_clean.csv.*.tmp"))


def test_cli_reports_safe_summary_and_defaultable_output(work_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = work_dir / "clean.csv"
    assert main([str(FIXTURE_PATH), "--output", str(output)]) == 0
    captured = capsys.readouterr()
    assert "Input rows: 22" in captured.out
    assert "Removed empty: 2" in captured.out
    assert "Removed exact duplicates: 1" in captured.out
    assert "Business duplicates removed: 0" in captured.out
    assert "Output rows: 19" in captured.out
    assert "Output:" in captured.out
    assert captured.err == ""


def test_cli_reports_non_utf_input_without_traceback(work_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = work_dir / "non_utf.csv"
    source.write_bytes(b"description,price\n\xff,1500\n")

    assert main([str(source), "--output", str(work_dir / "output.csv")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Task 3 cleaning failed:" in captured.err
    assert "Traceback" not in captured.err

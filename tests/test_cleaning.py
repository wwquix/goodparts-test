from __future__ import annotations

import csv
import re
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
SUBMISSION_PATH = Path(__file__).resolve().parents[1] / "catalog_clean.csv"


@pytest.fixture
def work_dir() -> Iterator[Path]:
    with TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
        yield Path(directory)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 500 руб", "1500.00"),
        ("1500.00", "1500.00"),
        ("8 990,00", "8990.00"),
        ("250р", "250.00"),
        ("250 руб", "250.00"),
        ("1500", "1500.00"),
        ("390.50", "390.50"),
        ("3 200 р", "3200.00"),
        ("от 450 руб", "450.00"),
        ("990,00", "990.00"),
        ("3200", "3200.00"),
        ("890 руб.", "890.00"),
        ("390,5", "390.50"),
        ("350 руб", "350.00"),
        ("12 500.00", "12500.00"),
        ("250", "250.00"),
        ("1 500 rub", "1500.00"),
    ],
)
def test_parse_price_supports_every_observed_syntax(raw: str, expected: str) -> None:
    assert parse_price(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", " ", "price 1500", "1.500,00", "от примерно 450"])
def test_parse_price_leaves_unobserved_or_ambiguous_syntax_empty(raw: str) -> None:
    assert parse_price(raw) is None


def test_brands_are_canonicalized_without_guessing_models() -> None:
    assert normalize_brand("mAvIcO") == "Mavico"
    assert normalize_brand("dba") == "DBA"
    assert normalize_brand("деталиус") == "Деталиус"
    assert normalize_brand("MF1041") is None
    assert extract_brand("MAVICO модель") == "Mavico"
    assert extract_brand("DBA серия") == "DBA"
    assert extract_brand("Деталиус изделие") == "Деталиус"
    assert extract_brand("MavicoX") is None


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Деталь OEM 8200123456", "8200123456"),
        ("Деталь OEM 2101-3502090", "2101-3502090"),
        ("MV1028F", None),
        ("DBA 1234", None),
        ("Размер 600мм/400мм", None),
        ("Деталь OEM 123", None),
    ],
)
def test_oem_accepts_only_observed_high_confidence_patterns(
    description: str, expected: str | None
) -> None:
    assert extract_oem(description) == expected


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Деталь 4шт.", 4),
        ("Деталь 2 ШТ", 2),
        ("Деталь комплект 3", 3),
        ("Деталь 3 компл.", 3),
        ("Деталь 4 к-т", 4),
        ("Деталь набор 6 шт", 6),
        ("Деталь пара", 2),
        ("Деталь комплект", None),
        ("Щетка 600мм/400мм", None),
        ("Сетка 40х40", None),
    ],
)
def test_quantity_accepts_only_observed_package_meanings(
    description: str, expected: int | None
) -> None:
    assert extract_quantity(description) == expected


def _write_source(
    path: Path,
    rows: list[tuple[str, str, str, str]],
    columns: tuple[str, ...] = ("offer_id", "name", "price", "stock"),
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(rows)


def _read_output(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def test_empty_exact_and_compatible_business_duplicates_are_handled(work_dir: Path) -> None:
    source = work_dir / "source.csv"
    output = work_dir / "output.csv"
    _write_source(
        source,
        [
            (" ", "", " ", ""),
            (" MV1 ", "Mavico first 1 шт", "250", ""),
            ("MV1", "Mavico second 1 шт", "250 руб", "7"),
            ("MV1", "Mavico third 1 шт", "250.00", "7"),
            ("MV1", "Mavico third 1 шт", "250.00", "7"),
        ],
    )

    result = clean_catalog(source, output)
    rows = _read_output(output)

    assert result.removed_empty == 1
    assert result.removed_exact_duplicates == 1
    assert result.business_duplicates_removed == 2
    assert result.output_rows == 1
    assert rows == [{"offer_id": "MV1", "name": "Mavico first 1 шт", "price": "250.00", "stock": "7", "brand": "Mavico", "oem": "", "quantity": "1"}]


def test_conflicting_prices_or_stock_and_missing_ids_are_never_business_deduped(work_dir: Path) -> None:
    source = work_dir / "source.csv"
    output = work_dir / "output.csv"
    _write_source(
        source,
        [
            ("P", "one", "250", "1"),
            ("P", "two", "350", "1"),
            ("S", "one", "250", "1"),
            ("S", "two", "250", "2"),
            ("", "missing one", "250", "1"),
            ("", "missing two", "250", "1"),
        ],
    )

    result = clean_catalog(source, output)
    assert result.business_duplicates_removed == 0
    assert result.output_rows == 6


def test_schema_must_be_exact_and_ordered(work_dir: Path) -> None:
    source = work_dir / "wrong.csv"
    _write_source(source, [("x", "name", "1", "2")], columns=("price", "offer_id", "name", "stock"))
    with pytest.raises(CatalogSchemaError, match="Expected source columns in order"):
        clean_catalog(source, work_dir / "output.csv")


def test_atomic_write_failures_preserve_final_and_remove_temp(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = work_dir / "source.csv"
    output = work_dir / "catalog_clean.csv"
    _write_source(source, [("MV1", "Mavico 1 шт", "250", "1")])
    output.write_bytes(b"existing final output")

    def broken_to_csv(*args: object, **kwargs: object) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(pd.DataFrame, "to_csv", broken_to_csv)
    with pytest.raises(OSError, match="simulated write failure"):
        clean_catalog(source, output)
    assert output.read_bytes() == b"existing final output"
    assert not list(work_dir.glob(".catalog_clean.csv.*.tmp"))


def test_atomic_replace_failure_preserves_final_and_removes_temp(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = work_dir / "source.csv"
    output = work_dir / "catalog_clean.csv"
    _write_source(source, [("MV1", "Mavico 1 шт", "250", "1")])
    output.write_bytes(b"existing final output")

    def broken_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(task3_clean.os, "replace", broken_replace)
    with pytest.raises(OSError, match="simulated replacement failure"):
        clean_catalog(source, output)
    assert output.read_bytes() == b"existing final output"
    assert not list(work_dir.glob(".catalog_clean.csv.*.tmp"))


def test_cli_reports_safe_summary(work_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = work_dir / "source.csv"
    output = work_dir / "output.csv"
    _write_source(source, [("MV1", "Mavico 1 шт", "250", "1")])
    assert main([str(source), "--output", str(output)]) == 0
    captured = capsys.readouterr()
    assert "Input rows: 1" in captured.out
    assert "Output rows: 1" in captured.out
    assert captured.err == ""


def test_cli_handles_non_utf_input_without_traceback(
    work_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = work_dir / "not_utf8.csv"
    source.write_bytes(b"\xff\xfe\x00")

    assert main([str(source)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Task 3 cleaning failed:")
    assert "Traceback" not in captured.err


def test_real_employer_fixture_has_expected_19_to_12_result(work_dir: Path) -> None:
    output = work_dir / "catalog_clean.csv"
    result = clean_catalog(FIXTURE_PATH, output)
    rows = _read_output(output)

    assert result.input_rows == 19
    assert result.removed_empty == 1
    assert result.removed_exact_duplicates == 0
    assert result.business_duplicates_removed == 6
    assert result.output_rows == 12
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    assert output.read_bytes() == SUBMISSION_PATH.read_bytes()
    assert [*rows[0]] == list(OUTPUT_COLUMNS)
    assert all(
        not row["price"] or re.fullmatch(r"\d+\.\d{2}", row["price"])
        for row in rows
    )
    assert [row["stock"] for row in rows if row["offer_id"] == "MF1041"] == ["103"]
    assert {row["oem"] for row in rows if row["oem"]} == {
        "8200123456",
        "2101-3502090",
    }
    quantities_by_offer = {row["offer_id"]: row["quantity"] for row in rows}
    assert quantities_by_offer["WB01"] == ""
    assert quantities_by_offer["DBA4000"] == "2"
    assert any(row["price"] == "" for row in rows)
    normalized_offer_ids = [row["offer_id"].strip().casefold() for row in rows if row["offer_id"].strip()]
    assert len(normalized_offer_ids) == len(set(normalized_offer_ids))

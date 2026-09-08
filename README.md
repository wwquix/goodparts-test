# GoodParts test assignment

This repository has three deliberately separate workflows: an Ozon product
export, a Telegram low-stock summary, and an offline employer-catalog cleaner.
The daily command composes Tasks 1 and 2 only.

## What it does

- Task 1 reads Ozon Seller data and atomically writes a UTF-8-SIG product CSV.
- Task 2 reads one explicit Task 1 CSV and sends a Russian plain-text Telegram
  summary.
- `python -m src.run_daily` gives Task 2 the exact Task 1 output path.
- Task 3 cleans the employer-provided `data/catalog_raw.csv`; it is not part of
  the daily pipeline.

## Architecture

```text
Ozon /v3/product/list ─┐
Ozon /v3/product/info/list ─┼─> strict product_id merge ─> atomic CSV ─> Telegram summary
Ozon /v5/product/info/prices ─┤                                  │
Ozon /v4/product/info/stocks ─┘                                  └─ explicit returned path

catalog_raw.csv ─> data-first cleaner ─> catalog_clean.csv        (separate Task 3 ETL)
```

## Setup

Python 3.12 or newer is required. Ozon credentials are required for Task 1;
Telegram credentials are required for Task 2. Generated runtime files and
`.env` remain ignored.

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

Edit only the local `.env`; never commit it. The only variable names used are
`OZON_CLIENT_ID`, `OZON_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, and `LOW_STOCK_THRESHOLD`.

## Tasks 1 and 2

Run `python -m src.task1_export`. The canonical product set is
`/v3/product/list`; its opaque pagination token is forwarded without changing
it, archived items are excluded, and `/v3/product/info/list` requests are
batched. Sources are merged by `product_id`. Prices come from
`/v5/product/info/prices` (`price.price`), and stock is
`sum(max(present-reserved, 0))`. The UTF-8-SIG export contains article
(`offer_id`), name, current price, stock, and one UTC export date, then is
published with atomic `os.replace`.

The client has bounded HTTP/network handling and bounded 429 retries using
`Retry-After` when supplied or backoff otherwise. Its safe logs redact values
loaded from local `.env`; neither request credentials nor response bodies are
written to the tracked submission.

Run `python -m src.task2_summary data/output/ozon_products_YYYY-MM-DD.csv`
with an explicit export. It sends via the Telegram Bot API using the local bot
token and chat ID from `.env`. Low stock means
`stock < LOW_STOCK_THRESHOLD`; an empty stock is unknown, not zero, and the
summary marks qualifying items with exact text `ЗАКАНЧИВАЕТСЯ`.
`python -m src.run_daily` runs Task 1 before initializing Task 2 and passes the
returned path directly.

For a separate read-only API inspection, run
`python scripts/diagnose_ozon.py`. A successful diagnostic keeps its actual
response JSON only in ignored paths under `data/diagnostics/`, including
`product_list_page_1.json`, `product_list_page_2.json`, `product_info.json`,
`product_prices.json`, and `product_stocks.json` when those calls occur.

## Task 3

Run `python -m src.task3_clean data/catalog_raw.csv`. The tracked input is the
employer-provided UTF-8-SIG CSV with `offer_id,name,price,stock`; it is not a
synthetic fixture. The output retains those fields and adds `brand,oem,quantity`.

The cleaner accepts only price forms observed in this file (spaces or comma/dot
decimals, `руб`/`р`/`rub`, and `от 450 руб`); `Decimal` produces two decimal
places. `от` is a lower-bound qualifier that the required output schema cannot
retain, so its numeric lower bound is emitted and the ambiguity is documented.
Brands are only Mavico (case variants), DBA, and Деталиус. OEM is only an
explicit `OEM` followed by the observed 10-digit or `4-7`-digit shape. Quantity
is only terminal numeric `шт`, explicit numeric комплект/компл/к-т, `набор N
шт`, or `пара` (=2); bare комплекты and dimensions remain empty.

Fully blank source rows and exact raw duplicates are removed. Business
duplicates are merged only for a non-empty normalized `offer_id` whose parsed
non-empty prices agree and whose non-empty stocks agree; the first name/order
is retained and a missing first stock may be filled from that compatible group.
Price or stock conflicts, missing IDs, and uncertain derived values remain
separate or empty.

## Submission artifacts

- `catalog_clean.csv`: verified Task 3 output, UTF-8-SIG, with
  `offer_id,name,price,stock,brand,oem,quantity`.
- `ozon_products_result.csv`: verified Task 1 export copy, UTF-8-SIG, with
  `offer_id,product_id,name,price,currency,stock,exported_at`.

## Tests

```powershell
python -m pytest
python -m compileall -q src scripts tests
python -m pip check
```

Task 3 regression tests cover every observed price syntax, the three brands,
OEM and quantity positives/false positives, blank and exact rows, compatible
and conflicting business duplicates, missing IDs, atomic failures, CLI output,
and the real input's 19-to-12 result. The Task 1/2 tests use fakes at the
network boundaries.

## AI usage and verification

AI was supplied the assignment, existing architecture, relevant official Ozon
API documentation/changelog excerpts, safe response-shape and diagnostic
summaries, plus the Task 3 schema/profile and representative rows. Prompts
separated confirmed facts, engineering decisions, and assumptions. Possible
hallucinated or outdated API claims were checked against official Ozon
documentation and changelog, live read-only diagnostics when performed and
their actual ignored response JSON paths, and regression tests. No raw
diagnostic JSON was run for this submission pass; the single live check was the
read-only Task 1 export described below.
In particular, the earlier assumption that `/v3/product/info/list` alone
provided all export data was corrected: product information remains there, while
prices and stocks use `/v5/product/info/prices` and `/v4/product/info/stocks`.

A single live read-only Task 1 export was run on 2026-09-08 and its returned
CSV path was validated before producing the root submission copy. A live
Telegram smoke was not executed because Telegram credentials are absent. No
credential values or raw diagnostic responses are included in this repository.

## Limitations

Not implemented: an external scheduler, monitoring/alerts, persistent history,
or a Telegram send without configured credentials.

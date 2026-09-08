# GoodParts test assignment

This repository contains three deliberately separate, small workflows: an Ozon
product export, a Telegram low-stock summary, and a controlled catalog-cleaning
demo. The daily command composes only Tasks 1 and 2.

## What it does

- Task 1 reads Ozon Seller data and atomically writes a UTF-8-SIG product CSV.
- Task 2 reads one explicit Task 1 CSV and sends a Russian plain-text Telegram
  summary.
- `python -m src.run_daily` runs Task 1, then gives Task 2 the exact returned
  CSV path.
- Task 3 is a separate, offline catalog-cleaning ETL; it is not imported or
  run by the daily pipeline.

## Architecture

```text
Ozon /v3/product/list ─┐
Ozon /v3/product/info/list ─┼─> strict product_id merge ─> atomic CSV ─> Telegram summary
Ozon /v5/product/info/prices ─┤                                  │
Ozon /v4/product/info/stocks ─┘                                  └─ explicit returned path

catalog_raw.csv ─> cleaning rules ─> catalog_clean.csv     (separate Task 3 ETL)
```

## Requirements

Python 3.12 or newer, plus Ozon Seller credentials for Task 1 and Telegram
credentials for Task 2. Generated files, diagnostics, and `.env` stay ignored
and uncommitted.

## Setup

From a clean Windows checkout:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

Edit the local `.env`; never commit it.

## Environment variables

Only these names are used:

- `OZON_CLIENT_ID`
- `OZON_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `LOW_STOCK_THRESHOLD`

## Task 1

Run `python -m src.task1_export`. The canonical product set is
`/v3/product/list`; `archived=true` items are excluded. Product data is merged
by `product_id`. Base price is `/v5/product/info/prices` → `price.price`.
Stock is `sum(max(present-reserved, 0))`; `stocks=[]` is known zero and a missing
stock item is unknown. The export uses one UTC timestamp, UTF-8-SIG, and atomic
`os.replace` publication.

### Diagnostic v2

Run `python scripts/diagnose_ozon.py` separately for read-only Task 1 API
diagnosis. With valid Ozon credentials it writes ignored raw JSON artifacts;
clean clones intentionally contain none.

## Task 2

Run `python -m src.task2_summary data/output/ozon_products_YYYY-MM-DD.csv` for
an explicit export. Low stock means `stock < threshold`; unknown is not zero.
Messages are plain text, split into 4000-character chunks, wait about 1.05
seconds only between multiple messages, and use bounded retries.

## Daily pipeline

Run:

```powershell
python -m src.run_daily
```

It loads Ozon configuration, creates and closes `OzonClient`, exports Task 1,
then—and only after success—loads Telegram configuration, creates and closes
`TelegramClient`, and calls Task 2 with `ExportResult.path`. It never searches
for a CSV. A Task 1 failure never initializes Telegram. A Task 2 failure leaves
the completed CSV in place and exits nonzero with a safe stage-specific error.

## Task 3

Run `python -m src.task3_clean data/catalog_raw.csv`. No original
`catalog_raw.csv` was supplied; the tracked controlled representative fixture
was created for the demo. Cleaning uses `Decimal`, fixture brands only, an
explicit OEM marker, and terminal quantity patterns only. Blank rows and exact
raw-field duplicates are removed; conflicts remain and ambiguity is missing.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m compileall -q src scripts tests
.venv\Scripts\python.exe -m pip check
```

The daily-pipeline tests use fakes only. They cover the exact current-path
handoff, stage isolation/failures, stale CSV regression, and a real Task 1 CSV
plus real Task 2 parsing/chunking with fake Ozon/Telegram boundaries.

## Data and edge-case decisions

Task 1 fails on contradictory IDs rather than silently merging them. Missing
secondary data remains empty in the CSV. Task 2 retains empty stock as unknown.
Task 3 intentionally avoids business-record merging because conflicting prices
have no authoritative winner.

## AI usage and verification

AI assisted with design review, hypotheses, test suggestions, and code review.
A credentialed local diagnostic run produced five ignored JSON artifacts; clean
clones intentionally do not contain them. The diagnostic showed that separate
`/v5/product/info/prices` and `/v4/product/info/stocks` checks were needed after
the initial `/v3/product/info/list` hypothesis; relevant Ozon API documentation
and changelog were cross-checked. A full credentialed
Ozon-to-Telegram end-to-end run was not executed in this checkout because
Telegram credentials are absent; this README makes no fake success claim.

## Limitations and production improvements

Not implemented: an external scheduler, structured monitoring, metrics/alerts,
persistent history, and more integration coverage.

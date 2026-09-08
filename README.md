# GoodParts test assignment

## Diagnostic setup

Requires Python 3.12 or newer.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Open `.env` and manually add the real `OZON_CLIENT_ID` and `OZON_API_KEY` values.
Do not commit this file.

Run Diagnostic v2 from the project root:

```powershell
python scripts/diagnose_ozon.py
```

Diagnostic v2 checks the live, read-only Ozon Seller API responses for:

- `/v3/product/list`, including a two-page pagination check with the exact
  opaque `last_id` returned by page 1;
- `/v3/product/info/list` for one real product from the list response;
- `/v5/product/info/prices` for the same real product;
- `/v4/product/info/stocks` for the same real product.

Complete pretty-printed raw JSON responses are saved locally in
`data/diagnostics/`. The diagnostic does not normalize prices or aggregate stocks.

## Production Ozon access layer

`src/config.py` loads and validates the two Ozon credentials. The synchronous
`OzonClient` owns connection lifecycle, timeout and bounded transient retries,
strict response validation, product pagination, product-info batching, and cursor
pagination for raw price and stock items.

## Task 1: Ozon to CSV

Run the complete Task 1 pipeline from the project root:

```powershell
python -m src.task1_export
```

The command takes `/v3/product/list` as the canonical product set, excludes
items whose `archived` value is `true`, and merges product info, prices, and
stocks strictly by `product_id`. Contradictory `offer_id` values and duplicate
product IDs fail the export instead of being merged silently. A product missing
from a secondary source remains in the CSV with the corresponding value empty.

CSV files are written to `data/output/ozon_products_YYYY-MM-DD.csv` with these
columns, in order:

```text
offer_id,product_id,name,price,currency,stock,exported_at
```

The encoding is UTF-8 with BOM (`utf-8-sig`) for Excel on Windows. One
timezone-aware UTC timestamp is used for every row and the filename. The file is
first completed in the same output directory and then published with an atomic
replacement, so a successful second run on the same UTC date replaces the first
file without exposing a partial CSV.

### Price decision

The CSV price is `/v5/product/info/prices` → `price.price`. Promotional fields
such as `marketing_seller_price` are not used as the base price. RUB values are
formatted to two decimal places without currency conversion. A missing or
invalid price becomes an empty value and is reported as a warning.

### Stock decision

The CSV stock value is the project's business interpretation of available
stock, not a universal Ozon field:

```text
sum(max(present - reserved, 0))
```

The sum covers all stock records returned by Ozon for the product. An existing
stock item with `stocks=[]` means known zero stock (`0`). A completely missing
stock item means unknown stock and is exported as an empty value. Malformed
stock records fail the export.

Install the test dependency and run the unit tests:

```powershell
python -m pip install -e ".[test]"
python -m pytest
```

Diagnostic v2, the production Ozon access layer, and Task 1 Ozon-to-CSV export
are implemented. Telegram delivery (Task 2), Task 3 cleaning, `run_daily`,
scheduling, Docker, and a database are intentionally not implemented yet.

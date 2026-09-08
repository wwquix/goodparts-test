"""Run a small, fact-finding diagnostic against the Ozon Seller API."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS_DIR = PROJECT_ROOT / "data" / "diagnostics"
BASE_URL = "https://api-seller.ozon.ru"
REQUEST_TIMEOUT_SECONDS = 20.0
PAGE_LIMIT = 2
SAFE_RESPONSE_EXCERPT_LENGTH = 500
_INVALID_JSON = object()


class DiagnosticError(RuntimeError):
    """Expected diagnostic failure with a safe, user-facing message."""


def load_credentials() -> tuple[str, str]:
    """Load required credentials without logging either value."""
    load_dotenv(PROJECT_ROOT / ".env")

    client_id = os.getenv("OZON_CLIENT_ID", "").strip()
    api_key = os.getenv("OZON_API_KEY", "").strip()
    missing = [
        name
        for name, value in (
            ("OZON_CLIENT_ID", client_id),
            ("OZON_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        joined = ", ".join(missing)
        raise DiagnosticError(
            f"Missing required environment variable(s): {joined}. "
            "Create .env from .env.example and add the real Ozon credentials."
        )

    return client_id, api_key


def redact_secrets(text: str, secrets: tuple[str, ...]) -> str:
    """Remove exact credential values from an error body before displaying it."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def redact_response_excerpt(text: str, secrets: tuple[str, ...]) -> str:
    """Redact the complete body before bounding user-facing error output."""
    return redact_secrets(text, secrets)[:SAFE_RESPONSE_EXCERPT_LENGTH]


def ensure_credentials_absent(payload: Any, secrets: tuple[str, ...]) -> None:
    """Refuse to print or save a response if it unexpectedly contains credentials."""
    serialized = json.dumps(payload, ensure_ascii=False)
    if any(secret and secret in serialized for secret in secrets):
        raise DiagnosticError(
            "The API response unexpectedly contains a credential value; "
            "refusing to print or save it."
        )


def post_json(
    client: httpx.Client,
    path: str,
    payload: dict[str, Any],
    secrets: tuple[str, ...],
) -> tuple[int, Any]:
    """Make one diagnostic POST and return its status and unmodified JSON body."""
    network_error: tuple[str, str] | None = None
    timed_out = False
    try:
        response = client.post(path, json=payload)
    except httpx.TimeoutException:
        timed_out = True
    except httpx.RequestError as exc:
        network_error = (type(exc).__name__, redact_secrets(str(exc), secrets))

    if timed_out:
        raise DiagnosticError(
            f"Request to {path} timed out after {REQUEST_TIMEOUT_SECONDS:g} seconds."
        )
    if network_error is not None:
        error_type, safe_details = network_error
        raise DiagnosticError(
            f"Network error while requesting {path}: {error_type}: {safe_details}"
        )

    if response.is_error:
        safe_body = redact_response_excerpt(response.text, secrets)
        raise DiagnosticError(
            f"HTTP error from {path}: status {response.status_code}\n"
            f"Response body:\n{safe_body}"
        )

    try:
        data = response.json()
    except ValueError:
        data = _INVALID_JSON
    if data is _INVALID_JSON:
        safe_body = redact_response_excerpt(response.text, secrets)
        raise DiagnosticError(
            f"{path} returned HTTP {response.status_code}, but the body is not JSON.\n"
            f"Response body:\n{safe_body}"
        )

    ensure_credentials_absent(data, secrets)
    return response.status_code, data


def json_type(value: Any) -> str:
    """Return the value's JSON type without coercing it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def child_path(parent: str, key: str) -> str:
    """Build an unambiguous JSON path for an object key."""
    if key.isidentifier():
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key, ensure_ascii=False)}]"


def iter_fields(payload: Any, path: str = "$") -> list[tuple[str, str, Any]]:
    """List every object field with its actual JSON path and value."""
    fields: list[tuple[str, str, Any]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            field_path = child_path(path, key)
            fields.append((field_path, key, value))
            fields.extend(iter_fields(value, field_path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            fields.extend(iter_fields(value, f"{path}[{index}]"))
    return fields


def find_list_fields(payload: Any, key: str) -> list[tuple[str, list[Any]]]:
    """Find every exact field name whose observed value is a JSON array."""
    return [
        (path, value)
        for path, field_name, value in iter_fields(payload)
        if field_name == key and isinstance(value, list)
    ]


def summarize_value(value: Any) -> str:
    """Render scalar facts while keeping object and array summaries compact."""
    if isinstance(value, dict):
        keys = json.dumps(list(value), ensure_ascii=False)
        return f"object keys={keys}"
    if isinstance(value, list):
        return f"array length={len(value)}"
    return json.dumps(value, ensure_ascii=False)


def print_matching_fields(
    payload: Any,
    label: str,
    *,
    exact_names: tuple[str, ...] = (),
    name_fragments: tuple[str, ...] = (),
) -> None:
    """Print only fields that are physically present in the response."""
    exact = set(exact_names)
    fragments = tuple(fragment.lower() for fragment in name_fragments)
    matches = [
        (path, value)
        for path, key, value in iter_fields(payload)
        if key in exact or any(fragment in key.lower() for fragment in fragments)
    ]

    if not matches:
        print(f"{label}: none present")
        return

    print(f"{label}:")
    for path, value in matches:
        print(f"  {path}: type={json_type(value)}, value={summarize_value(value)}")


def require_product_list_items(payload: Any, page_label: str) -> list[Any]:
    """Use the exact v3 product-list path and fail visibly if it changes."""
    if not isinstance(payload, dict):
        raise DiagnosticError(
            f"{page_label} is not a JSON object; expected items at $.result.items."
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise DiagnosticError(
            f"{page_label} has no object at $.result; raw response was saved."
        )
    items = result.get("items")
    if not isinstance(items, list):
        raise DiagnosticError(
            f"{page_label} has no array at $.result.items; raw response was saved."
        )
    return items


def save_response(filename: str, payload: Any, secrets: tuple[str, ...]) -> Path:
    """Persist the complete, unmodified JSON response after a secret-safety check."""
    ensure_credentials_absent(payload, secrets)
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    path = DIAGNOSTICS_DIR / filename
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def print_response(
    label: str,
    status_code: int,
    payload: Any,
    secrets: tuple[str, ...],
) -> None:
    """Pretty-print a response and the facts discoverable without normalization."""
    ensure_credentials_absent(payload, secrets)
    print(f"\n=== {label} ===")
    print(f"HTTP status: {status_code}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if isinstance(payload, dict):
        print(f"Top-level keys: {json.dumps(list(payload), ensure_ascii=False)}")
    else:
        print(f"Top-level type: {json_type(payload)}")

    item_fields = find_list_fields(payload, "items")
    if not item_fields:
        print("List-valued items paths: none present")
    for path, items in item_fields:
        print(f"Items path: {path}; count={len(items)}")
        for index, item in enumerate(items):
            if isinstance(item, dict):
                field_types = {
                    key: json_type(value) for key, value in item.items()
                }
                print(
                    f"  {path}[{index}] fields: "
                    f"{json.dumps(field_types, ensure_ascii=False)}"
                )
            else:
                print(f"  {path}[{index}] type: {json_type(item)}")

    print_matching_fields(
        payload,
        "Identifiers",
        exact_names=("product_id", "offer_id"),
    )
    print_matching_fields(
        payload,
        "Continuation fields",
        exact_names=("last_id", "cursor"),
    )


def print_request(path: str, payload: Any, secrets: tuple[str, ...]) -> None:
    """Print the exact safe request body without headers."""
    ensure_credentials_absent(payload, secrets)
    print(f"\nRequest body for POST {path}:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def select_product_identifier(
    items: list[Any],
) -> tuple[str, str | int] | None:
    """Prefer the first real offer_id, falling back to a real product_id."""
    products = [item for item in items if isinstance(item, dict)]

    for product in products:
        offer_id = product.get("offer_id")
        if isinstance(offer_id, str) and offer_id.strip():
            return "offer_id", offer_id

    for product in products:
        product_id = product.get("product_id")
        if isinstance(product_id, (str, int)) and not isinstance(product_id, bool):
            if str(product_id).strip():
                return "product_id", product_id

    return None


def item_identity(item: Any) -> tuple[str, str | int] | None:
    """Describe a list item by an observed identifier, without inventing one."""
    if not isinstance(item, dict):
        return None
    offer_id = item.get("offer_id")
    if isinstance(offer_id, str) and offer_id.strip():
        return "offer_id", offer_id
    product_id = item.get("product_id")
    if isinstance(product_id, (str, int)) and not isinstance(product_id, bool):
        if str(product_id).strip():
            return "product_id", product_id
    return None


def print_page_2_observation(page_1_items: list[Any], page_2_items: list[Any]) -> None:
    """Report what the continuation request actually returned."""
    page_1_ids = {identity for item in page_1_items if (identity := item_identity(item))}
    page_2_ids = {identity for item in page_2_items if (identity := item_identity(item))}
    overlap = page_1_ids & page_2_ids
    print("\nProduct-list pagination observation:")
    print("  Page 2 requested with the exact page 1 last_id: yes")
    print(f"  Page 2 returned items: {len(page_2_items)}")
    print(f"  Identifier overlap with page 1: {len(overlap)}")


def print_stock_record_counts(payload: Any) -> None:
    """Report observed stock arrays without aggregating quantities."""
    stock_fields = find_list_fields(payload, "stocks")
    if not stock_fields:
        print("Stock records: none present")
        return
    for path, records in stock_fields:
        print(f"Stock records at {path}: {len(records)}")


def run_diagnostic() -> None:
    """Fetch raw facts from four read-only Ozon Seller API endpoints."""
    client_id, api_key = load_credentials()
    secrets = (client_id, api_key)
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }

    selected_items: list[Any] = []
    with httpx.Client(
        base_url=BASE_URL,
        headers=headers,
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
    ) as client:
        page_1_payload = {
            "filter": {"visibility": "ALL"},
            "limit": PAGE_LIMIT,
        }
        print_request("/v3/product/list", page_1_payload, secrets)
        page_1_status, page_1 = post_json(
            client,
            "/v3/product/list",
            page_1_payload,
            secrets,
        )
        print_response(
            "POST /v3/product/list — page 1", page_1_status, page_1, secrets
        )
        print_matching_fields(
            page_1,
            "Archived-related fields",
            name_fragments=("archiv",),
        )
        page_1_path = save_response("product_list_page_1.json", page_1, secrets)
        print(f"Saved: {page_1_path.relative_to(PROJECT_ROOT)}")
        page_1_items = require_product_list_items(page_1, "Product-list page 1")
        selected_items.extend(page_1_items)

        result = page_1["result"]
        last_id = result.get("last_id")
        if last_id is not None and last_id != "":
            page_2_payload = {
                "filter": {"visibility": "ALL"},
                "last_id": last_id,
                "limit": PAGE_LIMIT,
            }
            print_request("/v3/product/list", page_2_payload, secrets)
            page_2_status, page_2 = post_json(
                client,
                "/v3/product/list",
                page_2_payload,
                secrets,
            )
            print_response(
                "POST /v3/product/list — page 2", page_2_status, page_2, secrets
            )
            print_matching_fields(
                page_2,
                "Archived-related fields",
                name_fragments=("archiv",),
            )
            page_2_path = save_response(
                "product_list_page_2.json", page_2, secrets
            )
            print(f"Saved: {page_2_path.relative_to(PROJECT_ROOT)}")
            page_2_items = require_product_list_items(
                page_2, "Product-list page 2"
            )
            selected_items.extend(page_2_items)
            print_page_2_observation(page_1_items, page_2_items)
        else:
            print("\nNo usable last_id was returned; page 2 was not requested.")

        print(
            "Visibility=ALL archive semantics: not conclusively verified by a "
            "small response from one account."
        )

        identifier = select_product_identifier(selected_items)
        if identifier is None:
            raise DiagnosticError(
                "No usable offer_id or product_id was found in the real product list "
                "response; dependent product diagnostics were not requested."
            )

        identifier_name, identifier_value = identifier
        print(f"\nUsing a real {identifier_name} from the product list response.")

        # The endpoint schema accepts an array containing one identifier type and
        # describes product_id items as int64 strings.
        request_identifier = (
            str(identifier_value)
            if identifier_name == "product_id"
            else identifier_value
        )
        failed_endpoints: list[str] = []

        info_payload = {identifier_name: [request_identifier]}
        print_request("/v3/product/info/list", info_payload, secrets)
        try:
            info_status, product_info = post_json(
                client,
                "/v3/product/info/list",
                info_payload,
                secrets,
            )
        except DiagnosticError as exc:
            failed_endpoints.append("/v3/product/info/list")
            print(f"Diagnostic request failed: {exc}", file=sys.stderr)
        else:
            print_response(
                "POST /v3/product/info/list — one product",
                info_status,
                product_info,
                secrets,
            )
            print_matching_fields(
                product_info,
                "Name fields",
                exact_names=("name",),
            )
            print_matching_fields(
                product_info,
                "Archived-related fields",
                name_fragments=("archiv",),
            )
            print_matching_fields(
                product_info,
                "Price-related fields",
                name_fragments=("price",),
            )
            print_matching_fields(
                product_info,
                "Stock-related fields",
                name_fragments=("stock",),
            )
            info_path = save_response("product_info.json", product_info, secrets)
            print(f"Saved: {info_path.relative_to(PROJECT_ROOT)}")

        prices_payload = {
            "cursor": "",
            "filter": {identifier_name: [request_identifier]},
            "limit": 1,
        }
        print_request("/v5/product/info/prices", prices_payload, secrets)
        try:
            prices_status, product_prices = post_json(
                client,
                "/v5/product/info/prices",
                prices_payload,
                secrets,
            )
        except DiagnosticError as exc:
            failed_endpoints.append("/v5/product/info/prices")
            print(f"Diagnostic request failed: {exc}", file=sys.stderr)
        else:
            print_response(
                "POST /v5/product/info/prices — one product",
                prices_status,
                product_prices,
                secrets,
            )
            print_matching_fields(
                product_prices,
                "Price-related fields",
                name_fragments=("price",),
            )
            prices_path = save_response(
                "product_prices.json", product_prices, secrets
            )
            print(f"Saved: {prices_path.relative_to(PROJECT_ROOT)}")

        stocks_payload = {
            "cursor": "",
            "filter": {identifier_name: [request_identifier]},
            "limit": 1,
        }
        print_request("/v4/product/info/stocks", stocks_payload, secrets)
        try:
            stocks_status, product_stocks = post_json(
                client,
                "/v4/product/info/stocks",
                stocks_payload,
                secrets,
            )
        except DiagnosticError as exc:
            failed_endpoints.append("/v4/product/info/stocks")
            print(f"Diagnostic request failed: {exc}", file=sys.stderr)
        else:
            print_response(
                "POST /v4/product/info/stocks — one product",
                stocks_status,
                product_stocks,
                secrets,
            )
            print_stock_record_counts(product_stocks)
            print_matching_fields(
                product_stocks,
                "Stock detail fields",
                exact_names=("present", "reserved"),
                name_fragments=("warehouse", "source", "type"),
            )
            stocks_path = save_response(
                "product_stocks.json", product_stocks, secrets
            )
            print(f"Saved: {stocks_path.relative_to(PROJECT_ROOT)}")

        if failed_endpoints:
            failed = ", ".join(failed_endpoints)
            raise DiagnosticError(
                f"Completed independent product checks where possible; failed: {failed}."
            )


def main() -> int:
    try:
        run_diagnostic()
    except DiagnosticError as exc:
        print(f"Diagnostic failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
    try:
        response = client.post(path, json=payload)
    except httpx.TimeoutException as exc:
        raise DiagnosticError(
            f"Request to {path} timed out after {REQUEST_TIMEOUT_SECONDS:g} seconds."
        ) from exc
    except httpx.RequestError as exc:
        safe_details = redact_secrets(str(exc), secrets)
        raise DiagnosticError(
            f"Network error while requesting {path}: "
            f"{type(exc).__name__}: {safe_details}"
        ) from exc

    if response.is_error:
        safe_body = redact_secrets(response.text, secrets)
        raise DiagnosticError(
            f"HTTP error from {path}: status {response.status_code}\n"
            f"Response body:\n{safe_body}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        safe_body = redact_secrets(response.text, secrets)
        raise DiagnosticError(
            f"{path} returned HTTP {response.status_code}, but the body is not JSON.\n"
            f"Response body:\n{safe_body}"
        ) from exc

    ensure_credentials_absent(data, secrets)
    return response.status_code, data


def find_first_value(payload: Any, key: str) -> Any | None:
    """Find the first value for an exact key without assuming response nesting."""
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = find_first_value(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_first_value(value, key)
            if found is not None:
                return found
    return None


def find_items(payload: Any) -> list[Any] | None:
    """Return an actual list stored under an ``items`` key, wherever it occurs."""
    items = find_first_value(payload, "items")
    return items if isinstance(items, list) else None


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

    items = find_items(payload)
    if items is None:
        print("Items count: unavailable (no list-valued 'items' field found)")
    else:
        print(f"Items count: {len(items)}")

    last_id = find_first_value(payload, "last_id")
    if last_id is None:
        print("last_id: not present")
    else:
        print(f"last_id: {last_id}")


def select_product_identifier(
    pages: list[Any],
) -> tuple[str, str | int] | None:
    """Prefer the first real offer_id, falling back to a real product_id."""
    products: list[dict[str, Any]] = []
    for page in pages:
        items = find_items(page)
        if items:
            products.extend(item for item in items if isinstance(item, dict))

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


def run_diagnostic() -> None:
    """Fetch two small product pages when possible, then one raw product detail."""
    client_id, api_key = load_credentials()
    secrets = (client_id, api_key)
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }

    pages: list[Any] = []
    with httpx.Client(
        base_url=BASE_URL,
        headers=headers,
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
    ) as client:
        page_1_status, page_1 = post_json(
            client,
            "/v3/product/list",
            {"limit": PAGE_LIMIT},
            secrets,
        )
        pages.append(page_1)
        print_response(
            "POST /v3/product/list — page 1", page_1_status, page_1, secrets
        )
        page_1_path = save_response("product_list_page_1.json", page_1, secrets)
        print(f"Saved: {page_1_path.relative_to(PROJECT_ROOT)}")

        last_id = find_first_value(page_1, "last_id")
        if last_id is not None and last_id != "":
            page_2_status, page_2 = post_json(
                client,
                "/v3/product/list",
                {"limit": PAGE_LIMIT, "last_id": last_id},
                secrets,
            )
            pages.append(page_2)
            print_response(
                "POST /v3/product/list — page 2", page_2_status, page_2, secrets
            )
            page_2_path = save_response(
                "product_list_page_2.json", page_2, secrets
            )
            print(f"Saved: {page_2_path.relative_to(PROJECT_ROOT)}")
        else:
            print("\nNo usable last_id was returned; page 2 was not requested.")

        identifier = select_product_identifier(pages)
        if identifier is None:
            raise DiagnosticError(
                "No usable offer_id or product_id was found in the real product list "
                "response; /v3/product/info/list was not requested."
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
        info_payload = {identifier_name: [request_identifier]}
        info_status, product_info = post_json(
            client,
            "/v3/product/info/list",
            info_payload,
            secrets,
        )
        print_response(
            "POST /v3/product/info/list — one product",
            info_status,
            product_info,
            secrets,
        )
        info_path = save_response("product_info.json", product_info, secrets)
        print(f"Saved: {info_path.relative_to(PROJECT_ROOT)}")


def main() -> int:
    try:
        run_diagnostic()
    except DiagnosticError as exc:
        print(f"Diagnostic failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the existing Task 1 export and Task 2 Telegram summary in order."""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import (
    OzonConfig,
    OzonConfigError,
    TelegramConfig,
    TelegramConfigError,
    load_ozon_config,
    load_telegram_config,
)
from .ozon_client import OzonClient
from .task1_export import ExportResult, OzonProductSource, export_ozon_products
from .task2_summary import SummaryResult, TelegramMessageSender, send_summary
from .telegram_client import TelegramClient


class _OzonClientFactory(Protocol):
    def __call__(
        self, config: OzonConfig
    ) -> AbstractContextManager[OzonProductSource]: ...


class _TelegramClientFactory(Protocol):
    def __call__(
        self, config: TelegramConfig
    ) -> AbstractContextManager[TelegramMessageSender]: ...


Exporter = Callable[[OzonProductSource], ExportResult]
SummarySender = Callable[[Path, TelegramMessageSender, int], SummaryResult]


@dataclass(frozen=True, slots=True)
class DailyRunResult:
    """The safe results of a fully completed daily Task 1 + Task 2 run."""

    export: ExportResult
    summary: SummaryResult


class DailyRunError(RuntimeError):
    """A safe, stage-specific daily-pipeline failure."""

    def __init__(
        self,
        stage: str,
        detail: str,
        *,
        export_result: ExportResult | None = None,
    ) -> None:
        self.stage = stage
        self.detail = detail
        self.export_result = export_result
        super().__init__(f"{stage} failed: {detail}")


def _safe_detail(error: Exception) -> str:
    """Keep known configuration diagnostics while withholding arbitrary errors."""
    if isinstance(error, (OzonConfigError, TelegramConfigError)):
        return str(error)
    return "unexpected error; details omitted for credential safety"


def run_daily(
    *,
    load_ozon: Callable[[], OzonConfig] = load_ozon_config,
    ozon_client_factory: _OzonClientFactory = OzonClient,
    exporter: Exporter = export_ozon_products,
    load_telegram: Callable[[], TelegramConfig] = load_telegram_config,
    telegram_client_factory: _TelegramClientFactory = TelegramClient,
    summary_sender: SummarySender = send_summary,
) -> DailyRunResult:
    """Export Ozon products, then send a summary for exactly that CSV path."""
    ozon_failure: DailyRunError | None = None
    try:
        ozon_config = load_ozon()
        with ozon_client_factory(ozon_config) as ozon_client:
            export_result = exporter(ozon_client)
    except Exception as error:
        ozon_failure = DailyRunError("Ozon export", _safe_detail(error))
    if ozon_failure is not None:
        raise ozon_failure

    telegram_failure: DailyRunError | None = None
    try:
        telegram_config = load_telegram()
        with telegram_client_factory(telegram_config) as telegram_client:
            summary_result = summary_sender(
                export_result.path,
                telegram_client,
                telegram_config.low_stock_threshold,
            )
    except Exception as error:
        telegram_failure = DailyRunError(
            "Telegram summary",
            _safe_detail(error),
            export_result=export_result,
        )
    if telegram_failure is not None:
        raise telegram_failure

    return DailyRunResult(export=export_result, summary=summary_result)


def main(run: Callable[[], DailyRunResult] = run_daily) -> int:
    """Run the daily pipeline and print only safe operational counters."""
    try:
        result = run()
    except DailyRunError as error:
        print(error, file=sys.stderr)
        if error.export_result is not None:
            print(f"CSV remains: {error.export_result.path}", file=sys.stderr)
        return 1

    print("Ozon export completed")
    print(f"CSV: {result.export.path}")
    print(f"Products: {result.export.products}")
    print()
    print("Telegram summary sent")
    print(f"Low stock: {result.summary.low_stock}")
    print(f"Messages: {result.summary.messages}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

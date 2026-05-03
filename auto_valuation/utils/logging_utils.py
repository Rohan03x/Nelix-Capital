"""
utils/logging_utils.py — Structured audit-trail logger.

Reference: Architecture Plan Part 33.3.

Usage:
    from auto_valuation.utils.logging_utils import get_logger
    log = get_logger("AAPL")
    log.info("UFCF computed", extra={"value": 1234.5})
    log.warning("TV > 80% of EV")
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Log record formatter ───────────────────────────────────────────────────────

class _JSONFormatter(logging.Formatter):
    """Writes one JSON object per line to the log file."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "ticker":  getattr(record, "ticker", ""),
            "module":  record.module,
            "msg":     record.getMessage(),
        }
        # Merge any 'extra' dict keys the caller passed in
        for key, val in record.__dict__.items():
            if key not in logging.LogRecord.__dict__ and key not in payload:
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    """Human-readable format for stdout."""

    COLOURS = {
        "DEBUG":    "\033[36m",   # cyan
        "INFO":     "\033[0m",    # default
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self.COLOURS.get(record.levelname, "")
        ticker = getattr(record, "ticker", "")
        prefix = f"[{ticker}] " if ticker else ""
        return f"{colour}{record.levelname:<8}{self.RESET} {prefix}{record.getMessage()}"


# ── Public API ─────────────────────────────────────────────────────────────────

def get_logger(ticker: str = "", logs_dir: str | Path = "logs") -> logging.Logger:
    """
    Return (or create) a logger for the given ticker.

    Writes structured JSON to  logs/{TICKER}_{date}.jsonl
    Prints human-readable output to stdout.
    """
    name = f"avs.{ticker.upper()}" if ticker else "avs"
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger   # already configured for this session

    logger.setLevel(logging.DEBUG)

    # ── File handler (JSON-lines) ───────────────────────────────────────────
    log_dir = Path(logs_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    tag = f"{ticker.upper()}_{date_str}" if ticker else f"avs_{date_str}"
    log_path = log_dir / f"{tag}.jsonl"

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_JSONFormatter())
    logger.addHandler(fh)

    # ── Console handler ─────────────────────────────────────────────────────
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(_ConsoleFormatter())
    logger.addHandler(ch)

    # Attach ticker as a default extra so every record carries it
    logger = logging.LoggerAdapter(logger, {"ticker": ticker.upper()})  # type: ignore[assignment]

    return logger


def log_run_header(logger: Any, ticker: str, version: str, scenario: str) -> None:
    """Log a standard header at the start of each valuation run."""
    logger.info(
        f"=== Automated Valuation System v{version} | "
        f"Ticker: {ticker} | Scenario: {scenario} | "
        f"Run: {datetime.now(timezone.utc).isoformat()} ==="
    )


def setup_logging(
    ticker: str = "",
    logs_dir: str | Path = "logs",
    level: int = logging.INFO,
    log_to_file: bool = True,
) -> logging.Logger:
    """
    Configure and return a logger for the given *ticker*.

    This is a thin wrapper around get_logger() that also accepts a
    logging *level* argument for callers that want to override the
    default INFO threshold.

    Reference: Architecture Plan Part 33.3.
    """
    logger = get_logger(ticker=ticker, logs_dir=logs_dir)
    # Adjust the level on the underlying logger object
    underlying = getattr(logger, "logger", logger)  # unwrap LoggerAdapter if needed
    underlying.setLevel(level)
    return logger

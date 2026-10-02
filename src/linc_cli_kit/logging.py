"""stderr-only logging, the sentinel pattern."""

from __future__ import annotations

import logging
import os
import sys


def configure_stderr_logging(level: str | None = None) -> None:
    name = (level or os.environ.get("LINC_LOG_LEVEL") or "WARNING").upper()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, name, logging.WARNING))


def configure_structlog_stderr() -> bool:
    """Point structlog's default logger factory at stderr; no-op if structlog is absent.

    Returns True if structlog was configured, False if it is not installed. The kit has no
    hard dependency on structlog — the import happens inside the function so adopters that
    do not use it pay nothing. Call this ABOVE your package imports: modules bind their
    logger at import time, so a later call leaves already-bound loggers on stdout.
    """
    try:
        import structlog
    except ImportError:
        return False
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    return True

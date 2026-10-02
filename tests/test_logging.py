import logging
import sys

import pytest

from linc_cli_kit import configure_stderr_logging, configure_structlog_stderr


def test_configure_stderr_logging_is_exported_and_targets_stderr():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        configure_stderr_logging("DEBUG")
        assert len(root.handlers) == 1
        assert root.handlers[0].stream is sys.stderr
        assert root.level == logging.DEBUG
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def test_configure_structlog_stderr_returns_false_when_structlog_absent(monkeypatch):
    # None in sys.modules makes `import structlog` raise ImportError — the shape an adopter
    # without structlog installed sees. It must not blow up the CLI's startup path.
    monkeypatch.setitem(sys.modules, "structlog", None)
    assert configure_structlog_stderr() is False


def test_configure_structlog_stderr_points_the_factory_at_stderr():
    structlog = pytest.importorskip("structlog")
    try:
        assert configure_structlog_stderr() is True
        factory = structlog.get_config()["logger_factory"]
        assert isinstance(factory, structlog.PrintLoggerFactory)
        assert factory._file is sys.stderr
    finally:
        structlog.reset_defaults()

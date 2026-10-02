"""linc-cli-kit — FastMCP registry → Click commands under one output contract.

Import cost is paid by every adopter CLI invocation, `--help` included, so the conformance
suite (`assert_cli_parity`, `ParityReport`: click.testing + unittest.mock) loads on first
attribute access (PEP 562), and `transport` imports httpx/anyio only when a call needs them.
"""

from typing import TYPE_CHECKING, Any

from linc_cli_kit.errors import ToolError, UsageError, parse_error_envelope
from linc_cli_kit.logging import configure_stderr_logging, configure_structlog_stderr
from linc_cli_kit.mount import (
    current_mode,
    current_output,
    current_timeout,
    install_globals,
    mount_tools,
)
from linc_cli_kit.output import emit, emit_exception, heartbeat, json_default, to_json
from linc_cli_kit.registry import ToolSpec, registry_of
from linc_cli_kit.transport import HttpDaemonTransport, LocalTransport

if TYPE_CHECKING:
    from linc_cli_kit.testing import ParityReport, assert_cli_parity

_LAZY = {"ParityReport": "linc_cli_kit.testing", "assert_cli_parity": "linc_cli_kit.testing"}

__all__ = [
    "HttpDaemonTransport",
    "LocalTransport",
    "ParityReport",
    "ToolError",
    "ToolSpec",
    "UsageError",
    "assert_cli_parity",
    "configure_stderr_logging",
    "configure_structlog_stderr",
    "current_mode",
    "current_output",
    "current_timeout",
    "emit",
    "emit_exception",
    "heartbeat",
    "install_globals",
    "json_default",
    "mount_tools",
    "parse_error_envelope",
    "registry_of",
    "to_json",
]


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # cache: later lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

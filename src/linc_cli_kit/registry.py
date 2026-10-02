"""The only module allowed to touch FastMCP's private tool manager.

`_tool_manager` is private SDK API (renamed in the SDK's v2 line). Everything
else in the kit consumes ToolSpec, so an SDK change is fixed in one place.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]
    is_async: bool


def registry_of(server: Any) -> list[ToolSpec]:
    """Return every tool registered on a FastMCP server as a ToolSpec."""
    manager = getattr(server, "_tool_manager", None)
    list_tools = getattr(manager, "list_tools", None)
    if manager is None or list_tools is None:
        raise TypeError(
            "registry_of() expects a FastMCP server with a FastMCP tool registry "
            f"(_tool_manager.list_tools); got {type(server).__name__}"
        )
    specs: list[ToolSpec] = []
    for tool in list_tools():
        for attr in ("name", "fn", "parameters", "is_async"):
            if not hasattr(tool, attr):
                raise TypeError(
                    f"FastMCP Tool lacks '{attr}' — SDK shape changed; update registry.py"
                )
        specs.append(
            ToolSpec(
                name=tool.name,
                description=(tool.description or "").strip(),
                parameters=dict(tool.parameters or {}),
                fn=tool.fn,
                is_async=bool(tool.is_async),
            )
        )
    return specs

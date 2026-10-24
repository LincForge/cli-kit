"""The only module allowed to touch FastMCP's private tool manager.

`_tool_manager` is private SDK API (renamed in the SDK's v2 line). Everything
else in the kit consumes ToolSpec, so an SDK change is fixed in one place.
"""

from __future__ import annotations

import contextlib
import functools
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]
    is_async: bool


def _tool_manager(server: Any, caller: str) -> tuple[Any, Callable[[], list[Any]]]:
    manager = getattr(server, "_tool_manager", None)
    list_tools = getattr(manager, "list_tools", None)
    if manager is None or not callable(list_tools):
        raise TypeError(
            f"{caller}() expects a FastMCP server with a FastMCP tool registry "
            f"(_tool_manager.list_tools); got {type(server).__name__}"
        )
    return manager, list_tools


def _harden_tool(tool: Any, *, strict_types: bool) -> None:
    parameters = getattr(tool, "parameters", None)
    if not isinstance(parameters, dict):
        raise TypeError(
            "FastMCP Tool lacks 'parameters' — SDK shape changed; update registry.py"
        )
    fn_metadata = getattr(tool, "fn_metadata", None)
    arg_model = getattr(fn_metadata, "arg_model", None)
    model_config = getattr(arg_model, "model_config", None)
    model_rebuild = getattr(arg_model, "model_rebuild", None)
    if (
        fn_metadata is None
        or arg_model is None
        or not isinstance(model_config, dict)
        or not callable(model_rebuild)
    ):
        raise TypeError(
            "FastMCP Tool lacks 'fn_metadata.arg_model' — SDK shape changed; update registry.py"
        )
    model_config["extra"] = "forbid"
    if strict_types:
        model_config["strict"] = True
    model_rebuild(force=True)
    parameters["additionalProperties"] = False


def strict_tools(server: Any, *, strict_types: bool = False) -> Any:
    """Reject unknown MCP tool arguments (`extra='forbid'` + `additionalProperties: false`).

    FastMCP's default argument model silently drops unknown keys, while Click rejects unknown
    CLI options with exit code 2. Calling `strict_tools(server)` brings the MCP surface into
    parity with the CLI contract and advertises `additionalProperties: false` via `list_tools()`.
    Pass `strict_types=True` to also set Pydantic `strict=True` (disabling lax string-to-number
    coercion).
    """
    manager, list_tools = _tool_manager(server, "strict_tools")
    for tool in list_tools():
        _harden_tool(tool, strict_types=strict_types)

    add_tool = getattr(manager, "_linc_orig_add_tool", None) or getattr(manager, "add_tool", None)
    if callable(add_tool):
        manager._linc_orig_add_tool = add_tool

        @functools.wraps(add_tool)
        def _strict_add_tool(*args: Any, **kwargs: Any) -> Any:
            tool = add_tool(*args, **kwargs)
            _harden_tool(tool, strict_types=strict_types)
            return tool

        manager.add_tool = _strict_add_tool
    return server


@contextlib.contextmanager
def stubbed_tool_fn(server: Any, name: str) -> Iterator[None]:
    """Temporarily replace a registered tool's callable with a no-op for validation probes."""
    _, list_tools = _tool_manager(server, "stubbed_tool_fn")
    for tool in list_tools():
        if getattr(tool, "name", None) == name:
            orig = tool.fn

            async def _async_noop(**_: Any) -> dict[str, bool]:
                return {"ok": True}

            def _sync_noop(**_: Any) -> dict[str, bool]:
                return {"ok": True}

            tool.fn = _async_noop if getattr(tool, "is_async", False) else _sync_noop
            try:
                yield
            finally:
                tool.fn = orig
            return
    yield


def registry_of(server: Any) -> list[ToolSpec]:
    """Return every tool registered on a FastMCP server as a ToolSpec."""
    _, list_tools = _tool_manager(server, "registry_of")
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


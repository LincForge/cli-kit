"""Toy FastMCP server covering every parameter shape found in the LINC fleet."""

from __future__ import annotations

from typing import Any

import click
import pytest
from mcp.server.fastmcp import FastMCP


def build_toy_server() -> FastMCP:
    server = FastMCP("toy")

    @server.tool()
    def toy_echo(device: str, count: int = 1, ratio: float = 0.5, loud: bool = False) -> dict:
        """Echo the inputs back. Example: toy echo --device X --count 2"""
        return {"device": device, "count": count, "ratio": ratio, "loud": loud}

    @server.tool()
    def toy_extras(device: str, extras: dict[str, Any] | None = None) -> dict:
        """Object-or-null parameter, the nerve_launch shape."""
        return {"device": device, "extras": extras}

    @server.tool()
    def toy_tags(tags: list[str], mode: str = "fast") -> dict:
        """Array parameter."""
        return {"tags": tags, "mode": mode}

    @server.tool()
    def toy_reserved(json: str, output: str = "x") -> dict:
        """Parameters colliding with kit globals."""
        return {"json": json, "output": output}

    @server.tool()
    def toy_fail(kind: str) -> dict:
        """kind=validation -> validation error; kind=raise -> exception; else generic error."""
        if kind == "validation":
            return {"error": "bad input", "validation_error": True}
        if kind == "raise":
            raise RuntimeError("boom")
        return {"error": "device offline"}

    @server.tool()
    async def toy_async(device: str) -> dict:
        """Async tool."""
        return {"device": device, "async": True}

    return server


@pytest.fixture
def toy_server() -> FastMCP:
    return build_toy_server()


@pytest.fixture
def toy_group() -> click.Group:
    @click.group()
    def cli() -> None:
        """toy"""

    return cli

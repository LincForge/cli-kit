import anyio
import pytest
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError as FastMCPToolError

from linc_cli_kit import strict_tools
from linc_cli_kit.registry import ToolSpec, registry_of


def test_registry_exposes_fn_schema_and_async_flag(toy_server):
    specs = {s.name: s for s in registry_of(toy_server)}
    assert set(specs) == {
        "toy_echo", "toy_extras", "toy_tags", "toy_reserved", "toy_fail", "toy_async"
    }
    echo = specs["toy_echo"]
    assert isinstance(echo, ToolSpec)
    assert callable(echo.fn)
    assert echo.parameters["required"] == ["device"]
    assert echo.parameters["properties"]["count"] == {
        "default": 1, "title": "Count", "type": "integer"
    }
    assert echo.description.startswith("Echo the inputs back")
    assert specs["toy_async"].is_async is True
    assert echo.is_async is False


def test_registry_fails_loudly_on_unknown_server_shape():
    class Bogus:  # no _tool_manager
        pass

    with pytest.raises(TypeError, match="FastMCP tool registry"):
        registry_of(Bogus())


def test_contract_strict_tools_rejects_unknown_key_that_fastmcp_ignores_by_default():
    """[contract] #15: without strict_tools(server), FastMCP silently drops unknown keys;
    with strict_tools(server), calling a tool with an unknown key raises ToolError naming it."""
    server = FastMCP("strict_probe")

    @server.tool()
    def tap(x: int, y: int) -> dict:
        return {"x": x, "y": y}

    # Baseline without the hook: extra keys are silently ignored by FastMCP.
    anyio.run(server.call_tool, "tap", {"x": 1, "y": 2, "hallucinated_flag": True})

    returned = strict_tools(server)
    assert returned is server

    with pytest.raises(FastMCPToolError, match="hallucinated_flag"):
        anyio.run(server.call_tool, "tap", {"x": 1, "y": 2, "hallucinated_flag": True})


def test_contract_strict_tools_preserves_valid_calls_and_context_injection():
    """[contract] #15: valid calls (including lax str->int coercion by default and FastMCP
    Context injection) still succeed after strict_tools(server); strict_types=True is opt-in."""
    server = FastMCP("ctx_probe")

    @server.tool()
    def inspect_device(device: str, count: int, ctx: Context) -> dict:
        return {"device": device, "count": count, "has_ctx": isinstance(ctx, Context)}

    strict_tools(server)

    # Valid call + Context injection + default lax int coercion ("3" -> 3) still work.
    blocks = anyio.run(server.call_tool, "inspect_device", {"device": "X", "count": "3"})
    assert '"count": 3' in blocks[0].text
    assert '"has_ctx": true' in blocks[0].text

    # Opt-in strict_types=True rejects str->int coercion too.
    strict_tools(server, strict_types=True)
    with pytest.raises(FastMCPToolError, match="count"):
        anyio.run(server.call_tool, "inspect_device", {"device": "X", "count": "3"})


def test_contract_strict_tools_sets_additional_properties_false_on_list_tools(toy_server):
    """[contract] #15: list_tools() reports additionalProperties: false for every tool."""
    strict_tools(toy_server)
    tools = anyio.run(toy_server.list_tools)
    assert len(tools) == 6
    for tool in tools:
        assert tool.inputSchema.get("additionalProperties") is False, tool.name
    for spec in registry_of(toy_server):
        assert spec.parameters.get("additionalProperties") is False, spec.name


def test_contract_strict_tools_fails_loudly_on_missing_sdk_internals():
    """[contract] #15: fails loudly (TypeError naming the SDK change) if _tool_manager or
    fn_metadata.arg_model is missing."""

    class NoManager:
        pass

    with pytest.raises(TypeError, match="FastMCP tool registry"):
        strict_tools(NoManager())

    class NoParamsTool:
        def __init__(self):
            self.name = "no_params"
            self.parameters = None
            self.fn_metadata = None

    class NoParamsManager:
        def list_tools(self):
            return [NoParamsTool()]

    class NoParamsServer:
        _tool_manager = NoParamsManager()

    with pytest.raises(TypeError, match=r"parameters.*SDK shape changed"):
        strict_tools(NoParamsServer())

    class BrokenTool:
        def __init__(self):
            self.name = "broken"
            self.parameters = {"type": "object"}
            self.fn_metadata = None

    class BrokenManager:
        def list_tools(self):
            return [BrokenTool()]

    class BrokenServer:
        _tool_manager = BrokenManager()

    with pytest.raises(TypeError, match=r"fn_metadata\.arg_model.*SDK shape changed"):
        strict_tools(BrokenServer())


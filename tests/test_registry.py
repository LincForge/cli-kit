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

    import pytest

    with pytest.raises(TypeError, match="FastMCP tool registry"):
        registry_of(Bogus())

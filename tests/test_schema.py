import json

import click
import pytest
from click.testing import CliRunner

from linc_cli_kit.schema import CoerceType, JsonType, cli_name, option_for, options_for


def test_cli_name_strips_prefix_and_hyphenates():
    assert cli_name("nerve_clear_app_data", "nerve_") == "clear-app-data"
    assert cli_name("citadel_up", "citadel_") == "up"
    assert cli_name("validate_manifest_file", "") == "validate-manifest-file"


def test_scalar_types_and_required():
    opt = option_for("count", {"type": "integer", "default": 1}, required=False)
    assert opt.opts == ["--count"]
    assert opt.type is click.INT
    assert opt.default == 1
    assert not opt.required
    opt = option_for("device", {"type": "string"}, required=True)
    assert opt.required is True and opt.type is click.STRING
    assert option_for("ratio", {"type": "number"}, required=False).type is click.FLOAT


def test_boolean_is_flag_pair():
    opt = option_for("loud", {"type": "boolean", "default": False}, required=False)
    assert opt.is_flag and opt.opts == ["--loud"] and opt.secondary_opts == ["--no-loud"]


def test_enum_is_choice():
    opt = option_for("mode", {"enum": ["fast", "slow"]}, required=False)
    assert isinstance(opt.type, click.Choice) and list(opt.type.choices) == ["fast", "slow"]


def test_array_of_scalars_is_multiple():
    opt = option_for("tags", {"type": "array", "items": {"type": "string"}}, required=True)
    assert opt.multiple and opt.type is click.STRING


def test_object_or_null_is_json_option():
    prop = {
        "anyOf": [
            {"additionalProperties": True, "type": "object"},
            {"type": "null"},
        ],
        "default": None,
    }
    opt = option_for("extras", prop, required=False)
    assert opt.opts == ["--extras-json"] and isinstance(opt.type, JsonType)
    assert opt.type.convert('{"a": 1}', None, None) == {"a": 1}
    with pytest.raises(click.BadParameter):
        opt.type.convert("{not json", None, None)


def test_scalar_union_coerces_in_order():
    prop = {"anyOf": [{"type": "integer"}, {"type": "string"}]}
    opt = option_for("val", prop, required=False)
    assert isinstance(opt.type, CoerceType)
    assert opt.type.convert("7", None, None) == 7
    assert opt.type.convert("x", None, None) == "x"


def test_reserved_names_are_remapped():
    opt = option_for("json", {"type": "string"}, required=True)
    assert opt.opts == ["--arg-json"] and opt.name == "json"
    opt = option_for("output", {"type": "string"}, required=False)
    assert opt.opts == ["--arg-output"]


def test_options_for_preserves_schema_order_and_required():
    params = {
        "properties": {
            "device": {"type": "string"},
            "count": {"type": "integer", "default": 1},
        },
        "required": ["device"],
    }
    opts = options_for(params)
    assert [o.name for o in opts] == ["device", "count"]
    assert [o.required for o in opts] == [True, False]
    assert opts[0].help == ""


def test_boolean_required_is_honored():
    opt = option_for("confirm", {"type": "boolean"}, required=True)
    assert opt.required is True


def test_required_option_ignores_schema_default():
    opt = option_for("device", {"type": "string", "default": "phone"}, required=True)
    assert opt.required is True


def test_object_and_array_defaults_pass_through():
    opt = option_for("config", {"type": "object", "default": {"a": 1}}, required=False)
    assert opt.default == {"a": 1}
    opt = option_for(
        "tags",
        {"type": "array", "items": {"type": "string"}, "default": ["x"]},
        required=False,
    )
    assert opt.default == ("x",)


def test_scalar_union_uses_canonical_order():
    opt = option_for("val", {"anyOf": [{"type": "string"}, {"type": "integer"}]}, required=False)
    assert opt.type.convert("7", None, None) == 7
    assert opt.type.convert("x", None, None) == "x"


def test_required_string_is_enforced_at_invocation():
    opt = option_for("device", {"type": "string", "default": "phone"}, required=True)
    cmd = click.Command(
        "c",
        params=[opt],
        callback=lambda **kw: click.echo(json.dumps(kw)),
    )
    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 2 and "Missing option" in result.output
    result = runner.invoke(cmd, ["--device", "X"])
    assert result.exit_code == 0


def test_required_boolean_is_enforced_at_invocation():
    opt = option_for("confirm", {"type": "boolean"}, required=True)
    cmd = click.Command(
        "c",
        params=[opt],
        callback=lambda **kw: click.echo(json.dumps(kw)),
    )
    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 2 and "Missing option" in result.output
    result = runner.invoke(cmd, ["--confirm"])
    assert result.exit_code == 0
    result = runner.invoke(cmd, ["--no-confirm"])
    assert result.exit_code == 0


def test_optional_default_still_applies():
    opt = option_for("device", {"type": "string", "default": "phone"}, required=False)
    cmd = click.Command(
        "c",
        params=[opt],
        callback=lambda **kw: click.echo(json.dumps(kw)),
    )
    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert output["device"] == "phone"

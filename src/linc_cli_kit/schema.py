"""JSON-schema property → click.Option (spec §5.3)."""

from __future__ import annotations

import json
from typing import Any

import click

RESERVED = frozenset({"json", "human", "stream", "output", "timeout", "log_level", "help", "yes"})
_SCALARS = {"integer": click.INT, "number": click.FLOAT, "string": click.STRING}


class JsonType(click.ParamType):
    name = "json"

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        if value is None or isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            self.fail(f"must be valid JSON: {exc}", param, ctx)


class CoerceType(click.ParamType):
    """Try each member type of a scalar union in canonical order."""

    name = "value"

    def __init__(self, members: list[dict[str, Any]]) -> None:
        canonical = ("integer", "number", "boolean", "object", "array", "string")

        def sort_key(m: dict[str, Any]) -> int:
            mtype = m.get("type")
            return canonical.index(mtype) if mtype in canonical else len(canonical)

        self.members = sorted(members, key=sort_key)

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        for member in self.members:
            kind = member.get("type")
            try:
                if kind == "integer":
                    return int(value)
                if kind == "number":
                    return float(value)
                if kind == "boolean":
                    return click.BOOL.convert(value, param, ctx)
                if kind in ("object", "array"):
                    return json.loads(value)
                if kind == "string":
                    return str(value)
            except (ValueError, TypeError, json.JSONDecodeError, click.BadParameter):
                continue
        self.fail(
            f"could not coerce {value!r} to any of {[m.get('type') for m in self.members]}",
            param,
            ctx,
        )


def cli_name(tool_name: str, prefix: str) -> str:
    name = tool_name[len(prefix):] if prefix and tool_name.startswith(prefix) else tool_name
    return name.replace("_", "-")


def _flag(name: str) -> str:
    base = name.replace("_", "-")
    return f"--arg-{base}" if name in RESERVED else f"--{base}"


def _members(prop: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in (prop.get("anyOf") or prop.get("oneOf") or []) if m.get("type") != "null"]


def option_for(name: str, prop: dict[str, Any], required: bool) -> click.Option:
    help_text = (prop.get("description") or "").strip()
    default = prop.get("default")
    kind = prop.get("type")
    members = _members(prop)
    flag = _flag(name)

    def build_kwargs() -> dict[str, Any]:
        kwargs: dict[str, Any] = {"required": required, "help": help_text}
        if not required and default is not None:
            kwargs["default"] = default
            kwargs["show_default"] = True
        return kwargs

    if "enum" in prop:
        kwargs = build_kwargs()
        kwargs["type"] = click.Choice([str(c) for c in prop["enum"]])
        return click.Option([flag, name], **kwargs)
    if kind == "boolean":
        kwargs = build_kwargs()
        kwargs["is_flag"] = True
        if not required:
            kwargs["default"] = bool(default) if default is not None else False
        return click.Option([f"{flag}/--no-{flag.lstrip('-')}", name], **kwargs)
    if kind in _SCALARS:
        kwargs = build_kwargs()
        kwargs["type"] = _SCALARS[kind]
        if not required:
            kwargs["show_default"] = default is not None
        return click.Option([flag, name], **kwargs)
    if kind == "array":
        item_kind = (prop.get("items") or {}).get("type")
        if item_kind in _SCALARS:
            kwargs = build_kwargs()
            kwargs["type"] = _SCALARS[item_kind]
            kwargs["multiple"] = True
            if not required and isinstance(default, list):
                kwargs["default"] = tuple(default)
            return click.Option([flag, name], **kwargs)
        kwargs = build_kwargs()
        kwargs["type"] = JsonType()
        if not required:
            kwargs["default"] = default
        return click.Option([f"{flag}-json", name], **kwargs)
    if kind == "object" or any(m.get("type") in ("object", "array") for m in members):
        kwargs = build_kwargs()
        kwargs["type"] = JsonType()
        if not required:
            kwargs["default"] = default
        return click.Option([f"{flag}-json", name], **kwargs)
    if members:
        kwargs = build_kwargs()
        kwargs["type"] = CoerceType(members)
        if not required:
            kwargs["default"] = default
        return click.Option([flag, name], **kwargs)
    kwargs = build_kwargs()
    kwargs["type"] = click.STRING
    if not required:
        kwargs["show_default"] = default is not None
    return click.Option([flag, name], **kwargs)


def options_for(parameters: dict[str, Any]) -> list[click.Option]:
    required = set(parameters.get("required", []))
    props = parameters.get("properties") or {}
    return [option_for(n, p, n in required) for n, p in props.items()]

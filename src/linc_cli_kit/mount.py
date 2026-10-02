"""Mount a FastMCP registry onto a Click group (spec §5.1, §5.3, §5.5)."""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from typing import Any

import click

from linc_cli_kit.errors import ToolError, UsageError
from linc_cli_kit.logging import configure_stderr_logging
from linc_cli_kit.output import (
    emit,
    emit_exception,
    harden_streams,
    heartbeat,
    resolve_mode,
    to_json,
)
from linc_cli_kit.registry import ToolSpec, registry_of
from linc_cli_kit.schema import cli_name, options_for
from linc_cli_kit.transport import LocalTransport, Transport

_META = "linc_cli_kit."

# click.ParamType.name -> the schema doc's normalized "type" string (ruling 4).
_TYPE_NAMES = {
    "text": "STRING",
    "integer": "INT",
    "float": "FLOAT",
    "boolean": "BOOL",
    "choice": "CHOICE",
    "json": "JSON",
    "value": "VALUE",
}


def current_mode() -> str:
    ctx = click.get_current_context(silent=True)
    if ctx is not None:
        mode = ctx.find_root().meta.get(_META + "mode")
        if mode is not None:
            return mode
    return resolve_mode(None)


def current_output() -> str | None:
    ctx = click.get_current_context(silent=True)
    return ctx.find_root().meta.get(_META + "output") if ctx is not None else None


def current_timeout() -> float | None:
    ctx = click.get_current_context(silent=True)
    return ctx.find_root().meta.get(_META + "timeout") if ctx is not None else None


def install_globals(group: click.Group) -> click.Group:
    """Add the kit's global options and stream/logging hardening to an existing group."""
    if any(getattr(p, "name", None) == "linc_mode" for p in group.params):
        return group

    # Built fresh per call (ruling 1) — Option instances must not be shared across groups.
    global_options: list[click.Option] = [
        click.Option(
            ["--json", "linc_mode"], flag_value="json", default=None,
            help="JSON on stdout (default).",
        ),
        click.Option(["--human", "linc_mode"], flag_value="human", help="Human rendering."),
        click.Option(
            ["--stream", "linc_mode"], flag_value="jsonl",
            help="JSON Lines, one record per line.",
        ),
        click.Option(
            ["--output", "-o", "linc_output"], default=None, help="Path for binary artifacts.",
        ),
        click.Option(
            ["--timeout", "linc_timeout"], type=float, default=None,
            help="Per-call timeout seconds.",
        ),
        click.Option(
            ["--log-level", "linc_log_level"], default=None, help="LINC_LOG_LEVEL override.",
        ),
    ]
    group.params.extend(global_options)

    # The invocation's resolved mode, kept past the context's teardown so an exception that
    # escapes to main() below is rendered the way the caller asked (--human vs JSON).
    invocation: dict[str, str | None] = {"mode": None}

    original_callback = group.callback

    def callback(**kwargs: Any) -> Any:
        ctx = click.get_current_context()
        ctx.meta[_META + "mode"] = resolve_mode(kwargs.pop("linc_mode", None))
        invocation["mode"] = ctx.meta[_META + "mode"]
        ctx.meta[_META + "output"] = kwargs.pop("linc_output", None)
        ctx.meta[_META + "timeout"] = kwargs.pop("linc_timeout", None)
        configure_stderr_logging(kwargs.pop("linc_log_level", None))
        harden_streams()
        return original_callback(**kwargs) if original_callback else None

    if original_callback is not None:
        callback = functools.wraps(original_callback)(callback)
    group.callback = callback

    original_main = group.main

    def main(*args: Any, **kwargs: Any) -> Any:
        # Hard-assigned, not setdefault: a caller passing standalone_mode=True (e.g. a
        # CliRunner.invoke(..., standalone_mode=True) test) must not be able to bypass the
        # envelope contract (ruled fix, finding 2).
        kwargs["standalone_mode"] = False
        invocation["mode"] = None
        try:
            rv = original_main(*args, **kwargs)
        except click.UsageError as exc:
            emit_exception(UsageError(exc.format_message()), mode=_mode())
            sys.exit(2)
        except click.exceptions.Exit as exc:
            sys.exit(exc.exit_code)
        except click.exceptions.Abort as exc:
            # Click converts KeyboardInterrupt (our SIGINT handler) into Abort.
            interrupted = isinstance(exc.__cause__ or exc.__context__, KeyboardInterrupt)
            err = ToolError(
                "interrupted" if interrupted else "aborted",
                code="INTERRUPTED" if interrupted else "ABORTED",
            )
            emit_exception(err, mode=_mode())
            sys.exit(130 if interrupted else 1)
        except Exception as exc:
            # A hand-written override or group callback that raises must still honour the
            # contract: envelope on stderr, empty stdout, no traceback. Generated commands
            # already catch inside their callback; this is the net for everything else.
            # SystemExit (e.g. our SIGTERM handler) is a BaseException and passes through.
            sys.exit(emit_exception(exc, mode=_mode()))
        else:
            # type(rv) is int, not isinstance: bool is an int subclass, and a callback that
            # happens to return True/False must not be mistaken for an exit code (minor 7).
            sys.exit(rv if type(rv) is int else 0)

    def _mode() -> str:
        return invocation["mode"] or resolve_mode(None)

    group.main = main
    return group


def _example_line(description: str) -> str | None:
    for line in description.splitlines():
        idx = line.find("Example:")
        if idx != -1:
            return line[idx:].strip()
    return None


def _build_command(
    spec: ToolSpec, name: str, transport: Transport, renderer: Callable[[Any], str] | None
) -> click.Command:
    options = options_for(spec.parameters)  # never mutates spec.parameters (ruling 9)
    options.sort(key=lambda o: not o.required)  # required before optional

    def callback(**kwargs: Any) -> None:
        ctx = click.get_current_context()
        payload = {k: v for k, v in kwargs.items() if v is not None and v != ()}
        mode = current_mode()
        try:
            with heartbeat(label=name, mode=mode):
                result = transport.call(spec, payload, timeout=current_timeout())
            # emit() runs INSIDE the try (finding 1): a renderer (--human) or a mid-stream
            # generator (--stream) that raises must still produce an envelope, not a bare
            # traceback with empty stdout/stderr.
            ctx.exit(
                emit(result, mode=mode, output_path=current_output(), renderer=renderer,
                     tool=name)
            )
        except click.exceptions.Exit:
            # ctx.exit() above raises this on the success path — it is not an error to wrap.
            raise
        except click.UsageError as exc:  # narrowed from ClickException (minor 8): a
            # click.FileError etc. is not a usage error and should fall through below.
            ctx.exit(emit_exception(UsageError(exc.format_message()), mode=mode))
        except Exception as exc:  # contract: never a raw traceback on stdout
            ctx.exit(emit_exception(exc, mode=mode))

    example = _example_line(spec.description)
    help_text = spec.description.split("\n\n")[0]
    cmd = click.Command(
        name, params=list(options), callback=callback, help=help_text, epilog=example,
    )
    cmd.linc_transport = transport  # ruling 7
    # Marks the command as kit-generated. The conformance suite classifies on THIS attribute,
    # not on callback.__module__ — an adopter is free to wrap or replace the callback.
    cmd.linc_kit_generated = True
    return cmd


def _normalized_type(ptype: click.ParamType) -> str:
    return _TYPE_NAMES.get(ptype.name, ptype.name.upper())


def _opt_doc(o: click.Parameter) -> dict[str, Any]:
    choices = list(o.type.choices) if isinstance(o.type, click.Choice) else None
    default = o.default
    if default == () or default is getattr(click.core, "UNSET", object()):
        default = None
    return {
        "flag": (o.opts or [o.name])[0],
        "type": _normalized_type(o.type),
        "required": bool(o.required),
        "default": default,
        "choices": choices,
        "help": (getattr(o, "help", "") or ""),
    }


def _schema_command(group: click.Group) -> click.Command:
    @click.command("schema", help="Emit the whole command tree as JSON (no --help crawl).")
    @click.option("--json", "as_json", is_flag=True, default=True, help="Always JSON.")
    def schema(as_json: bool) -> None:
        ctx = click.get_current_context(silent=True)
        root_name = ctx.find_root().info_name if ctx is not None else None
        doc = {
            "name": root_name or group.name,
            "globals": {
                (o.opts or [o.name])[0]: (o.help or "")
                for o in group.params
                if isinstance(o, click.Option)
            },
            "commands": {
                cname: {
                    "help": (cmd.help or "").split("\n")[0],
                    "options": {
                        p.name: _opt_doc(p) for p in cmd.params if isinstance(p, click.Option)
                    },
                }
                for cname, cmd in sorted(group.commands.items())
            },
        }
        click.echo(to_json(doc))

    schema.linc_kit_generated = True  # marks "ours" so a re-mount on the same group won't collide
    return schema


def mount_tools(
    group: click.Group,
    server: Any,
    *,
    prefix: str = "",
    transport: Transport | None = None,
    skip: dict[str, str] | None = None,
    renderers: dict[str, Callable[[Any], str]] | None = None,
) -> list[str]:
    """Reflect every registered tool into `group`. Hand-written commands win on name collision.

    Daemon-only marking is NOT a `mount_tools` concern (finding 3, spec §5.4) — it belongs to
    the `HttpDaemonTransport` a caller passes in via `transport=`, which already implements it.
    """
    if not any(getattr(p, "name", None) == "linc_mode" for p in group.params):
        raise RuntimeError("call install_globals(group) before mount_tools")
    transport = transport or LocalTransport()
    skip = skip or {}
    renderers = renderers or {}
    existing_schema = group.commands.get("schema")
    if existing_schema is not None and not getattr(existing_schema, "linc_kit_generated", False):
        raise ValueError("group already defines 'schema'; the kit reserves that command name")
    for tool_name, why in skip.items():
        if not why.strip():
            raise ValueError(f"skip['{tool_name}'] needs a justification string")
    specs = registry_of(server)
    unknown_skips = sorted(set(skip) - {spec.name for spec in specs})
    if unknown_skips:
        raise ValueError(f"skip references unknown tool(s): {unknown_skips}")
    mounted: list[str] = []
    for spec in specs:
        if spec.name in skip:
            continue
        name = cli_name(spec.name, prefix)
        if name in group.commands:
            continue
        group.add_command(_build_command(spec, name, transport, renderers.get(name)), name)
        mounted.append(name)
    group.add_command(_schema_command(group), "schema")
    group.linc_transport = transport  # ruling 7
    return mounted

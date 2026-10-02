"""Importable conformance suite (spec §5.5). Each repo calls assert_cli_parity in one test.

Nine checks, run per mounted tool (unless justified-skipped):
  1. coverage + justified skips
  2. --help exits 0 and names every required option
  3. stubbed success -> bare JSON on stdout, no ANSI, empty stderr, AND the payload the
     command handed the transport is exactly the sample (plus schema defaults) — a command
     that drops, renames, or invents a key is caught here, not in production
  4. stubbed validation error -> exit 2, empty stdout, envelope on stderr
  5. overrides (hand-written commands) pass a JSON-output check instead of 6/7 — and must
     actually call the transport (an override that hard-codes its output fails) — but DO get
     the same 3/4/8 output-contract coverage as generated commands whenever a sample is
     supplied for them (an override with no sample only gets the success-shaped half of 3,
     since there is no way to know a valid invocation to error-test against — see Deviations)
  6. option parity between the tool schema and the generated Click options, plus the
     reserved-name `--arg-*` remap
  7. execution consistency: a direct `LocalTransport().call(...)` matches the full CLI
     round trip through the real transport. That round trip also re-runs check 3's output
     contract (bare JSON, no ANSI) with the PROCESS stdout captured at the file-descriptor
     level: anything a tool writes there (a structlog logger bound to stdout at import, a
     child process inheriting fd 1) would corrupt the payload in a real CLI process, and
     CliRunner alone cannot see it
  8. stubbed generic error -> exit 1, empty stdout, envelope on stderr
  9. `schema --json` lists every mounted command

Every JSON parse goes through a named assertion that reports the tool and the stream.

Deviations:
  - Check 2's "names every required option" sub-check applies to generated commands only.
    Overrides are hand-written commands free to keep their own signature (check 5) — a
    schema-derived required-flag name may not exist on them at all, so scanning their
    --help output for it would fail on an override's *shape*, not on the JSON contract
    the suite actually cares about there.
  - Fail-closed rule for overrides: a `sample is None` override that cannot be invoked
    with zero arguments (i.e. it has its own required options and exits non-zero) is
    reported as a suite failure naming the tool, not silently treated as "nothing to
    check" — see `_check_override`. Provide a `samples[<tool>]` entry to unblock it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import click
from click.testing import CliRunner

from linc_cli_kit.output import to_json
from linc_cli_kit.registry import ToolSpec, registry_of
from linc_cli_kit.schema import RESERVED, cli_name
from linc_cli_kit.transport import LocalTransport

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# The two error-shaped stub payloads every command must translate into the right exit
# code + empty stdout + stderr envelope (checks 4/8), paired with their expected exit code.
_ERROR_STUBS = (({"error": "x", "validation_error": True}, 2), ({"error": "x"}, 1))


@dataclass
class ParityReport:
    tools: int
    commands: int
    skipped: int
    overrides: list[str] = field(default_factory=list)


def _loads(text: str, label: str, stream: str) -> Any:
    """json.loads as a named contract assertion: the tool, the stream and what it held."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{label}: {stream} is not valid JSON ({exc.msg}); got {text[:200]!r}"
        ) from exc


def _stream_fd(stream: Any) -> int | None:
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):  # io.UnsupportedOperation is both
        return None
    return fd if isinstance(fd, int) and fd >= 0 else None


@contextlib.contextmanager
def _process_stdout_leak() -> Iterator[Callable[[], str]]:
    """Capture what reaches the PROCESS stdout while CliRunner owns `sys.stdout`.

    CliRunner swaps `sys.stdout` for its own buffer, so a write through an object bound
    before the invocation (a structlog PrintLogger created at import, which holds the
    then-current `sys.stdout`) or straight to fd 1 (a child process) bypasses the result's
    `stdout` entirely. Every file descriptor behind the pre-invocation stdout objects, plus
    fd 1, is pointed at one temp file for the duration; a stdout object with no descriptor
    (an in-memory capture buffer) is diffed by length instead. Yields a callable returning
    whatever leaked.
    """
    streams = [s for s in (sys.stdout, sys.__stdout__) if s is not None]
    fds = {1}
    buffers: list[tuple[Any, int]] = []
    for stream in streams:
        with contextlib.suppress(Exception):
            stream.flush()
        fd = _stream_fd(stream)
        if fd is not None:
            fds.add(fd)
        elif hasattr(stream, "getvalue"):
            buffers.append((stream, len(stream.getvalue())))
    leaked: list[str] = []
    with tempfile.TemporaryFile() as sink:
        saved = {fd: os.dup(fd) for fd in sorted(fds)}
        try:
            for fd in saved:
                os.dup2(sink.fileno(), fd)
            yield lambda: "".join(leaked)
        finally:
            for stream in streams:
                with contextlib.suppress(Exception):
                    stream.flush()
            for fd, copy in saved.items():
                os.dup2(copy, fd)
                os.close(copy)
            sink.seek(0)
            leaked.append(sink.read().decode("utf-8", "replace"))
            for stream, before in buffers:
                leaked.append(stream.getvalue()[before:])


def _invoke(cli: click.Group, args: list[str], env: dict[str, str] | None = None) -> Any:
    runner = CliRunner()
    return runner.invoke(cli, args, env=env, catch_exceptions=False)


def _invoke_stubbed(
    cli: click.Group, transport_holder: Any, name: str, args: list[str], stub: Any
) -> tuple[Any, Any]:
    """Invoke `name` with `transport_holder.linc_transport.call` stubbed to return `stub`.

    Returns `(result, mock)` — the mock is kept so callers can assert on the payload the
    command actually sent (`mock.call_args.args[1]`), not just on what came back.

    `transport_holder` is whichever object carries the live `.linc_transport` to patch:
    the generated `cmd` (ruling 1 attaches `cmd.linc_transport`), or the `cli` group itself
    for an override (a hand-written command has no `cmd.linc_transport` of its own, so
    overrides are stubbed via `cli.linc_transport` — ruling 1)."""
    with patch.object(type(transport_holder.linc_transport), "call", return_value=stub) as mock:
        return _invoke(cli, [name, *args]), mock


def _sample_args(cmd: click.Command, sample: dict[str, Any]) -> list[str]:
    """Build CLI argv for `sample`. Arguments go positionally in param order (ruling 2);
    only Options are addressed by `param.opts[0]`."""
    positional: list[str] = []
    options: list[str] = []
    for param in cmd.params:
        if param.name not in sample:
            continue
        value = sample[param.name]
        if isinstance(param, click.Argument):
            positional.append(str(value))
            continue
        if not isinstance(param, click.Option):
            continue
        flag = param.opts[0]
        if param.is_flag:
            if value:
                options.append(flag)
            elif param.secondary_opts:
                options.append(param.secondary_opts[0])
        elif isinstance(value, (list, tuple)) and getattr(param, "multiple", False):
            for item in value:
                options += [flag, str(item)]
        elif isinstance(value, (dict, list)):
            options += [flag, json.dumps(value)]
        else:
            options += [flag, str(value)]
    return positional + options


def _norm(value: Any) -> Any:
    """Click hands `multiple=True` options a tuple; samples are written as lists."""
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(item) for item in value]
    return value


def _expected_payload(cmd: click.Command, sample: dict[str, Any]) -> dict[str, Any]:
    """What a correct generated command must hand the transport for `sample`: the sample's
    own values plus every non-required option's schema default, minus the Nones the callback
    filters out."""
    expected: dict[str, Any] = {}
    for param in cmd.params:
        if param.name in sample:
            expected[param.name] = sample[param.name]
        elif isinstance(param, click.Option) and not param.required:
            expected[param.name] = param.default
    return {k: v for k, v in expected.items() if v is not None and v != ()}


def _assert_payload(mock: Any, cmd: click.Command, spec: ToolSpec, sample: dict[str, Any]) -> None:
    assert mock.call_args is not None, f"{spec.name}: command never called the transport"
    sent = mock.call_args.args[1]
    expected = _expected_payload(cmd, sample)
    assert _norm(sent) == _norm(expected), (
        f"{spec.name}: payload sent to the transport {sent!r} != expected {expected!r} "
        f"for sample {sample!r}"
    )


def _minimal_sample(spec: ToolSpec) -> dict[str, Any]:
    """A throwaway argv-shaped sample covering every required property, used only to drive
    the stubbed output-contract checks (3/4/8) when the caller didn't supply one."""
    out: dict[str, Any] = {}
    props = spec.parameters.get("properties", {})
    defaults = {"integer": 1, "number": 1.0, "boolean": True, "array": ["x"], "object": {}}
    for name in spec.parameters.get("required", []):
        kind = props.get(name, {}).get("type")
        out[name] = defaults.get(kind, "x")
    return out


def _check_help_and_required(
    cli: click.Group, cmd: click.Command, spec: ToolSpec, name: str, generated: bool
) -> None:
    # 2. --help exits 0 and (for generated commands, whose signature is schema-derived —
    # see the Deviations note above) names every required option.
    r = _invoke(cli, [name, "--help"])
    assert r.exit_code == 0, f"{spec.name}: --help exited {r.exit_code}: {r.output}"
    if not generated:
        return
    for req in spec.parameters.get("required", []):
        opt = next(
            (p for p in cmd.params if isinstance(p, click.Option) and p.name == req), None
        )
        assert opt is not None, f"{spec.name}: no CLI option corresponds to required '{req}'"
        flag = opt.opts[0]
        assert flag in r.output, f"{spec.name}: --help does not mention required {flag}"


def _check_option_parity(cmd: click.Command, spec: ToolSpec) -> None:
    # 6. option parity + reserved-name remap (generated commands only; overrides keep
    # their own signature).
    want = set(spec.parameters.get("properties", {}))
    have = {p.name for p in cmd.params if isinstance(p, click.Option)}
    assert want == have, f"{spec.name}: option mismatch schema={sorted(want)} cli={sorted(have)}"
    for p in cmd.params:
        if p.name in RESERVED:
            assert p.opts[0].startswith("--arg-"), (
                f"{spec.name}: reserved name {p.name} not remapped"
            )


def _assert_error_stubs(
    cli: click.Group, transport_holder: Any, name: str, args: list[str], label: str
) -> None:
    """Checks 4/8: stubbed validation error (exit 2) and generic error (exit 1) each
    produce empty stdout and a JSON envelope with a truthy "error" key on stderr. Shared
    by generated commands and sampled overrides (Important 2)."""
    for stub, code in _ERROR_STUBS:
        r, _ = _invoke_stubbed(cli, transport_holder, name, args, stub)
        assert r.exit_code == code, (
            f"{label}: expected exit {code} got {r.exit_code}: {r.output}"
        )
        assert r.stdout == "", f"{label}: stdout not empty on failure (expected exit {code})"
        doc = _loads(r.stderr, label, "stderr")
        assert isinstance(doc, dict) and doc.get("error"), (
            f"{label}: no envelope on stderr (expected exit {code}); got {r.stderr[:200]!r}"
        )


def _check_stubbed_output_contract(
    cli: click.Group, cmd: click.Command, spec: ToolSpec, name: str, sample: dict[str, Any]
) -> None:
    # 3. stubbed success -> bare JSON on stdout, no ANSI, empty stderr.
    args = _sample_args(cmd, sample)
    stub_ok = {"ok": True}
    r, mock = _invoke_stubbed(cli, cmd, name, args, stub_ok)
    assert r.exit_code == 0, f"{spec.name}: stubbed success exited {r.exit_code}: {r.output}"
    assert not _ANSI.search(r.stdout), f"{spec.name}: ANSI escape on stdout"
    assert r.stderr == "", f"{spec.name}: stderr not empty on stubbed success"
    assert _loads(r.stdout, spec.name, "stdout") == stub_ok, (
        f"{spec.name}: stdout is not the bare JSON payload"
    )
    _assert_payload(mock, cmd, spec, sample)

    # 4/8.
    _assert_error_stubs(cli, cmd, name, args, spec.name)


def _check_execution_consistency(
    cli: click.Group, cmd: click.Command, spec: ToolSpec, name: str, sample: dict[str, Any]
) -> None:
    # 7. direct LocalTransport().call (handles async) vs. the full CLI round trip through
    # the real (unstubbed) transport.
    direct = LocalTransport().call(spec, sample)
    with _process_stdout_leak() as leaked:
        r = _invoke(cli, [name, *_sample_args(cmd, sample)])
    assert not leaked(), (
        f"{spec.name}: the real call wrote {leaked()[:200]!r} to the process stdout, which "
        "would corrupt the JSON payload in a real CLI process — send logs to stderr "
        "(configure_structlog_stderr / configure_stderr_logging) above the package imports"
    )
    assert r.exit_code == 0, (
        f"{spec.name}: execution-consistency call exited {r.exit_code}: {r.output}"
    )
    # Check 3's output contract, through the real transport this time.
    assert not _ANSI.search(r.stdout), f"{spec.name}: ANSI escape on stdout (real call)"
    got = _loads(r.stdout, spec.name, "stdout")
    assert got == json.loads(to_json(direct)), (
        f"{spec.name}: CLI output != direct transport call"
    )


def _check_override(
    cli: click.Group, cmd: click.Command, spec: ToolSpec, name: str, sample: dict[str, Any] | None
) -> None:
    # 5. overrides keep their own signature (checks 2's required-flag scan, 6, 7 are
    # skipped for them) but must still speak the JSON contract, and — when a sample is
    # supplied — get the same success/error stub coverage (3/4/8) as generated commands.
    label = f"{name} (override)"
    args = _sample_args(cmd, sample) if sample is not None else []
    r, mock = _invoke_stubbed(cli, cli, name, args, {"ok": True})
    if r.exit_code != 0:
        if sample is not None:
            raise AssertionError(f"{label}: exited {r.exit_code} with sample args {args}")
        # Important 1 (fail closed): an override with its own required arguments and no
        # caller-supplied sample must not be silently skipped — it would otherwise be
        # reported as "verified" without ever exercising its JSON contract.
        raise AssertionError(
            f"{spec.name}: override '{name}' could not be invoked without arguments "
            f"(exit {r.exit_code}); add a samples[{spec.name!r}] entry so the suite "
            "can verify its JSON contract"
        )
    _loads(r.stdout, label, "stdout")
    assert r.stderr == "", f"{label}: stderr not empty on success"

    # Important 2: a sampled override gets the same error-stub coverage (4/8) a generated
    # command gets — otherwise an override that ignores transport errors entirely (always
    # prints success) goes completely unverified.
    if sample is not None:
        _assert_error_stubs(cli, cli, name, args, label)

    # An override that hard-codes its output (or answers from local state) passes every
    # check above without ever exercising the transport — the README requires overrides to
    # route through `group.linc_transport`. Checked last so a sampled override that ignores
    # transport errors keeps its more specific exit-code message.
    assert mock.called, (
        f"{label}: never called the transport — route it through "
        "click.get_current_context().find_root().command.linc_transport, or delete it and "
        "keep its rendering as mount_tools(renderers=...)"
    )


@contextlib.contextmanager
def _preserved_process_state() -> Any:
    """Restore the signal handlers and root-logger state the CLI installs.

    `harden_streams()` installs `os._exit` SIGTERM/SIGINT handlers and
    `configure_stderr_logging()` strips the root logger's handlers. Both are correct for a
    real CLI process and hostile inside an adopter's pytest session, which keeps running
    after this suite returns."""
    sigterm = signal.getsignal(signal.SIGTERM)
    sigint = signal.getsignal(signal.SIGINT)
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        for sig, handler in ((signal.SIGTERM, sigterm), (signal.SIGINT, sigint)):
            # getsignal() returns None for a handler not installed from Python, and
            # signal.signal() refuses None; ValueError if we are off the main thread.
            if handler is not None:
                with contextlib.suppress(ValueError, TypeError, OSError):
                    signal.signal(sig, handler)
        root.handlers[:] = handlers
        root.setLevel(level)


def assert_cli_parity(
    cli: click.Group,
    server: Any,
    *,
    prefix: str,
    skip: dict[str, str] | None = None,
    samples: dict[str, dict[str, Any]] | None = None,
) -> ParityReport:
    with _preserved_process_state():
        return _run_parity(cli, server, prefix=prefix, skip=skip, samples=samples)


def _run_parity(
    cli: click.Group,
    server: Any,
    *,
    prefix: str,
    skip: dict[str, str] | None = None,
    samples: dict[str, dict[str, Any]] | None = None,
) -> ParityReport:
    skip = skip or {}
    samples = samples or {}
    specs = registry_of(server)
    overrides: list[str] = []
    mounted_names = {cli_name(s.name, prefix) for s in specs if s.name not in skip}

    # 1. coverage + justified skips.
    for tool, why in skip.items():
        assert why.strip(), f"skip for {tool} has no justification"
    for spec in specs:
        if spec.name in skip:
            continue
        name = cli_name(spec.name, prefix)
        assert name in cli.commands, (
            f"{spec.name}: no CLI command '{name}' (mount_tools missing it, "
            "or add a justified skip)"
        )
        cmd = cli.commands[name]
        # Attribute, not callback.__module__ (which a decorator copies or clobbers):
        # _build_command stamps every generated command with linc_kit_generated.
        generated = getattr(cmd, "linc_kit_generated", False)

        _check_help_and_required(cli, cmd, spec, name, generated)

        if not generated:
            overrides.append(name)
            _check_override(cli, cmd, spec, name, samples.get(spec.name))
            continue

        _check_option_parity(cmd, spec)
        sample = samples.get(spec.name)
        _check_stubbed_output_contract(cli, cmd, spec, name, sample or _minimal_sample(spec))
        if sample is not None:
            _check_execution_consistency(cli, cmd, spec, name, sample)

    # 9. schema --json lists every mounted command.
    r = _invoke(cli, ["schema", "--json"])
    assert r.exit_code == 0, f"schema --json failed: {r.output}"
    doc = _loads(r.stdout, "schema --json", "stdout")
    assert isinstance(doc, dict) and isinstance(doc.get("commands"), dict), (
        f"schema --json: stdout has no 'commands' object (got {r.stdout[:200]!r})"
    )
    listed = set(doc["commands"])
    assert mounted_names <= listed, f"schema --json missing {sorted(mounted_names - listed)}"

    return ParityReport(
        tools=len(specs), commands=len(cli.commands), skipped=len(skip), overrides=overrides
    )

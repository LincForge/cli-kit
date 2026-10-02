import functools
import json
import logging
import signal

import click
import pytest

from linc_cli_kit import emit
from linc_cli_kit.mount import install_globals, mount_tools
from linc_cli_kit.registry import registry_of
from linc_cli_kit.testing import assert_cli_parity
from linc_cli_kit.transport import LocalTransport


def test_conformant_group_passes(toy_server, toy_group):
    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=LocalTransport())
    report = assert_cli_parity(
        toy_group,
        toy_server,
        prefix="toy_",
        samples={"toy_echo": {"device": "X"}, "toy_tags": {"tags": ["a"]}},
    )
    assert report.tools == 6 and report.commands >= 7 and report.skipped == 0


def test_missing_command_is_named(toy_server, toy_group):
    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=LocalTransport())
    del toy_group.commands["tags"]
    with pytest.raises(AssertionError, match="toy_tags"):
        assert_cli_parity(toy_group, toy_server, prefix="toy_")


def test_override_that_prints_text_fails_json_check(toy_server, toy_group):
    @toy_group.command("echo")
    def echo() -> None:
        click.echo("not json")

    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=LocalTransport())
    with pytest.raises(AssertionError, match=r"echo.*JSON"):
        assert_cli_parity(toy_group, toy_server, prefix="toy_")


def test_skip_is_honored(toy_server, toy_group):
    install_globals(toy_group)
    mount_tools(
        toy_group, toy_server, prefix="toy_", transport=LocalTransport(),
        skip={"toy_fail": "test-only"},
    )
    report = assert_cli_parity(
        toy_group, toy_server, prefix="toy_", skip={"toy_fail": "test-only"}
    )
    assert report.skipped == 1


def test_unsampled_override_with_required_args_fails_closed(toy_server, toy_group):
    @toy_group.command("echo")
    @click.option("--device", required=True)
    def echo(device: str) -> None:
        click.echo(json.dumps({"device": device}))

    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=LocalTransport())
    with pytest.raises(AssertionError, match="samples"):
        assert_cli_parity(toy_group, toy_server, prefix="toy_")


def test_override_that_ignores_transport_errors_fails(toy_server, toy_group):
    @toy_group.command("echo")
    def echo() -> None:
        click.echo(json.dumps({"ok": True}))

    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=LocalTransport())
    with pytest.raises(AssertionError, match=r"echo.*exit 2"):
        assert_cli_parity(toy_group, toy_server, prefix="toy_", samples={"toy_echo": {}})


def test_override_routed_through_transport_passes(toy_server, toy_group):
    @toy_group.command("echo")
    @click.option("--device", required=True)
    def echo(device: str) -> None:
        ctx = click.get_current_context()
        transport = ctx.find_root().command.linc_transport
        spec = next(s for s in registry_of(toy_server) if s.name == "toy_echo")
        result = transport.call(spec, {"device": device})
        ctx.exit(emit(result, mode="json"))

    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=LocalTransport())
    report = assert_cli_parity(
        toy_group, toy_server, prefix="toy_", samples={"toy_echo": {"device": "X"}}
    )
    assert "echo" in report.overrides


_SAMPLES = {"toy_echo": {"device": "X"}, "toy_tags": {"tags": ["a"]}}


def _mounted(toy_server, toy_group):
    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=LocalTransport())
    return toy_group


def test_generated_command_payload_mismatch_fails(toy_server, toy_group):
    """A generated command that sends the transport a different payload than the CLI was
    given is a silent wrong-answer bug — the suite must catch it."""
    cli = _mounted(toy_server, toy_group)
    cmd = cli.commands["echo"]
    spec = next(s for s in registry_of(toy_server) if s.name == "toy_echo")

    def wrong_key(**kwargs) -> None:
        ctx = click.get_current_context()
        payload = {("devise" if k == "device" else k): v for k, v in kwargs.items()}
        ctx.exit(emit(cmd.linc_transport.call(spec, payload), mode="json"))

    cmd.callback = wrong_key
    with pytest.raises(AssertionError, match="payload"):
        assert_cli_parity(cli, toy_server, prefix="toy_", samples=_SAMPLES)


def test_wrapped_callback_is_still_treated_as_generated(toy_server, toy_group):
    """Classification is by the linc_kit_generated attribute on the command, not by
    callback.__module__ — an adopter may decorate the callback either way."""
    cli = _mounted(toy_server, toy_group)

    echo = cli.commands["echo"]
    echo_original = echo.callback

    @functools.wraps(echo_original)
    def wraps_wrapper(**kwargs):
        return echo_original(**kwargs)

    echo.callback = wraps_wrapper

    tags = cli.commands["tags"]
    tags_original = tags.callback

    def plain_wrapper(**kwargs):  # no functools.wraps: __module__ is THIS test module
        return tags_original(**kwargs)

    tags.callback = plain_wrapper
    assert plain_wrapper.__module__ != "linc_cli_kit.mount"

    report = assert_cli_parity(cli, toy_server, prefix="toy_", samples=_SAMPLES)
    # Neither landed in overrides => both went down the generated branch, which is the only
    # one that runs the option-parity check.
    assert report.overrides == []


def test_assert_cli_parity_restores_process_state(toy_server, toy_group):
    """The suite runs inside an adopter's pytest session: it must not leave os._exit signal
    handlers or a stripped root logger behind."""
    cli = _mounted(toy_server, toy_group)

    def sentinel_handler(signum, frame):  # pragma: no cover - never invoked
        pass

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_term = signal.signal(signal.SIGTERM, sentinel_handler)
    saved_int = signal.signal(signal.SIGINT, sentinel_handler)
    sentinel_log_handler = logging.NullHandler()
    root.handlers[:] = [sentinel_log_handler]
    root.setLevel(logging.CRITICAL)
    try:
        assert_cli_parity(cli, toy_server, prefix="toy_", samples=_SAMPLES)
        assert signal.getsignal(signal.SIGTERM) is sentinel_handler
        assert signal.getsignal(signal.SIGINT) is sentinel_handler
        assert root.handlers == [sentinel_log_handler]
        assert root.level == logging.CRITICAL
    finally:
        signal.signal(signal.SIGTERM, saved_term)
        signal.signal(signal.SIGINT, saved_int)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# --- linc-cli-kit#2: stdout pollution, transport-bypassing overrides, named JSON failures ---


def _polluting_server(write):
    """A toy server whose only tool runs `write()` before returning its payload."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("polluter")

    @server.tool()
    def pol_probe(device: str) -> dict:
        """Probe a device."""
        write()
        return {"device": device}

    return server


def _parity_on(server):
    @click.group()
    def cli() -> None:
        """polluter"""

    install_globals(cli)
    mount_tools(cli, server, prefix="pol_", transport=LocalTransport())
    return assert_cli_parity(cli, server, prefix="pol_", samples={"pol_probe": {"device": "X"}})


def test_real_call_writing_to_bound_stdout_fails():
    """The structlog failure mode: a logger bound at import time holds the process stdout
    object, which CliRunner does not swap out. The real-transport run must see it."""
    import sys

    bound = sys.stdout  # what a module-level logger captured at import

    def write() -> None:
        bound.write("device_probe event=log\n")
        bound.flush()

    with pytest.raises(AssertionError, match=r"pol_probe.*process stdout"):
        _parity_on(_polluting_server(write))


def test_real_call_writing_to_fd1_fails():
    """A subprocess or C extension inheriting fd 1 corrupts the payload the same way."""
    import os

    def write() -> None:
        os.write(1, b"adb: daemon started\n")

    with pytest.raises(AssertionError, match=r"pol_probe.*process stdout"):
        _parity_on(_polluting_server(write))


def test_real_call_printing_fails_with_named_json_assertion():
    """print() inside the tool lands in the CLI's stdout buffer. The failure must name the
    tool and the stream, not surface as a bare JSONDecodeError."""

    def write() -> None:
        print("debug: probing")

    with pytest.raises(AssertionError, match=r"pol_probe.*stdout is not valid JSON"):
        _parity_on(_polluting_server(write))


def test_clean_real_call_passes():
    report = _parity_on(_polluting_server(lambda: None))
    assert report.tools == 1


def test_unsampled_override_that_bypasses_transport_fails(toy_server, toy_group):
    """An override that hard-codes its output never exercises the daemon/local transport,
    so its JSON contract is fiction. Without a sample it used to pass silently."""

    @toy_group.command("echo")
    def echo() -> None:
        click.echo(json.dumps({"ok": True}))

    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=LocalTransport())
    with pytest.raises(AssertionError, match=r"echo \(override\).*never called the transport"):
        assert_cli_parity(toy_group, toy_server, prefix="toy_")


def test_non_json_stderr_envelope_is_a_named_assertion(toy_server, toy_group):
    """A command that writes text instead of an envelope on failure is named, with the
    offending stream, rather than raising JSONDecodeError."""
    cli = _mounted(toy_server, toy_group)
    cmd = cli.commands["echo"]
    spec = next(s for s in registry_of(toy_server) if s.name == "toy_echo")

    def text_errors(**kwargs) -> None:
        ctx = click.get_current_context()
        result = cmd.linc_transport.call(spec, {k: v for k, v in kwargs.items() if v is not None})
        if isinstance(result, dict) and result.get("error"):
            click.echo("Error: it broke", err=True)
            ctx.exit(2 if result.get("validation_error") else 1)
        ctx.exit(emit(result, mode="json"))

    cmd.callback = text_errors
    with pytest.raises(AssertionError, match=r"toy_echo.*stderr is not valid JSON"):
        assert_cli_parity(cli, toy_server, prefix="toy_")


def test_non_object_json_on_stderr_is_a_named_assertion(toy_server, toy_group):
    """Valid JSON that is not an envelope object (a list, a bare string) must fail as a
    named contract assertion, not an AttributeError."""
    cli = _mounted(toy_server, toy_group)
    cmd = cli.commands["echo"]
    spec = next(s for s in registry_of(toy_server) if s.name == "toy_echo")

    def list_errors(**kwargs) -> None:
        ctx = click.get_current_context()
        result = cmd.linc_transport.call(spec, {k: v for k, v in kwargs.items() if v is not None})
        if isinstance(result, dict) and result.get("error"):
            click.echo(json.dumps(["boom"]), err=True)
            ctx.exit(2 if result.get("validation_error") else 1)
        ctx.exit(emit(result, mode="json"))

    cmd.callback = list_errors
    with pytest.raises(AssertionError, match=r"toy_echo.*no envelope on stderr"):
        assert_cli_parity(cli, toy_server, prefix="toy_")

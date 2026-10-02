import json

import click
import pytest
from click.testing import CliRunner

from linc_cli_kit.mount import install_globals, mount_tools
from linc_cli_kit.transport import LocalTransport


def _cli(toy_server, toy_group, **kw):
    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=LocalTransport(), **kw)
    return toy_group


def test_every_tool_mounted_with_stripped_names(toy_server, toy_group):
    cli = _cli(toy_server, toy_group)
    assert {"echo", "extras", "tags", "reserved", "fail", "async", "schema"} <= set(cli.commands)


def test_json_default_and_named_options(toy_server, toy_group):
    args = ["echo", "--device", "X", "--count", "2", "--loud"]
    r = CliRunner().invoke(_cli(toy_server, toy_group), args)
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout) == {"device": "X", "count": 2, "ratio": 0.5, "loud": True}
    assert r.stderr == ""


def test_missing_required_is_exit_2_with_envelope(toy_server, toy_group):
    r = CliRunner().invoke(_cli(toy_server, toy_group), ["echo"])
    assert r.exit_code == 2 and r.stdout == ""
    assert json.loads(r.stderr)["error"]["category"] == "validation"


def test_tool_error_dict_routes_to_stderr(toy_server, toy_group):
    r = CliRunner().invoke(_cli(toy_server, toy_group), ["fail", "--kind", "x"])
    assert r.exit_code == 1 and r.stdout == ""
    assert json.loads(r.stderr)["error"]["message"] == "device offline"
    r = CliRunner().invoke(_cli(toy_server, toy_group), ["fail", "--kind", "validation"])
    assert r.exit_code == 2
    r = CliRunner().invoke(_cli(toy_server, toy_group), ["fail", "--kind", "raise"])
    assert r.exit_code == 1 and json.loads(r.stderr)["error"]["code"] == "RUNTIME_ERROR"


def test_json_object_and_array_options(toy_server, toy_group):
    cli = _cli(toy_server, toy_group)
    r = CliRunner().invoke(cli, ["extras", "--device", "X", "--extras-json", '{"a": 1}'])
    assert json.loads(r.stdout) == {"device": "X", "extras": {"a": 1}}
    r = CliRunner().invoke(cli, ["tags", "--tags", "a", "--tags", "b"])
    assert json.loads(r.stdout) == {"tags": ["a", "b"], "mode": "fast"}


def test_reserved_names_use_arg_prefix(toy_server, toy_group):
    args = ["reserved", "--arg-json", "v", "--arg-output", "o"]
    r = CliRunner().invoke(_cli(toy_server, toy_group), args)
    assert json.loads(r.stdout) == {"json": "v", "output": "o"}


def test_human_flag_and_env(toy_server, toy_group, monkeypatch):
    cli = _cli(toy_server, toy_group, renderers={"echo": lambda r: f"device={r['device']}"})
    r = CliRunner().invoke(cli, ["--human", "echo", "--device", "X"])
    assert r.stdout == "device=X\n"
    monkeypatch.setenv("LINC_OUTPUT", "human")
    r = CliRunner().invoke(cli, ["echo", "--device", "X"])
    assert r.stdout == "device=X\n"
    r = CliRunner().invoke(cli, ["--json", "echo", "--device", "X"])
    assert json.loads(r.stdout)["device"] == "X"


def test_hand_written_command_wins(toy_server, toy_group):
    @toy_group.command("echo")
    def echo() -> None:
        click.echo("handwritten")

    cli = _cli(toy_server, toy_group)
    r = CliRunner().invoke(cli, ["echo"])
    assert r.stdout == "handwritten\n"


def test_skip_requires_justification(toy_server, toy_group):
    cli = _cli(toy_server, toy_group, skip={"toy_fail": "test-only failure tool"})
    assert "fail" not in cli.commands


def test_help_lists_required_before_optional(toy_server, toy_group):
    r = CliRunner().invoke(_cli(toy_server, toy_group), ["echo", "--help"])
    assert r.output.index("--device") < r.output.index("--count")
    assert "Example: toy echo --device X --count 2" in r.output


def test_schema_json_lists_all_commands(toy_server, toy_group):
    r = CliRunner().invoke(
        _cli(toy_server, toy_group), ["schema", "--json"], prog_name="toy"
    )
    assert r.exit_code == 0
    doc = json.loads(r.stdout)
    # "name" is the invoked program name (argv[0]-ish), not the Python object's name — an
    # adopter whose console script differs from its Click group variable must still see the
    # name an agent would actually type.
    assert isinstance(doc["name"], str) and doc["name"]
    assert doc["name"] == "toy"
    echo = doc["commands"]["echo"]
    assert echo["options"]["device"] == {
        "flag": "--device",
        "type": "STRING",
        "required": True,
        "default": None,
        "choices": None,
        "help": "",
    }
    assert "schema" in doc["commands"] and doc["globals"]["--human"]


def test_mount_refuses_name_collision_with_schema(toy_server, toy_group):
    @toy_group.command("schema")
    def schema() -> None:
        pass

    with pytest.raises(ValueError, match="schema"):
        _cli(toy_server, toy_group)


def test_renderer_failure_yields_envelope(toy_server, toy_group):
    def bad_renderer(_result):
        return {}["missing"]  # KeyError, raised from inside emit()'s human-mode path

    cli = _cli(toy_server, toy_group, renderers={"echo": bad_renderer})
    r = CliRunner().invoke(cli, ["--human", "echo", "--device", "X"])
    assert r.exit_code == 1
    assert r.stdout == ""
    # output.py's human-mode envelope prints "Error: <message>" (no separate JSON "code"
    # field) — the KEY_ERROR classification itself is covered by test_errors.py's
    # test_generic_exception_code_is_class_name_upper_snake. What this test guards is the
    # regression: before finding-1's fix, this path crashed with a raw traceback and NEITHER
    # stdout NOR stderr carried anything — now it must be a proper (human-formatted) envelope.
    assert r.stderr != ""
    assert "missing" in r.stderr


def test_stream_generator_failure_yields_envelope(toy_server, toy_group):
    class _BoomTransport:
        def call(self, spec, payload, *, timeout=None):
            def gen():
                yield {"i": 1}
                raise RuntimeError("stream boom")

            return gen()

    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=_BoomTransport())
    r = CliRunner().invoke(toy_group, ["--stream", "echo", "--device", "X"])
    assert r.exit_code == 1
    assert '{"i": 1}' in r.stdout  # the one record already flushed before the raise
    assert json.loads(r.stderr)["error"]["code"] == "RUNTIME_ERROR"


def test_standalone_mode_cannot_bypass_envelope(toy_server, toy_group):
    cli = _cli(toy_server, toy_group)
    r = CliRunner().invoke(cli, ["echo"], standalone_mode=True)
    assert r.exit_code == 2
    assert r.stdout == ""
    assert json.loads(r.stderr)["error"]["category"] == "validation"


def test_timeout_flag_reaches_transport(toy_server, toy_group):
    seen = {}

    class _RecordingTransport:
        def call(self, spec, payload, *, timeout=None):
            seen["timeout"] = timeout
            return {"device": payload.get("device")}

    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=_RecordingTransport())
    r = CliRunner().invoke(toy_group, ["--timeout", "7", "echo", "--device", "X"])
    assert r.exit_code == 0, r.stderr
    assert seen["timeout"] == 7.0


def test_skip_with_blank_justification_raises(toy_server, toy_group):
    with pytest.raises(ValueError):
        _cli(toy_server, toy_group, skip={"toy_fail": " "})


def test_skip_unknown_tool_raises(toy_server, toy_group):
    with pytest.raises(ValueError, match="toy_bogus"):
        _cli(toy_server, toy_group, skip={"toy_bogus": "not a real tool"})


def test_mount_without_globals_raises(toy_server, toy_group):
    with pytest.raises(RuntimeError, match="install_globals"):
        mount_tools(toy_group, toy_server, prefix="toy_", transport=LocalTransport())


# --- linc-cli-kit#4: the process shell around every adopter CLI ---


def _with_override(toy_server, toy_group, body):
    toy_group.command("boom")(body)
    return _cli(toy_server, toy_group)


def test_raising_override_emits_envelope_not_traceback(toy_server, toy_group):
    def boom() -> None:
        """raises"""
        raise RuntimeError("override exploded")

    r = CliRunner().invoke(_with_override(toy_server, toy_group, boom), ["boom"])
    assert r.exit_code == 1, r.output
    assert r.stdout == ""
    err = json.loads(r.stderr.strip().splitlines()[-1])["error"]
    assert err["code"] == "RUNTIME_ERROR" and err["message"] == "override exploded"


def test_raising_override_honours_human_mode(toy_server, toy_group):
    def boom() -> None:
        """raises"""
        raise RuntimeError("override exploded")

    r = CliRunner().invoke(_with_override(toy_server, toy_group, boom), ["--human", "boom"])
    assert r.exit_code == 1
    assert r.stderr.startswith("Error: override exploded")


def test_override_raising_usage_error_keeps_exit_2(toy_server, toy_group):
    from linc_cli_kit import UsageError

    def boom() -> None:
        """raises a kit usage error"""
        raise UsageError("pick one of --a/--b")

    r = CliRunner().invoke(_with_override(toy_server, toy_group, boom), ["boom"])
    assert r.exit_code == 2
    assert json.loads(r.stderr)["error"]["code"] == "USAGE_ERROR"


def test_interrupt_is_exit_130_with_envelope(toy_server, toy_group):
    def boom() -> None:
        """interrupted"""
        raise KeyboardInterrupt

    r = CliRunner().invoke(_with_override(toy_server, toy_group, boom), ["boom"])
    assert r.exit_code == 130
    assert r.stdout == ""
    assert json.loads(r.stderr.strip().splitlines()[-1])["error"]["code"] == "INTERRUPTED"


def test_slow_failing_tool_leaves_envelope_as_last_stderr_line(toy_server, toy_group, monkeypatch):
    """The audit repro: a heartbeat before the envelope broke json.loads(stderr). Every line
    is now JSON and the envelope is the last one."""
    import functools
    import time

    from linc_cli_kit import mount as mount_mod
    from linc_cli_kit import parse_error_envelope
    from linc_cli_kit.output import heartbeat

    monkeypatch.setattr(mount_mod, "heartbeat", functools.partial(heartbeat, interval=0.02))

    class _SlowFail:
        def call(self, spec, payload, *, timeout=None):
            time.sleep(0.1)
            return {"error": "device offline"}

    install_globals(toy_group)
    mount_tools(toy_group, toy_server, prefix="toy_", transport=_SlowFail())
    r = CliRunner().invoke(toy_group, ["echo", "--device", "X"])
    assert r.exit_code == 1 and r.stdout == ""
    lines = r.stderr.strip().splitlines()
    assert len(lines) >= 2, "expected at least one heartbeat before the envelope"
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert json.loads(lines[-1])["error"]["message"] == "device offline"
    assert parse_error_envelope(r.stderr)["error"]["message"] == "device offline"


_SIGNAL_CLI = r'''
import sys, time, pathlib
import click
from mcp.server.fastmcp import FastMCP
from linc_cli_kit import install_globals, mount_tools

marker = pathlib.Path(sys.argv[1])
server = FastMCP("sig")

@server.tool()
def sig_hold(device: str) -> dict:
    """Hold a device lease until interrupted."""
    try:
        print("reserved", file=sys.stderr, flush=True)
        time.sleep(30)
    finally:
        marker.write_text("released")
    return {"ok": True}

@server.tool()
def sig_stubborn(device: str) -> dict:
    """Cleanup that never finishes, or code that swallows the exit."""
    import os
    try:
        print("reserved", file=sys.stderr, flush=True)
        time.sleep(30)
    except BaseException:
        if os.environ.get("SWALLOW"):
            time.sleep(30)  # a bare except/retry loop that eats SystemExit
        raise
    finally:
        time.sleep(30)  # blocking cleanup
    return {"ok": True}

@server.tool()
def sig_slowclean(device: str) -> dict:
    """Cleanup that takes a moment (a lease release round trip)."""
    try:
        print("reserved", file=sys.stderr, flush=True)
        time.sleep(30)
    finally:
        time.sleep(0.3)
        marker.write_text("released")
    return {"ok": True}

@click.group()
def cli():
    """sig"""

install_globals(cli)
mount_tools(cli, server, prefix="sig_")
cli(sys.argv[2:])
'''


@pytest.mark.parametrize(("sig", "code"), [("SIGTERM", 143), ("SIGINT", 130)])
def test_signal_runs_cleanup_in_a_real_process(tmp_path, sig, code):
    """os._exit skipped finally blocks, so nerve never released a reserved device on Ctrl-C
    or a harness kill. The exit codes stay 143/130."""
    import signal as signal_mod
    import subprocess
    import sys
    import time

    script = tmp_path / "sig_cli.py"
    script.write_text(_SIGNAL_CLI)
    marker = tmp_path / "lease"
    proc = subprocess.Popen(
        [sys.executable, str(script), str(marker), "hold", "--device", "X"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 20
    line = ""
    while "reserved" not in line and time.monotonic() < deadline:
        line = proc.stderr.readline()
    assert "reserved" in line, "tool never started"
    proc.send_signal(getattr(signal_mod, sig))
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == code
    assert marker.read_text() == "released"
    assert out == ""


def _start(tmp_path, *argv, env=None):
    import os
    import subprocess
    import sys
    import time

    script = tmp_path / "sig_cli.py"
    script.write_text(_SIGNAL_CLI)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path / "lease"), *argv],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, **(env or {})},
    )
    deadline = time.monotonic() + 20
    line = ""
    while "reserved" not in line and time.monotonic() < deadline:
        line = proc.stderr.readline()
    assert "reserved" in line, "tool never started"
    return proc


def test_second_sigterm_forces_exit_when_cleanup_blocks(tmp_path):
    """Review of #7: unwinding must not make SIGTERM unkillable. A repeat signal means the
    operator wants out now, so it hard-exits even though cleanup is still blocked."""
    import signal as signal_mod
    import time

    proc = _start(tmp_path, "stubborn", "--device", "X", env={"LINC_SIGNAL_GRACE": "60"})
    try:
        proc.send_signal(signal_mod.SIGTERM)
        time.sleep(1.0)  # past the 0.5s fan-out window
        assert proc.poll() is None, "first SIGTERM should be unwinding through the finally"
        started = time.monotonic()
        proc.send_signal(signal_mod.SIGTERM)
        proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 143
    assert time.monotonic() - started < 5


def test_sigterm_watchdog_exits_when_exit_is_swallowed(tmp_path):
    """A single SIGTERM still terminates within the grace period even when tool code
    swallows SystemExit (bare except / retry loop) or cleanup blocks."""
    import signal as signal_mod
    import time

    proc = _start(
        tmp_path, "stubborn", "--device", "X", env={"LINC_SIGNAL_GRACE": "0.5", "SWALLOW": "1"}
    )
    started = time.monotonic()
    try:
        proc.send_signal(signal_mod.SIGTERM)
        proc.communicate(timeout=15)
    finally:
        proc.kill()
    assert proc.returncode == 143
    assert time.monotonic() - started < 10


def test_heartbeat_without_mode_honours_the_human_flag(toy_server, toy_group):
    """Review of #7: a hand-written command calling heartbeat() with no mode must follow
    --human like every other part of the contract, not just LINC_OUTPUT."""
    import time

    from linc_cli_kit import heartbeat

    @toy_group.command("slow")
    def slow() -> None:
        """beats"""
        with heartbeat(interval=0.02, label="slow"):
            time.sleep(0.1)
        click.echo(json.dumps({"ok": True}))

    cli = _cli(toy_server, toy_group)
    r = CliRunner().invoke(cli, ["--human", "slow"])
    assert r.exit_code == 0
    assert "[slow]" in r.stderr and "elapsed" in r.stderr
    r = CliRunner().invoke(cli, ["slow"])
    assert json.loads(r.stderr.splitlines()[0])["event"] == "heartbeat"


def test_fanned_out_sigterm_does_not_skip_cleanup(tmp_path):
    """Review of #7: a wrapper in the same process group can deliver one termination
    request twice within milliseconds. That must not hard-exit past the cleanup."""
    import signal as signal_mod
    import time

    proc = _start(tmp_path, "slowclean", "--device", "X")
    try:
        proc.send_signal(signal_mod.SIGTERM)
        time.sleep(0.05)  # distinct deliveries: back-to-back pending signals coalesce
        proc.send_signal(signal_mod.SIGTERM)
        proc.communicate(timeout=15)
    finally:
        proc.kill()
    assert proc.returncode == 143
    assert (tmp_path / "lease").read_text() == "released"

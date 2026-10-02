import io
import json
import signal
import sys
import time
from contextlib import redirect_stderr, redirect_stdout

import pytest

from linc_cli_kit.errors import ToolError
from linc_cli_kit.output import emit, emit_exception, harden_streams, heartbeat, resolve_mode


@pytest.mark.parametrize(
    "flag,env,expected",
    [
        (None, {}, "json"),
        (None, {"LINC_OUTPUT": "human"}, "human"),
        (None, {"LINC_OUTPUT": "jsonl"}, "jsonl"),
        ("human", {"LINC_OUTPUT": "json"}, "human"),
        ("jsonl", {}, "jsonl"),
        (None, {"LINC_OUTPUT": "garbage"}, "json"),
    ],
)
def test_resolve_mode_precedence(flag, env, expected):
    assert resolve_mode(flag, env) == expected


def _capture(fn):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn()
    return code, out.getvalue(), err.getvalue()


def test_json_success_is_bare_compact_document():
    code, out, err = _capture(lambda: emit({"a": 1}, mode="json"))
    assert code == 0 and err == ""
    assert out == '{"a": 1}\n'


def test_failure_leaves_stdout_empty_and_envelope_on_stderr():
    code, out, err = _capture(lambda: emit({"error": "offline"}, mode="json"))
    assert code == 1 and out == ""
    assert json.loads(err)["error"]["message"] == "offline"


def test_validation_failure_exit_2():
    code, out, err = _capture(
        lambda: emit({"error": "bad", "validation_error": True}, mode="json")
    )
    assert code == 2 and out == "" and json.loads(err)["error"]["category"] == "validation"


def test_exception_path():
    code, out, err = _capture(
        lambda: emit_exception(ToolError("x", exit_code=1), mode="json")
    )
    assert code == 1 and out == "" and json.loads(err)["error"]["code"] == "TOOL_ERROR"


def test_human_failure_prints_error_prefix():
    code, out, err = _capture(
        lambda: emit_exception(ToolError("x", hint="try y"), mode="human")
    )
    assert code == 1 and out == "" and err == "Error: x\nHint: try y\n"


def test_human_success_without_renderer_pretty_prints():
    code, out, _ = _capture(lambda: emit({"a": [1, 2]}, mode="human"))
    assert code == 0 and out == '{\n  "a": [\n    1,\n    2\n  ]\n}\n'


def test_human_success_with_renderer():
    _, out, _ = _capture(
        lambda: emit({"n": 3}, mode="human", renderer=lambda r: f"{r['n']} devices")
    )
    assert out == "3 devices\n"


def test_jsonl_iterates_generator_and_appends_summary():
    def gen():
        yield {"event": "record", "i": 1}
        yield {"event": "record", "i": 2}

    code, out, _ = _capture(lambda: emit(gen(), mode="jsonl"))
    lines = [json.loads(line) for line in out.splitlines()]
    assert code == 0
    assert lines[:2] == [{"event": "record", "i": 1}, {"event": "record", "i": 2}]
    assert lines[2] == {"event": "summary", "records": 2, "exit_code": 0}


def test_jsonl_single_result_is_one_line():
    _, out, _ = _capture(lambda: emit({"a": 1}, mode="jsonl"))
    assert out == '{"a": 1}\n'


def test_image_result_is_written_to_path_and_referenced(tmp_path):
    from mcp.server.fastmcp import Image

    target = tmp_path / "shot.png"
    payload = [{"device": "X"}, Image(data=b"PNGDATA", format="png")]
    code, out, _ = _capture(lambda: emit(payload, mode="json", output_path=str(target)))
    doc = json.loads(out)
    assert code == 0
    assert doc["result"] == {"device": "X"}
    assert doc["artifact"]["path"] == str(target)
    assert doc["artifact"]["type"] == "image/png"
    assert doc["artifact"]["bytes"] == 7
    assert len(doc["artifact"]["sha256"]) == 64
    assert target.read_bytes() == b"PNGDATA"


def test_json_mode_materializes_generator():
    def gen():
        yield {"event": "record", "i": 1}
        yield {"event": "record", "i": 2}

    code, out, _ = _capture(lambda: emit(gen(), mode="json"))
    assert code == 0
    doc = json.loads(out)
    assert doc == [{"event": "record", "i": 1}, {"event": "record", "i": 2}]


def test_artifact_write_failure_is_envelope_not_traceback(tmp_path):
    from mcp.server.fastmcp import Image

    blocker = tmp_path / "blocker"
    blocker.write_text("file content")
    bad_path = str(blocker / "x.png")
    payload = [{"device": "X"}, Image(data=b"PNGDATA", format="png")]
    code, out, err = _capture(
        lambda: emit(payload, mode="json", output_path=bad_path)
    )
    assert code == 1 and out == ""
    assert json.loads(err)["error"]["code"] == "ARTIFACT_WRITE_FAILED"


def test_heartbeat_emits_text_lines_in_human_mode():
    out, err = io.StringIO(), io.StringIO()
    with (
        redirect_stdout(out),
        redirect_stderr(err),
        heartbeat(interval=0.05, label="t", mode="human"),
    ):
        # Poll instead of a fixed sleep: slow CI runners (macOS) can schedule the heartbeat
        # thread late, so a fixed 0.18 s window sometimes saw only one line.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _elapsed_lines(err.getvalue(), "t") < 2:
            time.sleep(0.02)
    assert _elapsed_lines(err.getvalue(), "t") >= 2


def _elapsed_lines(text: str, label: str) -> int:
    return sum(1 for line in text.split("\n") if f"[{label}]" in line and "elapsed" in line)


@pytest.mark.parametrize("mode", ["json", "jsonl", None])
def test_heartbeat_is_json_lines_outside_human_mode(mode, monkeypatch):
    """linc-cli-kit#4: a text beat before the envelope broke json parsing of stderr. Every
    beat is its own JSON object, so a consumer can parse stderr line by line."""
    monkeypatch.delenv("LINC_OUTPUT", raising=False)
    err = io.StringIO()
    with redirect_stderr(err), heartbeat(interval=0.05, label="t", mode=mode):
        time.sleep(0.18)
    lines = [line for line in err.getvalue().splitlines() if line]
    assert len(lines) >= 2
    for line in lines:
        beat = json.loads(line)
        assert beat["event"] == "heartbeat" and beat["label"] == "t"
        assert isinstance(beat["elapsed_s"], int)


def test_heartbeat_thread_is_joined_on_exit():
    """A beat must never land AFTER the envelope: the thread is gone once the block exits."""
    import threading

    err = io.StringIO()
    with redirect_stderr(err), heartbeat(interval=0.01, label="joined"):
        time.sleep(0.03)
        assert [t for t in threading.enumerate() if t.name == "linc-heartbeat-joined"]
    assert not [t for t in threading.enumerate() if t.name == "linc-heartbeat-joined"]


def test_parse_error_envelope_takes_the_last_envelope_line():
    from linc_cli_kit import parse_error_envelope

    stderr = (
        '{"event": "heartbeat", "label": "x", "elapsed_s": 10}\n'
        "WARNING linc_x: device slow\n"
        '{"error": {"code": "TOOL_ERROR", "message": "device offline"}}\n'
    )
    assert parse_error_envelope(stderr)["error"]["message"] == "device offline"
    assert parse_error_envelope('{"event": "heartbeat"}\nplain text\n') is None
    assert parse_error_envelope("") is None


def test_sigterm_raises_system_exit_so_cleanup_runs():
    """linc-cli-kit#4: os._exit skipped every finally block (nerve's lease release)."""
    import os

    saved_sigterm = signal.getsignal(signal.SIGTERM)
    saved_sigint = signal.getsignal(signal.SIGINT)
    cleaned = []
    try:
        harden_streams()
        with pytest.raises(SystemExit) as excinfo:
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)  # the handler fires before this returns
            finally:
                cleaned.append("term")
        assert excinfo.value.code == 143
        # Disarm the grace-period watchdog and the "second signal hard-exits" latch, or the
        # next signal (and the timer) would os._exit this pytest process.
        from linc_cli_kit import output

        output._reset_signal_state()
        # SIGINT keeps Python's semantics: every Ctrl-C raises, and a command that catches
        # one and carries on is not hard-killed by a later one (review of #7).
        for _ in range(2):
            with pytest.raises(KeyboardInterrupt):
                try:
                    os.kill(os.getpid(), signal.SIGINT)
                    time.sleep(1)
                finally:
                    cleaned.append("int")
        assert cleaned == ["term", "int", "int"]
    finally:
        from linc_cli_kit import output

        output._reset_signal_state()
        signal.signal(signal.SIGTERM, saved_sigterm)
        signal.signal(signal.SIGINT, saved_sigint)


def test_harden_streams_sets_line_buffering_and_signal_handlers():
    saved_sigterm = signal.getsignal(signal.SIGTERM)
    saved_sigint = signal.getsignal(signal.SIGINT)
    try:
        harden_streams()
        if hasattr(sys.stdout, "line_buffering"):
            assert sys.stdout.line_buffering is True
        from linc_cli_kit import output

        # Identity with the kit's handlers, not inequality with whatever was installed
        # before: an earlier CliRunner invocation may already have installed these.
        assert signal.getsignal(signal.SIGTERM) is output._on_sigterm
        assert signal.getsignal(signal.SIGINT) is output._on_sigint
    finally:
        signal.signal(signal.SIGTERM, saved_sigterm)
        signal.signal(signal.SIGINT, saved_sigint)


def test_watchdog_is_one_thread_started_outside_the_handler():
    """Review of #7: Thread.start() inside a signal handler can deadlock on threading's
    internal locks. harden_streams() starts one watchdog per process, reused."""
    import threading

    from linc_cli_kit import output

    saved = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    try:
        harden_streams()
        harden_streams()
        watchdogs = [t for t in threading.enumerate() if t.name == "linc-signal-watchdog"]
        assert len(watchdogs) == 1 and watchdogs[0].daemon
    finally:
        output._reset_signal_state()
        signal.signal(signal.SIGTERM, saved[0])
        signal.signal(signal.SIGINT, saved[1])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("inf", "max"), ("1e20", "max"), ("nan", 10.0), ("-3", 0.0), ("junk", 10.0), ("2.5", 2.5)],
)
def test_signal_grace_is_clamped(monkeypatch, raw, expected):
    """Review of #7: a grace above threading.TIMEOUT_MAX (or inf) made Condition.wait raise
    OverflowError and killed the watchdog thread mid-unwind."""
    import threading

    from linc_cli_kit import output

    monkeypatch.setenv("LINC_SIGNAL_GRACE", raw)
    want = threading.TIMEOUT_MAX if expected == "max" else expected
    assert output._signal_grace() == want


def test_watchdog_exits_outside_the_lock_without_flushing(monkeypatch):
    """Review of #7: exiting while holding the condition, after a stderr flush that can
    block on a full pipe, deadlocked the watchdog AND the repeat-SIGTERM path."""
    import os
    import threading
    import time as time_mod

    from linc_cli_kit import output

    seen: dict[str, object] = {}
    done, release = threading.Event(), threading.Event()

    class _StuckStderr:
        def write(self, _text):
            return 0

        def flush(self):
            seen["flushed"] = True

    def fake_exit(code):
        seen["code"] = code
        seen["lock_held"] = output._watchdog_cond._is_owned()
        done.set()
        release.wait(5)  # then the loop resumes and finds the watchdog disarmed

    saved = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    try:
        harden_streams()
        monkeypatch.setattr(os, "_exit", fake_exit)
        monkeypatch.setattr(sys, "stderr", _StuckStderr())
        with output._watchdog_cond:
            output._signal_state.update(term_at=time_mod.monotonic(), deadline=0.0)
            output._watchdog_cond.notify_all()
        assert done.wait(5), "watchdog never fired"
        assert seen == {"code": 143, "lock_held": False}
    finally:
        output._reset_signal_state()
        release.set()
        signal.signal(signal.SIGTERM, saved[0])
        signal.signal(signal.SIGINT, saved[1])

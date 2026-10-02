"""Output contract (spec §5.2): mode resolution, stdout/stderr routing, artifacts, heartbeat."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime
import hashlib
import json
import math
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from linc_cli_kit.errors import Outcome, ToolError, classify, classify_exception, safe_str

MODES = ("json", "human", "jsonl")


def resolve_mode(flag: str | None, env: Mapping[str, str] | None = None) -> str:
    if flag in MODES:
        return flag  # type: ignore[return-value]
    env = os.environ if env is None else env
    value = env.get("LINC_OUTPUT", "").lower()
    return value if value in MODES else "json"


def _pretty() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def json_default(obj: Any) -> Any:
    """`json.dumps(default=...)` for tool results: structured types become structured JSON.

    - dataclass instance -> object of its fields (shallow; nested values recurse through
      this hook, so nothing is deep-copied)
    - pydantic `BaseModel` instance -> `model_dump(mode="json")`
    - set / frozenset -> sorted list (by `repr` when the members are not mutually orderable)
    - bytes / bytearray / memoryview -> base64 string (standard alphabet, padded). Binary
      content large enough to matter should be an artifact (`-o/--output`), not a payload.
    - datetime / date / time -> ISO 8601 (`isoformat()`)
    - anything else -> `str(obj)` (Path, Decimal, UUID, Enum ... as before)
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    # A real pydantic model only, never a duck-typed `model_dump` attribute: a MagicMock
    # answers every attribute with a callable returning another mock, which recursed without
    # end. Looked up in sys.modules so the kit never imports pydantic itself.
    pydantic = sys.modules.get("pydantic")
    if pydantic is not None and isinstance(obj, pydantic.BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, (set, frozenset)):
        try:
            return sorted(obj)
        except TypeError:
            return sorted(obj, key=repr)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    return str(obj)


def to_json(doc: Any, *, indent: int | None = None) -> str:
    """Strict JSON: typed `json_default` and `allow_nan=False`.

    NaN/Infinity are not JSON (jq and strict parsers reject the whole document), so a
    non-finite float raises ValueError here; `emit` turns that into a NON_JSON_VALUE
    envelope instead of writing invalid output.
    """
    return json.dumps(doc, default=json_default, allow_nan=False, indent=indent)


def _non_json(exc: Exception) -> ToolError:
    return ToolError(
        f"result is not representable as JSON: {exc}",
        code="NON_JSON_VALUE",
        hint="return JSON-compatible values: None instead of NaN/Infinity, string dict keys",
        category="internal",
    )


def _finite(doc: Any, _seen: frozenset[int] = frozenset()) -> Any:
    """Make envelope details (tool data) always encodable: non-finite floats become strings
    ("nan"), other values go through `json_default` and are recursed into, non-JSON dict
    keys become `str(key)`, and a reference cycle becomes "<cycle>". The failure path must
    never fail, nor drop good fields because one nested value is bad."""
    if isinstance(doc, float):
        return doc if math.isfinite(doc) else repr(doc)
    if doc is None or isinstance(doc, (str, int, bool)):
        return doc
    if id(doc) in _seen:
        return "<cycle>"
    seen = _seen | {id(doc)}
    if isinstance(doc, dict):
        out: dict[Any, Any] = {}
        for k, v in doc.items():
            key = _finite_key(k)
            if key in out:  # only via conversion, e.g. a NaN key vs a real "nan" key
                n = 2
                while f"{key}#{n}" in out:
                    n += 1
                key = f"{key}#{n}"
            out[key] = _finite(v, seen)
        return out
    if isinstance(doc, (list, tuple)):
        return [_finite(v, seen) for v in doc]
    try:
        converted = json_default(doc)
    except Exception:  # a hostile __str__, an unset dataclass field ...
        return f"<unencodable {type(doc).__name__}>"
    return converted if isinstance(converted, str) else _finite(converted, seen)


def _finite_key(key: Any) -> str:
    """The key exactly as it will be spelled in the JSON text, so collisions are visible
    here: json.dumps itself writes 1 / None / True keys as "1" / "null" / "true"."""
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, bool):
        return json.dumps(key)
    if isinstance(key, float) and not math.isfinite(key):
        return safe_str(float(key))  # "nan" / "inf", even for a float subclass
    if isinstance(key, (int, float)):
        return json.dumps(key)
    return safe_str(key)


def _dumps(doc: Any, *, force_pretty: bool = False) -> str:
    indent = 2 if (force_pretty or _pretty()) else None
    return to_json(doc, indent=indent)


def _write_envelope(envelope: dict[str, Any], mode: str) -> None:
    if mode == "human":
        err = envelope["error"]
        sys.stderr.write(f"Error: {err['message']}\n")
        if err.get("hint"):
            sys.stderr.write(f"Hint: {err['hint']}\n")
    else:
        try:
            line = to_json(_finite(envelope))
        except Exception:  # never let the failure path fail; keep the error itself
            err = dict(envelope.get("error", {}))
            err["details"] = {}
            line = to_json(_finite({"error": err}))
        sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _artifact_reference(
    image: Any, output_path: str | None, tool: str
) -> dict[str, Any]:
    content = image.to_image_content()
    raw = base64.b64decode(content.data)
    mime = content.mimeType
    ext = mime.split("/")[-1]
    if output_path is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output_path = str(Path.home() / ".linc" / "artifacts" / tool / f"{stamp}.{ext}")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {
        "type": mime,
        "path": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _normalize(result: Any, output_path: str | None, tool: str) -> Any:
    """Turn a FastMCP content list [dict, Image] into {"result":..., "artifact":...}."""
    if isinstance(result, list) and any(
        hasattr(item, "to_image_content") for item in result
    ):
        data = next((item for item in result if isinstance(item, dict)), {})
        image = next(item for item in result if hasattr(item, "to_image_content"))
        return {
            "result": data,
            "artifact": _artifact_reference(image, output_path, tool),
        }
    return result


def emit(
    result: Any,
    *,
    mode: str,
    output_path: str | None = None,
    renderer: Callable[[Any], str] | None = None,
    tool: str = "tool",
) -> int:
    """Write result per the contract and return the exit code. Never raises."""
    if mode == "jsonl" and isinstance(result, Iterator):
        count = 0
        for record in result:
            try:
                line = to_json(record)
            except Exception as exc:  # NaN, bad keys, a hostile __str__, absurd nesting
                _write_envelope(_non_json(exc).envelope(), mode)
                return 1
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            count += 1
        sys.stdout.write(
            json.dumps({"event": "summary", "records": count, "exit_code": 0}) + "\n"
        )
        sys.stdout.flush()
        return 0
    if isinstance(result, Iterator) and mode != "jsonl":
        result = list(result)
    try:
        normalized = _normalize(result, output_path, tool)
    except OSError as exc:
        err = ToolError(
            f"could not write artifact: {exc}",
            code="ARTIFACT_WRITE_FAILED",
            category="internal",
        )
        _write_envelope(err.envelope(), mode)
        return 1
    outcome: Outcome = classify(normalized)
    if outcome.exit_code != 0:
        _write_envelope(outcome.envelope or {}, mode)
        return outcome.exit_code
    try:
        if mode == "human":
            # Human output is not the JSON contract: NaN reads fine there.
            text = (
                renderer(outcome.payload)
                if renderer
                else json.dumps(outcome.payload, default=json_default, indent=2)
            )
            text = text.rstrip("\n")
        elif mode == "jsonl":
            text = to_json(outcome.payload)
        else:
            text = _dumps(outcome.payload)
    except Exception as exc:  # NaN, bad keys, a hostile __str__, absurd nesting
        if mode == "human" and renderer is not None:
            raise  # a renderer bug, not an encoding problem: mount's handler envelopes it
        _write_envelope(_non_json(exc).envelope(), mode)
        return 1
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return 0


def emit_exception(exc: BaseException, *, mode: str) -> int:
    outcome = classify_exception(exc)
    _write_envelope(outcome.envelope or {}, mode)
    return outcome.exit_code


@contextlib.contextmanager
def heartbeat(
    interval: float = 10.0, label: str = "working", *, mode: str | None = None
) -> Iterator[None]:
    """Emit a stderr line every `interval` seconds so harness dead-man timers see activity.

    Outside `--human` mode each beat is one JSON object
    (`{"event": "heartbeat", "label": ..., "elapsed_s": ...}`), so every stderr line a
    command writes parses on its own, and the error envelope stays the LAST line (see
    `parse_error_envelope`). `mode=None` follows the invocation's `--human`/`--json` flag,
    then `LINC_OUTPUT`, like the rest of the contract. The thread is joined on exit, so no
    beat can land after the envelope.
    """
    if mode is None:
        # Call-time import: mount imports this module. current_mode() reads the invocation's
        # --human/--json flag first and falls back to LINC_OUTPUT outside a Click context.
        from linc_cli_kit.mount import current_mode

        mode = current_mode()
    as_json = resolve_mode(mode) != "human"
    stop = threading.Event()

    def _beat() -> None:
        started = time.monotonic()
        while not stop.wait(interval):
            elapsed = int(time.monotonic() - started)
            if as_json:
                line = json.dumps({"event": "heartbeat", "label": label, "elapsed_s": elapsed})
            else:
                line = f"[{label}] {elapsed}s elapsed"
            sys.stderr.write(line + "\n")
            sys.stderr.flush()

    thread = threading.Thread(target=_beat, name=f"linc-heartbeat-{label}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # Bounded: a beat is only ever a wait() plus one write, so this returns at once
        # unless stderr itself is blocked (and then the envelope would block too).
        thread.join(timeout=1.0)


_SIGNAL_GRACE_S = 10.0
_REPEAT_WINDOW_S = 0.5

# SIGTERM watchdog. ONE daemon thread per process, started from harden_streams() (never
# from the handler: Thread.start() takes threading's internal locks, which the interrupted
# main thread may already hold). The handler only records a deadline and notifies, under an
# RLock so a signal landing while the main thread holds it cannot self-deadlock.
_watchdog_cond = threading.Condition(threading.RLock())
_signal_state: dict[str, Any] = {"thread": None, "term_at": None, "deadline": None}


def _signal_grace() -> float:
    """LINC_SIGNAL_GRACE seconds, clamped to [0, threading.TIMEOUT_MAX]: a larger value (or
    `inf`, "never force") would make Condition.wait raise OverflowError in the watchdog."""
    try:
        grace = float(os.environ.get("LINC_SIGNAL_GRACE", _SIGNAL_GRACE_S))
    except ValueError:
        return _SIGNAL_GRACE_S
    if math.isnan(grace):
        return _SIGNAL_GRACE_S
    return min(max(0.0, grace), threading.TIMEOUT_MAX)


def _watchdog_loop() -> None:
    while True:
        with _watchdog_cond:
            deadline = _signal_state["deadline"]
            if deadline is None:
                _watchdog_cond.wait()
                continue
            remaining = deadline - time.monotonic()
            if remaining > 0:
                _watchdog_cond.wait(min(remaining, threading.TIMEOUT_MAX))
                continue
        # Outside the lock, and no stderr flush first: a stuck stderr pipe (the main thread
        # blocked mid-write holding its buffer lock) is exactly the stall this breaks.
        os._exit(143)


def _ensure_watchdog() -> None:
    thread = _signal_state["thread"]
    if thread is None or not thread.is_alive():
        thread = threading.Thread(target=_watchdog_loop, name="linc-signal-watchdog", daemon=True)
        thread.start()
        _signal_state["thread"] = thread


def _reset_signal_state() -> None:
    """Disarm the SIGTERM watchdog and repeat latch (each invocation, and tests)."""
    with _watchdog_cond:
        _signal_state.update(term_at=None, deadline=None)
        _watchdog_cond.notify_all()


def _on_sigterm(_signum: int, _frame: Any) -> None:
    """Raise SystemExit(143) so `finally` blocks, context managers and atexit hooks run on
    the way out (nerve releases its device lease there) — but termination is not optional:
    the watchdog hard-exits 143 if the unwind has not finished after LINC_SIGNAL_GRACE
    seconds (default 10; blocking cleanup, an event-loop worker thread, code that swallows
    SystemExit), and a repeat SIGTERM hard-exits at once. A repeat inside 0.5s is taken as
    the same signal fanned out by a wrapper in the process group and ignored."""
    now = time.monotonic()
    with _watchdog_cond:
        first = _signal_state["term_at"]
        if first is None:
            _signal_state.update(term_at=now, deadline=now + _signal_grace())
            _watchdog_cond.notify_all()
        elif now - first < _REPEAT_WINDOW_S:
            return
    if first is not None:
        os._exit(143)  # outside the lock, no flush (see _watchdog_loop)
    raise SystemExit(143)


def _on_sigint(_signum: int, _frame: Any) -> None:
    # Python's own semantics: every Ctrl-C raises KeyboardInterrupt (a command may catch it
    # and carry on, e.g. `index watch`). Click turns it into Abort; install_globals' main
    # maps that to the INTERRUPTED envelope and exit 130.
    raise KeyboardInterrupt


def harden_streams() -> None:
    """Line-buffer stdout/stderr; make SIGTERM/SIGINT unwind the stack (exit 143/130).

    The handlers raise `SystemExit(143)` / `KeyboardInterrupt` rather than calling
    `os._exit`, so cleanup code runs; for SIGTERM a watchdog and a repeat signal still
    guarantee the process ends (see `_on_sigterm`). A tool that must not be interrupted
    mid-operation should shield that section itself.
    """
    _reset_signal_state()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(line_buffering=True)

    with contextlib.suppress(ValueError):  # not in main thread (CliRunner)
        signal.signal(signal.SIGTERM, _on_sigterm)
        signal.signal(signal.SIGINT, _on_sigint)
        _ensure_watchdog()

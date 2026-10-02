# linc-cli-kit

Turn a [FastMCP](https://github.com/modelcontextprotocol/python-sdk) tool registry into a Click
CLI with one agent-grade output contract: JSON on stdout, a structured error envelope on stderr,
exit codes `0`/`1`/`2`, and a `schema --json` dump of the whole command tree. An importable
conformance suite proves the CLI still matches the MCP tools. An agent that has learned one CLI
built this way has learned them all.

## Install

```bash
pip install linc-cli-kit        # or: uv add linc-cli-kit
```

Python 3.12+. Runtime dependencies: `click` and `mcp`.

## Example

Save as `hello_cli.py`:

```python
import click
from mcp.server.fastmcp import FastMCP

from linc_cli_kit import LocalTransport, install_globals, mount_tools

server = FastMCP("hello")


@server.tool()
def hello_greet(name: str, times: int = 1) -> dict:
    """Greet someone. Example: hello greet --name Ada --times 2"""
    return {"greeting": " ".join([f"Hello, {name}!"] * times)}


@click.group()
def main() -> None:
    """hello: JSON on stdout by default, --human for text."""


install_globals(main)
mount_tools(main, server, prefix="hello_", transport=LocalTransport())

if __name__ == "__main__":
    main()
```

```console
$ python hello_cli.py greet --name Ada --times 2
{"greeting": "Hello, Ada! Hello, Ada!"}

$ python hello_cli.py greet; echo "exit=$?"
{"error": {"code": "USAGE_ERROR", "message": "Missing option '--name'.", "hint": "", "category": "validation", "retryable": false, "details": {}}}
exit=2

$ python hello_cli.py schema --json     # the whole command tree, for an agent to read once
```

The error envelope goes to stderr and stdout stays empty, so `python hello_cli.py greet | jq`
never parses half a document.

## Wiring a real tool

```python
# src/<tool>/cli/main.py: a real tool with an optional local daemon
import sys
import click
from linc_cli_kit import (
    HttpDaemonTransport, LocalTransport, configure_structlog_stderr,
    install_globals, mount_tools,
)

configure_structlog_stderr()   # MUST be above your package imports — see below

from <tool>.mcp._core import server            # noqa: E402
from <tool>.core.controller import Controller  # noqa: E402


@click.group()
def main() -> None:
    """<tool> — JSON on stdout by default; --human for text, --stream for JSON Lines."""


install_globals(main)          # adds the global flags; mount_tools refuses to run without it
main.add_command(my_hand_written_command)      # hand-written commands win on name collision

mount_tools(
    main, server, prefix="<tool>_",
    transport=HttpDaemonTransport(
        "<tool>", "http://127.0.0.1:8111",
        fallback=LocalTransport(before=ensure_initialized),
        daemon_only=frozenset({"<tool>_diff"}),   # tools that need the daemon's in-memory state
    ),
    skip={"<tool>_internal": "why this tool has no CLI surface"},
    renderers={"state": render_state},            # --human text per command
)
```

**Why `configure_structlog_stderr()` goes above the imports:** modules bind their logger at
import time, so a later call leaves already-bound loggers writing to stdout — which corrupts
the JSON payload and breaks `<tool> <cmd> | jq`. Same reason `install_globals` calls
`configure_stderr_logging()` for stdlib logging.

**Transports.** `LocalTransport(fn_for=..., before=...)` calls the tool function in-process
(`before` runs one-time init; `fn_for` swaps in a different callable per spec).
`HttpDaemonTransport(tool, default_url, fallback=..., daemon_only=..., timeout=...)` posts to
`{base}/tools/{name}`. It resolves the base from `LINC_<TOOL>_DAEMON`, then
`$LINC_HOME/<tool>/daemon.port` (`LINC_HOME` defaults to `~/.linc`), then `default_url`, and
falls back to `fallback` when the daemon is down. It never falls back on a **timeout**: the
daemon is still executing the tool, so a retry would run it twice.
[nerve](https://github.com/LincForge/nerve) is the worked example.

## Conformance test

```python
from linc_cli_kit import assert_cli_parity
from <tool>.cli.main import main, _registry_server


def test_cli_parity() -> None:
    report = assert_cli_parity(
        main, _registry_server(), prefix="<tool>_",
        skip={"<tool>_internal": "why this tool has no CLI surface"},
        samples={"<tool>_screenshot": {"device": "emulator-5554"}},
    )
    assert report.tools >= 40
```

Rules the suite enforces, so read them before adding a command:

- **Every hand-written override needs a `samples[<tool_name>]` entry.** Without one the suite
  fails closed rather than silently skipping the override's JSON contract.
- **Overrides must route through the group transport** (`ctx.find_root().command.linc_transport`),
  not call the tool function directly, so the suite's stubbed error payloads reach them and the
  exit-code contract is actually exercised. An override that never calls the transport (it
  hard-codes or locally computes its output) fails, with or without a sample.
- Generated commands are checked for option parity, `--help`, the payload handed to the
  transport, and execution consistency against a direct `LocalTransport` call.
- **A sampled generated command runs once through the real transport with the process stdout
  captured** at the file-descriptor level. A tool that prints, or a structlog logger bound to
  stdout at import, or a child process inheriting fd 1, fails the suite: in a real CLI process
  those bytes would land in the JSON payload.
- Reserved option names — `json`, `human`, `stream`, `output`, `timeout`, `log_level`, `help`,
  `yes` — are remapped to `--arg-<name>` on generated commands.

## Output contract

| | |
|---|---|
| Default | bare JSON on stdout, one document, no ANSI — strict JSON (no `NaN`/`Infinity`) |
| Encoding | dataclass → object · pydantic → `model_dump(mode="json")` · set → sorted list · bytes → base64 string · datetime/date/time → ISO 8601 · other → `str()`. A non-finite float is a `NON_JSON_VALUE` error (exit `1`), never invalid JSON. `linc_cli_kit.to_json` / `json_default` expose the same encoder |
| Modes | `--json` (default), `--human`, `--stream` (JSON Lines), or `LINC_OUTPUT=json\|human\|jsonl` |
| Failure | **empty stdout**, one `{"error": {code, message, hint, category, retryable, details}}` line on stderr — always the **last** line (see below) |
| Exit codes | `0` success · `1` tool/internal error · `2` usage/validation error |
| Discovery | `<tool> schema --json` dumps the whole command tree — no `--help` crawl |
| Artifacts | `-o/--output PATH` writes binary content and returns a `{type, path, bytes, sha256}` reference |
| Other globals | `--timeout SECONDS`, `--log-level LEVEL`; logs and heartbeats are stderr-only |
| Signals | SIGTERM → exit `143`, SIGINT → exit `130` with an `INTERRUPTED` envelope; both unwind the stack (`finally` / context managers run). SIGTERM is not optional: a watchdog hard-exits after `LINC_SIGNAL_GRACE` seconds (default 10; `inf` = never) if the unwind stalls, and a second SIGTERM (more than 0.5s after the first) hard-exits at once. SIGINT keeps Python's semantics — every Ctrl-C raises `KeyboardInterrupt` |

**Parsing stderr.** stderr may carry more than the envelope. A long call writes one
heartbeat line every 10s (`{"event": "heartbeat", "label": ..., "elapsed_s": ...}`, or
`[label] Ns elapsed` under `--human`), and stdlib/structlog logging goes to stderr too. So do
not `json.loads` the whole stream. The envelope is the **last line**: read
`stderr.strip().splitlines()[-1]`, or call `linc_cli_kit.parse_error_envelope(stderr)`. It
returns the last line that is an `{"error": {...}}` object, or `None`.

A hand-written command or group callback that raises is wrapped the same way as a generated
command: envelope, exit `1` (or the `ToolError`'s own `exit_code`), and no traceback.

## How this repo works

This is a release mirror of a private LINC repository. The code is built by LINC's AI software
factory under human review, and each release lands here as one commit. Issues are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md). To report a security problem, see [SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

---

Built by [LINC Innovations](https://lincinnovations.com/audit?utm_source=github&utm_medium=readme&utm_campaign=cli-kit). We diagnose flaky device test suites.

# Changelog

All notable changes to linc-cli-kit are listed here.

## [0.1.1]

First public release. (0.1.0 was an internal, never-published tag of an earlier commit.)

- `mount_tools` reflects a FastMCP tool registry into Click commands under one output contract:
  JSON on stdout (`--human`, `--stream` alternatives), an error envelope as the last stderr line,
  exit codes `0`/`1`/`2`, SIGTERM/SIGINT unwinding.
- `schema --json` dumps the whole command tree.
- Transports: in-process `LocalTransport`, and `HttpDaemonTransport` with local fallback (never on
  a timeout). The daemon port file resolves under `LINC_HOME`.
- `assert_cli_parity` conformance suite for adopters.
- Licence: Apache-2.0.

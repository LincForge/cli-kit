# Security

## Report a vulnerability

Use GitHub's **private vulnerability reporting** on this repository (Security → Report a
vulnerability). Please do not open a public issue for security problems. We aim to
acknowledge within 3 business days.

## Threat model in one paragraph

linc-cli-kit is a library and opens no listening socket. `HttpDaemonTransport` sends each
tool call as a JSON POST to the daemon base URL, which comes from `LINC_<TOOL>_DAEMON`, then the
port file under `$LINC_HOME` (default `~/.linc`), then the default you pass (normally a
127.0.0.1 address). Anyone who can set that variable or write that file can redirect your tool
calls, so treat both as trusted local configuration. Generated commands hand typed option
values to your tool functions unchanged: validating a value before it reaches a shell, a file
path or a device is the tool's job.

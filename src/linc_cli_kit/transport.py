"""Transports: in-process local call, and daemon-first HTTP with local fallback (spec §5.4)."""

from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from linc_cli_kit.errors import ToolError, UsageError
from linc_cli_kit.registry import ToolSpec

# httpx and anyio are imported where they are used, not here: `import linc_cli_kit` sits on
# every adopter CLI's startup path, and most invocations need neither (a local sync tool
# never touches anyio; --help never touches httpx).


class Transport(Protocol):
    def call(
        self, spec: ToolSpec, payload: dict[str, Any], *, timeout: float | None = None
    ) -> Any: ...


class LocalTransport:
    def __init__(
        self,
        fn_for: Callable[[ToolSpec], Callable[..., Any]] | None = None,
        before: Callable[[], None] | None = None,
    ) -> None:
        self._fn_for = fn_for or (lambda spec: spec.fn)
        self._before = before

    def call(
        self, spec: ToolSpec, payload: dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        # timeout is a no-op for an in-process call; accepted to satisfy the Transport protocol.
        if self._before is not None:
            self._before()
        fn = self._fn_for(spec)
        if inspect.iscoroutinefunction(fn):
            import anyio

            return anyio.run(functools.partial(fn, **payload))
        result = fn(**payload)
        if inspect.iscoroutine(result):
            import anyio

            return anyio.run(lambda: result)
        return result


class HttpDaemonTransport:
    def __init__(
        self,
        tool: str,
        default_url: str,
        *,
        fallback: Transport | None = None,
        timeout: float = 30.0,
        daemon_only: frozenset[str] = frozenset(),
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.tool = tool
        self.default_url = default_url.rstrip("/")
        self.fallback = fallback
        self.timeout = timeout
        self.daemon_only = daemon_only
        self._client_factory = client_factory

    def port_file(self) -> Path:
        """The daemon's port file: ``$LINC_HOME/<tool>/daemon.port`` (default ``~/.linc``)."""
        linc_home = os.environ.get("LINC_HOME") or str(Path.home() / ".linc")
        return Path(linc_home) / self.tool / "daemon.port"

    def base_url(self) -> str:
        env = os.environ.get(f"LINC_{self.tool.upper()}_DAEMON")
        if env:
            return env.rstrip("/")
        port_file = self.port_file()
        if port_file.is_file():
            try:
                port = port_file.read_text().strip()
            except OSError:
                pass
            else:
                if port.isdigit():
                    return f"http://127.0.0.1:{port}"
        return self.default_url

    def call(
        self, spec: ToolSpec, payload: dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        import httpx

        effective_timeout = timeout if timeout is not None else self.timeout
        url = f"{self.base_url()}/tools/{spec.name}"
        client_factory = self._client_factory or httpx.Client
        try:
            with client_factory() as client:
                resp = client.post(url, json=payload, timeout=effective_timeout)
        except httpx.TimeoutException as exc:
            # Never fall back on a timeout: a slow daemon is still executing the tool, so a
            # local retry would run it a second time. Timeout is retryable by the CALLER only.
            raise ToolError(
                f"{self.tool} daemon timed out after {effective_timeout}s at {url}",
                code="UPSTREAM_TIMEOUT",
                category="upstream_timeout",
                retryable=True,
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            if spec.name in self.daemon_only or self.fallback is None:
                raise ToolError(
                    f"{self.tool} daemon not reachable at {url}: {exc}",
                    code="DAEMON_UNAVAILABLE",
                    category="unavailable",
                    hint=f"start it with `{self.tool} daemon`",
                ) from exc
            return self.fallback.call(spec, payload)
        try:
            body = resp.json() if resp.content else {}
        except ValueError as exc:
            raise ToolError(
                f"{self.tool} daemon returned a non-JSON body (HTTP {resp.status_code})",
                code="DAEMON_ERROR",
                details={"status": resp.status_code},
            ) from exc
        if not isinstance(body, dict):
            raise ToolError(
                f"{self.tool} daemon returned a non-object body (HTTP {resp.status_code})",
                code="DAEMON_ERROR",
                details={"status": resp.status_code},
            )
        if resp.status_code == 200:
            return body.get("result", body)
        message = str(body.get("error", resp.text))
        if resp.status_code == 400:
            raise UsageError(message)
        raise ToolError(
            message, code="DAEMON_ERROR", details={"status": resp.status_code}
        )

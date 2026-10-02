import httpx
import pytest

from linc_cli_kit.errors import ToolError
from linc_cli_kit.registry import registry_of
from linc_cli_kit.transport import HttpDaemonTransport, LocalTransport


def _spec(server, name):
    return next(s for s in registry_of(server) if s.name == name)


def test_local_sync_and_async(toy_server):
    t = LocalTransport()
    assert t.call(_spec(toy_server, "toy_echo"), {"device": "X"}) == {
        "device": "X",
        "count": 1,
        "ratio": 0.5,
        "loud": False,
    }
    assert t.call(_spec(toy_server, "toy_async"), {"device": "X"}) == {
        "device": "X",
        "async": True,
    }


def test_local_before_hook_and_fn_for(toy_server):
    calls = []
    t = LocalTransport(
        fn_for=lambda spec: (lambda **kw: {"via": spec.name, **kw}),
        before=lambda: calls.append(1),
    )
    assert t.call(_spec(toy_server, "toy_echo"), {"device": "X"}) == {
        "via": "toy_echo",
        "device": "X",
    }
    assert calls == [1]


class _FakeClient:
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, timeout=None):
        return self.handler(url, json)


def _resp(status, body):
    return httpx.Response(status, json=body, request=httpx.Request("POST", "http://x"))


def test_daemon_success_unwraps_result(toy_server, monkeypatch):
    seen = {}

    def handler(url, body):
        seen["url"], seen["body"] = url, body
        return _resp(200, {"result": {"ok": 1}})

    t = HttpDaemonTransport(
        "toy", "http://127.0.0.1:9", client_factory=lambda: _FakeClient(handler)
    )
    assert t.call(_spec(toy_server, "toy_echo"), {"device": "X"}) == {"ok": 1}
    assert seen == {
        "url": "http://127.0.0.1:9/tools/toy_echo",
        "body": {"device": "X"},
    }


def test_daemon_400_is_usage_error(toy_server):
    t = HttpDaemonTransport(
        "toy",
        "http://127.0.0.1:9",
        client_factory=lambda: _FakeClient(lambda u, b: _resp(400, {"error": "unknown device"})),
    )
    with pytest.raises(ToolError) as info:
        t.call(_spec(toy_server, "toy_echo"), {"device": "X"})
    assert info.value.exit_code == 2 and info.value.message == "unknown device"


def test_daemon_500_is_tool_error(toy_server):
    t = HttpDaemonTransport(
        "toy",
        "http://127.0.0.1:9",
        client_factory=lambda: _FakeClient(lambda u, b: _resp(500, {"error": "crash"})),
    )
    with pytest.raises(ToolError) as info:
        t.call(_spec(toy_server, "toy_echo"), {"device": "X"})
    assert info.value.exit_code == 1 and info.value.code == "DAEMON_ERROR"


def test_daemon_down_falls_back_to_local(toy_server):
    def boom(u, b):
        raise httpx.ConnectError("refused")

    t = HttpDaemonTransport(
        "toy",
        "http://127.0.0.1:9",
        fallback=LocalTransport(),
        client_factory=lambda: _FakeClient(boom),
    )
    assert t.call(_spec(toy_server, "toy_async"), {"device": "X"}) == {
        "device": "X",
        "async": True,
    }


def test_daemon_only_tool_without_daemon_is_unavailable(toy_server):
    def boom(u, b):
        raise httpx.ConnectError("refused")

    t = HttpDaemonTransport(
        "toy",
        "http://127.0.0.1:9",
        fallback=LocalTransport(),
        daemon_only=frozenset({"toy_async"}),
        client_factory=lambda: _FakeClient(boom),
    )
    with pytest.raises(ToolError) as info:
        t.call(_spec(toy_server, "toy_async"), {"device": "X"})
    assert info.value.category == "unavailable" and info.value.retryable is True


def test_discovery_env_then_port_file_then_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LINC_HOME", raising=False)
    t = HttpDaemonTransport("toy", "http://127.0.0.1:9")
    assert t.base_url() == "http://127.0.0.1:9"
    (tmp_path / ".linc" / "toy").mkdir(parents=True)
    (tmp_path / ".linc" / "toy" / "daemon.port").write_text("8123\n")
    assert t.base_url() == "http://127.0.0.1:8123"
    monkeypatch.setenv("LINC_TOY_DAEMON", "http://10.0.0.5:8111")
    assert t.base_url() == "http://10.0.0.5:8111"


def test_port_file_lives_under_linc_home_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LINC_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("LINC_TOY_DAEMON", raising=False)
    (tmp_path / "home" / ".linc" / "toy").mkdir(parents=True)
    (tmp_path / "home" / ".linc" / "toy" / "daemon.port").write_text("7001\n")
    t = HttpDaemonTransport("toy", "http://127.0.0.1:9")
    assert t.port_file() == tmp_path / "state" / "toy" / "daemon.port"
    assert t.base_url() == "http://127.0.0.1:9"
    (tmp_path / "state" / "toy").mkdir(parents=True)
    (tmp_path / "state" / "toy" / "daemon.port").write_text("8124\n")
    assert t.base_url() == "http://127.0.0.1:8124"


def test_empty_linc_home_falls_back_to_home_dot_linc(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LINC_HOME", "")
    t = HttpDaemonTransport("toy", "http://127.0.0.1:9")
    assert t.port_file() == tmp_path / ".linc" / "toy" / "daemon.port"


def test_daemon_non_json_body_is_daemon_error(toy_server):
    def handler(u, b):
        return httpx.Response(200, text="<html>", request=httpx.Request("POST", "http://x"))

    t = HttpDaemonTransport(
        "toy", "http://127.0.0.1:9", client_factory=lambda: _FakeClient(handler)
    )
    with pytest.raises(ToolError) as info:
        t.call(_spec(toy_server, "toy_echo"), {"device": "X"})
    assert info.value.exit_code == 1 and info.value.code == "DAEMON_ERROR"


def test_daemon_call_timeout_override(toy_server):
    seen = []

    class _TimeoutRecordingClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, timeout=None):
            seen.append(timeout)
            return _resp(200, {"result": {"ok": 1}})

    t = HttpDaemonTransport(
        "toy", "http://127.0.0.1:9", client_factory=_TimeoutRecordingClient
    )
    t.call(_spec(toy_server, "toy_echo"), {"device": "X"})
    t.call(_spec(toy_server, "toy_echo"), {"device": "X"}, timeout=5.0)
    assert seen == [30.0, 5.0]


def test_daemon_non_object_body_is_daemon_error(toy_server):
    t = HttpDaemonTransport(
        "toy",
        "http://127.0.0.1:9",
        client_factory=lambda: _FakeClient(lambda u, b: _resp(200, [1, 2])),
    )
    with pytest.raises(ToolError) as info:
        t.call(_spec(toy_server, "toy_echo"), {"device": "X"})
    assert info.value.exit_code == 1 and info.value.code == "DAEMON_ERROR"


def test_daemon_timeout_is_upstream_timeout_not_fallback(toy_server):
    """A timeout must NEVER fall back: the daemon is still running the tool, so a local
    retry would execute it a second time."""
    fell_back = []

    def slow(u, b):
        raise httpx.ReadTimeout("too slow")

    t = HttpDaemonTransport(
        "toy",
        "http://127.0.0.1:9",
        fallback=LocalTransport(before=lambda: fell_back.append(1)),
        timeout=12.5,
        client_factory=lambda: _FakeClient(slow),
    )
    with pytest.raises(ToolError) as info:
        t.call(_spec(toy_server, "toy_echo"), {"device": "X"})
    assert info.value.code == "UPSTREAM_TIMEOUT"
    assert info.value.category == "upstream_timeout"
    assert info.value.retryable is True
    assert "12.5s" in info.value.message
    assert fell_back == [], "fallback ran on a timeout — the tool would execute twice"

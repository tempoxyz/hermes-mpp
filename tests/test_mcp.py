from __future__ import annotations

import asyncio
import inspect
import json
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mpp import Challenge, Credential, PaymentOutcomeUnknownError
from mpp.extensions.mcp import META_RECEIPT
from tools.mcp_tool import MCPServerTask

from hermes_mpp.mcp import (
    McpPaymentDeniedError,
    _HermesMcpSession,
    _ServerIdentity,
    instrument_mcp,
)

HARNESS = Path(__file__).parent / "harness" / "paid_mcp_server.py"


@dataclass
class FakeMethod:
    name: str = "tempo"
    _intents: dict[str, bool] = field(default_factory=lambda: {"charge": True})
    challenges: list[Challenge] = field(default_factory=list)

    async def create_credential(self, challenge: Challenge) -> Credential:
        self.challenges.append(challenge)
        return Credential(
            challenge=challenge.to_echo(),
            payload={"type": "transaction", "signature": "0xtest"},
            source="did:pkh:eip155:42431:0x0000000000000000000000000000000000000002",
        )


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _wait_until(predicate, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


@asynccontextmanager
async def _stdio_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: FakeMethod,
    *,
    name: str = "local",
    realm: str | None = None,
    challenge_realm: str | None = None,
    auto_pay: bool = True,
) -> AsyncIterator[tuple[MCPServerTask, Path]]:
    state_path = tmp_path / f"{name}.jsonl"
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("tools.osv_check.check_package_for_malware", lambda *_args: None)

    policy: dict[str, Any] = {"auto_pay": auto_pay}
    if realm is not None:
        policy["realm"] = realm
    config = {
        "command": sys.executable,
        "args": [
            str(HARNESS),
            "--transport",
            "stdio",
            "--realm",
            challenge_realm or realm or name,
            "--state",
            str(state_path),
        ],
        "connect_timeout": 10,
        "keepalive_interval": 300,
        "mpp": policy,
    }
    instrumentation = instrument_mcp(method, None)
    server = MCPServerTask(name)
    try:
        await asyncio.wait_for(server.start(config), timeout=15)
        assert isinstance(server.session, _HermesMcpSession)
        yield server, state_path
    finally:
        await server.shutdown()
        instrumentation.close()


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _wait_for_port(port: int) -> None:
    async with asyncio.timeout(10):
        while True:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            writer.close()
            await writer.wait_closed()
            del reader
            return


@asynccontextmanager
async def _http_server(
    tmp_path: Path,
    method: FakeMethod,
    transport: str,
    *,
    allow_origin: bool = True,
) -> AsyncIterator[tuple[MCPServerTask, Path]]:
    port = _free_port()
    state_path = tmp_path / f"{transport}.jsonl"
    origin = f"http://127.0.0.1:{port}"
    path = "/sse" if transport == "sse" else "/mcp"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(HARNESS),
        "--transport",
        transport,
        "--realm",
        f"127.0.0.1:{port}",
        "--state",
        str(state_path),
        "--port",
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    origins = [origin] if allow_origin else ["https://allowed.example"]
    instrumentation = instrument_mcp(method, origins)
    server = MCPServerTask(transport)
    config: dict[str, Any] = {
        "url": f"{origin}{path}",
        "connect_timeout": 10,
        "keepalive_interval": 300,
        "skip_preflight": True,
    }
    if transport == "sse":
        config["transport"] = "sse"
    try:
        await _wait_for_port(port)
        await asyncio.wait_for(server.start(config), timeout=15)
        assert isinstance(server.session, _HermesMcpSession)
        yield server, state_path
    finally:
        await server.shutdown()
        instrumentation.close()
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


def test_instrumentation_is_version_gated_and_restorable(monkeypatch) -> None:
    import hermes_cli

    method = FakeMethod()
    original = inspect.getattr_static(MCPServerTask, "_discover_tools")
    instrumentation = instrument_mcp(method, None)

    assert instrumentation.active
    assert inspect.getattr_static(MCPServerTask, "_discover_tools") is not original

    instrumentation.close()
    assert inspect.getattr_static(MCPServerTask, "_discover_tools") is original

    monkeypatch.setattr(hermes_cli, "__version__", "0.20.1")
    with pytest.raises(RuntimeError, match="Unsupported Hermes Agent '0.20.1'"):
        instrument_mcp(method, None)


def test_instrumentation_rejects_changed_private_signature(monkeypatch) -> None:
    async def incompatible(self, options):
        del self, options

    monkeypatch.setattr(MCPServerTask, "_discover_tools", incompatible)
    with pytest.raises(RuntimeError, match="_discover_tools signature"):
        instrument_mcp(FakeMethod(), None)


@pytest.mark.parametrize("policy", [False, [], "pay"])
def test_invalid_server_policy_fails_closed(policy) -> None:
    server = SimpleNamespace(
        name="invalid-policy",
        _config={"command": "server", "mpp": policy},
    )
    with pytest.raises(RuntimeError, match="mpp must be an object"):
        _ServerIdentity.from_server(server)


@pytest.mark.asyncio
async def test_shutdown_unwraps_a_live_session(tmp_path: Path, monkeypatch) -> None:
    async with _stdio_server(tmp_path, monkeypatch, FakeMethod(), name="shutdown") as (
        server,
        _state_path,
    ):
        assert isinstance(server.session, _HermesMcpSession)
        wrapper = server.session
        wrapper._owner.close()
        assert server.session is wrapper._session


@pytest.mark.asyncio
async def test_stdio_preserves_free_results_and_pays_rich_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    method = FakeMethod()
    async with _stdio_server(tmp_path, monkeypatch, method) as (server, state_path):
        assert server.session is not None
        free = await server.session.call_tool("free", meta={"caller": "free-meta"})
        assert free.content[0].text == "free-ok"
        assert free.structuredContent == {"free": True}
        assert free.isError is False
        assert free.receipt is None

        free_error = await server.session.call_tool("free_error")
        assert free_error.isError is True
        assert free_error.content[0].text == "free-error"
        assert free_error.receipt is None

        paid = await server.session.call_tool(
            "paid_rich",
            {"value": 42},
            meta={"caller": "paid-meta"},
        )
        assert [block.type for block in paid.content] == ["text", "image"]
        assert paid.content[0].text == "paid-ok"
        assert paid.content[1].mimeType == "image/png"
        assert paid.structuredContent["value"] == 42
        assert paid.structuredContent["caller"] == "paid-meta"
        assert paid.structuredContent[META_RECEIPT]["status"] == "success"
        assert paid.meta[META_RECEIPT]["reference"].startswith("receipt-paid_rich-")

    events = _events(state_path)
    assert events == [
        {"caller_meta": "free-meta", "credential": False, "tool": "free"},
        {"caller_meta": None, "credential": False, "tool": "free_error"},
        {"caller_meta": "paid-meta", "credential": False, "tool": "paid_rich"},
        {"caller_meta": "paid-meta", "credential": True, "tool": "paid_rich"},
    ]
    assert len(method.challenges) == 1


@pytest.mark.asyncio
async def test_normal_hermes_mcp_handler_pays_without_a_separate_tool(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import mcp_tool

    method = FakeMethod()
    async with _stdio_server(tmp_path, monkeypatch, method, name="handler") as (
        server,
        state_path,
    ):
        loop = asyncio.get_running_loop()
        monkeypatch.setitem(mcp_tool._servers, "handler", server)

        def run_on_test_loop(coro_or_factory, timeout=30):
            coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

        rendered_images: list[str] = []

        def render_image(block) -> str | None:
            if getattr(block, "type", None) != "image":
                return None
            rendered_images.append(block.mimeType)
            return "MEDIA:/tmp/paid-result.png"

        monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", run_on_test_loop)
        monkeypatch.setattr(mcp_tool, "_cache_mcp_image_block", render_image)

        paid_handler = mcp_tool._make_tool_handler("handler", "paid_rich", 10)
        paid = json.loads(await asyncio.to_thread(paid_handler, {"value": "normal-call"}))
        assert mcp_tool.mcp_prefixed_tool_name("handler", "paid_rich") == (
            "mcp__handler__paid_rich"
        )
        assert "paid-ok" in paid["result"]
        assert "MEDIA:/tmp/paid-result.png" in paid["result"]
        assert paid["structuredContent"]["value"] == "normal-call"
        assert paid["structuredContent"][META_RECEIPT]["status"] == "success"
        assert rendered_images == ["image/png"]

        error_handler = mcp_tool._make_tool_handler("handler", "free_error", 10)
        error = json.loads(await asyncio.to_thread(error_handler, {}))
        assert error == {"error": "free-error"}

    assert [event["credential"] for event in _events(state_path)] == [False, True, False]
    assert len(method.challenges) == 1


@pytest.mark.asyncio
async def test_stdio_denial_and_malformed_challenge_send_no_credential(
    tmp_path: Path,
    monkeypatch,
) -> None:
    denied_method = FakeMethod()
    async with _stdio_server(
        tmp_path,
        monkeypatch,
        denied_method,
        name="denied",
        auto_pay=False,
    ) as (server, denied_state):
        assert server.session is not None
        with pytest.raises(McpPaymentDeniedError, match="denied by policy"):
            await server.session.call_tool("paid_rich")
    assert _events(denied_state) == [
        {"caller_meta": None, "credential": False, "tool": "paid_rich"}
    ]
    assert denied_method.challenges == []

    malformed_method = FakeMethod()
    async with _stdio_server(
        tmp_path,
        monkeypatch,
        malformed_method,
        name="malformed-server",
    ) as (server, malformed_state):
        assert server.session is not None
        with pytest.raises(ValueError, match="malformed payment challenges"):
            await server.session.call_tool("malformed")
    assert _events(malformed_state) == [
        {"caller_meta": None, "credential": False, "tool": "malformed"}
    ]
    assert malformed_method.challenges == []


@pytest.mark.asyncio
async def test_stdio_realm_mismatch_sends_no_credential(tmp_path: Path, monkeypatch) -> None:
    method = FakeMethod()
    async with _stdio_server(
        tmp_path,
        monkeypatch,
        method,
        name="configured-server",
        realm="configured-realm",
        challenge_realm="challenge-controlled-realm",
    ) as (server, state_path):
        assert server.session is not None
        with pytest.raises(McpPaymentDeniedError, match="does not match configured realm"):
            await server.session.call_tool("paid_rich")

    assert _events(state_path) == [
        {"caller_meta": None, "credential": False, "tool": "paid_rich"}
    ]
    assert method.challenges == []


@pytest.mark.asyncio
async def test_stdio_retries_once_and_latches_uncertainty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    method = FakeMethod()
    async with _stdio_server(tmp_path, monkeypatch, method, name="retry") as (
        server,
        state_path,
    ):
        assert server.session is not None
        with pytest.raises(PaymentOutcomeUnknownError):
            await server.session.call_tool("retry_twice", meta={"caller": "retry-meta"})

    assert _events(state_path) == [
        {"caller_meta": "retry-meta", "credential": False, "tool": "retry_twice"},
        {"caller_meta": "retry-meta", "credential": True, "tool": "retry_twice"},
    ]
    assert len(method.challenges) == 1


@pytest.mark.asyncio
async def test_stdio_dropped_paid_response_is_not_repaid_after_reconnect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    method = FakeMethod()
    async with _stdio_server(tmp_path, monkeypatch, method, name="reconnect") as (
        server,
        state_path,
    ):
        assert server.session is not None
        first_session = server.session
        with pytest.raises(PaymentOutcomeUnknownError) as first:
            await server.session.call_tool("drop_after_credential")

        server._ready.clear()
        server._reconnect_event.set()
        await _wait_until(
            lambda: server._ready.is_set()
            and server.session is not None
            and server.session is not first_session,
            timeout=15,
        )
        assert isinstance(server.session, _HermesMcpSession)

        free = await server.session.call_tool("free")
        assert free.content[0].text == "free-ok"
        with pytest.raises(PaymentOutcomeUnknownError) as second:
            await server.session.call_tool("paid_rich")
        assert second.value is first.value

    events = _events(state_path)
    assert [event["credential"] for event in events] == [False, True, False, False]
    assert [event["tool"] for event in events] == [
        "drop_after_credential",
        "drop_after_credential",
        "free",
        "paid_rich",
    ]
    assert len(method.challenges) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
async def test_http_transports_pay_transparently(
    tmp_path: Path,
    transport: str,
) -> None:
    method = FakeMethod()
    async with _http_server(tmp_path, method, transport) as (server, state_path):
        assert server.session is not None
        free = await server.session.call_tool("free")
        paid = await server.session.call_tool(
            "paid_rich",
            {"value": transport},
            meta={"caller": transport},
        )
        assert free.content[0].text == "free-ok"
        assert paid.content[0].text == "paid-ok"
        assert paid.structuredContent["value"] == transport
        assert paid.structuredContent[META_RECEIPT]["status"] == "success"

    events = _events(state_path)
    assert [event["credential"] for event in events] == [False, False, True]
    assert events[-1]["caller_meta"] == transport
    assert len(method.challenges) == 1


@pytest.mark.asyncio
async def test_http_origin_policy_sends_no_credential(tmp_path: Path) -> None:
    method = FakeMethod()
    async with _http_server(
        tmp_path,
        method,
        "streamable-http",
        allow_origin=False,
    ) as (server, state_path):
        assert server.session is not None
        with pytest.raises(McpPaymentDeniedError, match=r"not in \$MPP_ALLOWED_ORIGINS"):
            await server.session.call_tool("paid_rich")

    assert _events(state_path) == [
        {"caller_meta": None, "credential": False, "tool": "paid_rich"}
    ]
    assert method.challenges == []

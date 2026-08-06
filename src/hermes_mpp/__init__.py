"""Hermes entry point for transparent MPP payments."""

from __future__ import annotations

import atexit
import threading
from typing import Any

from mpp.methods.tempo import TempoAccount
from mpp.runtime import PaymentRuntime

from .config import Config
from .httpx import HttpxInstrumentation, instrument_httpx
from .mcp import McpInstrumentation, instrument_mcp
from .tempo import ChallengeTempo

_instrumentation: HttpxInstrumentation | None = None
_mcp_instrumentation: McpInstrumentation | None = None
_method: ChallengeTempo | None = None
_lock = threading.Lock()


def _create_instrumentation(
    config: Config,
) -> tuple[ChallengeTempo, HttpxInstrumentation, McpInstrumentation]:
    account = TempoAccount.from_key(config.private_key)
    method = ChallengeTempo(account)

    def runtime_factory() -> PaymentRuntime:
        return PaymentRuntime([method])

    httpx_instrumentation = instrument_httpx(runtime_factory, config.allowed_origins)
    try:
        mcp_instrumentation = instrument_mcp(method, config.allowed_origins)
    except BaseException:
        httpx_instrumentation.close()
        raise
    return method, httpx_instrumentation, mcp_instrumentation


def register(ctx: Any) -> None:
    """Make Hermes HTTP and MCP requests payment-aware."""
    from .tool import register_tool

    global _instrumentation, _mcp_instrumentation, _method
    with _lock:
        instrumentation = _instrumentation
        mcp_instrumentation = _mcp_instrumentation
        if (
            instrumentation is None
            or not instrumentation.active
            or mcp_instrumentation is None
            or not mcp_instrumentation.active
            or _method is None
        ):
            if mcp_instrumentation is not None:
                mcp_instrumentation.close()
            if instrumentation is not None:
                instrumentation.close()
            method, instrumentation, mcp_instrumentation = _create_instrumentation(
                Config.from_env()
            )
        else:
            method = _method
        try:
            register_tool(ctx)
        except BaseException:
            mcp_instrumentation.close()
            instrumentation.close()
            _instrumentation = None
            _mcp_instrumentation = None
            _method = None
            raise
        _method = method
        _instrumentation = instrumentation
        _mcp_instrumentation = mcp_instrumentation


def _shutdown() -> None:
    """Remove process-global instrumentation. Public for deterministic tests."""
    global _instrumentation, _mcp_instrumentation, _method
    with _lock:
        if _mcp_instrumentation is not None:
            _mcp_instrumentation.close()
            _mcp_instrumentation = None
        if _instrumentation is not None:
            _instrumentation.close()
            _instrumentation = None
        _method = None


atexit.register(_shutdown)

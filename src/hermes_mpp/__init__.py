"""Hermes entry point for transparent MPP payments."""

from __future__ import annotations

import atexit
import threading
from typing import Any

from mpp.runtime import PaymentRuntime

from .config import Config
from .httpx import HttpxInstrumentation, instrument_httpx
from .session import SessionHost
from .tempo import ChallengeTempo

_instrumentation: HttpxInstrumentation | None = None
_lock = threading.Lock()


def _create_instrumentation(config: Config) -> HttpxInstrumentation:
    sessions = SessionHost(config)
    method = ChallengeTempo(sessions.account, sessions.manager_for_chain)

    def runtime_factory() -> PaymentRuntime:
        return PaymentRuntime([method])

    return instrument_httpx(
        runtime_factory,
        config.allowed_origins,
        session_hint=sessions.hint,
        close_session_store=sessions.close,
    )


def register(ctx: Any) -> None:
    """Make Hermes HTTP requests payment-aware."""
    from .tool import register_tool

    global _instrumentation
    with _lock:
        instrumentation = _instrumentation
        if instrumentation is None or not instrumentation.active:
            instrumentation = _create_instrumentation(Config.from_env())
        try:
            register_tool(ctx)
        except BaseException:
            instrumentation.close()
            _instrumentation = None
            raise
        _instrumentation = instrumentation


def _shutdown() -> None:
    """Remove process-global instrumentation. Public for deterministic tests."""
    global _instrumentation
    with _lock:
        if _instrumentation is not None:
            _instrumentation.close()
            _instrumentation = None


atexit.register(_shutdown)

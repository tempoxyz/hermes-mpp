"""Hermes entry point for transparent MPP payments."""

from __future__ import annotations

import atexit
import os
import threading
from typing import TYPE_CHECKING, Any

from .config import Config

if TYPE_CHECKING:
    from .httpx import HttpxInstrumentation

_instrumentation: HttpxInstrumentation | None = None
_lock = threading.Lock()


def _create_instrumentation(config: Config) -> HttpxInstrumentation:
    from mpp.methods.tempo import TempoAccount
    from mpp.runtime import PaymentRuntime

    from .httpx import instrument_httpx
    from .tempo import ChallengeTempo

    account = TempoAccount.from_key(config.private_key)

    def runtime_factory() -> PaymentRuntime:
        return PaymentRuntime([ChallengeTempo(account)])

    return instrument_httpx(runtime_factory, config.allowed_origins)


def register(ctx: Any) -> None:
    """Make Hermes HTTP requests payment-aware."""
    from .tool import register_tool

    # Hermes can discover and validate an unconfigured plugin. The tool's
    # requires_env gate keeps it unavailable until the user configures a wallet.
    if not os.environ.get("TEMPO_PRIVATE_KEY", "").strip():
        register_tool(ctx)
        return

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

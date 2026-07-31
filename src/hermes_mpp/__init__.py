"""Hermes entry point for transparent MPP payments."""

from __future__ import annotations

import atexit
import threading
from typing import Any

from mpp.methods.tempo import ChargeIntent, TempoAccount, tempo
from mpp.runtime import PaymentRuntime

from .config import Config
from .httpx import HttpxInstrumentation, instrument_httpx

_instrumentation: HttpxInstrumentation | None = None
_lock = threading.Lock()


def _create_instrumentation(config: Config) -> HttpxInstrumentation:
    account = TempoAccount.from_key(config.private_key)

    def runtime_factory() -> PaymentRuntime:
        method = tempo(
            account=account,
            intents={"charge": ChargeIntent()},
            rpc_url=config.rpc_url,
            client_id="hermes-agent",
        )
        return PaymentRuntime([method])

    return instrument_httpx(runtime_factory, config.allowed_origins)


def register(_ctx: Any) -> None:
    """Instrument HTTPX once when Hermes loads the plugin."""
    global _instrumentation
    with _lock:
        if _instrumentation is not None and _instrumentation.active:
            return
        _instrumentation = _create_instrumentation(Config.from_env())


def _shutdown() -> None:
    """Remove process-global instrumentation. Public for deterministic tests."""
    global _instrumentation
    with _lock:
        if _instrumentation is not None:
            _instrumentation.close()
            _instrumentation = None


atexit.register(_shutdown)

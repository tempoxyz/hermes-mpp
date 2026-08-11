from __future__ import annotations

from collections.abc import Sequence

from .httpx import HttpxInstrumentation
from .payment import Ledger, RuntimeFactory
from .requests import RequestsInstrumentation


class TransportInstrumentation:
    """Install HTTPX and Requests payment handling as one transaction."""

    def __init__(
        self,
        runtime_factory: RuntimeFactory,
        origins: Sequence[str] | None,
    ) -> None:
        ledger = Ledger()
        self.httpx = HttpxInstrumentation(runtime_factory, origins, ledger=ledger)
        self.requests = RequestsInstrumentation(runtime_factory, origins, ledger=ledger)

    @property
    def active(self) -> bool:
        return self.httpx.active and self.requests.active

    def enable(self) -> None:
        if self.active:
            return
        if self.httpx.active or self.requests.active:
            raise RuntimeError("HTTP transports are only partially instrumented")
        self.httpx.enable()
        try:
            self.requests.enable()
        except BaseException:
            self.httpx.close()
            raise

    def close(self) -> None:
        self.requests.close()
        self.httpx.close()


def instrument_transports(
    runtime_factory: RuntimeFactory,
    origins: Sequence[str] | None,
) -> TransportInstrumentation:
    instrumentation = TransportInstrumentation(runtime_factory, origins)
    instrumentation.enable()
    return instrumentation

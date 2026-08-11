from __future__ import annotations

import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
import requests
from mpp import Challenge, Credential
from mpp.errors import PaymentOutcomeUnknownError
from mpp.runtime import PaymentRuntime
from requests import Response, Session
from requests.adapters import BaseAdapter

from hermes_mpp.requests import instrument_requests
from hermes_mpp.transports import TransportInstrumentation, instrument_transports

ALLOWED = "https://allowed.test"


class FakeMethod:
    name = "tempo"

    def __init__(self) -> None:
        self.calls = 0

    async def create_credential(self, challenge: Challenge) -> Credential:
        self.calls += 1
        return Credential(challenge=challenge.to_echo(), payload={"hash": "0xpaid"})


def challenge() -> Challenge:
    return Challenge(
        id="cross-stack",
        method="tempo",
        intent="charge",
        request={"amount": "1"},
    )


def requests_response(request: requests.PreparedRequest, status: int) -> Response:
    response = Response()
    response.status_code = status
    response.url = request.url
    response.request = request
    response._content = b""
    if status == 402:
        response.headers["www-authenticate"] = challenge().to_www_authenticate(
            "allowed.test"
        )
    return response


class RequestsAdapter(BaseAdapter):
    def __init__(self, paid: threading.Event, release: threading.Event) -> None:
        self.paid = paid
        self.release = release

    def send(self, request: requests.PreparedRequest, **_: Any) -> Response:
        if "authorization" not in request.headers:
            return requests_response(request, 402)
        self.paid.set()
        assert self.release.wait(timeout=5)
        return requests_response(request, 200)

    def close(self) -> None:
        pass


def test_httpx_and_requests_share_duplicate_payment_ledger() -> None:
    method = FakeMethod()
    instrument_transports(lambda: PaymentRuntime([method]), [ALLOWED])
    paid = threading.Event()
    release = threading.Event()
    session = Session()
    session.mount("https://", RequestsAdapter(paid, release))

    def send_requests() -> int:
        return session.get(f"{ALLOWED}/same").status_code

    def httpx_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            headers={
                "www-authenticate": challenge().to_www_authenticate("allowed.test")
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(send_requests)
        assert paid.wait(timeout=5)
        with httpx.Client(transport=httpx.MockTransport(httpx_handler)) as client:
            with pytest.raises(PaymentOutcomeUnknownError):
                client.get(f"{ALLOWED}/same")
        release.set()
        assert first.result(timeout=5) == 200
    session.close()

    assert method.calls == 1


def test_transport_registration_rolls_back_httpx_when_requests_conflicts() -> None:
    method = FakeMethod()
    existing = instrument_requests(lambda: PaymentRuntime([method]), [ALLOWED])
    httpx_original = inspect.getattr_static(httpx.Client, "_send_single_request")
    instrumentation = TransportInstrumentation(lambda: PaymentRuntime([method]), [ALLOWED])

    with pytest.raises(RuntimeError, match="Requests is already instrumented"):
        instrumentation.enable()

    assert inspect.getattr_static(httpx.Client, "_send_single_request") is httpx_original
    assert not instrumentation.httpx.active
    assert existing.active

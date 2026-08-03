from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from mpp import Challenge, Credential
from mpp.errors import PaymentError, PaymentOutcomeUnknownError
from mpp.events import WILDCARD_EVENT, EventDispatcher
from mpp.runtime import PaymentRuntime

from hermes_mpp.httpx import (
    HttpxInstrumentation,
    _validate_httpx,
    instrument_httpx,
)

ALLOWED = "https://allowed.test"


class FakeMethod:
    name = "tempo"

    def __init__(self) -> None:
        self.calls = 0

    async def create_credential(self, challenge: Challenge) -> Credential:
        self.calls += 1
        return Credential(
            challenge=challenge.to_echo(),
            payload={"hash": "0xpaid"},
        )


def required(identifier: str, *, realm: str = "allowed.test") -> httpx.Response:
    challenge = Challenge(
        id=identifier,
        method="tempo",
        intent="charge",
        request={"amount": "1"},
    )
    return httpx.Response(
        402,
        headers={"www-authenticate": challenge.to_www_authenticate(realm)},
    )


def setup() -> tuple[FakeMethod, EventDispatcher, HttpxInstrumentation]:
    method = FakeMethod()
    events = EventDispatcher()
    instrumentation = instrument_httpx(
        lambda: PaymentRuntime([method], events=events),
        [ALLOWED],
    )
    return method, events, instrumentation


def paid_handler(
    identifier: str,
    requests: list[httpx.Request],
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return (
            httpx.Response(200, json={"paid": True})
            if "authorization" in request.headers
            else required(identifier)
        )

    return httpx.MockTransport(handler)


def assert_paid(requests: list[httpx.Request]) -> None:
    assert len(requests) == 2
    assert "authorization" not in requests[0].headers
    assert requests[1].headers["authorization"].startswith("Payment ")


def test_sync_async_preexisting_and_events() -> None:
    preexisting_requests: list[httpx.Request] = []
    preexisting = httpx.Client(transport=paid_handler("preexisting", preexisting_requests))
    method, events, instrumentation = setup()
    names: list[str] = []
    events.on(WILDCARD_EVENT, lambda event: names.append(str(event.name)))

    sync_requests: list[httpx.Request] = []
    with httpx.Client(transport=paid_handler("sync", sync_requests)) as client:
        assert client.get(f"{ALLOWED}/sync").status_code == 200
    assert preexisting.get(f"{ALLOWED}/preexisting").status_code == 200

    async_requests: list[httpx.Request] = []

    async def send() -> int:
        async with httpx.AsyncClient(transport=paid_handler("async", async_requests)) as client:
            return (await client.get(f"{ALLOWED}/async")).status_code

    assert asyncio.run(send()) == 200
    for requests in (sync_requests, preexisting_requests, async_requests):
        assert_paid(requests)
    assert method.calls == 3
    assert names.count("challenge.received") == 3
    assert names.count("credential.created") == 3
    assert names.count("payment.response") == 3

    instrumentation.close()
    preexisting.close()


@pytest.mark.asyncio
async def test_sync_client_called_from_running_loop() -> None:
    method, _, instrumentation = setup()
    requests: list[httpx.Request] = []
    threads: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        threads.append(threading.get_ident())
        requests.append(request)
        return httpx.Response(200) if "authorization" in request.headers else required("nested")

    caller = threading.get_ident()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.get(f"{ALLOWED}/nested").status_code == 200
    assert_paid(requests)
    assert threads == [caller, caller]
    assert method.calls == 1
    instrumentation.close()


def test_paid_retry_uses_updated_client_cookies() -> None:
    method, _, instrumentation = setup()
    cookies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie", ""))
        if "authorization" in request.headers:
            return httpx.Response(
                200 if "session=new" in cookies[-1] else 409,
            )
        response = required(request.url.path)
        response.headers["set-cookie"] = "session=new; Path=/"
        return response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.cookies.set("session", "old", domain="allowed.test", path="/")
        assert (
            client.get(
                f"{ALLOWED}/sync",
                headers={"cookie": "manual=yes; session=old"},
            ).status_code
            == 200
        )

    async def send() -> int:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            client.cookies.set("session", "old", domain="allowed.test", path="/")
            return (
                await client.get(
                    f"{ALLOWED}/async",
                    headers={"cookie": "manual=yes; session=old"},
                )
            ).status_code

    assert asyncio.run(send()) == 200
    assert cookies == ["manual=yes; session=old", "manual=yes; session=new"] * 2
    assert method.calls == 2
    instrumentation.close()


def test_paid_retry_honors_challenge_cookie_deletion() -> None:
    _, _, instrumentation = setup()
    cookies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie", ""))
        if "authorization" in request.headers:
            return httpx.Response(200)
        response = required("deleted-cookie")
        response.headers["set-cookie"] = "session=; Max-Age=0; Path=/"
        return response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.cookies.set("session", "old", domain="allowed.test", path="/")
        response = client.get(
            f"{ALLOWED}/cookies",
            headers={"cookie": "manual=yes; session=old"},
        )

    assert response.status_code == 200
    assert cookies == ["manual=yes; session=old", "manual=yes"]
    instrumentation.close()


@pytest.mark.parametrize(
    ("url", "set_cookie"),
    [
        (f"{ALLOWED}/paid", "session=new; Path=/other"),
        (f"{ALLOWED}/paid", "session=new; Domain=other.test; Path=/"),
        ("http://allowed.test/paid", "session=new; Secure; Path=/"),
        (f"{ALLOWED}/paid", "session=; Max-Age=0; Path=/other"),
    ],
)
def test_scoped_cookie_update_does_not_remove_manual_cookie(
    url: str,
    set_cookie: str,
) -> None:
    method = FakeMethod()
    instrumentation = instrument_httpx(
        lambda: PaymentRuntime([method]),
        [url.removesuffix("/paid")],
    )
    cookies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie", ""))
        if "authorization" in request.headers:
            return httpx.Response(200)
        response = required("scoped-cookie")
        response.headers["set-cookie"] = set_cookie
        return response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.get(url, headers={"cookie": "session=manual"}).status_code == 200

    assert cookies == ["session=manual", "session=manual"]
    instrumentation.close()


def test_origin_policy_includes_redirect_target() -> None:
    method, _, instrumentation = setup()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"location": "https://blocked.test/paid"},
            )
        return required("blocked", realm="blocked.test")

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        assert client.get(f"{ALLOWED}/start").status_code == 402

    assert len(requests) == 2
    assert method.calls == 0
    assert all("authorization" not in request.headers for request in requests)
    instrumentation.close()


def test_missing_origin_policy_allows_any_origin() -> None:
    method = FakeMethod()
    instrumentation = instrument_httpx(lambda: PaymentRuntime([method]), None)
    requests: list[httpx.Request] = []

    with httpx.Client(transport=paid_handler("anywhere", requests)) as client:
        assert client.get("https://anywhere.test/paid").status_code == 200

    assert_paid(requests)
    assert method.calls == 1
    instrumentation.close()


def test_same_origin_redirect_retries_the_challenged_url() -> None:
    _, _, instrumentation = setup()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/paid"})
        if "authorization" in request.headers:
            return httpx.Response(200)
        return required("redirect")

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        assert client.get(f"{ALLOWED}/start").status_code == 200

    assert [request.url.path for request in requests] == [
        "/start",
        "/paid",
        "/paid",
    ]
    assert "authorization" in requests[-1].headers
    instrumentation.close()


def test_paid_retry_can_follow_a_redirect() -> None:
    _, _, instrumentation = setup()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/done":
            return httpx.Response(200)
        if "authorization" in request.headers:
            return httpx.Response(302, headers={"location": "/done"})
        return required("paid-redirect")

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        assert client.get(f"{ALLOWED}/paid").status_code == 200

    assert [request.url.path for request in requests] == [
        "/paid",
        "/paid",
        "/done",
    ]
    assert "authorization" in requests[1].headers
    instrumentation.close()


def test_paid_retry_returns_redirect_when_following_is_disabled() -> None:
    _, _, instrumentation = setup()

    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            return httpx.Response(302, headers={"location": "/done"})
        return required("paid-no-follow")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = client.get(f"{ALLOWED}/paid", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/done"
    instrumentation.close()


def test_method_internal_httpx_is_not_instrumented_recursively() -> None:
    internal: list[httpx.Request] = []

    class RecursiveMethod(FakeMethod):
        async def create_credential(self, challenge: Challenge) -> Credential:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: (
                        internal.append(request),
                        required("internal"),
                    )[1]
                )
            ) as client:
                assert (await client.get(f"{ALLOWED}/internal")).status_code == 402
            return await super().create_credential(challenge)

    method = RecursiveMethod()
    instrumentation = instrument_httpx(
        lambda: PaymentRuntime([method]),
        [ALLOWED],
    )
    requests: list[httpx.Request] = []
    with httpx.Client(transport=paid_handler("outer", requests)) as client:
        assert client.get(f"{ALLOWED}/outer").status_code == 200

    assert len(internal) == 1
    assert "authorization" not in internal[0].headers
    assert method.calls == 1
    instrumentation.close()


def test_repeated_challenge_and_later_retry_fail_closed() -> None:
    method, _, instrumentation = setup()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return required("repeated")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.get(f"{ALLOWED}/repeated").status_code == 402
        with pytest.raises(PaymentOutcomeUnknownError):
            client.get(f"{ALLOWED}/repeated")

    assert len(requests) == 3
    assert method.calls == 1
    instrumentation.close()


def test_non_success_paid_response_stays_uncertain() -> None:
    method, _, instrumentation = setup()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "authorization" in request.headers:
            return httpx.Response(500, text="failed")
        return required(f"failed-{len(requests)}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = client.get(f"{ALLOWED}/failed")
        assert response.status_code == 500
        assert response.text == "failed"
        with pytest.raises(PaymentOutcomeUnknownError):
            client.get(f"{ALLOWED}/failed")

    assert len(requests) == 3
    assert method.calls == 1
    instrumentation.close()


def test_lost_paid_response_blocks_a_new_challenge_for_same_operation() -> None:
    method, _, instrumentation = setup()
    requests: list[httpx.Request] = []

    def lost(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return required("lost")
        raise httpx.ReadTimeout("lost", request=request)

    with httpx.Client(transport=httpx.MockTransport(lost)) as client:
        with pytest.raises(PaymentOutcomeUnknownError) as raised:
            client.get(f"{ALLOWED}/lost")
    assert isinstance(raised.value.__cause__, httpx.ReadTimeout)

    with httpx.Client(transport=paid_handler("different", requests)) as client:
        with pytest.raises(PaymentOutcomeUnknownError):
            client.get(f"{ALLOWED}/lost")

    assert len(requests) == 3
    assert method.calls == 1
    instrumentation.close()


def test_concurrent_equivalent_payment_is_not_sent_twice() -> None:
    method, _, instrumentation = setup()
    paid = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" not in request.headers:
            return required("concurrent")
        paid.set()
        assert release.wait(timeout=5)
        return httpx.Response(200)

    def send() -> int:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.get(f"{ALLOWED}/concurrent").status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(send)
        assert paid.wait(timeout=5)
        second = executor.submit(send)
        with pytest.raises(PaymentOutcomeUnknownError):
            second.result(timeout=5)
        release.set()
        assert first.result(timeout=5) == 200

    assert method.calls == 1
    instrumentation.close()


def test_close_restores_patches_without_interrupting_inflight_payment() -> None:
    _, _, instrumentation = setup()
    paid = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" not in request.headers:
            return required("shutdown")
        paid.set()
        assert release.wait(timeout=5)
        return httpx.Response(200)

    def send() -> int:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.get(f"{ALLOWED}/shutdown").status_code

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(send)
        assert paid.wait(timeout=5)
        instrumentation.close()
        release.set()
        assert future.result(timeout=5) == 200

    with httpx.Client(transport=httpx.MockTransport(lambda _: required("after-close"))) as client:
        assert client.get(f"{ALLOWED}/after-close").status_code == 402


def test_streaming_request_cannot_be_replayed() -> None:
    _, _, instrumentation = setup()

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            yield b"chunk"

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            b"".join(request.stream)
            return required("stream")

    with httpx.Client(transport=Transport()) as client:
        with pytest.raises(PaymentError, match="cannot be replayed"):
            client.send(
                httpx.Request("POST", f"{ALLOWED}/stream", stream=Stream()),
                stream=True,
            )
    instrumentation.close()


@pytest.mark.asyncio
async def test_async_streaming_request_cannot_be_replayed() -> None:
    _, _, instrumentation = setup()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            yield b"chunk"

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self,
            request: httpx.Request,
        ) -> httpx.Response:
            _ = [chunk async for chunk in request.stream]
            return required("async-stream")

    async with httpx.AsyncClient(transport=Transport()) as client:
        with pytest.raises(PaymentError, match="cannot be replayed"):
            await client.send(
                httpx.Request(
                    "POST",
                    f"{ALLOWED}/async-stream",
                    stream=Stream(),
                ),
                stream=True,
            )
    instrumentation.close()


def test_runtime_factory_failure_closes_challenge_response() -> None:
    closed = threading.Event()

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            yield b"challenge"

        def close(self) -> None:
            closed.set()

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            challenge = required("factory")
            return httpx.Response(
                402,
                headers=challenge.headers,
                stream=Stream(),
            )

    def fail() -> PaymentRuntime:
        raise RuntimeError("factory failed")

    instrumentation = instrument_httpx(fail, [ALLOWED])
    with httpx.Client(transport=Transport()) as client:
        with pytest.raises(RuntimeError, match="factory failed"):
            client.get(f"{ALLOWED}/factory")

    assert closed.is_set()
    instrumentation.close()


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://allowed.test",
        "https://*.allowed.test",
        "https://user:pass@allowed.test",
        "https://allowed.test/path",
        "https://allowed.test?query=1",
        "https://allowed.test#fragment",
    ],
)
def test_rejects_non_origin_allowlist_values(origin: str) -> None:
    method = FakeMethod()
    with pytest.raises(ValueError, match="Invalid HTTP origin"):
        HttpxInstrumentation(lambda: PaymentRuntime([method]), [origin])


def test_rejects_a_bare_origin_string() -> None:
    method = FakeMethod()
    with pytest.raises(TypeError, match="sequence of strings"):
        HttpxInstrumentation(lambda: PaymentRuntime([method]), ALLOWED)


def test_version_and_private_seam_compatibility_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "__version__", "0.29.0")
    with pytest.raises(RuntimeError, match="Unsupported HTTPX 0.29.0"):
        _validate_httpx()


def test_registration_is_transactional_and_restore_is_owner_safe() -> None:
    method = FakeMethod()
    first = instrument_httpx(lambda: PaymentRuntime([method]), [ALLOWED])
    with pytest.raises(RuntimeError, match="already instrumented"):
        instrument_httpx(lambda: PaymentRuntime([method]), [ALLOWED])

    original = inspect.getattr_static(httpx.Client, "_send_handling_redirects")

    def replacement(*_: Any, **__: Any) -> httpx.Response:
        raise AssertionError

    httpx.Client._send_handling_redirects = replacement  # type: ignore[method-assign]
    first.close()
    assert inspect.getattr_static(httpx.Client, "_send_handling_redirects") is replacement
    httpx.Client._send_handling_redirects = original  # type: ignore[method-assign]

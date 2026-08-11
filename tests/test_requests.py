from __future__ import annotations

import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from mpp import Challenge, Credential
from mpp.errors import PaymentError, PaymentOutcomeUnknownError
from mpp.events import WILDCARD_EVENT, EventDispatcher
from mpp.runtime import PaymentRuntime
from requests import Response, Session
from requests.adapters import BaseAdapter
from requests.cookies import RequestsCookieJar, extract_cookies_to_jar
from requests.structures import CaseInsensitiveDict
from urllib3._collections import HTTPHeaderDict

from hermes_mpp.requests import (
    RequestsInstrumentation,
    _validate_requests,
    instrument_requests,
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


class RawResponse:
    def __init__(self, headers: list[tuple[str, str]]) -> None:
        self.headers = HTTPHeaderDict(headers)
        message = Message()
        for name, value in headers:
            message.add_header(name, value)
        self._original_response = SimpleNamespace(msg=message)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class HandlerAdapter(BaseAdapter):
    def __init__(self, handler: Any) -> None:
        self.handler = handler

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> Response:
        return self.handler(request, kwargs)

    def close(self) -> None:
        pass


def make_response(
    request: requests.PreparedRequest,
    status: int,
    *,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
) -> Response:
    pairs = headers or []
    response = Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict(HTTPHeaderDict(pairs))
    response.url = request.url
    response.request = request
    response.raw = RawResponse(pairs)
    response._content = body
    response.cookies = RequestsCookieJar()
    extract_cookies_to_jar(response.cookies, request, response.raw)
    return response


def required(
    request: requests.PreparedRequest,
    identifier: str,
    *,
    realm: str = "allowed.test",
    headers: list[tuple[str, str]] | None = None,
) -> Response:
    challenge = Challenge(
        id=identifier,
        method="tempo",
        intent="charge",
        request={"amount": "1"},
    )
    return make_response(
        request,
        402,
        headers=[
            ("www-authenticate", challenge.to_www_authenticate(realm)),
            *(headers or []),
        ],
    )


def setup() -> tuple[FakeMethod, EventDispatcher, RequestsInstrumentation]:
    method = FakeMethod()
    events = EventDispatcher()
    instrumentation = instrument_requests(
        lambda: PaymentRuntime([method], events=events),
        [ALLOWED],
    )
    return method, events, instrumentation


def session_for(handler: Any) -> Session:
    session = Session()
    session.mount("https://", HandlerAdapter(handler))
    return session


def test_preexisting_session_hooks_and_events_observe_one_logical_response() -> None:
    wire: list[requests.PreparedRequest] = []

    def handler(request: requests.PreparedRequest, _: dict[str, Any]) -> Response:
        wire.append(request)
        if "authorization" in request.headers:
            return make_response(request, 200, body=b'{"paid": true}')
        return required(request, "preexisting")

    preexisting = session_for(handler)
    statuses: list[int] = []

    def hook(response: Response, **_: Any) -> Response:
        statuses.append(response.status_code)
        response.raise_for_status()
        return response

    preexisting.hooks["response"].append(hook)
    method, events, _ = setup()
    names: list[str] = []
    events.on(WILDCARD_EVENT, lambda event: names.append(str(event.name)))

    response = preexisting.get(f"{ALLOWED}/paid")

    assert response.status_code == 200
    assert response.json() == {"paid": True}
    assert len(wire) == 2
    assert "authorization" not in wire[0].headers
    assert wire[1].headers["authorization"].startswith("Payment ")
    assert statuses == [200]
    assert method.calls == 1
    assert names.count("challenge.received") == 1
    assert names.count("credential.created") == 1
    assert names.count("payment.response") == 1


def test_free_response_is_unchanged() -> None:
    method, _, _ = setup()
    wire: list[requests.PreparedRequest] = []

    with session_for(
        lambda request, _: wire.append(request)
        or make_response(request, 200, body=b"free")
    ) as session:
        response = session.get(f"{ALLOWED}/free")

    assert response.text == "free"
    assert len(wire) == 1
    assert method.calls == 0


@pytest.mark.parametrize(
    "authenticate",
    [
        'Payment id="unterminated',
        Challenge(
            id="unsupported",
            method="lightning",
            intent="charge",
            request={"amount": "1"},
        ).to_www_authenticate("allowed.test"),
    ],
)
def test_malformed_and_unsupported_challenges_remain_402(
    authenticate: str,
) -> None:
    method, events, _ = setup()
    names: list[str] = []
    events.on(WILDCARD_EVENT, lambda event: names.append(str(event.name)))

    with session_for(
        lambda request, _: make_response(
            request,
            402,
            headers=[("www-authenticate", authenticate)],
        )
    ) as session:
        response = session.get(f"{ALLOWED}/unsupported")

    assert response.status_code == 402
    assert method.calls == 0
    assert names.count("payment.failed") == 1


def test_post_body_and_challenge_cookie_are_replayed_exactly() -> None:
    method, _, _ = setup()
    bodies: list[bytes] = []
    cookies: list[str] = []

    def handler(request: requests.PreparedRequest, _: dict[str, Any]) -> Response:
        bodies.append(bytes(request.body))
        cookies.append(request.headers.get("cookie", ""))
        if "authorization" in request.headers:
            return make_response(request, 200)
        return required(
            request,
            "cookie",
            headers=[("set-cookie", "session=new; Path=/")],
        )

    with session_for(handler) as session:
        session.cookies.set("session", "old", domain="allowed.test", path="/")
        response = session.post(
            f"{ALLOWED}/paid",
            data=b"exact-body",
            headers={"cookie": "manual=yes; session=old"},
        )
        assert session.cookies.get("session", domain="allowed.test", path="/") == "new"

    assert response.status_code == 200
    assert bodies == [b"exact-body", b"exact-body"]
    assert cookies == ["manual=yes; session=old", "manual=yes; session=new"]
    assert method.calls == 1


def test_challenge_cookie_deletion_is_honored() -> None:
    setup()
    cookies: list[str] = []

    def handler(request: requests.PreparedRequest, _: dict[str, Any]) -> Response:
        cookies.append(request.headers.get("cookie", ""))
        if "authorization" in request.headers:
            return make_response(request, 200)
        return required(
            request,
            "deleted-cookie",
            headers=[("set-cookie", "session=; Max-Age=0; Path=/")],
        )

    with session_for(handler) as session:
        session.cookies.set("session", "old", domain="allowed.test", path="/")
        assert (
            session.get(
                f"{ALLOWED}/paid",
                headers={"cookie": "manual=yes; session=old"},
            ).status_code
            == 200
        )

    assert cookies == ["manual=yes; session=old", "manual=yes"]


def test_redirect_target_must_be_allowed() -> None:
    method, _, _ = setup()
    wire: list[requests.PreparedRequest] = []

    def handler(request: requests.PreparedRequest, _: dict[str, Any]) -> Response:
        wire.append(request)
        if request.url == f"{ALLOWED}/start":
            return make_response(
                request,
                302,
                headers=[("location", "https://blocked.test/paid")],
            )
        return required(request, "blocked", realm="blocked.test")

    with session_for(handler) as session:
        response = session.get(f"{ALLOWED}/start")

    assert response.status_code == 402
    assert len(wire) == 2
    assert method.calls == 0
    assert all("authorization" not in request.headers for request in wire)


def test_same_origin_redirect_pays_challenged_route() -> None:
    method, _, _ = setup()
    paths: list[str] = []
    statuses: list[int] = []

    def handler(request: requests.PreparedRequest, _: dict[str, Any]) -> Response:
        paths.append(request.path_url)
        if request.path_url == "/start":
            return make_response(request, 302, headers=[("location", "/paid")])
        if "authorization" in request.headers:
            return make_response(request, 200)
        return required(request, "redirect")

    with session_for(handler) as session:
        session.hooks["response"].append(
            lambda response, **_: statuses.append(response.status_code) or response
        )
        assert session.get(f"{ALLOWED}/start").status_code == 200

    assert paths == ["/start", "/paid", "/paid"]
    assert statuses == [302, 200]
    assert method.calls == 1


def test_paid_redirect_does_not_trigger_a_second_payment() -> None:
    method, _, _ = setup()
    wire: list[requests.PreparedRequest] = []

    def handler(request: requests.PreparedRequest, _: dict[str, Any]) -> Response:
        wire.append(request)
        if request.url == "https://blocked.test/second":
            return required(request, "second", realm="blocked.test")
        if "authorization" in request.headers:
            return make_response(
                request,
                302,
                headers=[("location", "https://blocked.test/second")],
            )
        return required(request, "first")

    with session_for(handler) as session:
        response = session.get(f"{ALLOWED}/paid")

    assert response.status_code == 402
    assert len(wire) == 3
    assert method.calls == 1
    assert "authorization" not in wire[-1].headers


def test_repeated_challenge_and_later_retry_fail_closed() -> None:
    method, _, _ = setup()
    wire: list[requests.PreparedRequest] = []

    def handler(request: requests.PreparedRequest, _: dict[str, Any]) -> Response:
        wire.append(request)
        return required(request, "repeated")

    with session_for(handler) as session:
        assert session.get(f"{ALLOWED}/repeated").status_code == 402
        with pytest.raises(PaymentOutcomeUnknownError):
            session.get(f"{ALLOWED}/repeated")

    assert len(wire) == 3
    assert method.calls == 1


def test_lost_paid_response_blocks_later_wallet_payments() -> None:
    method, _, _ = setup()
    wire: list[requests.PreparedRequest] = []

    def handler(request: requests.PreparedRequest, _: dict[str, Any]) -> Response:
        wire.append(request)
        if "authorization" in request.headers:
            raise requests.ConnectionError("lost")
        return required(request, request.path_url)

    with session_for(handler) as session:
        with pytest.raises(PaymentOutcomeUnknownError) as raised:
            session.get(f"{ALLOWED}/lost")
        assert isinstance(raised.value.__cause__, requests.ConnectionError)
        with pytest.raises(PaymentOutcomeUnknownError):
            session.get(f"{ALLOWED}/different")

    assert len(wire) == 3
    assert method.calls == 1


def test_concurrent_equivalent_payment_is_not_sent_twice() -> None:
    method, _, _ = setup()
    paid = threading.Event()
    release = threading.Event()

    def handler(request: requests.PreparedRequest, _: dict[str, Any]) -> Response:
        if "authorization" not in request.headers:
            return required(request, "concurrent")
        paid.set()
        assert release.wait(timeout=5)
        return make_response(request, 200)

    session = session_for(handler)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(session.get, f"{ALLOWED}/concurrent")
        assert paid.wait(timeout=5)
        second = executor.submit(session.get, f"{ALLOWED}/concurrent")
        with pytest.raises(PaymentOutcomeUnknownError):
            second.result(timeout=5)
        release.set()
        assert first.result(timeout=5).status_code == 200
    session.close()

    assert method.calls == 1


def test_streaming_request_body_is_not_replayed() -> None:
    method, _, _ = setup()

    def chunks() -> Any:
        yield b"chunk"

    with session_for(lambda request, _: required(request, "stream")) as session:
        with pytest.raises(PaymentError, match="cannot be replayed"):
            session.post(f"{ALLOWED}/stream", data=chunks())

    assert method.calls == 0


def test_method_internal_requests_call_is_not_instrumented_recursively() -> None:
    internal: list[requests.PreparedRequest] = []

    class RecursiveMethod(FakeMethod):
        async def create_credential(self, challenge: Challenge) -> Credential:
            with session_for(
                lambda request, _: internal.append(request)
                or required(request, "internal")
            ) as session:
                assert session.get(f"{ALLOWED}/internal").status_code == 402
            return await super().create_credential(challenge)

    method = RecursiveMethod()
    instrument_requests(lambda: PaymentRuntime([method]), [ALLOWED])
    with session_for(
        lambda request, _: make_response(request, 200)
        if "authorization" in request.headers
        else required(request, "outer")
    ) as session:
        assert session.get(f"{ALLOWED}/outer").status_code == 200

    assert len(internal) == 1
    assert method.calls == 1


def test_close_restores_patch() -> None:
    _, _, instrumentation = setup()
    instrumentation.close()

    assert inspect.getattr_static(Session, "send") is instrumentation._original
    with session_for(lambda request, _: required(request, "after-close")) as session:
        assert session.get(f"{ALLOWED}/after-close").status_code == 402


def test_version_and_private_seam_compatibility_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requests, "__version__", "2.34.0")
    with pytest.raises(RuntimeError, match="Unsupported Requests 2.34.0"):
        _validate_requests()

    monkeypatch.setattr(requests, "__version__", "2.33.0")

    def replacement(self: Session, request: Any, **kwargs: Any) -> Response:
        raise AssertionError((self, request, kwargs))

    monkeypatch.setattr(Session, "send", replacement)
    with pytest.raises(RuntimeError, match="Unsupported Requests seam"):
        _validate_requests()


def test_registration_is_idempotent_and_owner_safe() -> None:
    method = FakeMethod()
    first = instrument_requests(lambda: PaymentRuntime([method]), [ALLOWED])
    first.enable()
    with pytest.raises(RuntimeError, match="already instrumented"):
        instrument_requests(lambda: PaymentRuntime([method]), [ALLOWED])

    original = inspect.getattr_static(Session, "send")

    def replacement(*_: Any, **__: Any) -> Response:
        raise AssertionError

    Session.send = replacement  # type: ignore[method-assign]
    first.close()
    assert inspect.getattr_static(Session, "send") is replacement
    Session.send = original  # type: ignore[method-assign]

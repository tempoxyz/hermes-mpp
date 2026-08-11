from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from email.message import Message
from functools import wraps
from http.cookies import CookieError, SimpleCookie
from types import SimpleNamespace
from typing import Any

import httpx
import requests
from mpp import Challenge, Credential
from mpp.errors import (
    InvalidChallengeError,
    PaymentError,
    PaymentExpiredError,
    PaymentOutcomeUnknownError,
)
from mpp.runtime import PaymentRuntime
from requests import Response, Session
from requests.cookies import RequestsCookieJar, extract_cookies_to_jar, get_cookie_header
from requests.hooks import dispatch_hook
from requests.sessions import preferred_clock

from .payment import (
    BYPASS,
    Attempt,
    Ledger,
    Match,
    RuntimeFactory,
    create_credential,
    deletes_cookie,
    emit_failed,
    emit_response,
    match_challenge,
    origin,
    parse_origins,
    run_async,
    unknown_payment,
)

_SUPPORTED_REQUESTS = {(2, 31), (2, 32), (2, 33)}
_PATCH_LOCK = threading.Lock()
_OWNER: RequestsInstrumentation | None = None


@dataclass(slots=True)
class _Prepared:
    runtime: PaymentRuntime
    match: Match
    request: requests.PreparedRequest
    response: Response
    attempt: Attempt
    credential: Credential
    retry: requests.PreparedRequest


class RequestsInstrumentation:
    """Restorable process-global Requests 2.31–2.33 instrumentation."""

    def __init__(
        self,
        runtime_factory: RuntimeFactory,
        origins: Sequence[str] | None,
        *,
        ledger: Ledger | None = None,
    ) -> None:
        _validate_requests()
        self._runtime_factory = runtime_factory
        self._origins = parse_origins(origins)
        self._ledger = ledger or Ledger()
        self._original = inspect.getattr_static(Session, "send")

        @wraps(self._original)
        def send(
            session: Session,
            request: requests.PreparedRequest,
            **kwargs: Any,
        ) -> Response:
            return self._send(session, request, kwargs)

        self._wrapper = send

    @property
    def active(self) -> bool:
        return _OWNER is self

    def _send(
        self,
        session: Session,
        request: requests.PreparedRequest,
        kwargs: dict[str, Any],
    ) -> Response:
        response_hooks = request.hooks.get("response", [])
        bypass_token: contextvars.Token[bool] | None = None

        def payment_hook(response: Response, **hook_kwargs: Any) -> Response:
            nonlocal bypass_token

            # Session.send has already captured this internal hook. Restore the
            # caller's hooks before it copies the request for any redirect.
            request.hooks["response"] = response_hooks
            response_request = response.request or request
            response_request.hooks["response"] = response_hooks

            prepared: _Prepared | None = None
            if self._should_pay(response_request, response):
                prepared = run_async(
                    lambda: self._prepare_402(session, response_request, response)
                )

            if prepared is not None:
                adapter = session.get_adapter(url=prepared.retry.url)
                retry_token = BYPASS.set(True)
                try:
                    response = _adapter_send(adapter, prepared.retry, hook_kwargs)
                except Exception as cause:
                    failure = cause
                    error = run_async(lambda: self._fail_sent(prepared, failure))
                    raise error from cause
                finally:
                    BYPASS.reset(retry_token)
                response = run_async(lambda: self._finish(prepared, response))

            response = dispatch_hook(
                "response",
                {"response": response_hooks},
                response,
                **hook_kwargs,
            )
            if prepared is not None:
                # A redirect after the paid retry belongs to the same logical
                # request. Do not let it trigger an unbounded payment chain.
                bypass_token = BYPASS.set(True)
            return response

        request.hooks["response"] = [payment_hook]
        try:
            return self._original(session, request, **kwargs)
        finally:
            request.hooks["response"] = response_hooks
            if bypass_token is not None:
                BYPASS.reset(bypass_token)

    def _should_pay(
        self,
        request: requests.PreparedRequest,
        response: Response,
    ) -> bool:
        response_request = response.request or request
        allowed = self._origins is None or (
            origin(request.url) in self._origins
            and origin(response_request.url) in self._origins
        )
        return not BYPASS.get() and allowed and response.status_code == 402

    async def _prepare_402(
        self,
        session: Session,
        request: requests.PreparedRequest,
        response: Response,
    ) -> _Prepared | None:
        try:
            runtime = self._runtime_factory()
            match = _match(runtime, response)
        except BaseException:
            response.close()
            raise
        if match.error is not None:
            await emit_failed(runtime, match, request, response)
            return None
        if match.challenge is None or match.method is None:
            return None

        try:
            content = _body_bytes(request)
        except PaymentError:
            response.close()
            raise

        try:
            attempt = await self._ledger.begin(
                match.challenge,
                request,
                _attempt_keys(match.challenge, request, content),
            )
        except (PaymentOutcomeUnknownError, asyncio.CancelledError):
            response.close()
            raise

        try:
            response.content
            _store_response_cookies(session, request, response)
            credential = await create_credential(runtime, match, request, response)
            retry = _retry(
                request,
                credential,
                session.cookies,
                _changed_cookies(response, request),
            )
        except BaseException as error:
            self._ledger.complete(attempt)
            response.close()
            if isinstance(error, Exception):
                await emit_failed(runtime, match, request, response, error)
            if isinstance(error, (InvalidChallengeError, PaymentExpiredError)):
                return None
            raise

        response.close()
        self._ledger.sent(attempt, credential)
        return _Prepared(runtime, match, request, response, attempt, credential, retry)

    async def _fail_sent(
        self,
        prepared: _Prepared,
        cause: BaseException,
    ) -> PaymentOutcomeUnknownError:
        return await self._mark_uncertain(prepared, prepared.response, cause)

    async def _mark_uncertain(
        self,
        prepared: _Prepared,
        response: Response,
        cause: BaseException,
    ) -> PaymentOutcomeUnknownError:
        assert prepared.match.challenge is not None
        error = unknown_payment(
            prepared.match.challenge,
            prepared.credential,
            prepared.request,
            cause,
        )
        self._ledger.uncertain(prepared.attempt, error)
        await emit_failed(
            prepared.runtime,
            prepared.match,
            prepared.request,
            response,
            error,
            prepared.credential,
        )
        return error

    async def _finish(
        self,
        prepared: _Prepared,
        payment_response: Response,
    ) -> Response:
        if payment_response.status_code >= 400:
            await self._mark_uncertain(
                prepared,
                payment_response,
                RuntimeError(
                    f"Paid retry returned HTTP {payment_response.status_code}"
                ),
            )
        else:
            self._ledger.complete(prepared.attempt)
            await emit_response(
                prepared.runtime,
                prepared.match,
                prepared.credential,
                prepared.request,
                payment_response,
            )
        return payment_response

    def enable(self) -> None:
        global _OWNER
        with _PATCH_LOCK:
            if _OWNER is self:
                return
            if _OWNER is not None:
                raise RuntimeError("Requests is already instrumented")
            if inspect.getattr_static(Session, "send") is not self._original:
                raise RuntimeError("Requests is already instrumented")
            Session.send = self._wrapper  # type: ignore[method-assign]
            _OWNER = self

    def close(self) -> None:
        global _OWNER
        with _PATCH_LOCK:
            if _OWNER is not self:
                return
            if inspect.getattr_static(Session, "send") is self._wrapper:
                Session.send = self._original  # type: ignore[method-assign]
            _OWNER = None


def instrument_requests(
    runtime_factory: RuntimeFactory,
    origins: Sequence[str] | None,
    *,
    ledger: Ledger | None = None,
) -> RequestsInstrumentation:
    instrumentation = RequestsInstrumentation(runtime_factory, origins, ledger=ledger)
    instrumentation.enable()
    return instrumentation


def _adapter_send(
    adapter: requests.adapters.BaseAdapter,
    request: requests.PreparedRequest,
    kwargs: dict[str, Any],
) -> Response:
    start = preferred_clock()
    response = adapter.send(request, **kwargs)
    response.elapsed = timedelta(seconds=preferred_clock() - start)
    return response


def _match(runtime: PaymentRuntime, response: Response) -> Match:
    return match_challenge(runtime, _header_values(response, "www-authenticate"))


def _body_bytes(request: requests.PreparedRequest) -> bytes:
    body = request.body
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode()
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body)
    raise PaymentError(
        "Streaming request bodies cannot be replayed after a payment challenge. "
        "Use a buffered body for paid requests."
    )


def _retry(
    request: requests.PreparedRequest,
    credential: Credential,
    cookies: RequestsCookieJar,
    changed_cookies: set[str],
) -> requests.PreparedRequest:
    retry = request.copy()
    retry.headers["Authorization"] = credential.to_authorization()
    original_cookie = retry.headers.pop("Cookie", None)
    retry.prepare_cookies(cookies)
    updated_cookie = retry.headers.pop("Cookie", None)
    replaced = _cookie_names(updated_cookie) | changed_cookies
    merged = [
        part.strip()
        for part in (original_cookie or "").split(";")
        if part.strip() and part.partition("=")[0].strip() not in replaced
    ]
    if updated_cookie:
        merged.append(updated_cookie)
    if merged:
        retry.headers["Cookie"] = "; ".join(merged)
    return retry


def _store_response_cookies(
    session: Session,
    request: requests.PreparedRequest,
    response: Response,
) -> None:
    extract_cookies_to_jar(session.cookies, request, response.raw)
    session.cookies.update(response.cookies)


def _changed_cookies(
    response: Response,
    request: requests.PreparedRequest,
) -> set[str]:
    message = Message()
    for value in _header_values(response, "set-cookie"):
        parsed = SimpleCookie()
        try:
            parsed.load(value)
        except CookieError:
            continue
        for cookie in parsed.values():
            if deletes_cookie(cookie):
                cookie["expires"] = cookie["max-age"] = ""
            message.add_header("Set-Cookie", cookie.OutputString())

    if not message.get_all("Set-Cookie"):
        return set()

    raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
    jar = RequestsCookieJar()
    extract_cookies_to_jar(jar, request, raw)
    probe = request.copy()
    probe.headers.pop("Cookie", None)
    return _cookie_names(get_cookie_header(jar, probe))


def _cookie_names(value: str | None) -> set[str]:
    return {
        part.partition("=")[0].strip()
        for part in (value or "").split(";")
        if part.strip()
    }


def _header_values(response: Response, name: str) -> list[str]:
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    getlist = getattr(raw_headers, "getlist", None)
    if callable(getlist):
        values = list(getlist(name))
        if values:
            return values
    get_all = getattr(raw_headers, "get_all", None)
    if callable(get_all):
        values = list(get_all(name) or ())
        if values:
            return values
    value = response.headers.get(name)
    return [value] if value else []


def _attempt_keys(
    challenge: Challenge,
    request: requests.PreparedRequest,
    content: bytes,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    request_origin = origin(request.url)
    operation = request.headers.get("idempotency-key") or hashlib.sha256(content).hexdigest()
    return (
        ("challenge", *request_origin, challenge.id),
        (
            "request",
            *request_origin,
            request.method,
            str(httpx.URL(request.url or "")).split("#", 1)[0],
            operation,
        ),
    )


def _validate_requests() -> None:
    try:
        version = tuple(map(int, requests.__version__.split(".")[:2]))
    except ValueError as error:
        raise RuntimeError(
            f"Cannot determine Requests compatibility: {requests.__version__}"
        ) from error
    if version not in _SUPPORTED_REQUESTS:
        raise RuntimeError(
            f"Unsupported Requests {requests.__version__}; expected >=2.31,<2.34"
        )

    seam = inspect.getattr_static(Session, "send")
    parameters = tuple(inspect.signature(seam).parameters.values())
    if (
        getattr(seam, "__module__", None) != "requests.sessions"
        or getattr(seam, "__qualname__", None) != "Session.send"
        or tuple(parameter.name for parameter in parameters) != ("self", "request", "kwargs")
        or parameters[-1].kind is not inspect.Parameter.VAR_KEYWORD
        or inspect.iscoroutinefunction(seam)
    ):
        raise RuntimeError("Unsupported Requests seam: Session.send")

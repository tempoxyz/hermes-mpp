from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from functools import wraps
from http.cookies import CookieError, SimpleCookie
from typing import Any

import httpx
from mpp import Challenge, Credential
from mpp.errors import (
    InvalidChallengeError,
    PaymentError,
    PaymentExpiredError,
    PaymentOutcomeUnknownError,
)
from mpp.runtime import PaymentRuntime

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

_SUPPORTED_HTTPX = {(0, 27), (0, 28)}
_PATCH_LOCK = threading.Lock()
_OWNER: HttpxInstrumentation | None = None


@dataclass(slots=True)
class _Prepared:
    runtime: PaymentRuntime
    match: Match
    request: httpx.Request
    response: httpx.Response
    attempt: Attempt
    credential: Credential
    retry: httpx.Request


class HttpxInstrumentation:
    """Restorable process-global HTTPX 0.27–0.28 instrumentation."""

    def __init__(
        self,
        runtime_factory: RuntimeFactory,
        origins: Sequence[str] | None,
        *,
        ledger: Ledger | None = None,
    ) -> None:
        _validate_httpx()
        self._runtime_factory = runtime_factory
        self._origins = parse_origins(origins)
        self._ledger = ledger or Ledger()
        self._sync_original = inspect.getattr_static(httpx.Client, "_send_single_request")
        self._async_original = inspect.getattr_static(httpx.AsyncClient, "_send_single_request")

        @wraps(self._sync_original)
        def sync_send(
            client: httpx.Client,
            request: httpx.Request,
        ) -> httpx.Response:
            def raw(target: httpx.Request) -> httpx.Response:
                return self._sync_original(client, target)

            response = raw(request)
            if not self._should_pay(request, response):
                return response

            prepared = run_async(
                lambda: self._prepare_402(
                    response,
                    client.cookies,
                    asynchronous=False,
                )
            )
            if prepared is None:
                return response
            try:
                payment_response = raw(prepared.retry)
            except (Exception, asyncio.CancelledError) as cause:
                failure = cause
                error = run_async(lambda: self._fail_sent(prepared, failure))
                raise error from cause
            return run_async(lambda: self._finish(prepared, payment_response))

        @wraps(self._async_original)
        async def async_send(
            client: httpx.AsyncClient,
            request: httpx.Request,
        ) -> httpx.Response:
            async def send(target: httpx.Request) -> httpx.Response:
                return await self._async_original(client, target)

            response = await send(request)
            if not self._should_pay(request, response):
                return response
            prepared = await self._prepare_402(
                response,
                client.cookies,
                asynchronous=True,
            )
            if prepared is None:
                return response
            try:
                payment_response = await send(prepared.retry)
            except (Exception, asyncio.CancelledError) as cause:
                error = await self._fail_sent(prepared, cause)
                raise error from cause
            return await self._finish(prepared, payment_response)

        self._sync_wrapper = sync_send
        self._async_wrapper = async_send

    @property
    def active(self) -> bool:
        return _OWNER is self

    def _should_pay(
        self,
        request: httpx.Request,
        response: httpx.Response,
    ) -> bool:
        allowed = self._origins is None or (
            origin(request.url) in self._origins
            and origin(response.request.url) in self._origins
        )
        return not BYPASS.get() and allowed and response.status_code == 402

    async def _prepare_402(
        self,
        response: httpx.Response,
        cookies: httpx.Cookies,
        *,
        asynchronous: bool,
    ) -> _Prepared | None:
        request = response.request
        try:
            runtime = self._runtime_factory()
            match = _match(runtime, response)
        except BaseException:
            await _close_quietly(response, asynchronous)
            raise
        if match.error is not None:
            try:
                await emit_failed(runtime, match, request, response)
            except BaseException:
                await _close_quietly(response, asynchronous)
                raise
            return None
        if match.challenge is None or match.method is None:
            return None

        try:
            content = request.content
        except httpx.RequestNotRead as cause:
            await _close_quietly(response, asynchronous)
            raise PaymentError(
                "Streaming request bodies cannot be replayed after a payment challenge. "
                "Use a buffered body for paid requests."
            ) from cause

        try:
            attempt = await self._ledger.begin(
                match.challenge,
                request,
                _attempt_keys(match.challenge, request),
            )
        except (PaymentOutcomeUnknownError, asyncio.CancelledError):
            await _close_quietly(response, asynchronous)
            raise

        try:
            await _read(response, asynchronous)
            credential = await create_credential(runtime, match, request, response)
            retry = _retry(
                request,
                content,
                credential,
                cookies,
                _changed_cookies(response),
            )
        except BaseException as error:
            self._ledger.complete(attempt)
            await _close_quietly(response, asynchronous)
            if isinstance(error, Exception):
                await emit_failed(runtime, match, request, response, error)
            if isinstance(error, (InvalidChallengeError, PaymentExpiredError)):
                return None
            raise

        try:
            await _close(response, asynchronous)
        except BaseException:
            self._ledger.complete(attempt)
            raise
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
        response: httpx.Response,
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
        payment_response: httpx.Response,
    ) -> httpx.Response:
        if payment_response.is_error:
            await self._mark_uncertain(
                prepared,
                payment_response,
                RuntimeError(f"Paid retry returned HTTP {payment_response.status_code}"),
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
                raise RuntimeError("HTTPX is already instrumented")
            if (
                inspect.getattr_static(httpx.Client, "_send_single_request")
                is not self._sync_original
                or inspect.getattr_static(httpx.AsyncClient, "_send_single_request")
                is not self._async_original
            ):
                raise RuntimeError("HTTPX is already instrumented")

            httpx.Client._send_single_request = self._sync_wrapper  # type: ignore[method-assign]
            try:
                httpx.AsyncClient._send_single_request = self._async_wrapper  # type: ignore[method-assign]
            except BaseException:
                httpx.Client._send_single_request = self._sync_original  # type: ignore[method-assign]
                raise
            _OWNER = self

    def close(self) -> None:
        global _OWNER
        with _PATCH_LOCK:
            if _OWNER is not self:
                return
            if (
                inspect.getattr_static(httpx.Client, "_send_single_request") is self._sync_wrapper
            ):
                httpx.Client._send_single_request = self._sync_original  # type: ignore[method-assign]
            if (
                inspect.getattr_static(httpx.AsyncClient, "_send_single_request")
                is self._async_wrapper
            ):
                httpx.AsyncClient._send_single_request = self._async_original  # type: ignore[method-assign]
            _OWNER = None


def instrument_httpx(
    runtime_factory: RuntimeFactory,
    origins: Sequence[str] | None,
    *,
    ledger: Ledger | None = None,
) -> HttpxInstrumentation:
    instrumentation = HttpxInstrumentation(runtime_factory, origins, ledger=ledger)
    instrumentation.enable()
    return instrumentation


def _match(runtime: PaymentRuntime, response: httpx.Response) -> Match:
    return match_challenge(runtime, response.headers.get_list("www-authenticate"))


async def _read(response: httpx.Response, asynchronous: bool) -> None:
    if asynchronous:
        await response.aread()
    else:
        response.read()


async def _close(response: httpx.Response, asynchronous: bool) -> None:
    if asynchronous:
        await response.aclose()
    else:
        response.close()


async def _close_quietly(response: httpx.Response, asynchronous: bool) -> None:
    try:
        await _close(response, asynchronous)
    except BaseException:
        pass


def _retry(
    request: httpx.Request,
    content: bytes,
    credential: Credential,
    cookies: httpx.Cookies,
    changed_cookies: set[str],
) -> httpx.Request:
    headers = httpx.Headers(request.headers)
    headers["Authorization"] = credential.to_authorization()
    headers.pop("Cookie", None)
    retry = httpx.Request(
        request.method,
        request.url,
        headers=headers,
        content=content,
        extensions=dict(request.extensions),
    )
    httpx.Cookies(cookies).set_cookie_header(retry)
    original_cookie = request.headers.get("Cookie")
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


def _cookie_names(value: str | None) -> set[str]:
    return {part.partition("=")[0].strip() for part in (value or "").split(";") if part.strip()}


def _changed_cookies(response: httpx.Response) -> set[str]:
    request = httpx.Request("GET", response.request.url)
    response.cookies.set_cookie_header(request)
    names = _cookie_names(request.headers.get("cookie"))
    for value in response.headers.get_list("set-cookie"):
        cookies = SimpleCookie()
        try:
            cookies.load(value)
        except CookieError:
            continue
        for cookie in cookies.values():
            if not deletes_cookie(cookie):
                continue
            cookie["expires"] = cookie["max-age"] = ""
            synthetic = httpx.Response(
                200,
                headers={"set-cookie": cookie.OutputString()},
                request=response.request,
            )
            probe = httpx.Request("GET", response.request.url)
            synthetic.cookies.set_cookie_header(probe)
            names.update(_cookie_names(probe.headers.get("cookie")))
    return names


def _attempt_keys(
    challenge: Challenge,
    request: httpx.Request,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    request_origin = origin(request.url)
    operation = (
        request.headers.get("idempotency-key") or hashlib.sha256(request.content).hexdigest()
    )
    return (
        ("challenge", *request_origin, challenge.id),
        (
            "request",
            *request_origin,
            request.method,
            str(request.url).split("#", 1)[0],
            operation,
        ),
    )


def _validate_httpx() -> None:
    try:
        version = tuple(map(int, httpx.__version__.split(".")[:2]))
    except ValueError as error:
        raise RuntimeError(f"Cannot determine HTTPX compatibility: {httpx.__version__}") from error
    if version not in _SUPPORTED_HTTPX:
        raise RuntimeError(f"Unsupported HTTPX {httpx.__version__}; expected >=0.27,<0.29")

    expected = ("self", "request")
    for owner, asynchronous in ((httpx.Client, False), (httpx.AsyncClient, True)):
        seam = inspect.getattr_static(owner, "_send_single_request")
        if tuple(inspect.signature(seam).parameters) != expected:
            raise RuntimeError(f"Unsupported HTTPX seam: {owner.__name__}._send_single_request")
        if inspect.iscoroutinefunction(seam) is not asynchronous:
            raise RuntimeError(f"Unsupported HTTPX seam type: {owner.__name__}")

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import threading
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import wraps
from typing import Any

import httpx
from mpp import Challenge, Credential, ParseError
from mpp.errors import (
    InvalidChallengeError,
    PaymentError,
    PaymentExpiredError,
    PaymentOutcomeUnknownError,
)
from mpp.events import PAYMENT_FAILED, PAYMENT_RESPONSE
from mpp.runtime import Method, PaymentRuntime

RuntimeFactory = Callable[[], PaymentRuntime]
Origin = tuple[str, str, int | None]

_SUPPORTED_HTTPX = {(0, 27), (0, 28)}
_BYPASS = contextvars.ContextVar("hermes_mpp_httpx_bypass", default=False)
_PATCH_LOCK = threading.Lock()
_OWNER: HttpxInstrumentation | None = None


@dataclass(eq=False, slots=True)
class _Attempt:
    keys: tuple[tuple[Any, ...], tuple[Any, ...]]
    challenge: Challenge
    request: httpx.Request
    credential: Credential | None = None


class _Ledger:
    """Fail closed while an equivalent payment is active or uncertain."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[Any, ...], _Attempt | PaymentOutcomeUnknownError] = {}

    def begin(self, challenge: Challenge, request: httpx.Request) -> _Attempt:
        attempt = _Attempt(_attempt_keys(challenge, request), challenge, request)
        with self._lock:
            for key in attempt.keys:
                existing = self._entries.get(key)
                if isinstance(existing, PaymentOutcomeUnknownError):
                    raise existing
                if isinstance(existing, _Attempt):
                    raise PaymentOutcomeUnknownError(
                        existing.challenge,
                        RuntimeError("A matching payment is already in progress"),
                        credential=existing.credential,
                        request=existing.request,
                    )
            for key in attempt.keys:
                self._entries[key] = attempt
        return attempt

    def sent(self, attempt: _Attempt, credential: Credential) -> None:
        with self._lock:
            attempt.credential = credential

    def uncertain(
        self,
        attempt: _Attempt,
        error: PaymentOutcomeUnknownError,
    ) -> None:
        with self._lock:
            for key in attempt.keys:
                self._entries[key] = error

    def complete(self, attempt: _Attempt) -> None:
        with self._lock:
            for key in attempt.keys:
                if self._entries.get(key) is attempt:
                    self._entries.pop(key)


@dataclass(frozen=True, slots=True)
class _Match:
    challenges: list[Challenge]
    challenge: Challenge | None = None
    method: Method | None = None
    error: Exception | None = None


@dataclass(slots=True)
class _Prepared:
    runtime: PaymentRuntime
    match: _Match
    request: httpx.Request
    response: httpx.Response
    attempt: _Attempt
    credential: Credential
    retry: httpx.Request


class HttpxInstrumentation:
    """Restorable process-global HTTPX 0.27–0.28 instrumentation."""

    def __init__(self, runtime_factory: RuntimeFactory, origins: Sequence[str]) -> None:
        _validate_httpx()
        self._runtime_factory = runtime_factory
        self._origins = _parse_origins(origins)
        self._ledger = _Ledger()
        self._sync_original = inspect.getattr_static(httpx.Client, "_send_handling_redirects")
        self._async_original = inspect.getattr_static(httpx.AsyncClient, "_send_handling_redirects")

        @wraps(self._sync_original)
        def sync_send(
            client: httpx.Client,
            request: httpx.Request,
            follow_redirects: bool,
            history: list[httpx.Response],
        ) -> httpx.Response:
            def raw(
                target: httpx.Request,
                past: list[httpx.Response],
            ) -> httpx.Response:
                return self._sync_original(
                    client,
                    target,
                    follow_redirects=follow_redirects,
                    history=past,
                )

            response = raw(request, history)
            if not self._should_pay(request, response):
                return response

            prepared = _run_async(
                lambda: self._prepare_402(
                    response,
                    client.cookies,
                    asynchronous=False,
                )
            )
            if prepared is None:
                return response
            try:
                payment_response = raw(prepared.retry, list(response.history))
            except (Exception, asyncio.CancelledError) as cause:
                failure = cause
                error = _run_async(lambda: self._fail_sent(prepared, failure))
                raise error from cause
            return _run_async(lambda: self._finish(prepared, payment_response, asynchronous=False))

        @wraps(self._async_original)
        async def async_send(
            client: httpx.AsyncClient,
            request: httpx.Request,
            follow_redirects: bool,
            history: list[httpx.Response],
        ) -> httpx.Response:
            async def send(
                target: httpx.Request,
                past: list[httpx.Response],
            ) -> httpx.Response:
                return await self._async_original(
                    client,
                    target,
                    follow_redirects=follow_redirects,
                    history=past,
                )

            response = await send(request, history)
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
                payment_response = await send(prepared.retry, list(response.history))
            except (Exception, asyncio.CancelledError) as cause:
                error = await self._fail_sent(prepared, cause)
                raise error from cause
            return await self._finish(prepared, payment_response, asynchronous=True)

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
        return (
            not _BYPASS.get()
            and _origin(request.url) in self._origins
            and response.status_code == 402
            and _origin(response.request.url) in self._origins
        )

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
                await _emit_failed(runtime, match, request, response)
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
            attempt = self._ledger.begin(match.challenge, request)
        except PaymentOutcomeUnknownError:
            await _close_quietly(response, asynchronous)
            raise

        try:
            await _read(response, asynchronous)
            credential = await _create_credential(runtime, match, request, response)
            retry = _retry(request, content, credential, cookies)
        except BaseException as error:
            self._ledger.complete(attempt)
            await _close_quietly(response, asynchronous)
            if isinstance(error, Exception):
                await _emit_failed(runtime, match, request, response, error)
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
        assert prepared.match.challenge is not None
        error = _unknown(
            prepared.match.challenge,
            prepared.credential,
            prepared.request,
            cause,
        )
        self._ledger.uncertain(prepared.attempt, error)
        await _emit_failed(
            prepared.runtime,
            prepared.match,
            prepared.request,
            prepared.response,
            error,
            prepared.credential,
        )
        return error

    async def _finish(
        self,
        prepared: _Prepared,
        payment_response: httpx.Response,
        *,
        asynchronous: bool,
    ) -> httpx.Response:
        if not payment_response.is_success:
            assert prepared.match.challenge is not None
            cause = RuntimeError(f"Paid retry returned HTTP {payment_response.status_code}")
            error = _unknown(
                prepared.match.challenge,
                prepared.credential,
                prepared.request,
                cause,
            )
            self._ledger.uncertain(prepared.attempt, error)
            try:
                await _emit_failed(
                    prepared.runtime,
                    prepared.match,
                    prepared.request,
                    payment_response,
                    error,
                    prepared.credential,
                )
            finally:
                await _close_quietly(payment_response, asynchronous)
            raise error from cause

        self._ledger.complete(prepared.attempt)
        await _emit_response(
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
                inspect.getattr_static(httpx.Client, "_send_handling_redirects")
                is not self._sync_original
                or inspect.getattr_static(httpx.AsyncClient, "_send_handling_redirects")
                is not self._async_original
            ):
                raise RuntimeError("HTTPX is already instrumented")

            httpx.Client._send_handling_redirects = self._sync_wrapper  # type: ignore[method-assign]
            try:
                httpx.AsyncClient._send_handling_redirects = self._async_wrapper  # type: ignore[method-assign]
            except BaseException:
                httpx.Client._send_handling_redirects = self._sync_original  # type: ignore[method-assign]
                raise
            _OWNER = self

    def close(self) -> None:
        global _OWNER
        with _PATCH_LOCK:
            if _OWNER is not self:
                return
            if (
                inspect.getattr_static(httpx.Client, "_send_handling_redirects")
                is self._sync_wrapper
            ):
                httpx.Client._send_handling_redirects = self._sync_original  # type: ignore[method-assign]
            if (
                inspect.getattr_static(httpx.AsyncClient, "_send_handling_redirects")
                is self._async_wrapper
            ):
                httpx.AsyncClient._send_handling_redirects = self._async_original  # type: ignore[method-assign]
            _OWNER = None


def instrument_httpx(
    runtime_factory: RuntimeFactory,
    origins: Sequence[str],
) -> HttpxInstrumentation:
    instrumentation = HttpxInstrumentation(runtime_factory, origins)
    instrumentation.enable()
    return instrumentation


def _match(runtime: PaymentRuntime, response: httpx.Response) -> _Match:
    challenges: list[Challenge] = []
    parse_error: ParseError | None = None
    for header in response.headers.get_list("www-authenticate"):
        for field in _auth_challenges(header):
            if field.partition(" ")[0].lower() != "payment":
                continue
            try:
                challenges.append(Challenge.from_www_authenticate(field))
            except ParseError as error:
                parse_error = error

    try:
        challenge, method = runtime.match_challenge(challenges)
    except ValueError as error:
        return _Match(
            challenges,
            error=parse_error or (error if challenges else None),
        )
    return _Match(challenges, challenge, method)


async def _create_credential(
    runtime: PaymentRuntime,
    match: _Match,
    request: httpx.Request,
    response: httpx.Response,
) -> Credential:
    assert match.challenge is not None and match.method is not None
    token = _BYPASS.set(True)
    try:
        return await runtime.create_credential(
            match.challenge,
            match.method,
            event_payload={
                "challenges": match.challenges,
                "request": request,
                "response": response,
            },
        )
    finally:
        _BYPASS.reset(token)


async def _emit_failed(
    runtime: PaymentRuntime,
    match: _Match,
    request: httpx.Request,
    response: httpx.Response,
    error: Exception | None = None,
    credential: Credential | None = None,
) -> None:
    await _emit(
        runtime,
        PAYMENT_FAILED,
        {
            "challenge": match.challenge,
            "challenges": match.challenges,
            "credential": credential,
            "error": error or match.error,
            "method": match.method,
            "request": request,
            "response": response,
        },
    )


async def _emit_response(
    runtime: PaymentRuntime,
    match: _Match,
    credential: Credential,
    request: httpx.Request,
    response: httpx.Response,
) -> None:
    await _emit(
        runtime,
        PAYMENT_RESPONSE,
        {
            "challenge": match.challenge,
            "credential": credential,
            "method": match.method,
            "request": request,
            "response": response,
        },
    )


async def _emit(runtime: PaymentRuntime, name: str, payload: dict[str, Any]) -> None:
    token = _BYPASS.set(True)
    try:
        await runtime.events.emit(name, payload)
    finally:
        _BYPASS.reset(token)


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


def _run_async(factory: Callable[[], Awaitable[Any]]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(
            context.run,
            lambda: asyncio.run(factory()),
        ).result()


def _retry(
    request: httpx.Request,
    content: bytes,
    credential: Credential,
    cookies: httpx.Cookies,
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
    updated_cookie = retry.headers.get("Cookie")
    if original_cookie and updated_cookie:
        updated_names = {part.partition("=")[0].strip() for part in updated_cookie.split(";")}
        preserved = [
            part.strip()
            for part in original_cookie.split(";")
            if part.partition("=")[0].strip() not in updated_names
        ]
        retry.headers["Cookie"] = "; ".join([*preserved, updated_cookie])
    elif original_cookie:
        retry.headers["Cookie"] = original_cookie
    return retry


def _unknown(
    challenge: Challenge,
    credential: Credential,
    request: httpx.Request,
    cause: BaseException,
) -> PaymentOutcomeUnknownError:
    return PaymentOutcomeUnknownError(
        challenge,
        cause,
        credential=credential,
        request=request,
    )


def _auth_challenges(value: str) -> list[str]:
    fields: list[str] = []
    start = 0
    quoted = escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == "," and not quoted:
            fields.append(value[start:index])
            start = index + 1
    fields.append(value[start:])

    challenges: list[str] = []
    token = "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for field in fields:
        field = field.strip()
        end = 0
        while end < len(field) and field[end] in token:
            end += 1
        if (end and not field[end:].lstrip().startswith("=")) or not challenges:
            challenges.append(field)
        else:
            challenges[-1] += f", {field}"
    return challenges


def _origin(url: httpx.URL) -> Origin:
    return url.scheme, url.host, url.port


def _parse_origins(values: Sequence[str]) -> frozenset[Origin]:
    if isinstance(values, str):
        raise TypeError("origins must be a sequence of strings")
    origins: set[Origin] = set()
    for value in values:
        url = httpx.URL(value)
        if (
            not url.is_absolute_url
            or url.scheme not in {"http", "https"}
            or "*" in url.host
            or url.userinfo
            or url.path != "/"
            or url.query
            or url.fragment
        ):
            raise ValueError(f"Invalid HTTP origin: {value!r}")
        origins.add(_origin(url))
    if not origins:
        raise ValueError("At least one HTTP origin is required")
    return frozenset(origins)


def _attempt_keys(
    challenge: Challenge,
    request: httpx.Request,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    origin = _origin(request.url)
    operation = (
        request.headers.get("idempotency-key") or hashlib.sha256(request.content).hexdigest()
    )
    return (
        ("challenge", *origin, challenge.id),
        (
            "request",
            *origin,
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

    expected = ("self", "request", "follow_redirects", "history")
    for owner, asynchronous in ((httpx.Client, False), (httpx.AsyncClient, True)):
        seam = inspect.getattr_static(owner, "_send_handling_redirects")
        if tuple(inspect.signature(seam).parameters) != expected:
            raise RuntimeError(f"Unsupported HTTPX seam: {owner.__name__}._send_handling_redirects")
        if inspect.iscoroutinefunction(seam) is not asynchronous:
            raise RuntimeError(f"Unsupported HTTPX seam type: {owner.__name__}")

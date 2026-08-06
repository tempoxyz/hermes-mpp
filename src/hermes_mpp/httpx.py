from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import threading
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import wraps
from http.cookies import CookieError, Morsel, SimpleCookie
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
from mpp.methods.tempo.session import is_tip1034_session_challenge
from mpp.runtime import Method, PaymentRuntime

RuntimeFactory = Callable[[], PaymentRuntime]
SessionHint = Callable[[str], Awaitable[str | None]]
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
    holds_gate: bool = False


class _Ledger:
    """Serialize wallet payments and fail closed on duplicates or uncertainty."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gate = threading.Lock()
        self._entries: dict[tuple[Any, ...], _Attempt] = {}
        self._uncertain: PaymentOutcomeUnknownError | None = None

    async def begin(self, challenge: Challenge, request: httpx.Request) -> _Attempt:
        attempt = _Attempt(_attempt_keys(challenge, request), challenge, request)
        with self._lock:
            if self._uncertain is not None:
                raise self._uncertain
            for key in attempt.keys:
                existing = self._entries.get(key)
                if existing is not None:
                    raise PaymentOutcomeUnknownError(
                        existing.challenge,
                        RuntimeError("A matching payment is already in progress"),
                        credential=existing.credential,
                        request=existing.request,
                    )
            for key in attempt.keys:
                self._entries[key] = attempt

        try:
            while not self._gate.acquire(blocking=False):
                await asyncio.sleep(0.01)
            attempt.holds_gate = True
        except BaseException:
            self.complete(attempt)
            raise

        with self._lock:
            uncertain = self._uncertain
        if uncertain is not None:
            self.complete(attempt)
            raise uncertain
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
            self._uncertain = error
            self._remove(attempt)

    def complete(self, attempt: _Attempt) -> None:
        with self._lock:
            self._remove(attempt)

    def _remove(self, attempt: _Attempt) -> None:
        for key in attempt.keys:
            if self._entries.get(key) is attempt:
                self._entries.pop(key)
        self._release(attempt)

    def _release(self, attempt: _Attempt) -> None:
        if attempt.holds_gate:
            attempt.holds_gate = False
            self._gate.release()


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


def _method_supports_session(method: Method, challenge: Challenge) -> bool:
    checker = getattr(method, "can_handle_session_challenge", None)
    if checker is not None:
        return bool(checker(challenge))
    resolver = getattr(method, "session_manager_for", None)
    if resolver is None:
        return False
    try:
        manager = resolver(challenge)
    except (TypeError, ValueError):
        return False
    manager_checker = getattr(manager, "can_handle_challenge", None)
    return manager_checker is None or bool(manager_checker(challenge))


class _SyncOriginalTransport(httpx.BaseTransport):
    """Adapt HTTPX's original sync send seam to an upstream session transport."""

    def __init__(self, send: Callable[[httpx.Request], httpx.Response]) -> None:
        self._send = send

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._send(request)


class _AsyncOriginalTransport(httpx.AsyncBaseTransport):
    """Adapt HTTPX's original async send seam to an upstream session transport."""

    def __init__(
        self,
        send: Callable[[httpx.Request], Awaitable[httpx.Response]],
    ) -> None:
        self._send = send

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._send(request)


class HttpxInstrumentation:
    """Restorable process-global HTTPX 0.27–0.28 instrumentation."""

    def __init__(
        self,
        runtime_factory: RuntimeFactory,
        origins: Sequence[str] | None,
        *,
        session_hint: SessionHint | None = None,
        close_session_store: Callable[[], None] | None = None,
    ) -> None:
        _validate_httpx()
        self._runtime_factory = runtime_factory
        self._origins = _parse_origins(origins)
        self._ledger = _Ledger()
        self._session_hint = session_hint
        self._close_session_store = close_session_store
        self._session_store_closed = False
        self._sync_original = inspect.getattr_static(httpx.Client, "_send_single_request")
        self._async_original = inspect.getattr_static(httpx.AsyncClient, "_send_single_request")

        @wraps(self._sync_original)
        def sync_send(
            client: httpx.Client,
            request: httpx.Request,
        ) -> httpx.Response:
            def raw(target: httpx.Request) -> httpx.Response:
                return self._sync_original(client, target)

            self._apply_sync_session_hint(request)
            response = raw(request)
            if not self._should_pay(request, response):
                return response

            try:
                session_manager = self._session_manager(response)
            except BaseException:
                response.close()
                raise
            if session_manager is not None:
                try:
                    content = request.content
                except httpx.RequestNotRead as cause:
                    response.close()
                    raise PaymentError(
                        "Streaming request bodies cannot be replayed after a payment challenge. "
                        "Use a buffered body for paid requests."
                    ) from cause
                base = _replay(
                    request,
                    content,
                    client.cookies,
                    _changed_cookies(response),
                )
                from mpp.methods.tempo.session import SessionPaymentTransport

                token = _BYPASS.set(True)
                try:
                    return SessionPaymentTransport(
                        session_manager,
                        inner=_SyncOriginalTransport(raw),
                    ).handle_payment_required(base, response)
                finally:
                    _BYPASS.reset(token)

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
                payment_response = raw(prepared.retry)
            except (Exception, asyncio.CancelledError) as cause:
                failure = cause
                error = _run_async(lambda: self._fail_sent(prepared, failure))
                raise error from cause
            return _run_async(lambda: self._finish(prepared, payment_response))

        @wraps(self._async_original)
        async def async_send(
            client: httpx.AsyncClient,
            request: httpx.Request,
        ) -> httpx.Response:
            async def send(target: httpx.Request) -> httpx.Response:
                return await self._async_original(client, target)

            await self._apply_async_session_hint(request)
            response = await send(request)
            if not self._should_pay(request, response):
                return response
            try:
                session_manager = self._session_manager(response)
            except BaseException:
                await response.aclose()
                raise
            if session_manager is not None:
                try:
                    content = request.content
                except httpx.RequestNotRead as cause:
                    await response.aclose()
                    raise PaymentError(
                        "Streaming request bodies cannot be replayed after a payment challenge. "
                        "Use a buffered body for paid requests."
                    ) from cause
                base = _replay(
                    request,
                    content,
                    client.cookies,
                    _changed_cookies(response),
                )
                from mpp.methods.tempo.session import AsyncSessionPaymentTransport

                token = _BYPASS.set(True)
                try:
                    return await AsyncSessionPaymentTransport(
                        session_manager,
                        inner=_AsyncOriginalTransport(send),
                    ).handle_payment_required(base, response)
                finally:
                    _BYPASS.reset(token)
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

    def _may_hint(self, request: httpx.Request) -> bool:
        return (
            not _BYPASS.get()
            and self._session_hint is not None
            and (self._origins is None or _origin(request.url) in self._origins)
            and "Payment-Session" not in request.headers
        )

    def _apply_sync_session_hint(self, request: httpx.Request) -> None:
        if not self._may_hint(request):
            return
        assert self._session_hint is not None
        hint = _run_async(lambda: self._session_hint(str(request.url)))
        if hint is not None:
            request.headers["Payment-Session"] = hint

    async def _apply_async_session_hint(self, request: httpx.Request) -> None:
        if not self._may_hint(request):
            return
        assert self._session_hint is not None
        hint = await self._session_hint(str(request.url))
        if hint is not None:
            request.headers["Payment-Session"] = hint

    def _session_manager(self, response: httpx.Response) -> Any | None:
        runtime = self._runtime_factory()
        match = _match(runtime, response)
        if match.challenge is None or match.method is None:
            return None
        resolver = getattr(match.method, "session_manager_for", None)
        if resolver is None or not is_tip1034_session_challenge(match.challenge):
            return None
        return resolver(match.challenge)

    @property
    def active(self) -> bool:
        return _OWNER is self

    def _should_pay(
        self,
        request: httpx.Request,
        response: httpx.Response,
    ) -> bool:
        allowed = self._origins is None or (
            _origin(request.url) in self._origins and _origin(response.request.url) in self._origins
        )
        return not _BYPASS.get() and allowed and response.status_code == 402

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
            attempt = await self._ledger.begin(match.challenge, request)
        except (PaymentOutcomeUnknownError, asyncio.CancelledError):
            await _close_quietly(response, asynchronous)
            raise

        try:
            await _read(response, asynchronous)
            credential = await _create_credential(runtime, match, request, response)
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
        return await self._mark_uncertain(prepared, prepared.response, cause)

    async def _mark_uncertain(
        self,
        prepared: _Prepared,
        response: httpx.Response,
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
            if inspect.getattr_static(httpx.Client, "_send_single_request") is self._sync_wrapper:
                httpx.Client._send_single_request = self._sync_original  # type: ignore[method-assign]
            if (
                inspect.getattr_static(httpx.AsyncClient, "_send_single_request")
                is self._async_wrapper
            ):
                httpx.AsyncClient._send_single_request = self._async_original  # type: ignore[method-assign]
            _OWNER = None
        if self._close_session_store is not None and not self._session_store_closed:
            self._session_store_closed = True
            self._close_session_store()


def instrument_httpx(
    runtime_factory: RuntimeFactory,
    origins: Sequence[str] | None,
    *,
    session_hint: SessionHint | None = None,
    close_session_store: Callable[[], None] | None = None,
) -> HttpxInstrumentation:
    instrumentation = HttpxInstrumentation(
        runtime_factory,
        origins,
        session_hint=session_hint,
        close_session_store=close_session_store,
    )
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

    session_match = next(
        (
            (challenge, method)
            for challenge in challenges
            if is_tip1034_session_challenge(challenge)
            for method in runtime.methods
            if method.name == challenge.method and _method_supports_session(method, challenge)
        ),
        None,
    )
    non_session_challenges = [
        challenge
        for challenge in challenges
        if not (challenge.method == "tempo" and challenge.intent == "session")
    ]
    try:
        challenge, method = (
            session_match
            if session_match is not None
            else runtime.match_challenge(non_session_challenges)
        )
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
    changed_cookies: set[str],
) -> httpx.Request:
    retry = _replay(request, content, cookies, changed_cookies)
    retry.headers["Authorization"] = credential.to_authorization()
    return retry


def _replay(
    request: httpx.Request,
    content: bytes,
    cookies: httpx.Cookies,
    changed_cookies: set[str],
) -> httpx.Request:
    """Create a replayable request with challenge-response cookie updates."""

    headers = httpx.Headers(request.headers)
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
            if not _deletes_cookie(cookie):
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


def _deletes_cookie(cookie: Morsel[str]) -> bool:
    try:
        if cookie["max-age"] and int(cookie["max-age"]) <= 0:
            return True
        expires = parsedate_to_datetime(cookie["expires"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(UTC)
    except (TypeError, ValueError):
        return False


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


def _parse_origins(values: Sequence[str] | None) -> frozenset[Origin] | None:
    if values is None:
        return None
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

    expected = ("self", "request")
    for owner, asynchronous in ((httpx.Client, False), (httpx.AsyncClient, True)):
        seam = inspect.getattr_static(owner, "_send_single_request")
        if tuple(inspect.signature(seam).parameters) != expected:
            raise RuntimeError(f"Unsupported HTTPX seam: {owner.__name__}._send_single_request")
        if inspect.iscoroutinefunction(seam) is not asynchronous:
            raise RuntimeError(f"Unsupported HTTPX seam type: {owner.__name__}")

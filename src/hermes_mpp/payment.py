from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Awaitable, Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookies import Morsel
from typing import Any

import httpx
from mpp import Challenge, Credential, ParseError
from mpp.errors import PaymentOutcomeUnknownError
from mpp.events import PAYMENT_FAILED, PAYMENT_RESPONSE
from mpp.runtime import Method, PaymentRuntime

RuntimeFactory = Callable[[], PaymentRuntime]
Origin = tuple[str, str, int | None]

BYPASS = contextvars.ContextVar("hermes_mpp_transport_bypass", default=False)


@dataclass(eq=False, slots=True)
class Attempt:
    keys: tuple[tuple[Any, ...], tuple[Any, ...]]
    challenge: Challenge
    request: Any
    credential: Credential | None = None
    holds_gate: bool = False


class Ledger:
    """Serialize wallet payments across transports and fail closed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gate = threading.Lock()
        self._entries: dict[tuple[Any, ...], Attempt] = {}
        self._uncertain: PaymentOutcomeUnknownError | None = None

    async def begin(
        self,
        challenge: Challenge,
        request: Any,
        keys: tuple[tuple[Any, ...], tuple[Any, ...]],
    ) -> Attempt:
        attempt = Attempt(keys, challenge, request)
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

    def sent(self, attempt: Attempt, credential: Credential) -> None:
        with self._lock:
            attempt.credential = credential

    def uncertain(
        self,
        attempt: Attempt,
        error: PaymentOutcomeUnknownError,
    ) -> None:
        with self._lock:
            self._uncertain = error
            self._remove(attempt)

    def complete(self, attempt: Attempt) -> None:
        with self._lock:
            self._remove(attempt)

    def _remove(self, attempt: Attempt) -> None:
        for key in attempt.keys:
            if self._entries.get(key) is attempt:
                self._entries.pop(key)
        if attempt.holds_gate:
            attempt.holds_gate = False
            self._gate.release()


@dataclass(frozen=True, slots=True)
class Match:
    challenges: list[Challenge]
    challenge: Challenge | None = None
    method: Method | None = None
    error: Exception | None = None


def match_challenge(runtime: PaymentRuntime, headers: Iterable[str]) -> Match:
    challenges: list[Challenge] = []
    parse_error: ParseError | None = None
    for header in headers:
        for field in auth_challenges(header):
            if field.partition(" ")[0].lower() != "payment":
                continue
            try:
                challenges.append(Challenge.from_www_authenticate(field))
            except ParseError as error:
                parse_error = error

    try:
        challenge, method = runtime.match_challenge(challenges)
    except ValueError as error:
        return Match(
            challenges,
            error=parse_error or (error if challenges else None),
        )
    return Match(challenges, challenge, method)


async def create_credential(
    runtime: PaymentRuntime,
    match: Match,
    request: Any,
    response: Any,
) -> Credential:
    assert match.challenge is not None and match.method is not None
    token = BYPASS.set(True)
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
        BYPASS.reset(token)


async def emit_failed(
    runtime: PaymentRuntime,
    match: Match,
    request: Any,
    response: Any,
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


async def emit_response(
    runtime: PaymentRuntime,
    match: Match,
    credential: Credential,
    request: Any,
    response: Any,
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
    token = BYPASS.set(True)
    try:
        await runtime.events.emit(name, payload)
    finally:
        BYPASS.reset(token)


def run_async(factory: Callable[[], Awaitable[Any]]) -> Any:
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


def auth_challenges(value: str) -> list[str]:
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


def origin(value: httpx.URL | str | bytes | None) -> Origin:
    url = value if isinstance(value, httpx.URL) else httpx.URL(value or "")
    return url.scheme, url.host, url.port


def parse_origins(values: Sequence[str] | None) -> frozenset[Origin] | None:
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
        origins.add(origin(url))
    if not origins:
        raise ValueError("At least one HTTP origin is required")
    return frozenset(origins)


def deletes_cookie(cookie: Morsel[str]) -> bool:
    try:
        if cookie["max-age"] and int(cookie["max-age"]) <= 0:
            return True
        expires = parsedate_to_datetime(cookie["expires"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(UTC)
    except (TypeError, ValueError):
        return False


def unknown_payment(
    challenge: Challenge,
    credential: Credential,
    request: Any,
    cause: BaseException,
) -> PaymentOutcomeUnknownError:
    return PaymentOutcomeUnknownError(
        challenge,
        cause,
        credential=credential,
        request=request,
    )

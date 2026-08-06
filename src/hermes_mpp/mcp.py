from __future__ import annotations

import inspect
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

import httpx
from mpp import Challenge
from mpp.errors import PaymentOutcomeUnknownError
from mpp.extensions.mcp import META_RECEIPT, McpClient, McpToolResult
from mpp.runtime import Method

from .httpx import _parse_origins

Origin = tuple[str, str, int | None]

_SUPPORTED_HERMES_VERSIONS = {"0.19.0", "0.20.0"}
_PATCH_LOCK = threading.Lock()
_OWNER: McpInstrumentation | None = None


class McpPaymentDeniedError(PermissionError):
    """The configured Hermes MCP server policy denied credential creation."""


@dataclass(frozen=True, slots=True)
class _ServerIdentity:
    name: str
    transport: str
    endpoint: str
    realm: str
    origin: Origin | None
    auto_pay: bool

    @classmethod
    def from_server(cls, server: Any) -> _ServerIdentity:
        name = getattr(server, "name", None)
        config = getattr(server, "_config", None)
        if not isinstance(name, str) or not name:
            raise RuntimeError("Unsupported Hermes MCP server identity")
        if not isinstance(config, dict):
            raise RuntimeError(f"Unsupported Hermes MCP config for {name!r}")

        raw_policy = config.get("mpp")
        if raw_policy is None:
            raw_policy = {}
        if not isinstance(raw_policy, Mapping):
            raise RuntimeError(f"mcp_servers.{name}.mpp must be an object")
        auto_pay = raw_policy.get("auto_pay", True)
        if not isinstance(auto_pay, bool):
            raise RuntimeError(f"mcp_servers.{name}.mpp.auto_pay must be a boolean")

        if "url" in config:
            value = config.get("url")
            if not isinstance(value, str):
                raise RuntimeError(f"Unsupported Hermes MCP URL for {name!r}")
            url = httpx.URL(value)
            if (
                not url.is_absolute_url
                or url.scheme not in {"http", "https"}
                or url.userinfo
            ):
                raise RuntimeError(f"Unsupported Hermes MCP URL for {name!r}")
            transport = "sse" if config.get("transport") == "sse" else "streamable-http"
            endpoint = str(url)
            default_realm = url.netloc.decode("ascii")
            origin: Origin | None = (url.scheme, url.host, url.port)
        else:
            command = config.get("command")
            args = config.get("args", [])
            if not isinstance(command, str) or not command:
                raise RuntimeError(f"Unsupported Hermes MCP command for {name!r}")
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                raise RuntimeError(f"Unsupported Hermes MCP arguments for {name!r}")
            transport = "stdio"
            endpoint = repr((command, tuple(args)))
            default_realm = name
            origin = None

        realm = raw_policy.get("realm", default_realm)
        if not isinstance(realm, str) or not realm.strip():
            raise RuntimeError(f"mcp_servers.{name}.mpp.realm must be a non-empty string")

        return cls(
            name=name,
            transport=transport,
            endpoint=endpoint,
            realm=realm.strip(),
            origin=origin,
            auto_pay=auto_pay,
        )


@dataclass(slots=True)
class _ServerPaymentState:
    identity: _ServerIdentity
    allowed_origins: frozenset[Origin] | None
    _uncertain: PaymentOutcomeUnknownError | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def authorize(self, challenge: Challenge) -> None:
        with self._lock:
            uncertain = self._uncertain
        if uncertain is not None:
            raise uncertain

        identity = self.identity
        if not identity.auto_pay:
            raise McpPaymentDeniedError(
                f"MPP payment denied by policy for MCP server {identity.name!r}"
            )
        if identity.origin is not None and self.allowed_origins is not None:
            if identity.origin not in self.allowed_origins:
                raise McpPaymentDeniedError(
                    f"MPP payment denied for MCP server {identity.name!r}: "
                    "its configured origin is not in $MPP_ALLOWED_ORIGINS"
                )

        realm = getattr(challenge, "realm", None)
        if not isinstance(realm, str) or realm != identity.realm:
            raise McpPaymentDeniedError(
                f"MPP payment denied for MCP server {identity.name!r}: "
                f"challenge realm {realm!r} does not match configured realm "
                f"{identity.realm!r} ({identity.transport})"
            )

    def mark_uncertain(self, error: PaymentOutcomeUnknownError) -> None:
        with self._lock:
            if self._uncertain is None:
                self._uncertain = error


class _BoundMethod:
    def __init__(self, method: Method, state: _ServerPaymentState) -> None:
        self._method = method
        self._state = state
        self.name = method.name

    @property
    def _intents(self) -> Any:
        return (
            getattr(self._method, "intents", None)
            or getattr(self._method, "_intents", None)
            or {"charge": True}
        )

    async def create_credential(self, challenge: Challenge):
        self._state.authorize(challenge)
        return await self._method.create_credential(challenge)


class _ReceiptResult:
    """Expose an MCP receipt through Hermes' existing structured-content path."""

    def __init__(self, result: McpToolResult) -> None:
        self._result = result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._result, name)

    @property
    def structuredContent(self) -> dict[str, Any]:  # noqa: N802 - MCP wire name
        structured = getattr(self._result, "structuredContent", None)
        merged = dict(structured) if structured is not None else {}
        if META_RECEIPT not in merged:
            assert self._result.receipt is not None
            merged[META_RECEIPT] = self._result.receipt.to_dict()
        return merged


class _HermesMcpSession:
    def __init__(
        self,
        session: Any,
        method: Method,
        state: _ServerPaymentState,
        owner: McpInstrumentation,
    ) -> None:
        self._session = session
        self._client = McpClient(session, methods=[_BoundMethod(method, state)])
        self._state = state
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        try:
            result = await self._client.call_tool(
                name,
                arguments,
                timeout=timeout,
                meta=meta,
            )
        except PaymentOutcomeUnknownError as error:
            self._state.mark_uncertain(error)
            raise
        if result.receipt is not None:
            return _ReceiptResult(result)
        return result


class McpInstrumentation:
    """Restorable, version-gated Hermes MCP session instrumentation."""

    def __init__(self, method: Method, origins: Sequence[str] | None) -> None:
        owner, seam = _validate_hermes()
        self._method = method
        self._allowed_origins = _parse_origins(origins)
        self._owner_class = owner
        self._original = seam
        self._states: dict[_ServerIdentity, _ServerPaymentState] = {}
        self._servers: dict[int, Any] = {}

        @wraps(seam)
        async def discover(server: Any) -> Any:
            self._wrap_session(server)
            return await seam(server)

        self._wrapper = discover

    @property
    def active(self) -> bool:
        return _OWNER is self

    def _wrap_session(self, server: Any) -> None:
        with _PATCH_LOCK:
            if _OWNER is not self:
                return
            session = getattr(server, "session", None)
            if session is None:
                return
            if isinstance(session, _HermesMcpSession):
                if session._owner is self:
                    return
                raise RuntimeError("Hermes MCP session is already instrumented")

            from mcp import ClientSession

            if not isinstance(session, ClientSession):
                raise RuntimeError(
                    f"Unsupported Hermes MCP session type: {type(session).__module__}."
                    f"{type(session).__qualname__}"
                )

            identity = _ServerIdentity.from_server(server)
            state = self._states.get(identity)
            if state is None:
                state = _ServerPaymentState(identity, self._allowed_origins)
                self._states[identity] = state
            server.session = _HermesMcpSession(session, self._method, state, self)
            self._servers[id(server)] = server

    def enable(self) -> None:
        global _OWNER
        with _PATCH_LOCK:
            if _OWNER is self:
                return
            if _OWNER is not None:
                raise RuntimeError("Hermes MCP is already instrumented")
            if inspect.getattr_static(self._owner_class, "_discover_tools") is not self._original:
                raise RuntimeError("Hermes MCP is already instrumented")
            self._owner_class._discover_tools = self._wrapper
            _OWNER = self

    def close(self) -> None:
        global _OWNER
        with _PATCH_LOCK:
            if _OWNER is not self:
                return
            if inspect.getattr_static(self._owner_class, "_discover_tools") is self._wrapper:
                self._owner_class._discover_tools = self._original
            for server in self._servers.values():
                session = getattr(server, "session", None)
                if isinstance(session, _HermesMcpSession) and session._owner is self:
                    server.session = session._session
            self._servers.clear()
            self._states.clear()
            _OWNER = None


def instrument_mcp(method: Method, origins: Sequence[str] | None) -> McpInstrumentation:
    instrumentation = McpInstrumentation(method, origins)
    instrumentation.enable()
    return instrumentation


def _validate_hermes() -> tuple[type[Any], Any]:
    import hermes_cli
    from tools.mcp_tool import MCPServerTask

    version = getattr(hermes_cli, "__version__", None)
    if version not in _SUPPORTED_HERMES_VERSIONS:
        supported = ", ".join(sorted(_SUPPORTED_HERMES_VERSIONS))
        raise RuntimeError(f"Unsupported Hermes Agent {version!r}; expected {supported}")
    if MCPServerTask.__module__ != "tools.mcp_tool":
        raise RuntimeError("Unsupported Hermes MCPServerTask class")
    slots = set(getattr(MCPServerTask, "__slots__", ()))
    if not {"name", "session", "_config"}.issubset(slots):
        raise RuntimeError("Unsupported Hermes MCPServerTask layout")

    seam = inspect.getattr_static(MCPServerTask, "_discover_tools")
    if tuple(inspect.signature(seam).parameters) != ("self",):
        raise RuntimeError("Unsupported Hermes MCPServerTask._discover_tools signature")
    if not inspect.iscoroutinefunction(seam):
        raise RuntimeError("Unsupported Hermes MCPServerTask._discover_tools type")
    return MCPServerTask, seam

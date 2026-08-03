from __future__ import annotations

import json
from typing import Any

import httpx
from tools.url_safety import async_is_safe_url, redirect_target_from_response

_MAX_BODY_BYTES = 50_000
_MAX_HEADERS = 50
_MAX_HEADER_VALUE_CHARS = 1_000
_SENSITIVE_HEADER_MARKERS = (
    "api-key",
    "authentication-info",
    "authorization",
    "cookie",
    "secret",
    "token",
)

SCHEMA = {
    "name": "mpp_fetch",
    "description": (
        "Send HTTP requests with Hermes's already-configured MPP wallet. Use this directly "
        "for HTTP APIs and purchases: it handles supported 402 challenges automatically. "
        "Do not install or invoke curl, mppx, or another payment client."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute HTTP or HTTPS URL."},
            "method": {"type": "string", "default": "GET"},
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "body": {"type": "string", "description": "Raw request body."},
            "json": {"description": "JSON body: object, array, scalar, or null."},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}


async def _guard_redirect(response: httpx.Response) -> None:
    target = redirect_target_from_response(response)
    if target and not await async_is_safe_url(target):
        raise ValueError("Blocked redirect to a private or internal URL")


async def _body(response: httpx.Response) -> tuple[str, bool]:
    chunks: list[bytes] = []
    size = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        remaining = _MAX_BODY_BYTES - size
        if remaining <= 0:
            truncated = True
            break
        chunks.append(chunk[:remaining])
        size += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
            break
    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"), truncated


def _headers(response: httpx.Response) -> dict[str, str]:
    visible: dict[str, str] = {}
    for name, value in response.headers.items():
        if len(visible) == _MAX_HEADERS:
            break
        if len(name) <= 200 and not any(part in name.lower() for part in _SENSITIVE_HEADER_MARKERS):
            visible[name] = value[:_MAX_HEADER_VALUE_CHARS]
    return visible


async def mpp_fetch(args: dict[str, Any], **_: Any) -> str:
    if "body" in args and "json" in args:
        raise ValueError("body and json are mutually exclusive")
    url = args["url"]
    if not isinstance(url, str) or not await async_is_safe_url(url):
        raise ValueError("Blocked private or internal URL")

    headers = httpx.Headers(args.get("headers"))
    request: dict[str, Any] = {"headers": headers}
    if "body" in args:
        request["content"] = args["body"]
    if "json" in args:
        if args["json"] is None:
            headers.setdefault("Content-Type", "application/json")
            request["content"] = "null"
        else:
            request["json"] = args["json"]

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=60,
        event_hooks={"response": [_guard_redirect]},
    ) as client:
        async with client.stream(args.get("method", "GET").upper(), url, **request) as response:
            body, truncated = await _body(response)
            result = {
                "status": response.status_code,
                "url": str(response.url),
                "headers": _headers(response),
                "body": body,
                "truncated": truncated,
            }
    return json.dumps(
        result,
        ensure_ascii=False,
    )


def register_tool(ctx: Any) -> None:
    ctx.register_tool(
        name="mpp_fetch",
        toolset="mpp",
        schema=SCHEMA,
        handler=mpp_fetch,
        requires_env=["TEMPO_PRIVATE_KEY"],
        is_async=True,
        description="Payment-aware HTTP requests",
        emoji="💳",
    )

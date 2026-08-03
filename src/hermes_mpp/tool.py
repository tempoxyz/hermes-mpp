from __future__ import annotations

import json
from typing import Any

import httpx

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
            "json": {"type": "object", "description": "JSON request body."},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}


async def mpp_fetch(args: dict[str, Any], **_: Any) -> str:
    if "body" in args and "json" in args:
        raise ValueError("body and json are mutually exclusive")

    request: dict[str, Any] = {"headers": args.get("headers")}
    if "body" in args:
        request["content"] = args["body"]
    if "json" in args:
        request["json"] = args["json"]

    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        response = await client.request(
            args.get("method", "GET").upper(),
            args["url"],
            **request,
        )
    return json.dumps(
        {
            "status": response.status_code,
            "url": str(response.url),
            "headers": dict(response.headers),
            "body": response.text,
        },
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

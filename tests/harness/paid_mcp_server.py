#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mpp.extensions.mcp import (
    META_CREDENTIAL,
    META_RECEIPT,
    MCPChallenge,
    PaymentRequiredError,
)

_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00"
    b"\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
).decode()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "streamable-http", "sse"), required=True)
    parser.add_argument("--realm", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def _meta(req: types.CallToolRequest) -> dict[str, Any]:
    if req.params.meta is None:
        return {}
    return req.params.meta.model_dump(by_alias=True, mode="json", exclude_none=True)


def _record(path: Path, **event: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, sort_keys=True) + "\n")


def _challenge(realm: str, tool: str, number: int) -> MCPChallenge:
    return MCPChallenge(
        id=f"{tool}-{number}",
        realm=realm,
        method="tempo",
        intent="charge",
        request={
            "amount": "1",
            "currency": "pathUSD",
            "recipient": "0x0000000000000000000000000000000000000001",
            "methodDetails": {"chainId": 42431},
        },
        expires=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        description=f"Deterministic payment for {tool}",
    )


def build_server(realm: str, state_path: Path, port: int) -> FastMCP:
    app = FastMCP(
        "hermes-mpp-test",
        host="127.0.0.1",
        port=port,
        log_level="ERROR",
        streamable_http_path="/mcp",
        sse_path="/sse",
        message_path="/messages/",
    )
    server = app._mcp_server
    issued: dict[str, int] = {}

    tools = [
        types.Tool(name="free", description="Free text tool", inputSchema={"type": "object"}),
        types.Tool(
            name="free_error",
            description="Free tool-level error",
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="paid_rich",
            description="Paid rich-content tool",
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="malformed",
            description="Malformed payment challenge",
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="retry_twice",
            description="Challenges the credential retry again",
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="drop_after_credential",
            description="Drops the response after receiving a credential",
            inputSchema={"type": "object"},
        ),
    ]

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    async def call_tool(req: types.CallToolRequest) -> types.ServerResult:
        tool = req.params.name
        arguments = req.params.arguments or {}
        meta = _meta(req)
        credential = meta.get(META_CREDENTIAL)
        _record(
            state_path,
            tool=tool,
            credential=credential is not None,
            caller_meta=meta.get("caller"),
        )

        if tool == "free":
            return types.ServerResult(
                types.CallToolResult(
                    content=[types.TextContent(type="text", text="free-ok")],
                    structuredContent={"free": True},
                )
            )
        if tool == "free_error":
            return types.ServerResult(
                types.CallToolResult(
                    content=[types.TextContent(type="text", text="free-error")],
                    isError=True,
                )
            )
        if tool == "malformed":
            raise McpError(
                types.ErrorData(
                    code=-32042,
                    message="Payment Required",
                    data={"challenges": [{"id": "missing-fields"}]},
                )
            )

        if credential is None:
            issued[tool] = issued.get(tool, 0) + 1
            raise PaymentRequiredError([_challenge(realm, tool, issued[tool])])

        challenge = credential.get("challenge", {}) if isinstance(credential, dict) else {}
        challenge_id = challenge.get("id", "unknown")
        if tool == "retry_twice":
            issued[tool] = issued.get(tool, 0) + 1
            raise PaymentRequiredError([_challenge(realm, tool, issued[tool])])
        if tool == "drop_after_credential":
            os._exit(73)

        receipt = {
            "status": "success",
            "challengeId": challenge_id,
            "method": "tempo",
            "timestamp": "2026-08-06T00:00:00Z",
            "reference": f"receipt-{challenge_id}",
        }
        return types.ServerResult(
            types.CallToolResult(
                content=[
                    types.TextContent(type="text", text="paid-ok"),
                    types.ImageContent(type="image", data=_PNG, mimeType="image/png"),
                ],
                structuredContent={
                    "value": arguments.get("value"),
                    "caller": meta.get("caller"),
                },
                _meta={META_RECEIPT: receipt},
            )
        )

    server.request_handlers[types.CallToolRequest] = call_tool
    return app


def main() -> None:
    args = _arguments()
    server = build_server(args.realm, args.state, args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()

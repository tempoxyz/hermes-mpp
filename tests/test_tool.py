from __future__ import annotations

import json

import httpx
import pytest

import hermes_mpp.tool as tool


@pytest.mark.asyncio
async def test_fetches_json_with_ordinary_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    client_type = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"paid": True}, headers={"x-result": "ok"})

    monkeypatch.setattr(
        tool.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )

    result = json.loads(
        await tool.mpp_fetch(
            {
                "url": "https://shop.test/buy",
                "method": "post",
                "headers": {"x-request": "yes"},
                "json": {"name": "Parv"},
            }
        )
    )

    assert result["status"] == 201
    assert json.loads(result["body"]) == {"paid": True}
    assert result["headers"]["x-result"] == "ok"
    assert requests[0].method == "POST"
    assert requests[0].headers["x-request"] == "yes"
    assert json.loads(requests[0].content) == {"name": "Parv"}


@pytest.mark.asyncio
async def test_rejects_two_body_forms() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        await tool.mpp_fetch({"url": "https://shop.test", "body": "x", "json": {}})

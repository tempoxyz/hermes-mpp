from __future__ import annotations

import json

import httpx
import pytest

import hermes_mpp.tool as tool


@pytest.fixture(autouse=True)
def allow_test_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allowed(_: str) -> bool:
        return True

    monkeypatch.setattr(tool, "async_is_safe_url", allowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"name": "Parv"}, [1, 2], "value", 1, True, None])
async def test_fetches_json_with_ordinary_httpx(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    requests: list[httpx.Request] = []
    client_type = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"paid": True},
            headers={
                "set-cookie": "session=secret",
                "x-api-key": "secret",
                "x-result": "ok",
            },
        )

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
                "json": payload,
            }
        )
    )

    assert result["status"] == 201
    assert json.loads(result["body"]) == {"paid": True}
    assert result["truncated"] is False
    assert result["headers"]["x-result"] == "ok"
    assert "set-cookie" not in result["headers"]
    assert "x-api-key" not in result["headers"]
    assert requests[0].method == "POST"
    assert requests[0].headers["x-request"] == "yes"
    assert json.loads(requests[0].content) == payload


def test_schema_accepts_any_json_value() -> None:
    assert "type" not in tool.SCHEMA["parameters"]["properties"]["json"]


@pytest.mark.asyncio
async def test_rejects_two_body_forms() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        await tool.mpp_fetch({"url": "https://shop.test", "body": "x", "json": {}})


@pytest.mark.asyncio
async def test_blocks_private_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unsafe(_: str) -> bool:
        return False

    monkeypatch.setattr(tool, "async_is_safe_url", unsafe)
    with pytest.raises(ValueError, match="private or internal"):
        await tool.mpp_fetch({"url": "http://127.0.0.1/private"})


@pytest.mark.asyncio
async def test_blocks_private_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    client_type = httpx.AsyncClient

    async def safe(url: str) -> bool:
        return url == "https://public.test/start"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(tool, "async_is_safe_url", safe)
    monkeypatch.setattr(
        tool.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )

    with pytest.raises(ValueError, match="Blocked redirect"):
        await tool.mpp_fetch({"url": "https://public.test/start"})


@pytest.mark.asyncio
async def test_caps_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        tool.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 50_001)),
            **kwargs,
        ),
    )

    result = json.loads(await tool.mpp_fetch({"url": "https://public.test/large"}))
    assert len(result["body"]) == 50_000
    assert result["truncated"] is True

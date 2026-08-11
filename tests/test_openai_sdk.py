from __future__ import annotations

import httpx
from mpp import Challenge, Credential
from mpp.runtime import PaymentRuntime
from openai import OpenAI

from hermes_mpp.httpx import instrument_httpx


class FakeMethod:
    name = "tempo"

    def __init__(self) -> None:
        self.calls = 0

    async def create_credential(self, challenge: Challenge) -> Credential:
        self.calls += 1
        return Credential(challenge=challenge.to_echo(), payload={"hash": "0xpaid"})


def test_unmodified_openai_sdk_handles_payment_challenge_without_api_key() -> None:
    origin = "https://model.test"
    challenge = Challenge(
        id="openai-sdk",
        method="tempo",
        intent="charge",
        request={"amount": "1"},
    )
    wire: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wire.append(request)
        if request.headers.get("authorization", "").startswith("Payment "):
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-paid",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "openai/gpt-oss-120b",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "payment aware",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 2,
                        "total_tokens": 3,
                    },
                },
            )
        return httpx.Response(
            402,
            headers={
                "www-authenticate": challenge.to_www_authenticate("model.test")
            },
        )

    method = FakeMethod()
    instrument_httpx(lambda: PaymentRuntime([method]), [origin])
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAI(
            api_key="no-key-required",
            base_url=f"{origin}/v1",
            http_client=http_client,
        )
        result = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": "Reply with OK"}],
        )

    assert result.choices[0].message.content == "payment aware"
    assert len(wire) == 2
    assert wire[0].headers["authorization"] == "Bearer no-key-required"
    assert wire[1].headers["authorization"].startswith("Payment ")
    assert wire[0].content == wire[1].content
    assert method.calls == 1

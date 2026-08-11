from __future__ import annotations

import json
from typing import Any

import pytest
import requests
from firecrawl import Firecrawl
from mpp import Challenge, Credential
from mpp.runtime import PaymentRuntime
from requests import Response, Session
from requests.adapters import BaseAdapter

from hermes_mpp.requests import instrument_requests


class FakeMethod:
    name = "tempo"

    def __init__(self) -> None:
        self.calls = 0

    async def create_credential(self, challenge: Challenge) -> Credential:
        self.calls += 1
        return Credential(challenge=challenge.to_echo(), payload={"hash": "0xpaid"})


def test_unmodified_firecrawl_sdk_handles_payment_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire: list[dict[str, Any]] = []
    challenge = Challenge(
        id="firecrawl-sdk",
        method="tempo",
        intent="charge",
        request={"amount": "1"},
    )

    class Adapter(BaseAdapter):
        def send(
            self,
            request: requests.PreparedRequest,
            **_: Any,
        ) -> Response:
            wire.append(
                {
                    "path": request.path_url,
                    "body": request.body,
                    "authorization": request.headers.get("authorization"),
                }
            )
            response = Response()
            response.url = request.url
            response.request = request
            if request.headers.get("authorization"):
                payload = json.dumps(
                    {
                        "success": True,
                        "data": {
                            "web": [
                                {
                                    "url": "https://mpp.dev",
                                    "title": "Machine Payments Protocol",
                                }
                            ]
                        },
                    }
                ).encode()
                response.status_code = 200
                response.headers["content-type"] = "application/json"
                response._content = payload
                return response

            response.status_code = 402
            response.headers["www-authenticate"] = challenge.to_www_authenticate(
                "firecrawl.test"
            )
            response._content = b""
            return response

        def close(self) -> None:
            pass

    adapter = Adapter()
    monkeypatch.setattr(Session, "get_adapter", lambda *_args, **_kwargs: adapter)
    origin = "https://firecrawl.test"
    method = FakeMethod()
    instrument_requests(lambda: PaymentRuntime([method]), [origin])

    result = Firecrawl(api_url=origin, max_retries=1).search(
        query="Machine Payments Protocol",
        limit=1,
    )

    assert result.web is not None
    assert result.web[0].title == "Machine Payments Protocol"
    assert [request["path"] for request in wire] == ["/v2/search", "/v2/search"]
    assert wire[0]["body"] == wire[1]["body"]
    assert wire[0]["authorization"] is None
    assert str(wire[1]["authorization"]).startswith("Payment ")
    assert method.calls == 1

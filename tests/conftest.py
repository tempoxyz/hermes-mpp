from __future__ import annotations

import inspect
from collections.abc import Iterator

import httpx
import pytest
import requests

import hermes_mpp
import hermes_mpp.httpx as httpx_integration
import hermes_mpp.requests as requests_integration

SEAMS = (
    (httpx.Client, "_send_single_request"),
    (httpx.AsyncClient, "_send_single_request"),
    (requests.Session, "send"),
)
ORIGINAL_SEAMS = tuple(inspect.getattr_static(owner, name) for owner, name in SEAMS)


@pytest.fixture(autouse=True)
def restore_transports() -> Iterator[None]:
    if httpx_integration._OWNER is not None:
        httpx_integration._OWNER.close()
    if requests_integration._OWNER is not None:
        requests_integration._OWNER.close()
    hermes_mpp._shutdown()
    yield
    if httpx_integration._OWNER is not None:
        httpx_integration._OWNER.close()
    if requests_integration._OWNER is not None:
        requests_integration._OWNER.close()
    hermes_mpp._shutdown()
    for (owner, name), original in zip(SEAMS, ORIGINAL_SEAMS, strict=True):
        setattr(owner, name, original)

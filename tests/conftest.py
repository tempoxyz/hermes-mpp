from __future__ import annotations

import inspect
from collections.abc import Iterator

import httpx
import pytest
from tools.mcp_tool import MCPServerTask

import hermes_mpp
import hermes_mpp.httpx as httpx_integration
import hermes_mpp.mcp as mcp_integration

SEAMS = (
    (httpx.Client, "_send_single_request"),
    (httpx.AsyncClient, "_send_single_request"),
)
ORIGINAL_SEAMS = tuple(inspect.getattr_static(owner, name) for owner, name in SEAMS)
ORIGINAL_MCP_SEAM = inspect.getattr_static(MCPServerTask, "_discover_tools")


@pytest.fixture(autouse=True)
def restore_instrumentation() -> Iterator[None]:
    if httpx_integration._OWNER is not None:
        httpx_integration._OWNER.close()
    if mcp_integration._OWNER is not None:
        mcp_integration._OWNER.close()
    hermes_mpp._shutdown()
    yield
    if httpx_integration._OWNER is not None:
        httpx_integration._OWNER.close()
    if mcp_integration._OWNER is not None:
        mcp_integration._OWNER.close()
    hermes_mpp._shutdown()
    for (owner, name), original in zip(SEAMS, ORIGINAL_SEAMS, strict=True):
        setattr(owner, name, original)
    setattr(MCPServerTask, "_discover_tools", ORIGINAL_MCP_SEAM)

from __future__ import annotations

import inspect
from pathlib import Path

import httpx
import pytest
from tools.mcp_tool import MCPServerTask

import hermes_mpp

TEST_PRIVATE_KEY = "0x" + "11" * 32


def test_real_hermes_entrypoint_load_is_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    bundled = tmp_path / "bundled"
    home.mkdir()
    bundled.mkdir()
    (home / "config.yaml").write_text(
        "_config_version: 33\nplugins:\n  enabled:\n    - mpp\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("TEMPO_PRIVATE_KEY", TEST_PRIVATE_KEY)
    monkeypatch.delenv("MPP_ALLOWED_ORIGINS", raising=False)

    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["mpp"]
    assert loaded.enabled, loaded.error
    assert loaded.manifest.source == "entrypoint"
    assert loaded.tools_registered == ["mpp_fetch"]
    assert loaded.hooks_registered == []
    assert loaded.commands_registered == []
    assert loaded.middleware_registered == []
    instrumentation = hermes_mpp._instrumentation
    assert instrumentation is not None and instrumentation.active
    mcp_instrumentation = hermes_mpp._mcp_instrumentation
    assert mcp_instrumentation is not None and mcp_instrumentation.active
    assert mcp_instrumentation._method is hermes_mpp._method

    manager.discover_and_load(force=True)
    assert hermes_mpp._instrumentation is instrumentation
    assert hermes_mpp._mcp_instrumentation is mcp_instrumentation

    hermes_mpp._shutdown()
    assert hermes_mpp._instrumentation is None
    assert hermes_mpp._mcp_instrumentation is None
    assert (
        inspect.getattr_static(httpx.Client, "_send_single_request")
        is instrumentation._sync_original
    )
    assert (
        inspect.getattr_static(MCPServerTask, "_discover_tools")
        is mcp_instrumentation._original
    )


def test_registration_failure_restores_httpx(monkeypatch) -> None:
    class Context:
        def register_tool(self, **_kwargs) -> None:
            raise RuntimeError("conflict")

    monkeypatch.setenv("TEMPO_PRIVATE_KEY", TEST_PRIVATE_KEY)
    original = inspect.getattr_static(httpx.Client, "_send_single_request")
    original_mcp = inspect.getattr_static(MCPServerTask, "_discover_tools")

    with pytest.raises(RuntimeError, match="conflict"):
        hermes_mpp.register(Context())

    assert hermes_mpp._instrumentation is None
    assert hermes_mpp._mcp_instrumentation is None
    assert inspect.getattr_static(httpx.Client, "_send_single_request") is original
    assert inspect.getattr_static(MCPServerTask, "_discover_tools") is original_mcp

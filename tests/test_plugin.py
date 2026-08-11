from __future__ import annotations

import inspect
from pathlib import Path

import httpx
import pytest
import requests

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

    manager.discover_and_load(force=True)
    assert hermes_mpp._instrumentation is instrumentation

    hermes_mpp._shutdown()
    assert hermes_mpp._instrumentation is None
    assert (
        inspect.getattr_static(httpx.Client, "_send_single_request")
        is instrumentation.httpx._sync_original
    )
    assert inspect.getattr_static(requests.Session, "send") is instrumentation.requests._original


def test_registration_failure_restores_transports(monkeypatch) -> None:
    class Context:
        def register_tool(self, **_kwargs) -> None:
            raise RuntimeError("conflict")

    monkeypatch.setenv("TEMPO_PRIVATE_KEY", TEST_PRIVATE_KEY)
    httpx_original = inspect.getattr_static(httpx.Client, "_send_single_request")
    requests_original = inspect.getattr_static(requests.Session, "send")

    with pytest.raises(RuntimeError, match="conflict"):
        hermes_mpp.register(Context())

    assert hermes_mpp._instrumentation is None
    assert inspect.getattr_static(httpx.Client, "_send_single_request") is httpx_original
    assert inspect.getattr_static(requests.Session, "send") is requests_original

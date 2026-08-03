from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_mpp import cli

PRIVATE_KEY = "0x" + "11" * 32


def test_wallet_generates_and_persists_a_private_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=value\n", encoding="utf-8")
    monkeypatch.delenv("TEMPO_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "")
    monkeypatch.setattr(cli.secrets, "token_hex", lambda _: "11" * 32)

    account = cli._wallet(env_file)

    assert account.private_key == PRIVATE_KEY.removeprefix("0x")
    assert env_file.read_text(encoding="utf-8") == (
        f"EXISTING=value\nTEMPO_PRIVATE_KEY={PRIVATE_KEY}\n"
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_wallet_reuses_existing_key_without_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"TEMPO_PRIVATE_KEY={PRIVATE_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
    )

    assert cli._wallet(env_file).private_key == PRIVATE_KEY.removeprefix("0x")


def test_wallet_rejects_invalid_key_without_modifying_file(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=value\n", encoding="utf-8")
    monkeypatch.setenv("TEMPO_PRIVATE_KEY", "invalid")

    with pytest.raises(SystemExit, match="Invalid Tempo private key"):
        cli._wallet(env_file)

    assert env_file.read_text(encoding="utf-8") == "EXISTING=value\n"


def test_install_targets_hermes_and_enables_plugin(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_hermes_bin", lambda _, name: Path(f"/hermes/{name}"))
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr(cli.importlib.metadata, "version", lambda _: "1.2.3")
    monkeypatch.setattr(cli, "_wallet", lambda _: SimpleNamespace(address="0xabc"))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    cli.install()

    assert calls[0][0] == [
        "/usr/bin/uv",
        "pip",
        "uninstall",
        "--python",
        "/hermes/python",
        "mpp-hermes",
    ]
    assert calls[1][0] == [
        "/usr/bin/uv",
        "pip",
        "install",
        "--python",
        "/hermes/python",
        "hermes-mpp==1.2.3",
    ]
    assert calls[2][0] == [
        "/hermes/hermes",
        "plugins",
        "enable",
        "mpp",
        "--no-allow-tool-override",
    ]
    assert capsys.readouterr().out == "Installed hermes-mpp. Wallet: 0xabc\n"


def test_uninstall_disables_plugin_and_keeps_wallet(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_hermes_bin", lambda _, name: Path(f"/hermes/{name}"))
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    cli.uninstall()

    assert calls[0][0] == ["/hermes/hermes", "plugins", "disable", "mpp"]
    assert calls[1][0] == [
        "/usr/bin/uv",
        "pip",
        "uninstall",
        "--python",
        "/hermes/python",
        "hermes-mpp",
    ]
    assert capsys.readouterr().out == (
        "Uninstalled hermes-mpp. The wallet remains in Hermes's .env file.\n"
    )

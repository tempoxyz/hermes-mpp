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
    (tmp_path / ".env").write_text(f"TEMPO_PRIVATE_KEY={PRIVATE_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        cli,
        "_hermes_installation",
        lambda *_: cli.HermesInstallation(Path("/hermes/python"), Path("/hermes/hermes")),
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr(cli.importlib.metadata, "version", lambda _: "1.2.3")
    monkeypatch.setattr(cli, "_wallet", lambda _: SimpleNamespace(address="0xabc"))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    cli.install(allowed_origins=["https://mpp.dev", "https://api.example.com"])

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
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        f"TEMPO_PRIVATE_KEY={PRIVATE_KEY}\n"
        "MPP_ALLOWED_ORIGINS=https://mpp.dev,https://api.example.com\n"
    )
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert capsys.readouterr().out == "Installed hermes-mpp. Wallet: 0xabc\n"


def test_install_rejects_invalid_origin_before_changes(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_hermes_installation",
        lambda *_: (_ for _ in ()).throw(AssertionError("unexpected install")),
    )

    with pytest.raises(SystemExit, match="Invalid HTTP origin"):
        cli.install(allowed_origins=["https://mpp.dev/path"])


def test_uninstall_disables_plugin_and_keeps_wallet(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        cli,
        "_hermes_installation",
        lambda *_: cli.HermesInstallation(Path("/hermes/python"), Path("/hermes/hermes")),
    )
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


def test_hermes_installation_prefers_path(tmp_path: Path, monkeypatch) -> None:
    environment = tmp_path / "venv" / "bin"
    environment.mkdir(parents=True)
    python = environment / "python"
    hermes = environment / "hermes"
    python.touch()
    hermes.touch()
    launcher = tmp_path / "hermes"
    launcher.symlink_to(hermes)
    monkeypatch.delenv("HERMES_PYTHON", raising=False)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: str(launcher) if name == "hermes" else None
    )
    monkeypatch.setattr(cli, "_has_hermes", lambda candidate: candidate == python.resolve())

    assert cli._hermes_installation(tmp_path) == cli.HermesInstallation(
        python.resolve(), hermes.resolve()
    )


def test_hermes_installation_accepts_override(tmp_path: Path, monkeypatch) -> None:
    environment = tmp_path / "venv" / "bin"
    environment.mkdir(parents=True)
    python = environment / "python"
    hermes = environment / "hermes"
    python.touch()
    hermes.touch()
    monkeypatch.setattr(cli, "_has_hermes", lambda candidate: candidate == python.resolve())

    assert cli._hermes_installation(tmp_path, str(python)) == cli.HermesInstallation(
        python.resolve(), hermes.resolve()
    )


def test_hermes_installation_falls_back_to_managed_venv(
    tmp_path: Path, monkeypatch
) -> None:
    environment = tmp_path / "hermes-agent" / "venv" / "bin"
    environment.mkdir(parents=True)
    python = environment / "python"
    hermes = environment / "hermes"
    python.touch()
    hermes.touch()
    monkeypatch.delenv("HERMES_PYTHON", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    monkeypatch.setattr(cli, "_has_hermes", lambda candidate: candidate == python)

    assert cli._hermes_installation(tmp_path) == cli.HermesInstallation(python, hermes)

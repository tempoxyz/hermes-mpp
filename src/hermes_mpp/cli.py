from __future__ import annotations

import argparse
import getpass
import importlib.metadata
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mpp.methods.tempo import TempoAccount

from .httpx import _parse_origins


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def _bin(directory: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return directory / f"{name}{suffix}"


@dataclass(frozen=True)
class HermesInstallation:
    python: Path
    hermes: Path


def _has_hermes(python: Path) -> bool:
    if not python.is_file():
        return False
    try:
        result = subprocess.run(
            [str(python), "-c", "import hermes_cli"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _from_python(python: Path) -> HermesInstallation | None:
    python = python.expanduser().absolute()
    hermes = _bin(python.parent, "hermes")
    if hermes.is_file() and _has_hermes(python):
        return HermesInstallation(python, hermes)
    return None


def _from_hermes(hermes: Path) -> HermesInstallation | None:
    resolved = hermes.expanduser().resolve()
    return _from_python(_bin(resolved.parent, "python"))


def _hermes_installation(home: Path, override: str | None = None) -> HermesInstallation:
    explicit = override or os.environ.get("HERMES_PYTHON")
    if explicit:
        installation = _from_python(Path(explicit))
        if installation is None:
            raise SystemExit(f"Hermes is not installed for Python: {explicit}")
        return installation

    command = shutil.which("hermes")
    candidates = []
    if command:
        candidates.append(_from_hermes(Path(command)))

    scripts = "Scripts" if os.name == "nt" else "bin"
    candidates.append(_from_python(_bin(home / "hermes-agent" / "venv" / scripts, "python")))
    if os.name != "nt":
        candidates.append(_from_python(Path("/usr/local/lib/hermes-agent/venv/bin/python")))

    for installation in candidates:
        if installation is not None:
            return installation
    raise SystemExit(
        "Hermes is not installed. Install Hermes or pass --hermes-python PATH."
    )


def _env_key(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("TEMPO_PRIVATE_KEY="):
            return line.partition("=")[2].strip().strip("'\"") or None
    return None


def _write_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text("\n".join([*lines, f"{key}={value}"]) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def _wallet(env_file: Path) -> TempoAccount:
    private_key = _env_key(env_file) or os.environ.get("TEMPO_PRIVATE_KEY", "").strip()
    if not private_key:
        private_key = getpass.getpass(
            "Tempo private key (leave blank to generate a new wallet): "
        ).strip()
    if not private_key:
        private_key = "0x" + secrets.token_hex(32)
    try:
        account = TempoAccount.from_key(private_key)
    except Exception as error:
        raise SystemExit("Invalid Tempo private key.") from error

    _write_env(env_file, "TEMPO_PRIVATE_KEY", private_key)
    return account


def install(
    python_override: str | None = None,
    *,
    allowed_origins: list[str] | None = None,
) -> None:
    if allowed_origins is not None:
        try:
            _parse_origins(allowed_origins)
        except (TypeError, ValueError) as error:
            raise SystemExit(str(error)) from None
    home = _hermes_home()
    installation = _hermes_installation(home, python_override)
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to install hermes-mpp.")

    package = f"hermes-mpp=={importlib.metadata.version('hermes-mpp')}"
    subprocess.run(
        [uv, "pip", "uninstall", "--python", str(installation.python), "mpp-hermes"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [uv, "pip", "install", "--python", str(installation.python), package], check=True
    )
    env_file = home / ".env"
    account = _wallet(env_file)
    if allowed_origins is not None:
        _write_env(env_file, "MPP_ALLOWED_ORIGINS", ",".join(allowed_origins))
    subprocess.run(
        [str(installation.hermes), "plugins", "enable", "mpp", "--no-allow-tool-override"],
        check=True,
        env={**os.environ, "HERMES_HOME": str(home)},
    )
    print(f"Installed hermes-mpp. Wallet: {account.address}")


def uninstall(python_override: str | None = None) -> None:
    home = _hermes_home()
    installation = _hermes_installation(home, python_override)
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to uninstall hermes-mpp.")

    subprocess.run(
        [str(installation.hermes), "plugins", "disable", "mpp"],
        check=True,
        env={**os.environ, "HERMES_HOME": str(home)},
    )
    subprocess.run(
        [uv, "pip", "uninstall", "--python", str(installation.python), "hermes-mpp"],
        check=True,
    )
    print("Uninstalled hermes-mpp. The wallet remains in Hermes's .env file.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage hermes-mpp for Hermes Agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("install", "Install into Hermes and configure a wallet."),
        ("uninstall", "Disable and remove hermes-mpp from Hermes."),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument(
            "--hermes-python",
            metavar="PATH",
            help="Python executable used by Hermes (or set HERMES_PYTHON).",
        )
        if command == "install":
            command_parser.add_argument(
                "--allowed-origin",
                action="append",
                metavar="ORIGIN",
                help="Exact origin allowed to charge the wallet; repeat to allow more than one.",
            )
    args = parser.parse_args()
    if args.command == "install":
        install(args.hermes_python, allowed_origins=args.allowed_origin)
    else:
        uninstall(args.hermes_python)


if __name__ == "__main__":
    main()

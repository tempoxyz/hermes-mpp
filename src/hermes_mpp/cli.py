from __future__ import annotations

import argparse
import getpass
import importlib.metadata
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from mpp.methods.tempo import TempoAccount


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def _hermes_bin(home: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = home / "hermes-agent" / "venv" / ("Scripts" if os.name == "nt" else "bin")
    executable = path / f"{name}{suffix}"
    if not executable.exists():
        raise SystemExit("Hermes is not installed. Install Hermes first, then rerun this command.")
    return executable


def _env_key(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("TEMPO_PRIVATE_KEY="):
            return line.partition("=")[2].strip().strip("'\"") or None
    return None


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

    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    lines = [line for line in lines if not line.startswith("TEMPO_PRIVATE_KEY=")]
    env_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = env_file.with_name(f"{env_file.name}.tmp")
    temporary.write_text("\n".join([*lines, f"TEMPO_PRIVATE_KEY={private_key}"]) + "\n")
    temporary.chmod(0o600)
    temporary.replace(env_file)
    return account


def install() -> None:
    home = _hermes_home()
    python = _hermes_bin(home, "python")
    hermes = _hermes_bin(home, "hermes")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to install hermes-mpp.")

    package = f"hermes-mpp=={importlib.metadata.version('hermes-mpp')}"
    subprocess.run(
        [uv, "pip", "uninstall", "--python", str(python), "mpp-hermes"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run([uv, "pip", "install", "--python", str(python), package], check=True)
    account = _wallet(home / ".env")
    subprocess.run(
        [str(hermes), "plugins", "enable", "mpp", "--no-allow-tool-override"],
        check=True,
        env={**os.environ, "HERMES_HOME": str(home)},
    )
    print(f"Installed hermes-mpp. Wallet: {account.address}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install and configure hermes-mpp.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("install", help="Install into Hermes and configure a wallet.")
    args = parser.parse_args()
    if args.command == "install":
        install()


if __name__ == "__main__":
    main()

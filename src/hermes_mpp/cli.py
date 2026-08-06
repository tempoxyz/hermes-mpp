from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.metadata
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
from mpp.methods.tempo import TempoAccount
from mpp.methods.tempo.session import SQLiteSessionStore
from mpp.methods.tempo.session.transport import SessionPaymentTransport

from .config import Config
from .session import SessionHost


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


def _session_db(home: Path) -> Path:
    return Path(
        os.environ.get("MPP_SESSION_DB", str(home / "mpp-sessions.sqlite3"))
    ).expanduser()


def _session_config(home: Path) -> Config:
    values = dict(os.environ)
    values["HERMES_HOME"] = str(home)
    if not values.get("TEMPO_PRIVATE_KEY", "").strip():
        private_key = _env_key(home / ".env")
        if private_key is not None:
            values["TEMPO_PRIVATE_KEY"] = private_key
    try:
        return Config.from_env(values)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def list_sessions() -> None:
    """Print the durable Tempo sessions known to Hermes."""

    store = SQLiteSessionStore(_session_db(_hermes_home()))
    try:
        records = asyncio.run(store.list())
    finally:
        store.close()
    if not records:
        print("No Tempo sessions.")
        return
    print("CHANNEL\tSTATUS\tCHAIN\tDEPOSIT\tAUTHORIZED\tSPENT\tPENDING\tRESOURCE")
    for record in records:
        pending = "-" if record.pending is None else record.pending.action.value
        print(
            "\t".join(
                (
                    record.channel_id,
                    record.status.value,
                    str(record.chain_id),
                    str(record.deposit),
                    str(record.authorized_cumulative),
                    str(record.spent),
                    pending,
                    record.resource_url,
                )
            )
        )


async def _close_session(channel_id: str) -> tuple[int, str]:
    host = SessionHost(_session_config(_hermes_home()))
    transport: SessionPaymentTransport | None = None
    try:
        record = await host.store.get_by_channel(channel_id)
        if record is None:
            raise SystemExit(f"Unknown Tempo session channel: {channel_id}")
        if not record.resource_url:
            raise SystemExit("Tempo session has no stored resource URL for close negotiation")
        transport = SessionPaymentTransport(host.manager_for_chain(record.chain_id))
        try:
            response = transport.close_session(record.channel_id, record.resource_url)
            response.read()
        except httpx.HTTPError as error:
            raise SystemExit(f"Tempo session close failed: {error}") from error
        return response.status_code, record.channel_id
    finally:
        if transport is not None:
            transport.close()
        host.close()


def close_session(channel_id: str) -> None:
    """Cooperatively close one durable Tempo session."""

    status, normalized = asyncio.run(_close_session(channel_id))
    if not 200 <= status < 300:
        raise SystemExit(f"Tempo session close returned HTTP {status}")
    print(f"Closed Tempo session {normalized}.")


def install(python_override: str | None = None) -> None:
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
    account = _wallet(home / ".env")
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
    sessions_parser = subparsers.add_parser(
        "sessions", help="List or close durable Tempo sessions."
    )
    session_commands = sessions_parser.add_subparsers(
        dest="session_command", required=True
    )
    session_commands.add_parser("list", help="List durable Tempo sessions.")
    close_parser = session_commands.add_parser(
        "close", help="Cooperatively close a Tempo session."
    )
    close_parser.add_argument("channel_id", metavar="CHANNEL_ID")
    args = parser.parse_args()
    if args.command == "install":
        install(args.hermes_python)
    elif args.command == "uninstall":
        uninstall(args.hermes_python)
    elif args.session_command == "list":
        list_sessions()
    else:
        close_session(args.channel_id)


if __name__ == "__main__":
    main()

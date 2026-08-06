from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from mpp.methods.tempo.session import (
    ChannelDescriptor,
    ChannelState,
    SessionRecord,
    SessionStatus,
    channel_scope,
    compute_channel_id,
)

from hermes_mpp import cli
from hermes_mpp.config import Config
from hermes_mpp.session import SessionHost

PRIVATE_KEY = "0x" + "11" * 32
PAYEE = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20
ESCROW = "0x4D50500000000000000000000000000000000000"
CHAIN_ID = 42431
RESOURCE = "https://service.test/session"


@dataclass
class FakeRpc:
    async def gas_price(self) -> int:
        return 1

    async def channel_state(self, escrow: str, channel_id: str) -> ChannelState:
        return ChannelState(0, 100, 0)


def config(tmp_path: Path) -> Config:
    return Config(
        private_key=PRIVATE_KEY,
        allowed_origins=None,
        session_db=tmp_path / "mpp-sessions.sqlite3",
        session_max_deposit=1_000,
        session_max_top_up=500,
        session_max_spend=1_000,
    )


def record(host: SessionHost) -> SessionRecord:
    descriptor = ChannelDescriptor(
        payer=host.account.address,
        payee=PAYEE,
        operator="0x" + "00" * 20,
        token=TOKEN,
        salt="0x" + "01" * 32,
        authorized_signer=host.account.address,
        expiring_nonce_hash="0x" + "02" * 32,
    )
    return SessionRecord(
        scope=channel_scope(
            payee=PAYEE,
            token=TOKEN,
            escrow=ESCROW,
            chain_id=CHAIN_ID,
        ),
        channel_id=compute_channel_id(descriptor, escrow=ESCROW, chain_id=CHAIN_ID),
        descriptor=descriptor,
        escrow=ESCROW,
        chain_id=CHAIN_ID,
        deposit=100,
        authorized_cumulative=20,
        accepted_cumulative=20,
        settled=0,
        spent=10,
        status=SessionStatus.ACTIVE,
        resource_url=RESOURCE,
    )


def test_session_host_shares_managers_and_persists_hints(tmp_path: Path) -> None:
    host = SessionHost(config(tmp_path), rpc_factory=lambda _chain_id: FakeRpc())
    saved = record(host)
    asyncio.run(host.store.save(saved))

    assert host.manager_for_chain(CHAIN_ID) is host.manager_for_chain(CHAIN_ID)
    assert host.manager_for_chain(4217) is not host.manager_for_chain(CHAIN_ID)
    assert asyncio.run(host.hint(RESOURCE)) == saved.channel_id
    host.close()

    restarted = SessionHost(config(tmp_path), rpc_factory=lambda _chain_id: FakeRpc())
    assert asyncio.run(restarted.hint(RESOURCE)) == saved.channel_id
    restarted.close()


def test_cli_lists_durable_sessions(tmp_path: Path, monkeypatch, capsys) -> None:
    host = SessionHost(config(tmp_path), rpc_factory=lambda _chain_id: FakeRpc())
    saved = record(host)
    asyncio.run(host.store.save(saved))
    host.close()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    cli.list_sessions()

    output = capsys.readouterr().out
    assert "CHANNEL\tSTATUS\tCHAIN" in output
    assert saved.channel_id in output
    assert f"\tactive\t{CHAIN_ID}\t100\t20\t10\t-\t{RESOURCE}" in output


def test_cli_close_uses_stored_chain_and_resource(tmp_path: Path, monkeypatch, capsys) -> None:
    host = SessionHost(config(tmp_path), rpc_factory=lambda _chain_id: FakeRpc())
    saved = record(host)
    asyncio.run(host.store.save(saved))
    host.close()
    calls: list[tuple[str, str]] = []

    def close_session(self, channel_id: str, resource_url: str) -> httpx.Response:
        calls.append((channel_id, resource_url))
        return httpx.Response(200, content=b"")

    monkeypatch.setattr(cli.SessionPaymentTransport, "close_session", close_session)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TEMPO_PRIVATE_KEY", PRIVATE_KEY)

    cli.close_session(saved.channel_id)

    assert calls == [(saved.channel_id, RESOURCE)]
    assert capsys.readouterr().out == f"Closed Tempo session {saved.channel_id}.\n"


def test_cli_close_rejects_unknown_channel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TEMPO_PRIVATE_KEY", PRIVATE_KEY)

    with pytest.raises(SystemExit, match="Unknown Tempo session channel"):
        cli.close_session("0x" + "ff" * 32)

"""Hermes-specific Tempo session configuration and durable storage wiring."""

from __future__ import annotations

import threading
from collections.abc import Callable

from mpp.methods.tempo import TempoAccount
from mpp.methods.tempo._defaults import rpc_url_for_chain
from mpp.methods.tempo.session import (
    SessionPolicy,
    SessionRecord,
    SessionStatus,
    SQLiteSessionStore,
    TempoAccountCredentialProvider,
    TempoSessionManager,
    TempoSessionRpc,
)
from mpp.methods.tempo.session.protocol import SessionRpc

from .config import Config

RpcFactory = Callable[[int], SessionRpc]


class SessionHost:
    """Own the host policy, SQLite location, signer, and per-chain managers."""

    def __init__(
        self,
        config: Config,
        *,
        rpc_factory: RpcFactory | None = None,
    ) -> None:
        self.config = config
        self.account = TempoAccount.from_key(config.private_key)
        self.provider = TempoAccountCredentialProvider(self.account)
        self.store = SQLiteSessionStore(config.session_db)
        self.policy = SessionPolicy(
            max_deposit=config.session_max_deposit,
            max_top_up=config.session_max_top_up,
            max_cumulative_spend=config.session_max_spend,
        )
        self._rpc_factory = rpc_factory or (
            lambda chain_id: TempoSessionRpc(rpc_url_for_chain(chain_id))
        )
        self._lock = threading.RLock()
        self._managers: dict[int, TempoSessionManager] = {}
        self._closed = False

    def manager_for_chain(self, chain_id: int) -> TempoSessionManager:
        """Return one manager per chain so its per-channel locks stay shared."""

        with self._lock:
            if self._closed:
                raise RuntimeError("Tempo session host is closed")
            manager = self._managers.get(chain_id)
            if manager is None:
                manager = TempoSessionManager(
                    provider=self.provider,
                    store=self.store,
                    policy=self.policy,
                    rpc=self._rpc_factory(chain_id),
                    chain_id=chain_id,
                )
                self._managers[chain_id] = manager
            return manager

    async def hint(self, resource_url: str) -> str | None:
        """Return the durable active channel for an exact resource URL."""

        for record in await self.store.list():
            if (
                record.resource_url == resource_url
                and record.status != SessionStatus.CLOSED
                and record.close_requested_at == 0
            ):
                return record.channel_id
        return None

    async def list_sessions(self) -> list[SessionRecord]:
        return await self.store.list()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.store.close()

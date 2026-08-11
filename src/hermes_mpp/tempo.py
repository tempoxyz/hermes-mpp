from __future__ import annotations

from collections.abc import Callable

from mpp import Challenge, Credential
from mpp.errors import InvalidChallengeError
from mpp.methods.tempo import CHAIN_ID, ChargeIntent, TempoAccount, tempo
from mpp.methods.tempo.session import TempoSessionManager, is_tip1034_session_challenge

SessionManagerFactory = Callable[[int], TempoSessionManager]


class ChallengeTempo:
    """Tempo charge method that follows the challenge's chain."""

    name = "tempo"

    def __init__(
        self,
        account: TempoAccount,
        session_manager_factory: SessionManagerFactory | None = None,
    ) -> None:
        self.account = account
        self._session_manager_factory = session_manager_factory

    @staticmethod
    def _session_chain_id(challenge: Challenge) -> int:
        details = challenge.request.get("methodDetails", {})
        if not isinstance(details, dict):
            raise TypeError("methodDetails must be an object")
        value = details.get("chainId")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("session challenge requires a positive integer chainId")
        return value

    def session_manager_for(self, challenge: Challenge) -> TempoSessionManager:
        """Return the durable manager for the challenge's Tempo chain."""

        if self._session_manager_factory is None:
            raise ValueError("Tempo sessions are not configured")
        if not is_tip1034_session_challenge(challenge):
            raise ValueError("pympp supports TIP-1034 sessionProtocol v2")
        manager = self._session_manager_factory(self._session_chain_id(challenge))
        if not manager.can_handle_challenge(challenge):
            raise ValueError("Tempo session challenge is outside local network policy")
        return manager

    def can_handle_session_challenge(self, challenge: Challenge) -> bool:
        """Return whether Hermes can construct the challenge's pinned manager."""

        try:
            self.session_manager_for(challenge)
        except (TypeError, ValueError):
            return False
        return True

    async def create_credential(self, challenge: Challenge) -> Credential:
        try:
            if challenge.intent == "session":
                return await self.session_manager_for(challenge).prepare(
                    challenge,
                    resource_url="",
                )
            details = challenge.request.get("methodDetails", {})
            if not isinstance(details, dict):
                raise TypeError("methodDetails must be an object")
            value = details.get("chainId", CHAIN_ID)
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError
            chain_id = int(value)
            method = tempo(
                account=self.account,
                intents={"charge": ChargeIntent()},
                chain_id=chain_id,
                client_id="hermes-agent",
            )
            return await method.create_credential(challenge)
        except (KeyError, TypeError, ValueError) as error:
            reason = str(error) or "invalid or unsupported Tempo challenge"
            raise InvalidChallengeError(challenge.id, reason) from error

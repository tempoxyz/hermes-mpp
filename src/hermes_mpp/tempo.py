from __future__ import annotations

from mpp import Challenge, Credential
from mpp.errors import InvalidChallengeError
from mpp.methods.tempo import CHAIN_ID, ChargeIntent, TempoAccount, tempo


class ChallengeTempo:
    """Tempo charge method that follows the challenge's chain."""

    name = "tempo"

    def __init__(self, account: TempoAccount) -> None:
        self.account = account

    async def create_credential(self, challenge: Challenge) -> Credential:
        try:
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

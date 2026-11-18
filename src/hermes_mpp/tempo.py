from __future__ import annotations

from dataclasses import replace

from mpp import Challenge
from mpp.methods.tempo import CHAIN_ID, TESTNET_CHAIN_ID, TempoAccount
from mpp.methods.tempo.client import charge
from mpp.runtime import ClientMethod

SUPPORTED_CHAIN_IDS = (CHAIN_ID, TESTNET_CHAIN_ID)


def _chain_id(challenge: Challenge) -> int | None:
    details = challenge.request.get("methodDetails", {})
    if not isinstance(details, dict):
        return None
    value = details.get("chainId", CHAIN_ID)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def tempo_charge(account: TempoAccount) -> ClientMethod:
    """Tempo charge method that pays on the supported chain the challenge names."""
    method = charge(
        account=account,
        client_id="hermes-agent",
        allowed_chain_ids=SUPPORTED_CHAIN_IDS,
    )
    inner = method.can_handle_challenge

    def can_handle_challenge(challenge: Challenge) -> bool:
        if _chain_id(challenge) not in SUPPORTED_CHAIN_IDS:
            return False
        return inner is None or inner(challenge)

    return replace(method, can_handle_challenge=can_handle_challenge)

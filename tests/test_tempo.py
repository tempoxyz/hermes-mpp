from __future__ import annotations

import pytest
from mpp import Challenge
from mpp.methods.tempo import CHAIN_ID, PATH_USD, TESTNET_CHAIN_ID, TempoAccount
from mpp.runtime import ClientMethod

from hermes_mpp.tempo import tempo_charge

PRIVATE_KEY = "0x" + "11" * 32
RECIPIENT = "0x742d35Cc6634c0532925a3b844bC9e7595F8fE00"


def challenge(chain_id: object = None) -> Challenge:
    details = {} if chain_id is None else {"chainId": chain_id}
    return Challenge(
        id="challenge",
        method="tempo",
        intent="charge",
        request={
            "amount": "1",
            "currency": PATH_USD,
            "recipient": RECIPIENT,
            "methodDetails": details,
        },
    )


def method() -> ClientMethod:
    return tempo_charge(TempoAccount.from_key(PRIVATE_KEY))


def test_is_a_tempo_charge_method() -> None:
    assert method().key == "tempo/charge"


@pytest.mark.parametrize("chain_id", [TESTNET_CHAIN_ID, str(CHAIN_ID), None])
def test_accepts_supported_challenge_chain(chain_id: object) -> None:
    accepts = method().can_handle_challenge
    assert accepts is not None and accepts(challenge(chain_id))


@pytest.mark.parametrize("chain_id", [True, "invalid", 999, 1.5])
def test_skips_invalid_or_unsupported_chain(chain_id: object) -> None:
    accepts = method().can_handle_challenge
    assert accepts is not None and not accepts(challenge(chain_id))


def test_skips_malformed_method_details() -> None:
    offered = challenge()
    offered.request["methodDetails"] = []
    accepts = method().can_handle_challenge
    assert accepts is not None and not accepts(offered)

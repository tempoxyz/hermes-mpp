from __future__ import annotations

import pytest
from mpp import Challenge, Credential
from mpp.errors import InvalidChallengeError
from mpp.methods.tempo import CHAIN_ID, MACH, TempoAccount

import hermes_mpp.tempo as tempo_module
from hermes_mpp.tempo import ChallengeTempo

PRIVATE_KEY = "0x" + "11" * 32


def challenge(chain_id: object = None) -> Challenge:
    details = {} if chain_id is None else {"chainId": chain_id}
    return Challenge(
        id="challenge",
        method="tempo",
        intent="charge",
        request={"methodDetails": details},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "expected"),
    [(42431, 42431), ("4217", 4217), (None, CHAIN_ID)],
)
async def test_uses_challenge_chain(
    monkeypatch: pytest.MonkeyPatch,
    requested: object,
    expected: int,
) -> None:
    calls: list[int] = []
    credential = Credential(challenge="echo", payload={})

    class Method:
        async def create_credential(self, _: Challenge) -> Credential:
            return credential

    def tempo(**kwargs):
        calls.append(kwargs["chain_id"])
        return Method()

    monkeypatch.setattr(tempo_module, "tempo", tempo)
    method = ChallengeTempo(TempoAccount.from_key(PRIVATE_KEY))

    assert await method.create_credential(challenge(requested)) is credential
    assert calls == [expected]


@pytest.mark.asyncio
async def test_forwards_mach_charge_to_pympp(monkeypatch: pytest.MonkeyPatch) -> None:
    credential = Credential(challenge="echo", payload={})
    offered = Challenge(
        id="mach",
        method="tempo",
        intent="charge",
        request={
            "amount": "1",
            "currency": MACH,
            "recipient": "0x742d35Cc6634c0532925a3b844bC9e7595F8fE00",
            "methodDetails": {"chainId": CHAIN_ID},
        },
    )

    class Method:
        async def create_credential(self, challenge: Challenge) -> Credential:
            assert challenge.request["currency"] == MACH
            return credential

    monkeypatch.setattr(tempo_module, "tempo", lambda **_: Method())

    method = ChallengeTempo(TempoAccount.from_key(PRIVATE_KEY))
    assert await method.create_credential(offered) is credential


@pytest.mark.asyncio
@pytest.mark.parametrize("chain_id", [True, "invalid", 999])
async def test_rejects_invalid_or_unsupported_chain(
    monkeypatch: pytest.MonkeyPatch,
    chain_id: object,
) -> None:
    if chain_id == 999:
        monkeypatch.setattr(
            tempo_module,
            "tempo",
            lambda **_: (_ for _ in ()).throw(ValueError("unsupported")),
        )
    method = ChallengeTempo(TempoAccount.from_key(PRIVATE_KEY))

    with pytest.raises(InvalidChallengeError):
        await method.create_credential(challenge(chain_id))

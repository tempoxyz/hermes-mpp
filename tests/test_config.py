from __future__ import annotations

import pytest

from hermes_mpp.config import Config


def test_reads_minimal_environment() -> None:
    config = Config.from_env(
        {
            "TEMPO_PRIVATE_KEY": "  0xsecret  ",
            "MPP_ALLOWED_ORIGINS": "https://one.test, https://two.test ",
            "TEMPO_RPC_URL": " https://rpc.test ",
        }
    )

    assert config == Config(
        private_key="0xsecret",
        allowed_origins=("https://one.test", "https://two.test"),
        rpc_url="https://rpc.test",
    )


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "$TEMPO_PRIVATE_KEY is required"),
        (
            {"TEMPO_PRIVATE_KEY": "0xsecret"},
            "$MPP_ALLOWED_ORIGINS is required",
        ),
    ],
)
def test_requires_key_and_origins(
    env: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message.replace("$", r"\$")):
        Config.from_env(env)

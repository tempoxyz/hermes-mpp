from __future__ import annotations

import pytest

from hermes_mpp.config import Config


def test_reads_minimal_environment() -> None:
    config = Config.from_env(
        {
            "TEMPO_PRIVATE_KEY": "  0xsecret  ",
            "MPP_ALLOWED_ORIGINS": "https://one.test, https://two.test ",
        }
    )

    assert config == Config(
        private_key="0xsecret",
        allowed_origins=("https://one.test", "https://two.test"),
    )


def test_allows_every_origin_when_allowlist_is_unset_or_blank() -> None:
    assert Config.from_env({"TEMPO_PRIVATE_KEY": "0xsecret"}).allowed_origins is None
    assert (
        Config.from_env(
            {"TEMPO_PRIVATE_KEY": "0xsecret", "MPP_ALLOWED_ORIGINS": " , "}
        ).allowed_origins
        is None
    )


def test_requires_key() -> None:
    with pytest.raises(ValueError, match=r"\$TEMPO_PRIVATE_KEY is required"):
        Config.from_env({})

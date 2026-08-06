from __future__ import annotations

from pathlib import Path

import pytest

from hermes_mpp.config import Config


def test_reads_minimal_environment() -> None:
    config = Config.from_env(
        {
            "TEMPO_PRIVATE_KEY": "  0xsecret  ",
            "MPP_ALLOWED_ORIGINS": "https://one.test, https://two.test ",
            "HERMES_HOME": "/tmp/hermes-test",
        }
    )

    assert config == Config(
        private_key="0xsecret",
        allowed_origins=("https://one.test", "https://two.test"),
        session_db=Path("/tmp/hermes-test/mpp-sessions.sqlite3"),
        session_max_deposit=10_000_000,
        session_max_top_up=5_000_000,
        session_max_spend=10_000_000,
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


def test_reads_session_storage_and_policy() -> None:
    config = Config.from_env(
        {
            "TEMPO_PRIVATE_KEY": "0xsecret",
            "MPP_SESSION_DB": "/tmp/custom-sessions.sqlite3",
            "MPP_SESSION_MAX_DEPOSIT": "100",
            "MPP_SESSION_MAX_TOP_UP": "25",
            "MPP_SESSION_MAX_SPEND": "80",
        }
    )
    assert config.session_db == Path("/tmp/custom-sessions.sqlite3")
    assert config.session_max_deposit == 100
    assert config.session_max_top_up == 25
    assert config.session_max_spend == 80


@pytest.mark.parametrize(
    "name",
    ["MPP_SESSION_MAX_DEPOSIT", "MPP_SESSION_MAX_TOP_UP", "MPP_SESSION_MAX_SPEND"],
)
def test_rejects_invalid_session_policy(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        Config.from_env({"TEMPO_PRIVATE_KEY": "0xsecret", name: "0"})

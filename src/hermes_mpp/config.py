from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _positive_amount(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name, str(default)).strip()
    try:
        amount = int(raw)
    except ValueError as error:
        raise ValueError(f"${name} must be an integer number of base units") from error
    if amount <= 0:
        raise ValueError(f"${name} must be positive")
    return amount


@dataclass(frozen=True, slots=True)
class Config:
    private_key: str
    allowed_origins: tuple[str, ...] | None
    session_db: Path
    session_max_deposit: int
    session_max_top_up: int
    session_max_spend: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        values = os.environ if env is None else env
        private_key = values.get("TEMPO_PRIVATE_KEY", "").strip()
        if not private_key:
            raise ValueError("$TEMPO_PRIVATE_KEY is required")

        allowed_origins = tuple(
            value.strip()
            for value in values.get("MPP_ALLOWED_ORIGINS", "").split(",")
            if value.strip()
        )
        hermes_home = Path(
            values.get("HERMES_HOME", str(Path.home() / ".hermes"))
        ).expanduser()
        session_db = Path(
            values.get("MPP_SESSION_DB", str(hermes_home / "mpp-sessions.sqlite3"))
        ).expanduser()

        return cls(
            private_key=private_key,
            allowed_origins=allowed_origins or None,
            session_db=session_db,
            session_max_deposit=_positive_amount(
                values, "MPP_SESSION_MAX_DEPOSIT", 10_000_000
            ),
            session_max_top_up=_positive_amount(
                values, "MPP_SESSION_MAX_TOP_UP", 5_000_000
            ),
            session_max_spend=_positive_amount(
                values, "MPP_SESSION_MAX_SPEND", 10_000_000
            ),
        )

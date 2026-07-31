from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Config:
    private_key: str
    allowed_origins: tuple[str, ...]
    rpc_url: str | None = None

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
        if not allowed_origins:
            raise ValueError("$MPP_ALLOWED_ORIGINS is required")

        return cls(
            private_key=private_key,
            allowed_origins=allowed_origins,
            rpc_url=values.get("TEMPO_RPC_URL", "").strip() or None,
        )

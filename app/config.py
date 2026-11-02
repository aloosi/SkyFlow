"""Application settings, resolved from environment variables with sane defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping

from dotenv import load_dotenv

# Existing process environment wins, so Docker and CI overrides are kept.
load_dotenv()

# Share of total meals allocated to each meal type (must sum to 1.0).
DEFAULT_MEAL_MIX: Mapping[str, float] = MappingProxyType(
    {
        "Standard": 0.50,
        "Halal": 0.25,
        "Vegetarian": 0.12,
        "Vegan": 0.05,
        "Special Dietary": 0.08,
    }
)


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    app_name: str = "SkyFlow API"
    app_version: str = "1.0.0"
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///./skyflow.db"
        )
    )
    # Delays at or above this many minutes (cumulative) trigger the safety buffer.
    long_delay_threshold_minutes: int = field(
        default_factory=lambda: _env_int("LONG_DELAY_THRESHOLD_MINUTES", 60)
    )
    # Extra meals (as a fraction) prepared for long-delayed flights: 0.05 == 5%.
    safety_buffer_pct: float = field(
        default_factory=lambda: _env_float("SAFETY_BUFFER_PCT", 0.05)
    )
    meal_mix: Mapping[str, float] = DEFAULT_MEAL_MIX
    # Seconds to wait for in-flight background tasks during shutdown.
    shutdown_grace_seconds: float = field(
        default_factory=lambda: _env_float("SHUTDOWN_GRACE_SECONDS", 10.0)
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

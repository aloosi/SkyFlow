"""Shared fixtures: an isolated SQLite file per test and an ASGI client wired to it."""
from __future__ import annotations

from datetime import timedelta
from typing import Awaitable, Callable

import httpx
import pytest_asyncio

from app import database
from app.database import build_engine, build_session_factory, utcnow
from app.main import app
from app.models import (
    Flight,
    FlightStatus,
    MealInventory,
    MealStatus,
    PassengerManifest,
    AircraftType,
)
from app.services import background_tasks
from app.services.catering_engine import compute_meal_requirements


@pytest_asyncio.fixture
async def engine(tmp_path):
    eng = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.init_db(eng)
    yield eng
    await background_tasks.wait_for_pending(timeout=5)
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return build_session_factory(engine)


@pytest_asyncio.fixture
async def client(session_factory):
    async def _session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[database.get_session] = _session
    app.dependency_overrides[database.get_session_factory] = lambda: session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


FlightMaker = Callable[..., Awaitable[Flight]]


@pytest_asyncio.fixture
async def make_flight(session_factory) -> FlightMaker:
    """Create a flight + manifest + inventories (nothing assembled by default)."""

    async def _make(
        flight_id: str = "QR1001",
        *,
        economy: int = 100,
        business: int = 10,
        first: int = 0,
        crew: int = 10,
        minutes_ahead: int = 120,
        status: FlightStatus = FlightStatus.SCHEDULED,
        delay_minutes: int = 0,
        assembled_fraction: float = 0.0,
    ) -> Flight:
        async with session_factory() as session:
            async with session.begin():
                flight = Flight(
                    flight_id=flight_id,
                    airline="Qatar Airways",
                    aircraft_type=AircraftType.A350,
                    departure_time=utcnow() + timedelta(minutes=minutes_ahead),
                    status=status,
                    destination="London",
                    delay_minutes=delay_minutes,
                )
                flight.manifest = PassengerManifest(
                    flight_id=flight_id,
                    economy_count=economy,
                    business_count=business,
                    first_class_count=first,
                    crew_count=crew,
                )
                required = compute_meal_requirements(
                    economy + business + first + crew,
                    delay_minutes=delay_minutes,
                    cancelled=status is FlightStatus.CANCELLED,
                )
                for meal_type, qty in required.items():
                    flight.meal_inventories.append(
                        MealInventory(
                            flight_id=flight_id,
                            meal_type=meal_type,
                            quantity_required=qty,
                            quantity_assembled=int(qty * assembled_fraction),
                            status=MealStatus.PENDING,
                        )
                    )
                session.add(flight)
        return flight

    return _make

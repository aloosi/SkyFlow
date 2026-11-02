"""Seed the SQLite database with mock flights, manifests and meal inventories.

    python scripts/seed_db.py              # 300 flights, skipped if data exists
    python scripts/seed_db.py --force      # wipe and re-seed
    python scripts/seed_db.py --count 500 --seed 7
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
from faker import Faker  # noqa: E402
from sqlalchemy import delete, func, select  # noqa: E402

from app import database  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import (  # noqa: E402
    AircraftType,
    AuditLog,
    Flight,
    FlightStatus,
    MealInventory,
    MealStatus,
    PassengerManifest,
)
from app.services.catering_engine import compute_meal_requirements, derive_meal_status  # noqa: E402

DESTINATIONS = [
    "London", "Paris", "Frankfurt", "Madrid", "Rome", "Istanbul", "Dubai", "Riyadh",
    "Kuwait City", "Muscat", "Cairo", "Nairobi", "Johannesburg", "Mumbai", "Delhi",
    "Karachi", "Dhaka", "Colombo", "Bangkok", "Singapore", "Kuala Lumpur", "Manila",
    "Tokyo", "Seoul", "Sydney", "Melbourne", "New York", "Washington", "Chicago",
    "Los Angeles", "Toronto", "Sao Paulo", "Casablanca", "Amman", "Beirut", "Tehran",
]

# (economy, business, first_class, crew) inclusive ranges per aircraft type.
AIRCRAFT_PROFILE = {
    AircraftType.A320: {"eco": (110, 144), "biz": (8, 12), "first": (0, 0), "crew": (5, 6)},
    AircraftType.A350: {"eco": (190, 250), "biz": (30, 46), "first": (0, 0), "crew": (10, 13)},
    AircraftType.B777: {"eco": (240, 320), "biz": (35, 48), "first": (4, 8), "crew": (12, 16)},
}
AIRCRAFT_WEIGHTS = {AircraftType.A320: 0.40, AircraftType.A350: 0.35, AircraftType.B777: 0.25}

STATUS_WEIGHTS = {
    FlightStatus.SCHEDULED: 0.82,
    FlightStatus.DELAYED: 0.13,
    FlightStatus.CANCELLED: 0.03,
    FlightStatus.BOARDING: 0.02,
}


def build_flight_frames(
    count: int, rng: random.Random, fake: Faker, now: datetime
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate flights + manifests as DataFrames (departures spread over the next 24h)."""
    flights, manifests = [], []
    for i in range(count):
        aircraft = rng.choices(list(AIRCRAFT_WEIGHTS), weights=list(AIRCRAFT_WEIGHTS.values()))[0]
        profile = AIRCRAFT_PROFILE[aircraft]
        # Even spread across 24h plus jitter keeps ~300 departures/day realistic.
        scheduled = now + timedelta(minutes=int((i + 1) * 1440 / count) + rng.randint(-3, 3))
        status = rng.choices(list(STATUS_WEIGHTS), weights=list(STATUS_WEIGHTS.values()))[0]
        delay = 0
        if status is FlightStatus.DELAYED:
            delay = rng.choice([15, 30, 45, 60, 90, 120, 180, 240])
        if status is FlightStatus.BOARDING:
            scheduled = now + timedelta(minutes=rng.randint(10, 40))

        flight_id = f"QR{1001 + i}"
        flights.append(
            {
                "flight_id": flight_id,
                "airline": "Qatar Airways",
                "aircraft_type": aircraft,
                "departure_time": scheduled + timedelta(minutes=delay),
                "status": status,
                "destination": rng.choice(DESTINATIONS),
                "delay_minutes": delay,
            }
        )
        manifests.append(
            {
                "flight_id": flight_id,
                "economy_count": rng.randint(*profile["eco"]),
                "business_count": rng.randint(*profile["biz"]),
                "first_class_count": rng.randint(*profile["first"]),
                "crew_count": rng.randint(*profile["crew"]),
                "last_updated": fake.date_time_between(start_date="-6h", end_date="now"),
            }
        )
    return pd.DataFrame(flights), pd.DataFrame(manifests)


def _assembly_fraction(status: FlightStatus, minutes_to_departure: float, rng: random.Random) -> float:
    """Closer departures are further along in assembly."""
    if status is FlightStatus.BOARDING:
        return 1.0
    if minutes_to_departure < 120:
        return rng.uniform(0.6, 1.0)
    if minutes_to_departure < 360:
        return rng.uniform(0.2, 0.7)
    return rng.uniform(0.0, 0.2)


def _py(value):
    """pandas Timestamp -> plain datetime for SQLAlchemy."""
    return value.to_pydatetime() if isinstance(value, pd.Timestamp) else value


async def seed(count: int, seed_value: int, force: bool) -> int:
    await database.init_db()
    rng = random.Random(seed_value)
    fake = Faker()
    Faker.seed(seed_value)
    settings = get_settings()
    now = database.utcnow()

    async with database.SessionLocal() as session:
        # Single transaction for check + wipe + insert: all-or-nothing seeding.
        async with session.begin():
            existing = await session.scalar(select(func.count()).select_from(Flight))
            if existing and not force:
                print(f"Database already holds {existing} flights; use --force to re-seed.")
                return 0

            flights_df, manifests_df = build_flight_frames(count, rng, fake, now)
            merged = flights_df.merge(manifests_df.drop(columns=["last_updated"]), on="flight_id")
            manifest_rows = {r["flight_id"]: r for r in manifests_df.to_dict("records")}

            if existing:
                # Children first; SQLite enforces the foreign keys.
                for model in (AuditLog, MealInventory, PassengerManifest, Flight):
                    await session.execute(delete(model))

            for row in merged.to_dict("records"):
                m = manifest_rows[row["flight_id"]]
                flight = Flight(
                    flight_id=row["flight_id"],
                    airline=row["airline"],
                    aircraft_type=row["aircraft_type"],
                    departure_time=_py(row["departure_time"]),
                    status=row["status"],
                    destination=row["destination"],
                    delay_minutes=int(row["delay_minutes"]),
                )
                manifest = PassengerManifest(
                    flight_id=flight.flight_id,
                    economy_count=int(m["economy_count"]),
                    business_count=int(m["business_count"]),
                    first_class_count=int(m["first_class_count"]),
                    crew_count=int(m["crew_count"]),
                    last_updated=_py(m["last_updated"]),
                )
                flight.manifest = manifest

                required = compute_meal_requirements(
                    manifest.total_passengers,
                    delay_minutes=flight.delay_minutes,
                    cancelled=flight.status is FlightStatus.CANCELLED,
                    settings=settings,
                )
                minutes = (flight.departure_time - now).total_seconds() / 60
                fraction = _assembly_fraction(flight.status, minutes, rng)
                for meal_type, quantity in required.items():
                    assembled = int(quantity * fraction)
                    flight.meal_inventories.append(
                        MealInventory(
                            flight_id=flight.flight_id,
                            meal_type=meal_type,
                            quantity_required=quantity,
                            quantity_assembled=assembled,
                            status=derive_meal_status(quantity, assembled, MealStatus.PENDING),
                        )
                    )
                flight.audit_logs.append(
                    AuditLog(
                        flight_id=flight.flight_id,
                        event_type="SEED",
                        description=(
                            f"Initial manifest loaded: {manifest.total_passengers} on board, "
                            f"{sum(required.values())} meals required"
                        ),
                    )
                )
                session.add(flight)

    print(f"Seeded {count} flights (with manifests, meal inventories and audit entries).")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--count", type=int, default=300, help="number of flights (default 300)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducible data")
    parser.add_argument("--force", action="store_true", help="wipe existing data and re-seed")
    args = parser.parse_args()
    asyncio.run(seed(args.count, args.seed, args.force))


if __name__ == "__main__":
    main()

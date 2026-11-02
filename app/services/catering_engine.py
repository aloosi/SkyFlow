"""Meal demand recalculation logic.

Two layers:

* Pure functions (`compute_meal_requirements`, `derive_meal_status`) with no I/O,
  trivially unit-testable.
* `apply_event`, which mutates ORM state for one disruption event *inside a
  caller-managed transaction*. It never commits; the caller decides whether the
  whole change (flight + manifest + inventories + audit log) lands or rolls back.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import (
    AuditLog,
    Flight,
    FlightStatus,
    MealInventory,
    MealStatus,
    MealType,
    PassengerManifest,
)
from app.schemas.flight_event import DisruptionEvent, DisruptionEventType


class FlightNotFoundError(LookupError):
    """Raised when an event references a flight that does not exist."""


@dataclass(frozen=True)
class RecalcResult:
    flight_id: str
    event_type: str
    previous_total: int
    new_total: int
    buffer_applied: bool
    ignored: bool = False


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #
def buffer_applies(delay_minutes: int, settings: Optional[Settings] = None) -> bool:
    settings = settings or get_settings()
    return delay_minutes >= settings.long_delay_threshold_minutes


def compute_meal_requirements(
    total_passengers: int,
    delay_minutes: int = 0,
    cancelled: bool = False,
    settings: Optional[Settings] = None,
) -> dict[MealType, int]:
    """Split the meal demand for a flight across meal types.

    * Demand = everyone on board (passengers + crew), one meal each.
    * Long delays (cumulative >= threshold) add a safety buffer (default 5%),
      rounded up, because food may need to be re-served or replaced.
    * Cancelled flights need no meals.
    * The per-type split uses the largest-remainder method so the parts always
      sum exactly to the total.
    """
    settings = settings or get_settings()
    mix: Mapping[str, float] = settings.meal_mix
    meal_types = [MealType(name) for name in mix]

    if cancelled or total_passengers <= 0:
        return {meal: 0 for meal in meal_types}

    factor = 1 + (settings.safety_buffer_pct if buffer_applies(delay_minutes, settings) else 0)
    # round() first so float noise (100 * 1.05 == 105.00000000000001) can't add a meal.
    total = math.ceil(round(total_passengers * factor, 6))

    raw = {MealType(name): total * share for name, share in mix.items()}
    allocation = {meal: math.floor(value) for meal, value in raw.items()}
    leftover = total - sum(allocation.values())
    by_remainder = sorted(raw, key=lambda meal: raw[meal] - allocation[meal], reverse=True)
    for meal in by_remainder[:leftover]:
        allocation[meal] += 1
    return allocation


def derive_meal_status(
    required: int, assembled: int, current: MealStatus = MealStatus.PENDING
) -> MealStatus:
    """Dispatched is terminal; otherwise status follows assembly progress."""
    if current is MealStatus.DISPATCHED:
        return MealStatus.DISPATCHED
    if required > 0 and assembled >= required:
        return MealStatus.READY
    if assembled > 0:
        return MealStatus.IN_PROGRESS
    return MealStatus.PENDING


# --------------------------------------------------------------------------- #
# Stateful logic (runs inside the caller's transaction)
# --------------------------------------------------------------------------- #
def _describe_event(event: DisruptionEvent, flight: Flight) -> str:
    kind = event.event_type
    if kind is DisruptionEventType.FLIGHT_DELAY:
        return (
            f"Flight delayed by {event.delay_minutes} min "
            f"(cumulative {flight.delay_minutes} min)"
        )
    if kind is DisruptionEventType.PASSENGER_COUNT_CHANGE:
        d = event.passenger_delta
        return (
            f"Passenger change: economy {d.economy:+d}, business {d.business:+d}, "
            f"first {d.first_class:+d}, crew {d.crew:+d}"
        )
    if kind is DisruptionEventType.FLIGHT_CANCELLATION:
        return "Flight cancelled"
    return f"Status set to {event.new_status.value}"


def _apply_to_flight(flight: Flight, manifest: PassengerManifest, event: DisruptionEvent) -> None:
    kind = event.event_type
    if kind is DisruptionEventType.FLIGHT_DELAY:
        flight.departure_time = flight.departure_time + timedelta(minutes=event.delay_minutes)
        flight.delay_minutes += event.delay_minutes
        # A flight already boarding keeps that status; otherwise it is now delayed.
        if flight.status is not FlightStatus.BOARDING:
            flight.status = FlightStatus.DELAYED
    elif kind is DisruptionEventType.PASSENGER_COUNT_CHANGE:
        d = event.passenger_delta
        manifest.economy_count = max(manifest.economy_count + d.economy, 0)
        manifest.business_count = max(manifest.business_count + d.business, 0)
        manifest.first_class_count = max(manifest.first_class_count + d.first_class, 0)
        manifest.crew_count = max(manifest.crew_count + d.crew, 0)
        manifest.last_updated = utcnow()
    elif kind is DisruptionEventType.FLIGHT_CANCELLATION:
        flight.status = FlightStatus.CANCELLED
    elif kind is DisruptionEventType.STATUS_CHANGE:
        flight.status = event.new_status


def _recalculate_inventories(
    flight: Flight, manifest: PassengerManifest, settings: Settings
) -> int:
    """Upsert one inventory row per meal type; return the new total required."""
    required = compute_meal_requirements(
        manifest.total_passengers,
        delay_minutes=flight.delay_minutes,
        cancelled=flight.status is FlightStatus.CANCELLED,
        settings=settings,
    )
    existing = {inv.meal_type: inv for inv in flight.meal_inventories}
    for meal_type, quantity in required.items():
        inventory = existing.get(meal_type)
        if inventory is None:
            inventory = MealInventory(
                flight_id=flight.flight_id,
                meal_type=meal_type,
                quantity_required=0,
                quantity_assembled=0,
                status=MealStatus.PENDING,
            )
            flight.meal_inventories.append(inventory)
        inventory.quantity_required = quantity
        inventory.status = derive_meal_status(
            quantity, inventory.quantity_assembled, inventory.status
        )
    return sum(required.values())


async def apply_event(
    session: AsyncSession,
    event: DisruptionEvent,
    settings: Optional[Settings] = None,
) -> RecalcResult:
    """Apply one event and recalculate meals. Does NOT commit."""
    settings = settings or get_settings()
    flight = await session.get(
        Flight,
        event.flight_id,
        options=[selectinload(Flight.manifest), selectinload(Flight.meal_inventories)],
    )
    if flight is None:
        raise FlightNotFoundError(event.flight_id)

    previous_total = sum(inv.quantity_required for inv in flight.meal_inventories)

    # Operational noise on a cancelled flight must not resurrect its meal orders.
    if (
        flight.status is FlightStatus.CANCELLED
        and event.event_type is not DisruptionEventType.STATUS_CHANGE
    ):
        session.add(
            AuditLog(
                flight_id=flight.flight_id,
                event_type="EVENT_IGNORED",
                description=(
                    f"{event.event_type.value} ignored: flight is cancelled "
                    f"(meals required stay at {previous_total})"
                ),
            )
        )
        return RecalcResult(
            flight.flight_id, event.event_type.value, previous_total, previous_total, False, True
        )

    manifest = flight.manifest
    if manifest is None:
        manifest = PassengerManifest(
            flight_id=flight.flight_id,
            economy_count=0,
            business_count=0,
            first_class_count=0,
            crew_count=0,
        )
        flight.manifest = manifest

    _apply_to_flight(flight, manifest, event)
    new_total = _recalculate_inventories(flight, manifest, settings)
    buffered = buffer_applies(flight.delay_minutes, settings) and new_total > 0

    description = (
        f"{_describe_event(event, flight)}. Meals required {previous_total} -> {new_total} "
        f"({new_total - previous_total:+d})"
        + (f"; {settings.safety_buffer_pct:.0%} safety buffer applied" if buffered else "")
        + (f". Reason: {event.reason}" if event.reason else "")
    )
    session.add(
        AuditLog(
            flight_id=flight.flight_id,
            event_type=event.event_type.value,
            description=description,
        )
    )
    # Flush so constraint violations surface here, inside the caller's try/except.
    await session.flush()
    return RecalcResult(
        flight.flight_id, event.event_type.value, previous_total, new_total, buffered
    )

"""Unit tests for meal demand logic, event application and transactional integrity."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, Flight, FlightStatus, MealInventory, MealStatus, MealType, PassengerManifest
from app.schemas import DisruptionEvent, DisruptionEventType, PassengerDelta
from app.services import background_tasks, catering_engine
from app.services.catering_engine import (
    FlightNotFoundError,
    compute_meal_requirements,
    derive_meal_status,
)


# ----------------------------- pure functions ------------------------------ #
@pytest.mark.parametrize("pax", [1, 7, 99, 100, 250, 333, 401])
def test_split_always_sums_to_total(pax):
    assert sum(compute_meal_requirements(pax).values()) == pax


def test_all_meal_types_present():
    assert set(compute_meal_requirements(100)) == set(MealType)


def test_long_delay_adds_five_percent_buffer_rounded_up():
    assert sum(compute_meal_requirements(100, delay_minutes=60).values()) == 105
    assert sum(compute_meal_requirements(101, delay_minutes=120).values()) == 107  # ceil(106.05)


def test_short_delay_gets_no_buffer():
    assert sum(compute_meal_requirements(100, delay_minutes=59).values()) == 100


@pytest.mark.parametrize("pax", [0, -5])
def test_no_passengers_no_meals(pax):
    assert sum(compute_meal_requirements(pax).values()) == 0


def test_cancelled_flight_needs_no_meals():
    assert sum(compute_meal_requirements(200, delay_minutes=300, cancelled=True).values()) == 0


@pytest.mark.parametrize(
    "required,assembled,current,expected",
    [
        (10, 0, MealStatus.PENDING, MealStatus.PENDING),
        (10, 4, MealStatus.PENDING, MealStatus.IN_PROGRESS),
        (10, 10, MealStatus.IN_PROGRESS, MealStatus.READY),
        (10, 12, MealStatus.IN_PROGRESS, MealStatus.READY),
        (12, 10, MealStatus.READY, MealStatus.IN_PROGRESS),  # demand rose: no longer ready
        (10, 10, MealStatus.DISPATCHED, MealStatus.DISPATCHED),  # terminal
    ],
)
def test_derive_meal_status(required, assembled, current, expected):
    assert derive_meal_status(required, assembled, current) is expected


# ------------------------------ apply_event -------------------------------- #
async def _totals(session_factory, flight_id):
    async with session_factory() as s:
        return await s.scalar(
            select(func.sum(MealInventory.quantity_required)).where(MealInventory.flight_id == flight_id)
        )


def _delay(minutes, flight_id="QR1001"):
    return DisruptionEvent(flight_id=flight_id, event_type=DisruptionEventType.FLIGHT_DELAY, delay_minutes=minutes)


async def test_delay_shifts_departure_and_marks_delayed(session_factory, make_flight):
    original = await make_flight(minutes_ahead=120)
    await background_tasks.execute_event(session_factory, _delay(30))
    async with session_factory() as s:
        flight = await s.get(Flight, "QR1001")
    assert flight.status is FlightStatus.DELAYED
    assert flight.delay_minutes == 30
    assert int((flight.departure_time - original.departure_time).total_seconds()) == 1800


async def test_buffer_kicks_in_once_cumulative_delay_reaches_threshold(session_factory, make_flight):
    await make_flight(economy=90, business=0, crew=10)  # 100 meals
    await background_tasks.execute_event(session_factory, _delay(30))
    assert await _totals(session_factory, "QR1001") == 100
    result = await background_tasks.execute_event(session_factory, _delay(45))  # cumulative 75
    assert result.buffer_applied is True
    assert await _totals(session_factory, "QR1001") == 105


async def test_passenger_surge_raises_requirements_and_preserves_assembly(session_factory, make_flight):
    await make_flight(economy=100, business=0, crew=0, assembled_fraction=1.0)  # all 100 assembled
    event = DisruptionEvent(
        flight_id="QR1001",
        event_type=DisruptionEventType.PASSENGER_COUNT_CHANGE,
        passenger_delta=PassengerDelta(economy=20),
    )
    result = await background_tasks.execute_event(session_factory, event)
    assert (result.previous_total, result.new_total) == (100, 120)
    async with session_factory() as s:
        inventories = (await s.scalars(select(MealInventory).where(MealInventory.flight_id == "QR1001"))).all()
        manifest = await s.get(PassengerManifest, "QR1001")
    assert manifest.economy_count == 120
    assert sum(i.quantity_assembled for i in inventories) == 100  # kitchen work is never erased
    assert all(i.status is MealStatus.IN_PROGRESS for i in inventories if i.quantity_required > i.quantity_assembled)


async def test_negative_delta_is_clamped_at_zero(session_factory, make_flight):
    await make_flight(economy=5, business=0, crew=0)
    event = DisruptionEvent(
        flight_id="QR1001",
        event_type=DisruptionEventType.PASSENGER_COUNT_CHANGE,
        passenger_delta=PassengerDelta(economy=-50),
    )
    await background_tasks.execute_event(session_factory, event)
    async with session_factory() as s:
        assert (await s.get(PassengerManifest, "QR1001")).economy_count == 0
    assert await _totals(session_factory, "QR1001") == 0


async def test_cancellation_zeroes_meals(session_factory, make_flight):
    await make_flight()
    event = DisruptionEvent(flight_id="QR1001", event_type=DisruptionEventType.FLIGHT_CANCELLATION)
    await background_tasks.execute_event(session_factory, event)
    async with session_factory() as s:
        assert (await s.get(Flight, "QR1001")).status is FlightStatus.CANCELLED
    assert await _totals(session_factory, "QR1001") == 0


async def test_events_on_cancelled_flight_are_ignored_but_audited(session_factory, make_flight):
    await make_flight(status=FlightStatus.CANCELLED)
    result = await background_tasks.execute_event(session_factory, _delay(90))
    assert result.ignored is True
    async with session_factory() as s:
        flight = await s.get(Flight, "QR1001")
        log = await s.scalar(select(AuditLog).where(AuditLog.event_type == "EVENT_IGNORED"))
    assert flight.delay_minutes == 0 and flight.status is FlightStatus.CANCELLED
    assert log is not None


async def test_each_event_writes_one_structured_audit_record(session_factory, make_flight):
    await make_flight()
    await background_tasks.execute_event(session_factory, _delay(90))
    async with session_factory() as s:
        logs = (await s.scalars(select(AuditLog).where(AuditLog.flight_id == "QR1001"))).all()
    assert len(logs) == 1
    assert logs[0].event_type == "FLIGHT_DELAY"
    assert "5% safety buffer applied" in logs[0].description
    assert "->" in logs[0].description


async def test_unknown_flight_raises(session_factory):
    with pytest.raises(FlightNotFoundError):
        await background_tasks.execute_event(session_factory, _delay(30, flight_id="QR9999"))


# ------------------------- transactional integrity ------------------------- #
async def test_failure_rolls_back_every_change(session_factory, make_flight, monkeypatch):
    """If anything fails after mutation, flight, manifest and inventories must all revert."""
    await make_flight(economy=90, business=0, crew=10)

    real_recalc = catering_engine._recalculate_inventories

    def exploding_recalc(*args, **kwargs):
        real_recalc(*args, **kwargs)  # inventories ARE mutated in the session...
        raise RuntimeError("boom")  # ...then the transaction fails

    monkeypatch.setattr(catering_engine, "_recalculate_inventories", exploding_recalc)

    event = DisruptionEvent(
        flight_id="QR1001",
        event_type=DisruptionEventType.PASSENGER_COUNT_CHANGE,
        passenger_delta=PassengerDelta(economy=50),
    )
    with pytest.raises(RuntimeError):
        await background_tasks.execute_event(session_factory, event)

    async with session_factory() as s:
        assert (await s.get(PassengerManifest, "QR1001")).economy_count == 90
        assert (await s.get(Flight, "QR1001")).delay_minutes == 0
    assert await _totals(session_factory, "QR1001") == 100
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_process_event_swallows_error_and_records_failure(session_factory, make_flight, monkeypatch):
    await make_flight()

    async def broken(*_a, **_k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(catering_engine, "apply_event", broken)
    assert await background_tasks.process_event(session_factory, _delay(30)) is None
    async with session_factory() as s:
        log = await s.scalar(select(AuditLog).where(AuditLog.event_type == "RECALC_FAILED"))
    assert log is not None and "db exploded" in log.description


async def test_concurrent_events_on_one_flight_do_not_lose_updates(session_factory, make_flight):
    import asyncio

    await make_flight(economy=100, business=0, crew=0)
    events = [
        DisruptionEvent(
            flight_id="QR1001",
            event_type=DisruptionEventType.PASSENGER_COUNT_CHANGE,
            passenger_delta=PassengerDelta(economy=1),
        )
        for _ in range(10)
    ]
    await asyncio.gather(*(background_tasks.execute_event(session_factory, e) for e in events))
    async with session_factory() as s:
        assert (await s.get(PassengerManifest, "QR1001")).economy_count == 110
    assert await _totals(session_factory, "QR1001") == 110

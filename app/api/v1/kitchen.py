"""Kitchen priority queue and inventory/audit logs."""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_session, utcnow
from app.models import AuditLog, Flight, FlightStatus, MealStatus
from app.schemas import (
    AuditLogOut,
    AuditLogPage,
    KitchenQueueItem,
    KitchenQueueResponse,
    MealInventoryOut,
    QueuePriority,
)

router = APIRouter(prefix="/kitchen", tags=["kitchen"])

CRITICAL_WINDOW_MIN = 60
HIGH_WINDOW_MIN = 180


def _priority(minutes_to_departure: int) -> QueuePriority:
    if minutes_to_departure <= CRITICAL_WINDOW_MIN:
        return QueuePriority.CRITICAL
    if minutes_to_departure <= HIGH_WINDOW_MIN:
        return QueuePriority.HIGH
    return QueuePriority.NORMAL


@router.get(
    "/queue",
    response_model=KitchenQueueResponse,
    summary="Live prioritised kitchen prep queue",
    description=(
        "Upcoming, non-cancelled flights that still have meals to assemble, "
        "ordered by closest departure time (ties: most meals outstanding first). "
        "Reflects the latest delays and manifest changes."
    ),
)
async def kitchen_queue(
    hours_ahead: int = Query(24, ge=1, le=72, description="Look-ahead window"),
    limit: int = Query(50, ge=1, le=300),
    session: AsyncSession = Depends(get_session),
) -> KitchenQueueResponse:
    now = utcnow()
    flights = await session.scalars(
        select(Flight)
        .where(
            Flight.status != FlightStatus.CANCELLED,
            Flight.departure_time >= now,
            Flight.departure_time <= now + timedelta(hours=hours_ahead),
        )
        .options(selectinload(Flight.meal_inventories))
        .order_by(Flight.departure_time, Flight.flight_id)
        .execution_options(populate_existing=True)
    )

    entries = []
    for flight in flights:
        meals = flight.meal_inventories
        required = sum(m.quantity_required for m in meals)
        assembled = sum(m.quantity_assembled for m in meals)
        remaining = sum(max(m.quantity_required - m.quantity_assembled, 0) for m in meals)
        if required == 0 or remaining == 0 or all(
            m.status is MealStatus.DISPATCHED for m in meals
        ):
            continue  # nothing left for the kitchen to do
        minutes = int((flight.departure_time - now).total_seconds() // 60)
        entries.append((flight, meals, required, assembled, remaining, minutes))

    # Closest departure first; for equal departures, biggest outstanding workload first.
    entries.sort(key=lambda e: (e[0].departure_time, -e[4], e[0].flight_id))

    items = [
        KitchenQueueItem(
            rank=rank,
            flight_id=flight.flight_id,
            destination=flight.destination,
            aircraft_type=flight.aircraft_type,
            flight_status=flight.status,
            departure_time=flight.departure_time,
            minutes_to_departure=minutes,
            priority=_priority(minutes),
            total_required=required,
            total_assembled=assembled,
            total_remaining=remaining,
            meals=[MealInventoryOut.model_validate(m) for m in meals],
        )
        for rank, (flight, meals, required, assembled, remaining, minutes) in enumerate(
            entries[:limit], start=1
        )
    ]
    return KitchenQueueResponse(generated_at=now, count=len(items), items=items)


@router.get(
    "/logs",
    response_model=AuditLogPage,
    summary="Inventory & event audit log (newest first)",
)
async def inventory_logs(
    flight_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> AuditLogPage:
    filters = []
    if flight_id:
        filters.append(AuditLog.flight_id == flight_id)
    if event_type:
        filters.append(AuditLog.event_type == event_type)
    total = await session.scalar(select(func.count()).select_from(AuditLog).where(*filters))
    rows = await session.scalars(
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return AuditLogPage(
        total=total or 0,
        limit=limit,
        offset=offset,
        items=[AuditLogOut.model_validate(r) for r in rows],
    )

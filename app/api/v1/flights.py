"""Flight queries and status updates."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.database import get_session, get_session_factory
from app.models import Flight, FlightStatus
from app.schemas import (
    DisruptionEvent,
    DisruptionEventType,
    FlightDetail,
    FlightList,
    FlightStatusUpdate,
)
from app.services import background_tasks
from app.services.catering_engine import FlightNotFoundError

router = APIRouter(prefix="/flights", tags=["flights"])


async def _load_detail(session: AsyncSession, flight_id: str) -> Optional[FlightDetail]:
    flight = await session.scalar(
        select(Flight)
        .where(Flight.flight_id == flight_id)
        .options(selectinload(Flight.manifest), selectinload(Flight.meal_inventories))
        .execution_options(populate_existing=True)
    )
    if flight is None:
        return None
    detail = FlightDetail.model_validate(flight)
    detail.total_meals_required = sum(m.quantity_required for m in flight.meal_inventories)
    detail.total_meals_assembled = sum(m.quantity_assembled for m in flight.meal_inventories)
    return detail


@router.get("/", response_model=FlightList, summary="List flights")
async def list_flights(
    status: Optional[FlightStatus] = Query(None, description="Filter by flight status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> FlightList:
    filters = [Flight.status == status] if status else []
    total = await session.scalar(select(func.count()).select_from(Flight).where(*filters))
    rows = await session.scalars(
        select(Flight)
        .where(*filters)
        .order_by(Flight.departure_time, Flight.flight_id)
        .limit(limit)
        .offset(offset)
    )
    return FlightList(total=total or 0, limit=limit, offset=offset, items=list(rows))


@router.get("/{flight_id}", response_model=FlightDetail, summary="Flight detail")
async def get_flight(
    flight_id: str, session: AsyncSession = Depends(get_session)
) -> FlightDetail:
    detail = await _load_detail(session, flight_id)
    if detail is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"Flight {flight_id} not found")
    return detail


@router.patch(
    "/{flight_id}/status",
    response_model=FlightDetail,
    summary="Update a flight's status",
    description=(
        "Synchronous: the status change and the resulting meal recalculation "
        "commit in one transaction (e.g. Cancelled zeroes the meal requirement)."
    ),
)
async def update_flight_status(
    flight_id: str,
    body: FlightStatusUpdate,
    session: AsyncSession = Depends(get_session),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> FlightDetail:
    try:
        event = DisruptionEvent(
            flight_id=flight_id,
            event_type=DisruptionEventType.STATUS_CHANGE,
            new_status=body.status,
            reason=body.reason,
        )
    except ValidationError:  # malformed ID can't match any flight
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"Flight {flight_id} not found")
    try:
        await background_tasks.execute_event(session_factory, event)
    except FlightNotFoundError:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"Flight {flight_id} not found")
    detail = await _load_detail(session, flight_id)
    assert detail is not None
    return detail

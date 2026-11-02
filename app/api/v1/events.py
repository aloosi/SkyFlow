"""Real-time disruption event ingestion."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_session, get_session_factory
from app.models import Flight
from app.schemas import DisruptionAccepted, DisruptionEvent
from app.services import background_tasks

router = APIRouter(prefix="/events", tags=["events"])


@router.post(
    "/disruption",
    response_model=DisruptionAccepted,
    status_code=http_status.HTTP_202_ACCEPTED,
    summary="Ingest a flight disruption event",
    description=(
        "Validates the payload, then returns `202 Accepted` immediately. Meal "
        "recalculation, inventory updates and audit logging run in a background "
        "task, so this call never waits on the kitchen engine."
    ),
)
async def ingest_disruption(
    event: DisruptionEvent,
    session: AsyncSession = Depends(get_session),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> DisruptionAccepted:
    # Cheap existence check so producers get fast feedback on bad flight IDs.
    exists = await session.scalar(
        select(Flight.flight_id).where(Flight.flight_id == event.flight_id)
    )
    if exists is None:
        raise HTTPException(
            http_status.HTTP_404_NOT_FOUND, f"Flight {event.flight_id} not found"
        )

    background_tasks.schedule_event(session_factory, event)
    return DisruptionAccepted(event_id=str(uuid.uuid4()), flight_id=event.flight_id)

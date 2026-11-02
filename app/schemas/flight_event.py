"""Schemas for flights and for real-time disruption events."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import AircraftType, FlightStatus
from app.schemas.inventory import MealInventoryOut, PassengerManifestOut


class DisruptionEventType(str, Enum):
    FLIGHT_DELAY = "FLIGHT_DELAY"
    PASSENGER_COUNT_CHANGE = "PASSENGER_COUNT_CHANGE"
    FLIGHT_CANCELLATION = "FLIGHT_CANCELLATION"
    STATUS_CHANGE = "STATUS_CHANGE"


class PassengerDelta(BaseModel):
    """Signed change to each manifest bucket (counts are clamped at zero)."""

    economy: int = Field(0, ge=-1000, le=1000)
    business: int = Field(0, ge=-1000, le=1000)
    first_class: int = Field(0, ge=-1000, le=1000)
    crew: int = Field(0, ge=-1000, le=1000)

    def is_empty(self) -> bool:
        return not any((self.economy, self.business, self.first_class, self.crew))


class DisruptionEvent(BaseModel):
    flight_id: str = Field(pattern=r"^[A-Z0-9]{2,3}\d{1,4}$", examples=["QR1001"])
    event_type: DisruptionEventType
    delay_minutes: Optional[int] = Field(
        None, gt=0, le=1440, description="Required for FLIGHT_DELAY"
    )
    passenger_delta: Optional[PassengerDelta] = Field(
        None, description="Required (and non-zero) for PASSENGER_COUNT_CHANGE"
    )
    new_status: Optional[FlightStatus] = Field(
        None, description="Required for STATUS_CHANGE"
    )
    reason: Optional[str] = Field(None, max_length=200)

    @model_validator(mode="after")
    def _check_payload_matches_type(self) -> "DisruptionEvent":
        kind = self.event_type
        if kind is DisruptionEventType.FLIGHT_DELAY and self.delay_minutes is None:
            raise ValueError("delay_minutes is required for FLIGHT_DELAY events")
        if kind is DisruptionEventType.PASSENGER_COUNT_CHANGE and (
            self.passenger_delta is None or self.passenger_delta.is_empty()
        ):
            raise ValueError(
                "passenger_delta with at least one non-zero value is required "
                "for PASSENGER_COUNT_CHANGE events"
            )
        if kind is DisruptionEventType.STATUS_CHANGE and self.new_status is None:
            raise ValueError("new_status is required for STATUS_CHANGE events")
        return self


class DisruptionAccepted(BaseModel):
    event_id: str
    flight_id: str
    status: str = "accepted"
    detail: str = "Event queued; meal requirements are being recalculated."


class FlightStatusUpdate(BaseModel):
    status: FlightStatus
    reason: Optional[str] = Field(None, max_length=200)


class FlightSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    flight_id: str
    airline: str
    aircraft_type: AircraftType
    departure_time: datetime
    status: FlightStatus
    destination: str
    delay_minutes: int


class FlightList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[FlightSummary]


class FlightDetail(FlightSummary):
    manifest: Optional[PassengerManifestOut] = None
    meal_inventories: list[MealInventoryOut] = []
    total_meals_required: int = 0
    total_meals_assembled: int = 0

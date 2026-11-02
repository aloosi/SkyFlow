"""Schemas for meal inventory, manifests, audit logs and the kitchen queue."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models import AircraftType, FlightStatus, MealStatus, MealType


class MealInventoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    meal_type: MealType
    quantity_required: int
    quantity_assembled: int
    status: MealStatus

    @computed_field  # type: ignore[prop-decorator]
    @property
    def quantity_remaining(self) -> int:
        return max(self.quantity_required - self.quantity_assembled, 0)


class PassengerManifestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    economy_count: int
    business_count: int
    first_class_count: int
    crew_count: int
    last_updated: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_passengers(self) -> int:
        return (
            self.economy_count
            + self.business_count
            + self.first_class_count
            + self.crew_count
        )


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    flight_id: str
    event_type: str
    description: str
    timestamp: datetime


class AuditLogPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AuditLogOut]


class QueuePriority(str, Enum):
    CRITICAL = "CRITICAL"  # departs within 60 minutes with work outstanding
    HIGH = "HIGH"  # departs within 3 hours
    NORMAL = "NORMAL"


class KitchenQueueItem(BaseModel):
    rank: int = Field(description="1 = prepare first")
    flight_id: str
    destination: str
    aircraft_type: AircraftType
    flight_status: FlightStatus
    departure_time: datetime
    minutes_to_departure: int
    priority: QueuePriority
    total_required: int
    total_assembled: int
    total_remaining: int
    meals: list[MealInventoryOut]


class KitchenQueueResponse(BaseModel):
    generated_at: datetime
    count: int
    items: list[KitchenQueueItem]

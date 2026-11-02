"""Flight, passenger manifest and audit log ORM models."""
from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, str_enum, utcnow

if TYPE_CHECKING:
    from app.models.meal import MealInventory


class FlightStatus(str, enum.Enum):
    SCHEDULED = "Scheduled"
    DELAYED = "Delayed"
    CANCELLED = "Cancelled"
    BOARDING = "Boarding"


class AircraftType(str, enum.Enum):
    A320 = "A320"
    B777 = "B777"
    A350 = "A350"


class Flight(Base):
    __tablename__ = "flights"

    flight_id: Mapped[str] = mapped_column(String(10), primary_key=True)
    airline: Mapped[str] = mapped_column(String(64), nullable=False)
    aircraft_type: Mapped[AircraftType] = mapped_column(
        str_enum(AircraftType, 8), nullable=False
    )
    departure_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True
    )
    status: Mapped[FlightStatus] = mapped_column(
        str_enum(FlightStatus, 16),
        nullable=False,
        default=FlightStatus.SCHEDULED,
        index=True,
    )
    destination: Mapped[str] = mapped_column(String(64), nullable=False)
    # Cumulative delay applied to the flight (departure_time already includes it).
    delay_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    manifest: Mapped[Optional["PassengerManifest"]] = relationship(
        back_populates="flight", uselist=False, cascade="all, delete-orphan"
    )
    meal_inventories: Mapped[list["MealInventory"]] = relationship(
        back_populates="flight", cascade="all, delete-orphan", order_by="MealInventory.id"
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(
        back_populates="flight", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("delay_minutes >= 0", name="ck_flights_delay_non_negative"),
    )


class PassengerManifest(Base):
    __tablename__ = "passenger_manifests"

    # One manifest per flight, so the FK doubles as the primary key.
    flight_id: Mapped[str] = mapped_column(
        ForeignKey("flights.flight_id", ondelete="CASCADE"), primary_key=True
    )
    economy_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    business_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_class_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    crew_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )

    flight: Mapped[Flight] = relationship(back_populates="manifest")

    __table_args__ = (
        CheckConstraint(
            "economy_count >= 0 AND business_count >= 0 "
            "AND first_class_count >= 0 AND crew_count >= 0",
            name="ck_manifest_counts_non_negative",
        ),
    )

    @property
    def total_passengers(self) -> int:
        """Everyone who needs a meal: passengers of all classes plus crew."""
        return (
            self.economy_count
            + self.business_count
            + self.first_class_count
            + self.crew_count
        )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flight_id: Mapped[str] = mapped_column(
        ForeignKey("flights.flight_id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )

    flight: Mapped[Flight] = relationship(back_populates="audit_logs")

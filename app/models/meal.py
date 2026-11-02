"""Meal inventory ORM model."""
from __future__ import annotations

import enum

from sqlalchemy import CheckConstraint, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, str_enum
from app.models.flight import Flight


class MealType(str, enum.Enum):
    STANDARD = "Standard"
    HALAL = "Halal"
    VEGETARIAN = "Vegetarian"
    VEGAN = "Vegan"
    SPECIAL_DIETARY = "Special Dietary"


class MealStatus(str, enum.Enum):
    PENDING = "Pending"
    IN_PROGRESS = "In-Progress"
    READY = "Ready"
    DISPATCHED = "Dispatched"


class MealInventory(Base):
    __tablename__ = "meal_inventories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flight_id: Mapped[str] = mapped_column(
        ForeignKey("flights.flight_id", ondelete="CASCADE"), nullable=False, index=True
    )
    meal_type: Mapped[MealType] = mapped_column(str_enum(MealType, 16), nullable=False)
    quantity_required: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quantity_assembled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[MealStatus] = mapped_column(
        str_enum(MealStatus, 16), nullable=False, default=MealStatus.PENDING
    )

    flight: Mapped[Flight] = relationship(back_populates="meal_inventories")

    __table_args__ = (
        UniqueConstraint("flight_id", "meal_type", name="uq_meal_flight_type"),
        CheckConstraint(
            "quantity_required >= 0 AND quantity_assembled >= 0",
            name="ck_meal_quantities_non_negative",
        ),
    )

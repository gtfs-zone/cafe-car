from typing import TYPE_CHECKING

from pydantic import model_validator
from sqlalchemy import CheckConstraint
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.service_alert import ServiceAlert


class InformedEntity(SQLModel, table=True):
    __tablename__ = "informed_entity"
    __table_args__ = (
        CheckConstraint(
            "agency_id IS NOT NULL OR route_id IS NOT NULL OR route_type IS NOT NULL "
            "OR direction_id IS NOT NULL OR stop_id IS NOT NULL OR trip_id IS NOT NULL",
            name="ck_informed_entity_has_specifier",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    service_alert_id: int = Field(foreign_key="service_alert.id")

    # EntitySelector fields
    agency_id: str | None = Field(default=None)
    route_id: str | None = Field(default=None)
    route_type: int | None = Field(default=None)
    direction_id: int | None = Field(default=None)
    stop_id: str | None = Field(default=None)

    # TripDescriptor fields (scheduled trips only)
    trip_id: str | None = Field(default=None)
    trip_route_id: str | None = Field(default=None)
    trip_direction_id: int | None = Field(default=None)
    trip_start_time: str | None = Field(default=None)  # "HH:MM:SS"
    trip_start_date: str | None = Field(default=None)  # "YYYYMMDD"

    alert: "ServiceAlert" = Relationship(back_populates="entities")

    @model_validator(mode="after")
    def check_e033(self) -> "InformedEntity":
        specifiers = [
            self.agency_id,
            self.route_id,
            self.route_type,
            self.direction_id,
            self.stop_id,
            self.trip_id,
        ]
        if not any(s is not None for s in specifiers):
            raise ValueError(
                "E033: informed_entity must have at least one specifier "
                "(agency_id, route_id, route_type, direction_id, stop_id, or trip_id)"
            )
        if self.direction_id is not None and not self.route_id:
            raise ValueError("direction_id requires route_id to also be set")
        return self

    def __str__(self) -> str:
        parts = [
            f"agency={self.agency_id}" if self.agency_id else None,
            f"route={self.route_id}" if self.route_id else None,
            f"route_type={self.route_type}" if self.route_type is not None else None,
            f"stop={self.stop_id}" if self.stop_id else None,
            f"trip={self.trip_id}" if self.trip_id else None,
        ]
        return ", ".join(p for p in parts if p) or "entity"

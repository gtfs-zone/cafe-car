from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Column, DateTime
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.feed import Feed
    from app.models.informed_entity import InformedEntity


class ServiceAlert(SQLModel, table=True):
    __tablename__ = "service_alert"

    id: int | None = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="feed.id")

    header_text: str = Field(max_length=512)
    description_text: str = Field(max_length=2048)
    url: str | None = Field(default=None, max_length=512)

    cause: str | None = Field(default=None)
    effect: str | None = Field(default=None)
    severity_level: str | None = Field(default=None)

    active_period_start: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    active_period_end: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    feed: "Feed" = Relationship(back_populates="alerts")
    entities: list["InformedEntity"] = Relationship(back_populates="alert")

    def __str__(self) -> str:
        return self.header_text[:64]

    def __repr__(self) -> str:
        return f"ServiceAlert(id={self.id}, header_text={self.header_text!r})"

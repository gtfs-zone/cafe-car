from datetime import datetime
from enum import StrEnum

from sqlalchemy import Column, DateTime, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class LoadStatus(StrEnum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"


class FeedLoadStatus(SQLModel, table=True):
    __tablename__ = "feed_load_status"

    feed_id: int = Field(foreign_key="feed.id", primary_key=True)
    last_loaded_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    status: str = Field(default=LoadStatus.pending)
    error_message: str | None = Field(default=None)
    stop_count: int = Field(default=0)
    route_count: int = Field(default=0)
    trip_count: int = Field(default=0)


class GtfsStop(SQLModel, table=True):
    __tablename__ = "gtfs_stop"
    __table_args__ = (UniqueConstraint("feed_id", "stop_id"),)

    id: int | None = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="feed.id")
    stop_id: str
    stop_name: str
    stop_lat: float
    stop_lon: float
    stop_code: str | None = Field(default=None)
    stop_desc: str | None = Field(default=None)


class GtfsRoute(SQLModel, table=True):
    __tablename__ = "gtfs_route"
    __table_args__ = (UniqueConstraint("feed_id", "route_id"),)

    id: int | None = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="feed.id")
    route_id: str
    agency_id: str | None = Field(default=None)
    route_short_name: str
    route_long_name: str
    route_type: int


class GtfsTrip(SQLModel, table=True):
    __tablename__ = "gtfs_trip"
    __table_args__ = (UniqueConstraint("feed_id", "trip_id"),)

    id: int | None = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="feed.id")
    trip_id: str
    route_id: str
    service_id: str
    trip_headsign: str | None = Field(default=None)
    direction_id: int | None = Field(default=None)


class GtfsStopTime(SQLModel, table=True):
    __tablename__ = "gtfs_stop_time"
    __table_args__ = (Index("ix_gtfs_stop_time_feed_trip", "feed_id", "trip_id"),)

    id: int | None = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="feed.id")
    trip_id: str
    stop_id: str
    arrival_time: str  # never null per project rules
    departure_time: str  # never null per project rules
    stop_sequence: int

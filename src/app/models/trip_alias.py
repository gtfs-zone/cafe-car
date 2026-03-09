from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

if TYPE_CHECKING:
    from app.models.feed import Feed


class TripAlias(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="feed.id")
    alias: str = Field(max_length=64)
    trip_id: str = Field(max_length=256)

    feed: "Feed" = Relationship(back_populates="aliases")

    __table_args__ = (UniqueConstraint("feed_id", "alias"),)

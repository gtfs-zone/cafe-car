import re
from typing import TYPE_CHECKING, Optional

from pydantic import AnyHttpUrl, TypeAdapter, field_validator
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.driver import Driver
    from app.models.gtfs_static import GtfsStaticFeed
    from app.models.service_alert import ServiceAlert
    from app.models.trip_alias import TripAlias
    from app.models.user import User

_url_validator = TypeAdapter(AnyHttpUrl)

_FEED_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")


class Feed(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    feed_name: str = Field(unique=True, index=True, max_length=64)
    static_feed_url: str
    owner_id: int = Field(foreign_key="user.id")
    gtfs_static_feed_id: int | None = Field(default=None, foreign_key="gtfs_static_feed.id")
    owner: "User" = Relationship(back_populates="feeds")
    drivers: list["Driver"] = Relationship(back_populates="feed")
    alerts: list["ServiceAlert"] = Relationship(back_populates="feed")
    aliases: list["TripAlias"] = Relationship(back_populates="feed")
    gtfs_static_feed: Optional["GtfsStaticFeed"] = Relationship()

    @field_validator("feed_name")
    @classmethod
    def validate_feed_name(cls, v: str) -> str:
        if not _FEED_NAME_RE.match(v):
            raise ValueError(
                "feed_name must start with a lowercase letter and contain only "
                "lowercase letters, digits, underscores, and hyphens (3–64 chars)"
            )
        return v

    @field_validator("static_feed_url")
    @classmethod
    def validate_static_feed_url(cls, v: str) -> str:
        try:
            _url_validator.validate_python(v)
        except Exception:
            raise ValueError("Must be a valid http or https URL") from None
        return v

    def __str__(self) -> str:
        return self.feed_name

    def __repr__(self) -> str:
        return f"Feed(id={self.id}, feed_name={self.feed_name!r})"

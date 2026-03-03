import re
from typing import TYPE_CHECKING

from pydantic import field_validator
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.feed import Feed

_ALPHANUMERIC_RE = re.compile(r"^[a-zA-Z0-9]{3,32}$")


class Driver(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True, max_length=32)
    password: str = Field(max_length=32)  # hashed (TODO)
    feed_id: int = Field(foreign_key="feed.id")

    feed: "Feed" = Relationship(back_populates="drivers")

    @field_validator("username", "password")
    @classmethod
    def validate_alphanumeric(cls, v: str) -> str:
        if not _ALPHANUMERIC_RE.match(v):
            raise ValueError("Must be 3–32 alphanumeric characters")
        return v

    def __str__(self) -> str:
        return self.username

    def __repr__(self) -> str:
        return f"Driver(id={self.id}, username={self.username!r})"

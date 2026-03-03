from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.feed import Feed


class Driver(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password: str  # hashed
    feed_id: int = Field(foreign_key="feed.id")

    feed: "Feed" = Relationship(back_populates="drivers")

    def __str__(self) -> str:
        return self.username

    def __repr__(self) -> str:
        return f"Driver(id={self.id}, username={self.username!r})"

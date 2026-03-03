from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.driver import Driver
    from app.models.user import User


class Feed(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    feed_name: str = Field(unique=True, index=True)
    static_feed_url: str
    owner_id: int = Field(foreign_key="user.id")

    owner: "User" = Relationship(back_populates="feeds")
    drivers: list["Driver"] = Relationship(back_populates="feed")

    def __str__(self) -> str:
        return self.feed_name

    def __repr__(self) -> str:
        return f"Feed(id={self.id}, feed_name={self.feed_name!r})"

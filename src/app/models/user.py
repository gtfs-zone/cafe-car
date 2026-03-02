from typing import TYPE_CHECKING

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.feed import Feed


class User(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("provider", "provider_subject"),)

    id: int | None = Field(default=None, primary_key=True)
    provider: str
    provider_subject: str
    email: str | None = None
    display_name: str | None = None

    feeds: list["Feed"] = Relationship(back_populates="owner")

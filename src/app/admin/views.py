from typing import Any

from sqladmin import ModelView
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.models.driver import Driver
from app.models.feed import Feed
from app.models.user import User


def _current_subject(request: Request) -> str:
    return request.session.get("subject", "")


class FeedAdmin(ModelView, model=Feed):
    column_list = [Feed.id, Feed.feed_name, Feed.static_feed_url, Feed.owner_id]
    column_searchable_list = [Feed.feed_name]
    name = "Feed"
    name_plural = "Feeds"

    def _base_query(self, subject: str):
        return (
            select(Feed)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    async def get_list_query(self):
        # Overridden per-request in list method; return base select
        return select(Feed)

    async def get_count_query(self):
        return select(func.count()).select_from(Feed)

    async def scaffold_list(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ):
        subject = _current_subject(request)
        session: AsyncSession = kwargs.get("session") or request.state.session
        result = await session.execute(self._base_query(subject))
        return result.scalars().all()

    async def insert_model(self, request: Request, data: dict) -> Feed:
        subject = _current_subject(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            select(User).where(User.provider_subject == subject)
        )
        owner = result.scalar_one_or_none()
        if owner is None:
            raise ValueError("Authenticated user not found in database")
        data["owner_id"] = owner.id
        return await super().insert_model(request, data)

    async def _get_owned_feed(self, request: Request, pk: Any) -> Feed:
        subject = _current_subject(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            self._base_query(subject).where(Feed.id == int(pk))
        )
        feed = result.scalar_one_or_none()
        if feed is None:
            raise PermissionError("Feed not found or access denied")
        return feed

    async def update_model(self, request: Request, pk: Any, data: dict) -> Feed:
        await self._get_owned_feed(request, pk)
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: Any) -> None:
        await self._get_owned_feed(request, pk)
        await super().delete_model(request, pk)


class DriverAdmin(ModelView, model=Driver):
    column_list = [Driver.id, Driver.username, Driver.feed_id]
    column_searchable_list = [Driver.username]
    name = "Driver"
    name_plural = "Drivers"

    def _base_query(self, subject: str):
        return (
            select(Driver)
            .join(Feed, Driver.feed_id == Feed.id)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    async def insert_model(self, request: Request, data: dict) -> Driver:
        subject = _current_subject(request)
        session: AsyncSession = request.state.session
        feed_id = data.get("feed_id")
        result = await session.execute(
            select(Feed)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
            .where(Feed.id == int(feed_id))
        )
        feed = result.scalar_one_or_none()
        if feed is None:
            raise PermissionError("Feed not found or access denied")
        return await super().insert_model(request, data)

    async def _get_owned_driver(self, request: Request, pk: Any) -> Driver:
        subject = _current_subject(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            self._base_query(subject).where(Driver.id == int(pk))
        )
        driver = result.scalar_one_or_none()
        if driver is None:
            raise PermissionError("Driver not found or access denied")
        return driver

    async def update_model(self, request: Request, pk: Any, data: dict) -> Driver:
        await self._get_owned_driver(request, pk)
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: Any) -> None:
        await self._get_owned_driver(request, pk)
        await super().delete_model(request, pk)

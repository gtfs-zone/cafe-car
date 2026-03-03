from typing import Any

from markupsafe import Markup
from sqladmin import ModelView
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from wtforms import SelectField
from wtforms.validators import URL, Length, Regexp

from app.admin.context import current_subject_var
from app.models.driver import Driver
from app.models.feed import Feed
from app.models.user import User


def _current_subject(request: Request) -> str:
    return request.session.get("subject", "")


class FeedAdmin(ModelView, model=Feed):
    form_args = {
        "feed_name": {
            "validators": [
                Length(min=3, max=64, message="feed_name must be 3–64 characters"),
                Regexp(
                    r"^[a-z][a-z0-9_-]*$",
                    message="Must start with [a-z], then [a-z0-9_-] only",
                ),
            ]
        },
        "static_feed_url": {
            "validators": [URL(message="Must be a valid http or https URL")]
        },
    }
    column_list = [Feed.feed_name, Feed.static_feed_url]
    column_formatters = {
        Feed.feed_name: lambda m, a: Markup(f'<a href="/feed/edit/{m.id}">{m.feed_name}</a>'),
    }
    column_searchable_list = [Feed.feed_name]
    form_excluded_columns = ["owner", "drivers", "owner_id"]
    name = "Feed"
    name_plural = "Feeds"

    def _base_query(self, subject: str):
        return (
            select(Feed)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    def list_query(self, request: Request):
        return self._base_query(_current_subject(request))

    def count_query(self, request: Request):
        subject = _current_subject(request)
        return (
            select(func.count(Feed.id))
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    def details_query(self, request: Request):
        pk = request.path_params["pk"]
        return self._base_query(_current_subject(request)).where(Feed.id == int(pk))

    def form_edit_query(self, request: Request):
        pk = request.path_params["pk"]
        return self._base_query(_current_subject(request)).where(Feed.id == int(pk))

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
    form_args = {
        "username": {
            "validators": [
                Length(min=3, max=32, message="username must be 3–32 characters"),
                Regexp(r"^[a-zA-Z0-9]+$", message="username must be alphanumeric only"),
            ]
        },
        "password": {
            "validators": [
                Length(min=3, max=32, message="password must be 3–32 characters"),
                Regexp(r"^[a-zA-Z0-9]+$", message="password must be alphanumeric only"),
            ]
        },
    }
    column_list = [Driver.username, "feed"]
    column_formatters = {
        Driver.username: lambda m, a: Markup(f'<a href="/driver/edit/{m.id}">{m.username}</a>'),
    }
    column_searchable_list = [Driver.username]
    form_excluded_columns = ["feed"]
    name = "Driver"
    name_plural = "Drivers"

    async def scaffold_form(self, rules=None):
        Form = await super().scaffold_form(rules)
        subject = current_subject_var.get()
        async with self.session_maker() as session:
            result = await session.execute(
                select(Feed)
                .join(User, Feed.owner_id == User.id)
                .where(User.provider_subject == subject)
            )
            feeds = result.scalars().all()
        Form.feed_id = SelectField(
            "Feed Name",
            choices=[(f.id, f.feed_name) for f in feeds],
            coerce=int,
        )
        return Form

    def _base_query(self, subject: str):
        return (
            select(Driver)
            .join(Feed, Driver.feed_id == Feed.id)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    def list_query(self, request: Request):
        return self._base_query(_current_subject(request))

    def count_query(self, request: Request):
        subject = _current_subject(request)
        return (
            select(func.count(Driver.id))
            .join(Feed, Driver.feed_id == Feed.id)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    def details_query(self, request: Request):
        pk = request.path_params["pk"]
        return self._base_query(_current_subject(request)).where(Driver.id == int(pk))

    def form_edit_query(self, request: Request):
        pk = request.path_params["pk"]
        return self._base_query(_current_subject(request)).where(Driver.id == int(pk))

    async def insert_model(self, request: Request, data: dict) -> Driver:
        subject = _current_subject(request)
        session: AsyncSession = request.state.session
        feed_id = data.get("feed_id")
        if not feed_id:
            raise ValueError("A feed must be selected")
        result = await session.execute(
            select(Feed)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
            .where(Feed.id == feed_id)
        )
        if result.scalar_one_or_none() is None:
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

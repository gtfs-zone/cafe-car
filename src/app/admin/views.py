from datetime import UTC, datetime
from typing import Any

from markupsafe import Markup
from sqladmin import ModelView
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.requests import Request
from wtforms import DateTimeLocalField, SelectField
from wtforms.validators import URL, Length, Optional, Regexp

from app.admin.context import current_subject_var
from app.models.driver import Driver
from app.models.feed import Feed
from app.models.informed_entity import InformedEntity
from app.models.service_alert import ServiceAlert
from app.models.user import User

_STATUS_BADGE = {
    "pending": '<span style="color:#f59e0b;font-weight:bold">pending</span>',
    "running": '<span style="color:#3b82f6;font-weight:bold">running</span>',
    "success": '<span style="color:#22c55e;font-weight:bold">success</span>',
    "failed": '<span style="color:#ef4444;font-weight:bold">failed</span>',
}

_CAUSE_CHOICES = [
    ("", "—"),
    ("UNKNOWN_CAUSE", "Unknown Cause"),
    ("OTHER_CAUSE", "Other Cause"),
    ("TECHNICAL_PROBLEM", "Technical Problem"),
    ("STRIKE", "Strike"),
    ("DEMONSTRATION", "Demonstration"),
    ("ACCIDENT", "Accident"),
    ("HOLIDAY", "Holiday"),
    ("WEATHER", "Weather"),
    ("MAINTENANCE", "Maintenance"),
    ("CONSTRUCTION", "Construction"),
    ("POLICE_ACTIVITY", "Police Activity"),
    ("MEDICAL_EMERGENCY", "Medical Emergency"),
]

_EFFECT_CHOICES = [
    ("", "—"),
    ("NO_SERVICE", "No Service"),
    ("REDUCED_SERVICE", "Reduced Service"),
    ("SIGNIFICANT_DELAYS", "Significant Delays"),
    ("DETOUR", "Detour"),
    ("ADDITIONAL_SERVICE", "Additional Service"),
    ("MODIFIED_SERVICE", "Modified Service"),
    ("OTHER_EFFECT", "Other Effect"),
    ("UNKNOWN_EFFECT", "Unknown Effect"),
    ("STOP_MOVED", "Stop Moved"),
    ("NO_EFFECT", "No Effect"),
    ("ACCESSIBILITY_ISSUE", "Accessibility Issue"),
]

_SEVERITY_CHOICES = [
    ("", "—"),
    ("UNKNOWN_SEVERITY", "Unknown"),
    ("INFO", "Info"),
    ("WARNING", "Warning"),
    ("SEVERE", "Severe"),
]


def _fmt_utc_dt(val: datetime | None) -> Markup:
    if val is None:
        return Markup("")
    iso = val.strftime("%Y-%m-%dT%H:%M:%SZ")
    return Markup(f'<time data-utc="{iso}">{iso}</time>')


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
    column_list = [
        Feed.feed_name,
        Feed.static_feed_url,
        "load_status_badge",
        "last_loaded_at",
        "reload_action",
    ]
    column_labels = {
        "load_status_badge": "Status",
        "last_loaded_at": "Last Loaded",
        "reload_action": "",
    }
    column_formatters = {
        Feed.feed_name: lambda m, a: Markup(f'<a href="/feed/edit/{m.id}">{m.feed_name}</a>'),
        "load_status_badge": lambda m, a: Markup(
            _STATUS_BADGE.get(
                m.gtfs_static_feed.status if m.gtfs_static_feed else "",
                '<span style="color:#9ca3af">—</span>',
            )
        ),
        "last_loaded_at": lambda m, a: (
            m.gtfs_static_feed.last_loaded_at.strftime("%Y-%m-%d %H:%M UTC")
            if m.gtfs_static_feed and m.gtfs_static_feed.last_loaded_at
            else "—"
        ),
        "reload_action": lambda m, a: Markup(
            f'<form method="post" action="/feeds/{m.id}/reload" style="margin:0">'
            f'<button type="submit" style="cursor:pointer">&#8635; Reload</button>'
            f"</form>"
        ),
    }
    column_searchable_list = [Feed.feed_name]
    form_excluded_columns = ["owner", "drivers", "alerts", "owner_id", "gtfs_static_feed"]
    name = "Feed"
    name_plural = "Feeds"

    def _base_query(self, subject: str):
        return (
            select(Feed)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    def list_query(self, request: Request):
        return self._base_query(_current_subject(request)).options(
            selectinload(Feed.gtfs_static_feed)
        )

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

    async def after_model_change(
        self, data: dict, model: Feed, is_created: bool, request: Request
    ) -> None:
        try:
            from app.celery_client import celery_app

            celery_app.send_task("worker.tasks.load_feed", args=[model.id])
        except Exception:
            pass  # worker unavailable; task will be triggered manually


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


_OPTIONAL_ALERT_FIELDS = ("cause", "effect", "severity_level", "url")


def _clear_empty_optional_fields(data: dict) -> None:
    for field in _OPTIONAL_ALERT_FIELDS:
        if data.get(field) == "":
            data[field] = None


def _make_alert_datetimes_utc(data: dict) -> None:
    for field in ("active_period_start", "active_period_end"):
        val = data.get(field)
        if isinstance(val, datetime) and val.tzinfo is None:
            data[field] = val.replace(tzinfo=UTC)


class ServiceAlertAdmin(ModelView, model=ServiceAlert):
    edit_template = "sqladmin/service_alert_edit.html"
    details_template = "sqladmin/service_alert_detail.html"

    form_args = {
        "header_text": {"validators": [Length(max=512)]},
        "description_text": {"validators": [Length(max=2048)]},
        "url": {
            "validators": [Optional(), URL(message="Must be a valid http or https URL")]
        },
        "cause": {"choices": _CAUSE_CHOICES},
        "effect": {"choices": _EFFECT_CHOICES},
        "severity_level": {"choices": _SEVERITY_CHOICES},
        "active_period_start": {"label": "Active Period Start"},
        "active_period_end": {"label": "Active Period End"},
    }
    form_overrides = {
        "cause": SelectField,
        "effect": SelectField,
        "severity_level": SelectField,
        "active_period_start": DateTimeLocalField,
        "active_period_end": DateTimeLocalField,
    }
    column_formatters = {
        ServiceAlert.active_period_start: lambda m, a: _fmt_utc_dt(m.active_period_start),
        ServiceAlert.active_period_end: lambda m, a: _fmt_utc_dt(m.active_period_end),
        "entity_summary": lambda m, a: Markup(
            "<br>".join(str(e) for e in m.entities) or "<em>none</em>"
        ),
    }
    column_list = [
        ServiceAlert.header_text,
        ServiceAlert.cause,
        ServiceAlert.effect,
        ServiceAlert.severity_level,
        ServiceAlert.active_period_start,
        ServiceAlert.active_period_end,
        "entity_summary",
        "feed",
    ]
    column_labels = {"entity_summary": "Entities"}
    column_searchable_list = [ServiceAlert.header_text]
    form_excluded_columns = ["feed", "entities"]
    name = "Service Alert"
    name_plural = "Service Alerts"

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
            select(ServiceAlert)
            .join(Feed, ServiceAlert.feed_id == Feed.id)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    def list_query(self, request: Request):
        return self._base_query(_current_subject(request)).options(
            selectinload(ServiceAlert.entities)
        )

    def count_query(self, request: Request):
        subject = _current_subject(request)
        return (
            select(func.count(ServiceAlert.id))
            .join(Feed, ServiceAlert.feed_id == Feed.id)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    def details_query(self, request: Request):
        pk = request.path_params["pk"]
        subject = _current_subject(request)
        return self._base_query(subject).where(ServiceAlert.id == int(pk))

    def form_edit_query(self, request: Request):
        pk = request.path_params["pk"]
        subject = _current_subject(request)
        return self._base_query(subject).where(ServiceAlert.id == int(pk))

    async def insert_model(self, request: Request, data: dict) -> ServiceAlert:
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
        _clear_empty_optional_fields(data)
        _make_alert_datetimes_utc(data)
        return await super().insert_model(request, data)

    async def _get_owned_alert(self, request: Request, pk: Any) -> ServiceAlert:
        subject = _current_subject(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            self._base_query(subject).where(ServiceAlert.id == int(pk))
        )
        alert = result.scalar_one_or_none()
        if alert is None:
            raise PermissionError("Service alert not found or access denied")
        return alert

    async def update_model(self, request: Request, pk: Any, data: dict) -> ServiceAlert:
        await self._get_owned_alert(request, pk)
        _clear_empty_optional_fields(data)
        _make_alert_datetimes_utc(data)
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: Any) -> None:
        await self._get_owned_alert(request, pk)
        await super().delete_model(request, pk)


_ROUTE_TYPE_CHOICES = [
    (0, "0 — Tram / Light Rail"),
    (1, "1 — Subway / Metro"),
    (2, "2 — Rail"),
    (3, "3 — Bus"),
    (4, "4 — Ferry"),
    (5, "5 — Cable Tram"),
    (6, "6 — Aerial Lift"),
    (7, "7 — Funicular"),
    (11, "11 — Trolleybus"),
    (12, "12 — Monorail"),
]


class InformedEntityAdmin(ModelView, model=InformedEntity):
    def is_visible(self, request: Request) -> bool:
        return False

    column_list = [
        "alert",
        InformedEntity.agency_id,
        InformedEntity.route_id,
        InformedEntity.route_type,
        InformedEntity.direction_id,
        InformedEntity.stop_id,
        InformedEntity.trip_id,
        InformedEntity.trip_route_id,
        InformedEntity.trip_start_time,
        InformedEntity.trip_start_date,
    ]
    form_excluded_columns = ["alert"]
    name = "Informed Entity"
    name_plural = "Informed Entities"

    async def scaffold_form(self, rules=None):
        Form = await super().scaffold_form(rules)
        subject = current_subject_var.get()
        async with self.session_maker() as session:
            result = await session.execute(
                select(ServiceAlert)
                .join(Feed, ServiceAlert.feed_id == Feed.id)
                .join(User, Feed.owner_id == User.id)
                .where(User.provider_subject == subject)
            )
            alerts = result.scalars().all()
        Form.service_alert_id = SelectField(
            "Service Alert",
            choices=[(a.id, str(a)) for a in alerts],
            coerce=int,
        )
        Form.route_type = SelectField(
            "Route Type",
            choices=[("", "—")] + _ROUTE_TYPE_CHOICES,
            coerce=lambda x: None if x == "" else int(x),
            validators=[Optional()],
        )
        return Form

    def _base_query(self, subject: str):
        return (
            select(InformedEntity)
            .join(ServiceAlert, InformedEntity.service_alert_id == ServiceAlert.id)
            .join(Feed, ServiceAlert.feed_id == Feed.id)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    def list_query(self, request: Request):
        return self._base_query(_current_subject(request))

    def count_query(self, request: Request):
        subject = _current_subject(request)
        return (
            select(func.count(InformedEntity.id))
            .join(ServiceAlert, InformedEntity.service_alert_id == ServiceAlert.id)
            .join(Feed, ServiceAlert.feed_id == Feed.id)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
        )

    def details_query(self, request: Request):
        pk = request.path_params["pk"]
        subject = _current_subject(request)
        return self._base_query(subject).where(InformedEntity.id == int(pk))

    def form_edit_query(self, request: Request):
        pk = request.path_params["pk"]
        subject = _current_subject(request)
        return self._base_query(subject).where(InformedEntity.id == int(pk))

    async def _check_alert_ownership(self, request: Request, alert_id: int) -> None:
        subject = _current_subject(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            select(ServiceAlert)
            .join(Feed, ServiceAlert.feed_id == Feed.id)
            .join(User, Feed.owner_id == User.id)
            .where(User.provider_subject == subject)
            .where(ServiceAlert.id == alert_id)
        )
        if result.scalar_one_or_none() is None:
            raise PermissionError("Service alert not found or access denied")

    async def insert_model(self, request: Request, data: dict) -> InformedEntity:
        alert_id = data.get("service_alert_id")
        if not alert_id:
            raise ValueError("A service alert must be selected")
        await self._check_alert_ownership(request, int(alert_id))
        return await super().insert_model(request, data)

    async def _get_owned_entity(self, request: Request, pk: Any) -> InformedEntity:
        subject = _current_subject(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            self._base_query(subject).where(InformedEntity.id == int(pk))
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise PermissionError("Informed entity not found or access denied")
        return entity

    async def update_model(
        self, request: Request, pk: Any, data: dict
    ) -> InformedEntity:
        await self._get_owned_entity(request, pk)
        alert_id = data.get("service_alert_id")
        if alert_id:
            await self._check_alert_ownership(request, int(alert_id))
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: Any) -> None:
        await self._get_owned_entity(request, pk)
        await super().delete_model(request, pk)

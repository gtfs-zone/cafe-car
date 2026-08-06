from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from markupsafe import Markup, escape
from railroad_club.models.feed import Feed
from railroad_club.models.informed_entity import InformedEntity
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.tracker import Tracker, generate_tracker_id
from railroad_club.models.tracker_rule import TrackerRule
from sqladmin import ModelView
from sqlalchemy import Select, func, select
from sqlalchemy.orm import selectinload
from wtforms import DateTimeLocalField, SelectField
from wtforms.validators import URL, Length, Optional, Regexp

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from starlette.requests import Request

from cafe_car.admin.access import accessible_feed_ids, owned_feed_ids
from cafe_car.admin.context import current_user_id_var

_STATUS_BADGE = {
    "pending": '<span class="badge bg-yellow">pending</span>',
    "running": '<span class="badge bg-blue">running</span>',
    "success": '<span class="badge bg-green">success</span>',
    "failed": '<span class="badge bg-red">failed</span>',
}

_CAUSE_CHOICES = [
    ("", "-"),
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
    ("", "-"),
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
    ("", "-"),
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


def _current_user_id(request: Request) -> int:
    """0 when unauthenticated, which every access subquery matches nothing for."""
    return int(request.session.get("user_id") or 0)


def _link(href: str, label: object) -> Markup:
    """An escaped anchor. Never interpolate model text into Markup directly:
    nickname, header_text and trip_id are all free text, and Tracker.id is
    caller-supplied at creation, so unescaped interpolation is stored XSS."""
    return Markup(f'<a href="{escape(href)}">{escape(label)}</a>')


class ScopedModelView(ModelView):
    """Every view in this admin is per-user scoped and has no details page:
    the edit page is the only page for an object, showing anything
    non-editable read-only. The metaclass short-circuits without a `model=`
    kwarg, so this stays an ordinary base class, but note it only reads
    `name`, `column_list` and friends off the *concrete* subclass, so those
    must never move up here."""

    can_view_details: ClassVar[bool] = False


class FeedAdmin(ScopedModelView, model=Feed):
    edit_template = "sqladmin/feed_edit.html"
    form_args: ClassVar[dict] = {
        "feed_name": {
            "validators": [
                Length(min=3, max=64, message="feed_name must be 3-64 characters"),
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
    column_list: ClassVar[list] = [
        Feed.feed_name,
        Feed.static_feed_url,
        "access_badge",
        "load_status_badge",
        "last_loaded_at",
    ]
    column_labels: ClassVar[dict] = {
        "access_badge": "Access",
        "load_status_badge": "Status",
        "last_loaded_at": "Last Loaded",
    }
    column_formatters: ClassVar[dict] = {
        Feed.feed_name: lambda m, a: _link(f"/feed/edit/{m.id}", m.feed_name),
        # Formatters get no request, so read the per-request ContextVar.
        # Deliberately not blue: a blue badge here read as a broken link.
        "access_badge": lambda m, a: Markup(
            '<span class="badge bg-green">owner</span>'
            if m.owner_id == current_user_id_var.get()
            else '<span class="badge bg-secondary">shared with me</span>'
        ),
        "load_status_badge": lambda m, a: Markup(
            _STATUS_BADGE.get(
                m.gtfs_static_feed.status if m.gtfs_static_feed else "",
                '<span class="text-muted">-</span>',
            )
        ),
        "last_loaded_at": lambda m, a: (
            _fmt_utc_dt(m.gtfs_static_feed.last_loaded_at)
            if m.gtfs_static_feed and m.gtfs_static_feed.last_loaded_at
            else Markup("-")
        ),
    }
    column_searchable_list: ClassVar[list] = [Feed.feed_name]
    form_excluded_columns: ClassVar[list] = [
        "owner",
        "trackers",
        "alerts",
        "owner_id",
        "gtfs_static_feed",
        # Membership is managed through its own owner-checked routes, never a
        # form widget. Leaving it in also makes WTForms touch the relationship
        # on a detached instance, which raises DetachedInstanceError.
        "members",
        # Same again for pending invites: WTForms' process() calls hasattr()
        # across every attribute, which lazy-loads this one on a detached
        # instance and blows up the edit form.
        "invites",
    ]
    name = "Feed"
    name_plural = "Feeds"

    def _base_query(self, user_id: int) -> Select[tuple[Feed]]:
        return select(Feed).where(Feed.id.in_(accessible_feed_ids(user_id)))

    def list_query(self, request: Request) -> Select[tuple[Feed]]:
        return self._base_query(_current_user_id(request)).options(
            selectinload(Feed.gtfs_static_feed)
        )

    def count_query(self, request: Request) -> Select[tuple[int]]:
        user_id = _current_user_id(request)
        return select(func.count(Feed.id)).where(
            Feed.id.in_(accessible_feed_ids(user_id))
        )

    def form_edit_query(self, request: Request) -> Select[tuple[Feed]]:
        # The edit page is the feed hub, and SQLAdmin closes the query's session
        # before rendering, so everything the template touches must be eager
        # loaded or it raises DetachedInstanceError. Safe alongside
        # form_excluded_columns: exclusion is what keeps WTForms off these, and a
        # selectinload'ed collection is already populated either way.
        pk = request.path_params["pk"]
        return (
            self._base_query(_current_user_id(request))
            .where(Feed.id == int(pk))
            .options(
                selectinload(Feed.owner),
                selectinload(Feed.gtfs_static_feed),
                selectinload(Feed.trackers).selectinload(Tracker.rules),
                selectinload(Feed.alerts),
            )
        )

    async def insert_model(self, request: Request, data: dict) -> Feed:
        user_id = _current_user_id(request)
        if not user_id:
            raise PermissionError("Not authenticated")
        # The creator owns it. Ownership is never taken from the form.
        data["owner_id"] = user_id
        return await super().insert_model(request, data)

    async def _get_accessible_feed(self, request: Request, pk: str | int) -> Feed:
        """A feed the caller owns or is a member of."""
        user_id = _current_user_id(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            self._base_query(user_id).where(Feed.id == int(pk))
        )
        feed = result.scalar_one_or_none()
        if feed is None:
            raise PermissionError("Feed not found or access denied")
        return feed

    async def _get_owned_feed(self, request: Request, pk: str | int) -> Feed:
        """A feed the caller *owns*. Members are refused."""
        user_id = _current_user_id(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            select(Feed)
            .where(Feed.id.in_(owned_feed_ids(user_id)))
            .where(Feed.id == int(pk))
        )
        feed = result.scalar_one_or_none()
        if feed is None:
            raise PermissionError("Only the owner of this feed can do that")
        return feed

    async def update_model(self, request: Request, pk: str | int, data: dict) -> Feed:
        # Members may edit a feed's contents...
        await self._get_accessible_feed(request, pk)
        # ...but ownership is never settable from a form. Dropping the key
        # here, not merely hiding the field, is what stops a crafted POST.
        data.pop("owner_id", None)
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: str | int) -> None:
        await self._get_owned_feed(request, pk)
        await super().delete_model(request, pk)

    async def after_model_change(
        self, data: dict, model: Feed, is_created: bool, request: Request
    ) -> None:
        with contextlib.suppress(Exception):
            from cafe_car.celery_client import celery_app

            celery_app.send_task("schedule_foamer.tasks.load_feed", args=[model.id])


class TrackerAdmin(ScopedModelView, model=Tracker):
    edit_template = "sqladmin/tracker_edit.html"
    # The secret id and the QR both live on the tracker's own page, which the
    # nickname links to; a "provisioning" column here would just duplicate it.
    column_list: ClassVar[list] = [Tracker.nickname, "feed"]
    column_formatters: ClassVar[dict] = {
        Tracker.nickname: lambda m, a: _link(f"/tracker/edit/{m.id}", m.nickname),
    }
    column_searchable_list: ClassVar[list] = [Tracker.nickname]
    # ``id`` is a secret pet-name, prefilled with a random default and editable
    # at creation time only; it is never editable after creation (it's baked
    # into the Traccar device, tracker_rule FK, and Redis keys).
    form_include_pk: ClassVar[bool] = True
    form_excluded_columns: ClassVar[list] = ["feed", "rules"]
    form_create_rules: ClassVar[list] = ["id", "nickname", "feed_id"]
    form_edit_rules: ClassVar[list] = ["nickname", "feed_id"]
    name = "Tracker"
    name_plural = "Trackers"

    async def scaffold_form(self, rules: list | None = None) -> type:
        Form = await super().scaffold_form(rules)
        if rules is not None and "id" not in rules and hasattr(Form, "id"):
            delattr(Form, "id")
        user_id = current_user_id_var.get()
        async with self.session_maker() as session:
            result = await session.execute(
                select(Feed).where(Feed.id.in_(accessible_feed_ids(user_id)))
            )
            feeds = result.scalars().all()
        Form.feed_id = SelectField(
            "Feed Name",
            choices=[(f.id, f.feed_name) for f in feeds],
            coerce=int,
        )
        return Form

    def _base_query(self, user_id: int) -> Select[tuple[Tracker]]:
        return select(Tracker).where(Tracker.feed_id.in_(accessible_feed_ids(user_id)))

    def list_query(self, request: Request) -> Select[tuple[Tracker]]:
        return self._base_query(_current_user_id(request))

    def count_query(self, request: Request) -> Select[tuple[int]]:
        user_id = _current_user_id(request)
        return select(func.count(Tracker.id)).where(
            Tracker.feed_id.in_(accessible_feed_ids(user_id))
        )

    def form_edit_query(self, request: Request) -> Select[tuple[Tracker]]:
        # ``rules`` is rendered on the edit page and the session is closed before
        # the template runs, so it has to be eager loaded.
        pk = request.path_params["pk"]
        return (
            self._base_query(_current_user_id(request))
            .where(Tracker.id == pk)
            .options(selectinload(Tracker.rules))
        )

    async def insert_model(self, request: Request, data: dict) -> Tracker:
        user_id = _current_user_id(request)
        session: AsyncSession = request.state.session
        feed_id = data.get("feed_id")
        if not feed_id:
            raise ValueError("A feed must be selected")
        result = await session.execute(
            select(Feed)
            .where(Feed.id.in_(accessible_feed_ids(user_id)))
            .where(Feed.id == feed_id)
        )
        if result.scalar_one_or_none() is None:
            raise PermissionError("Feed not found or access denied")
        tracker_id = (data.get("id") or "").strip()
        data["id"] = tracker_id or generate_tracker_id()
        return await super().insert_model(request, data)

    async def _get_owned_tracker(self, request: Request, pk: str | int) -> Tracker:
        user_id = _current_user_id(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            self._base_query(user_id).where(Tracker.id == pk)
        )
        tracker = result.scalar_one_or_none()
        if tracker is None:
            raise PermissionError("Tracker not found or access denied")
        return tracker

    async def update_model(
        self, request: Request, pk: str | int, data: dict
    ) -> Tracker:
        await self._get_owned_tracker(request, pk)
        # ``id`` is immutable after creation; never let a crafted POST change it
        # (it's the Traccar uniqueId / Redis key / tracker_rule FK target).
        data.pop("id", None)
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: str | int) -> None:
        await self._get_owned_tracker(request, pk)
        await super().delete_model(request, pk)

    async def after_model_change(
        self, data: dict, model: Tracker, is_created: bool, request: Request
    ) -> None:
        if is_created:
            # Auto-create the matching Traccar device (uniqueId = tracker id).
            # Best-effort: never block tracker creation on Traccar availability.
            try:
                from cafe_car.traccar import get_traccar_client

                await get_traccar_client().ensure_device(
                    name=model.nickname, unique_id=model.id
                )
            except Exception:
                logging.getLogger(__name__).warning(
                    "Failed to auto-create Traccar device for tracker %s",
                    model.id,
                    exc_info=True,
                )


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


class ServiceAlertAdmin(ScopedModelView, model=ServiceAlert):
    edit_template = "sqladmin/service_alert_edit.html"

    form_args: ClassVar[dict] = {
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
    form_overrides: ClassVar[dict] = {
        "cause": SelectField,
        "effect": SelectField,
        "severity_level": SelectField,
        "active_period_start": DateTimeLocalField,
        "active_period_end": DateTimeLocalField,
    }
    column_formatters: ClassVar[dict] = {
        ServiceAlert.header_text: (
            lambda m, a: _link(f"/service-alert/edit/{m.id}", m.header_text)
        ),
        ServiceAlert.active_period_start: (
            lambda m, a: _fmt_utc_dt(m.active_period_start)
        ),
        ServiceAlert.active_period_end: lambda m, a: _fmt_utc_dt(m.active_period_end),
        # str(e) carries free-text route/stop ids straight from the user.
        "entity_summary": lambda m, a: Markup(
            Markup("<br>").join(escape(e) for e in m.entities) or "<em>none</em>"
        ),
    }
    column_list: ClassVar[list] = [
        ServiceAlert.header_text,
        ServiceAlert.cause,
        ServiceAlert.effect,
        ServiceAlert.severity_level,
        ServiceAlert.active_period_start,
        ServiceAlert.active_period_end,
        "entity_summary",
        "feed",
    ]
    column_labels: ClassVar[dict] = {"entity_summary": "Entities"}
    column_searchable_list: ClassVar[list] = [ServiceAlert.header_text]
    form_excluded_columns: ClassVar[list] = ["feed", "entities"]
    name = "Service Alert"
    name_plural = "Service Alerts"

    async def scaffold_form(self, rules: list | None = None) -> type:
        Form = await super().scaffold_form(rules)
        user_id = current_user_id_var.get()
        async with self.session_maker() as session:
            result = await session.execute(
                select(Feed).where(Feed.id.in_(accessible_feed_ids(user_id)))
            )
            feeds = result.scalars().all()
        Form.feed_id = SelectField(
            "Feed Name",
            choices=[(f.id, f.feed_name) for f in feeds],
            coerce=int,
        )
        return Form

    def _base_query(self, user_id: int) -> Select[tuple[ServiceAlert]]:
        return select(ServiceAlert).where(
            ServiceAlert.feed_id.in_(accessible_feed_ids(user_id))
        )

    def list_query(self, request: Request) -> Select[tuple[ServiceAlert]]:
        return self._base_query(_current_user_id(request)).options(
            selectinload(ServiceAlert.entities)
        )

    def count_query(self, request: Request) -> Select[tuple[int]]:
        user_id = _current_user_id(request)
        return select(func.count(ServiceAlert.id)).where(
            ServiceAlert.feed_id.in_(accessible_feed_ids(user_id))
        )

    def form_edit_query(self, request: Request) -> Select[tuple[ServiceAlert]]:
        pk = request.path_params["pk"]
        user_id = _current_user_id(request)
        return self._base_query(user_id).where(ServiceAlert.id == int(pk))

    async def insert_model(self, request: Request, data: dict) -> ServiceAlert:
        user_id = _current_user_id(request)
        session: AsyncSession = request.state.session
        feed_id = data.get("feed_id")
        if not feed_id:
            raise ValueError("A feed must be selected")
        result = await session.execute(
            select(Feed)
            .where(Feed.id.in_(accessible_feed_ids(user_id)))
            .where(Feed.id == feed_id)
        )
        if result.scalar_one_or_none() is None:
            raise PermissionError("Feed not found or access denied")
        _clear_empty_optional_fields(data)
        _make_alert_datetimes_utc(data)
        return await super().insert_model(request, data)

    async def _get_owned_alert(self, request: Request, pk: str | int) -> ServiceAlert:
        user_id = _current_user_id(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            self._base_query(user_id).where(ServiceAlert.id == int(pk))
        )
        alert = result.scalar_one_or_none()
        if alert is None:
            raise PermissionError("Service alert not found or access denied")
        return alert

    async def update_model(
        self, request: Request, pk: str | int, data: dict
    ) -> ServiceAlert:
        await self._get_owned_alert(request, pk)
        _clear_empty_optional_fields(data)
        _make_alert_datetimes_utc(data)
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: str | int) -> None:
        await self._get_owned_alert(request, pk)
        await super().delete_model(request, pk)


_ROUTE_TYPE_CHOICES = [
    (0, "0 - Tram / Light Rail"),
    (1, "1 - Subway / Metro"),
    (2, "2 - Rail"),
    (3, "3 - Bus"),
    (4, "4 - Ferry"),
    (5, "5 - Cable Tram"),
    (6, "6 - Aerial Lift"),
    (7, "7 - Funicular"),
    (11, "11 - Trolleybus"),
    (12, "12 - Monorail"),
]


class InformedEntityAdmin(ScopedModelView, model=InformedEntity):
    def is_visible(self, request: Request) -> bool:
        return False

    column_list: ClassVar[list] = [
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
    form_excluded_columns: ClassVar[list] = ["alert"]
    name = "Informed Entity"
    name_plural = "Informed Entities"

    async def scaffold_form(self, rules: list | None = None) -> type:
        Form = await super().scaffold_form(rules)
        user_id = current_user_id_var.get()
        async with self.session_maker() as session:
            result = await session.execute(
                select(ServiceAlert).where(
                    ServiceAlert.feed_id.in_(accessible_feed_ids(user_id))
                )
            )
            alerts = result.scalars().all()
        Form.service_alert_id = SelectField(
            "Service Alert",
            choices=[(a.id, str(a)) for a in alerts],
            coerce=int,
        )
        Form.route_type = SelectField(
            "Route Type",
            choices=[("", "-"), *_ROUTE_TYPE_CHOICES],
            coerce=lambda x: None if x == "" else int(x),
            validators=[Optional()],
        )
        return Form

    def _base_query(self, user_id: int) -> Select[tuple[InformedEntity]]:
        return (
            select(InformedEntity)
            .join(ServiceAlert, InformedEntity.service_alert_id == ServiceAlert.id)
            .where(ServiceAlert.feed_id.in_(accessible_feed_ids(user_id)))
        )

    def list_query(self, request: Request) -> Select[tuple[InformedEntity]]:
        return self._base_query(_current_user_id(request))

    def count_query(self, request: Request) -> Select[tuple[int]]:
        user_id = _current_user_id(request)
        return (
            select(func.count(InformedEntity.id))
            .join(ServiceAlert, InformedEntity.service_alert_id == ServiceAlert.id)
            .where(ServiceAlert.feed_id.in_(accessible_feed_ids(user_id)))
        )

    def form_edit_query(self, request: Request) -> Select[tuple[InformedEntity]]:
        pk = request.path_params["pk"]
        user_id = _current_user_id(request)
        return self._base_query(user_id).where(InformedEntity.id == int(pk))

    async def _check_alert_ownership(self, request: Request, alert_id: int) -> None:
        user_id = _current_user_id(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            select(ServiceAlert)
            .where(ServiceAlert.feed_id.in_(accessible_feed_ids(user_id)))
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

    async def _get_owned_entity(
        self, request: Request, pk: str | int
    ) -> InformedEntity:
        user_id = _current_user_id(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            self._base_query(user_id).where(InformedEntity.id == int(pk))
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise PermissionError("Informed entity not found or access denied")
        return entity

    async def update_model(
        self, request: Request, pk: str | int, data: dict
    ) -> InformedEntity:
        await self._get_owned_entity(request, pk)
        alert_id = data.get("service_alert_id")
        if alert_id:
            await self._check_alert_ownership(request, int(alert_id))
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: str | int) -> None:
        await self._get_owned_entity(request, pk)
        await super().delete_model(request, pk)


class TrackerRuleAdmin(ScopedModelView, model=TrackerRule):
    column_list: ClassVar[list] = [
        "tracker",
        TrackerRule.trip_id,
        TrackerRule.monday,
        TrackerRule.tuesday,
        TrackerRule.wednesday,
        TrackerRule.thursday,
        TrackerRule.friday,
        TrackerRule.saturday,
        TrackerRule.sunday,
        TrackerRule.start_time,
        TrackerRule.end_time,
    ]
    column_formatters: ClassVar[dict] = {
        TrackerRule.trip_id: (
            lambda m, a: _link(f"/tracker-rule/edit/{m.id}", m.trip_id)
        ),
    }
    column_searchable_list: ClassVar[list] = [TrackerRule.trip_id]
    form_excluded_columns: ClassVar[list] = ["tracker"]
    name = "Tracker Rule"
    name_plural = "Tracker Rules"

    async def scaffold_form(self, rules: list | None = None) -> type:
        Form = await super().scaffold_form(rules)
        user_id = current_user_id_var.get()
        async with self.session_maker() as session:
            result = await session.execute(
                select(Tracker)
                .where(Tracker.feed_id.in_(accessible_feed_ids(user_id)))
                .order_by(Tracker.nickname)
            )
            trackers = result.scalars().all()
        Form.tracker_id = SelectField(
            "Tracker",
            choices=[(t.id, t.nickname) for t in trackers],
            coerce=str,
        )
        return Form

    def _base_query(self, user_id: int) -> Select[tuple[TrackerRule]]:
        return (
            select(TrackerRule)
            .join(Tracker, TrackerRule.tracker_id == Tracker.id)
            .where(Tracker.feed_id.in_(accessible_feed_ids(user_id)))
        )

    def list_query(self, request: Request) -> Select[tuple[TrackerRule]]:
        return self._base_query(_current_user_id(request))

    def count_query(self, request: Request) -> Select[tuple[int]]:
        user_id = _current_user_id(request)
        return (
            select(func.count(TrackerRule.id))
            .join(Tracker, TrackerRule.tracker_id == Tracker.id)
            .where(Tracker.feed_id.in_(accessible_feed_ids(user_id)))
        )

    def form_edit_query(self, request: Request) -> Select[tuple[TrackerRule]]:
        pk = request.path_params["pk"]
        return self._base_query(_current_user_id(request)).where(
            TrackerRule.id == int(pk)
        )

    async def insert_model(self, request: Request, data: dict) -> TrackerRule:
        user_id = _current_user_id(request)
        session: AsyncSession = request.state.session
        tracker_id = data.get("tracker_id")
        if not tracker_id:
            raise ValueError("A tracker must be selected")
        result = await session.execute(
            select(Tracker)
            .where(Tracker.feed_id.in_(accessible_feed_ids(user_id)))
            .where(Tracker.id == tracker_id)
        )
        if result.scalar_one_or_none() is None:
            raise PermissionError("Tracker not found or access denied")
        return await super().insert_model(request, data)

    async def _get_owned_rule(self, request: Request, pk: str | int) -> TrackerRule:
        user_id = _current_user_id(request)
        session: AsyncSession = request.state.session
        result = await session.execute(
            self._base_query(user_id).where(TrackerRule.id == int(pk))
        )
        rule = result.scalar_one_or_none()
        if rule is None:
            raise PermissionError("Tracker rule not found or access denied")
        return rule

    async def update_model(
        self, request: Request, pk: str | int, data: dict
    ) -> TrackerRule:
        await self._get_owned_rule(request, pk)
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: str | int) -> None:
        await self._get_owned_rule(request, pk)
        await super().delete_model(request, pk)

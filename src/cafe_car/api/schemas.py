"""What the API is allowed to say.

Every response is built from a model here, field by field, and never by dumping
an ORM object. That is what keeps ``Tracker.device_key`` - the Traccar
provisioning credential - out of a response that has no business carrying it,
and it is why :class:`TrackerOut` and :class:`TrackerDetailOut` are two types
rather than one with an optional field: a list endpoint that returns the wrong
one fails to typecheck rather than leaking.

It also sidesteps the trap that outlived SQLAdmin's ``form_excluded_columns``:
a serializer that walked relationships on a detached instance would raise
``MissingGreenlet`` under the async session. Selecting explicitly means the
route decides what it loads.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class MeOut(BaseModel):
    """The signed-in person, as the frontend's header needs them."""

    user_id: int
    email: str | None
    display_name: str | None
    is_admin: bool
    # Keycloak's self-serve Account Console, where a person links another
    # provider. Empty in a deployment that has not configured one.
    account_url: str | None


class LoadStatusOut(BaseModel):
    """Where the static feed's last load got to.

    Mirrors ``GtfsStaticFeed``. Phase 6 pushes this same shape down the event
    stream, so a client can apply an update without a second representation.
    """

    status: str
    error_message: str | None
    timezone: str | None
    last_loaded_at: datetime | None
    started_at: datetime | None
    next_retry_at: datetime | None


class FeedOut(BaseModel):
    id: int
    feed_name: str
    static_feed_url: str
    owner_id: int
    owner_name: str | None
    # Whether this caller *is* the owner. A fact about the row, so an admin
    # looking at someone else's feed sees False.
    is_owner: bool
    # Whether this caller may do the owner-only things: transfer, delete,
    # manage members. True for the owner and for an admin, matching exactly
    # what `owned_feed` will let them do.
    can_manage: bool
    vehicle_positions_url: str
    trip_updates_url: str
    service_alerts_url: str
    # None when the feed has never been handed to schedule-foamer.
    load: LoadStatusOut | None


class TrackerOut(BaseModel):
    """A tracker as everything except its own detail page sees it.

    ``id`` is a surrogate key and carries no secret, so it is the address a
    client keeps in navigation state, in the map layer and in a log line.
    ``nickname`` is the public GTFS-RT label, unique within a feed but a display
    name rather than an identity.
    """

    id: str
    nickname: str
    feed_id: int


class TrackerDetailOut(TrackerOut):
    """A tracker plus its provisioning credential.

    Returned only by the tracker detail endpoint, which a client reaches
    deliberately when someone opens the properties panel. ``device_key`` is the
    Traccar ``uniqueId``; there is no password behind it, so it is the whole
    secret.
    """

    device_key: str


class InformedEntityOut(BaseModel):
    id: int
    service_alert_id: int
    agency_id: str | None
    route_id: str | None
    route_type: int | None
    direction_id: int | None
    stop_id: str | None
    trip_id: str | None
    trip_route_id: str | None
    trip_direction_id: int | None
    trip_start_time: str | None
    trip_start_date: str | None


class AlertOut(BaseModel):
    id: int
    feed_id: int
    header_text: str
    description_text: str
    url: str | None
    cause: str | None
    effect: str | None
    severity_level: str | None
    active_period_start: datetime | None
    active_period_end: datetime | None
    entity_count: int


class AlertDetailOut(AlertOut):
    entities: list[InformedEntityOut]


class MemberOut(BaseModel):
    """Someone who may work on a feed, including its owner."""

    user_id: int
    email: str | None
    display_name: str | None
    is_owner: bool
    added_by_user_id: int | None
    # None for the owner, who was never added by anybody.
    created_at: datetime | None


class InviteOut(BaseModel):
    """A share waiting for an address that has not signed in yet."""

    id: int
    email: str
    invited_by_user_id: int | None
    created_at: datetime


class PeopleOut(BaseModel):
    members: list[MemberOut]
    invites: list[InviteOut]


class RuleExceptionOut(BaseModel):
    """One service date added to or removed from a rule."""

    id: int
    date: date
    exception_type: str


class TrackerRuleOut(BaseModel):
    """A recurrence rule, as stored.

    ``start_time``/``end_time`` are seconds since service midnight, so an
    ``end_time`` past 86400 is a window that runs into the next calendar day.
    """

    id: int
    tracker_id: str
    trip_id: str
    monday: bool
    tuesday: bool
    wednesday: bool
    thursday: bool
    friday: bool
    saturday: bool
    sunday: bool
    start_date: date
    end_date: date | None
    start_time: int
    end_time: int
    exceptions: list[RuleExceptionOut]


class AssignmentOut(BaseModel):
    """One rule occurring on one service date.

    ``service_date`` is the date the window *starts* in feed-local time, and it
    is the trip's GTFS-RT ``start_date``. A window crossing midnight appears
    once, on the day it started, with an ``end_time`` past 86400.
    """

    rule_id: int
    tracker_id: str
    tracker_nickname: str
    trip_id: str
    service_date: date
    start_time: int
    end_time: int

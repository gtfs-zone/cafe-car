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

import re
from datetime import UTC, date, datetime

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    TypeAdapter,
    field_validator,
    model_validator,
)

# Runtime imports: pydantic resolves these annotations when it builds the
# alert models, so a TYPE_CHECKING block would break them.
from railroad_club.models.gtfs_upload import FeedSourceKind
from railroad_club.models.tracker_rule import ExceptionType

from cafe_car.alert_enums import (
    AlertCause,
    AlertEffect,
    AlertSeverity,
)

_url_validator = TypeAdapter(AnyHttpUrl)

# The same shape ``Feed.feed_name`` enforces. Restated rather than imported:
# SQLModel skips validators on ``table=True`` models, so the model's own
# validator never runs on a write and this is the only thing standing between a
# request body and the column.
_FEED_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")


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


class GtfsUploadOut(BaseModel):
    """One uploaded schedule zip, as the feed page's history shows it.

    ``object_key`` is not here. It is where the bytes are in the bucket, which
    is nobody's business outside the two apps that read it; the client works in
    upload ids and in the public URL.
    """

    id: str
    sha256: str
    size_bytes: int
    original_filename: str
    uploaded_by_user_id: int | None
    uploaded_at: datetime
    # Whether this is the upload the feed is currently serving. The one thing
    # a client would otherwise have to derive by comparing ids.
    is_current: bool


class FeedOut(BaseModel):
    id: int
    feed_name: str
    # 'url' or 'hosted'. Mirrors `FeedSourceKind`; a string here because the
    # client switches on it and an enum would serialize the same anyway.
    source_kind: str
    # Null on a hosted feed, which has no upstream URL to show.
    static_feed_url: str | None
    # Where a consumer downloads the schedule: our own permanent URL when
    # hosted, the upstream one when not.
    hosted_url: str | None
    # The upload being served, or null on a url-sourced feed.
    current_upload: GtfsUploadOut | None
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


class FeedCreate(BaseModel):
    """A new feed. The owner is the caller and is never taken from the body.

    A hosted feed is created empty and its zip is uploaded afterwards, so
    ``static_feed_url`` is required for a url feed and refused for a hosted
    one. The check is on the model rather than in the route because it is a
    fact about the pair of fields, not about who is asking.
    """

    feed_name: str
    source_kind: str = FeedSourceKind.url
    static_feed_url: str | None = None

    @field_validator("source_kind")
    @classmethod
    def check_source_kind(cls, v: str) -> str:
        if v not in tuple(FeedSourceKind):
            allowed = ", ".join(FeedSourceKind)
            raise ValueError(f"source_kind must be one of: {allowed}")
        return v

    @model_validator(mode="after")
    def check_source(self) -> FeedCreate:
        if self.source_kind == FeedSourceKind.url and not self.static_feed_url:
            raise ValueError("A linked feed needs a static feed URL")
        if self.source_kind == FeedSourceKind.hosted and self.static_feed_url:
            raise ValueError("A hosted feed has no static feed URL")
        return self

    @field_validator("feed_name")
    @classmethod
    def check_name(cls, v: str) -> str:
        if not _FEED_NAME_RE.match(v):
            raise ValueError(
                "feed_name must start with a lowercase letter and contain only "
                "lowercase letters, digits, underscores and hyphens (3-64 chars)"
            )
        return v

    @field_validator("static_feed_url")
    @classmethod
    def check_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            _url_validator.validate_python(v)
        except Exception:
            raise ValueError("Must be a valid http or https URL") from None
        # Returned as typed, not as pydantic re-serializes it: `AnyHttpUrl`
        # appends a trailing slash to a bare host, which would silently rewrite
        # the URL somebody pasted.
        return v


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


# ─── What a write is allowed to say ──────────────────────────────────────────
# Request bodies are as narrow as the response ones, and for the same reason:
# a field that is not here cannot be set by a crafted POST. Ownership, ids and
# `device_key` are all absent from every model below, so none of them is
# settable from a body.


class FeedUpdate(BaseModel):
    """An edit to a feed. Absent means unchanged, which is what PATCH means.

    ``owner_id`` is not here. Ownership moves through ``/transfer`` alone,
    which checks that the recipient is already a member.
    """

    feed_name: str | None = None
    source_kind: str | None = None
    static_feed_url: str | None = None

    _check_name = field_validator("feed_name")(FeedCreate.check_name.__func__)  # type: ignore[attr-defined]
    _check_url = field_validator("static_feed_url")(FeedCreate.check_url.__func__)  # type: ignore[attr-defined]
    _check_kind = field_validator("source_kind")(FeedCreate.check_source_kind.__func__)  # type: ignore[attr-defined]


class FeedTransfer(BaseModel):
    """Hand a feed to one of its members, who must already be one."""

    new_owner_id: int


class TrackerCreate(BaseModel):
    """A new tracker.

    ``device_key`` is optional and settable **at creation only**, matching the
    admin this replaces: it is baked into the provisioned Traccar device, so
    changing it afterwards would leave the device answering for a credential
    the row no longer holds. Left out, the model generates a pet-name one.
    """

    nickname: str
    device_key: str | None = None

    @field_validator("nickname")
    @classmethod
    def check_nickname(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("A tracker needs a nickname")
        if len(v) > 64:
            raise ValueError("A nickname is at most 64 characters")
        return v

    @field_validator("device_key")
    @classmethod
    def check_device_key(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 64:
            raise ValueError("A device key is at most 64 characters")
        return v


class TrackerBulkCreate(BaseModel):
    """Several trackers at once, named ``{prefix}{n}``.

    The prefix is used exactly as typed, separator included, so ``bus-`` gives
    ``bus-1``. Numbering continues past whatever the feed already has under
    that prefix rather than colliding with it: ``(feed_id, nickname)`` is
    unique, and a bulk create is the easiest way to trip over that.
    """

    prefix: str
    count: int

    @field_validator("prefix")
    @classmethod
    def check_prefix(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("A prefix is needed to name the trackers")
        if len(v) > 48:
            raise ValueError("A prefix is at most 48 characters")
        return v


class TrackerUpdate(BaseModel):
    """A rename, and nothing else.

    ``id`` is the Redis key and the ``tracker_rule`` foreign key; ``device_key``
    is baked into the Traccar device. Both are immutable after creation, so
    neither appears here.
    """

    nickname: str

    _check_nickname = field_validator("nickname")(TrackerCreate.check_nickname.__func__)  # type: ignore[attr-defined]


class ProvisioningOut(BaseModel):
    """What a phone needs to start reporting as this tracker.

    Every field is derived from ``device_key`` and is therefore just as secret:
    the config URL contains it in a query parameter and the QR encodes that URL.
    Served by the provisioning route alone, and shown in the properties panel
    alone.
    """

    device_key: str
    config_url: str
    qr_svg: str


class AlertWrite(BaseModel):
    """The editable half of a service alert.

    ``cause``, ``effect`` and ``severity_level`` are the GTFS-RT enumerations
    by name, shared with the ingest API so both writers agree on what a feed may
    publish. The active period is two instants; a naive datetime is read as UTC,
    because that is what the columns store and what the old admin coerced to.
    """

    header_text: str
    description_text: str
    url: str | None = None
    cause: AlertCause | None = None
    effect: AlertEffect | None = None
    severity_level: AlertSeverity | None = None
    active_period_start: datetime | None = None
    active_period_end: datetime | None = None

    @field_validator("header_text", "description_text")
    @classmethod
    def check_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("This field cannot be empty")
        return v

    @field_validator("url")
    @classmethod
    def check_url(cls, v: str | None) -> str | None:
        if not v:
            return None
        try:
            _url_validator.validate_python(v)
        except Exception:
            raise ValueError("Must be a valid http or https URL") from None
        return v

    @field_validator("active_period_start", "active_period_end")
    @classmethod
    def as_utc(cls, v: datetime | None) -> datetime | None:
        # A `datetime-local` input has no zone. Stamping UTC here is what the
        # admin did, and it is the only reading that does not depend on which
        # machine the browser is on.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    @model_validator(mode="after")
    def check_window(self) -> AlertWrite:
        start, end = self.active_period_start, self.active_period_end
        if start is not None and end is not None and end < start:
            raise ValueError("The alert ends before it starts")
        return self


class InformedEntityWrite(BaseModel):
    """One entity selector on an alert.

    The check constraint the column carries is restated as a validator so a
    selector that names nothing is a 422 against the form rather than an
    IntegrityError. ``direction_id`` without a ``route_id`` is the second half
    of that rule and means nothing on its own.
    """

    agency_id: str | None = None
    route_id: str | None = None
    route_type: int | None = None
    direction_id: int | None = None
    stop_id: str | None = None
    trip_id: str | None = None
    trip_route_id: str | None = None
    trip_direction_id: int | None = None
    trip_start_time: str | None = None
    trip_start_date: str | None = None

    @field_validator("*")
    @classmethod
    def blank_is_absent(cls, v: object) -> object:
        # Every field is optional and the form posts empty strings for the ones
        # nobody filled in; an empty selector must be absent, not "".
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @model_validator(mode="after")
    def check_specifier(self) -> InformedEntityWrite:
        if not any(
            v is not None
            for v in (
                self.agency_id,
                self.route_id,
                self.route_type,
                self.direction_id,
                self.stop_id,
                self.trip_id,
            )
        ):
            raise ValueError(
                "An informed entity needs at least one of agency_id, route_id, "
                "route_type, direction_id, stop_id or trip_id"
            )
        if self.direction_id is not None and not self.route_id:
            raise ValueError("direction_id needs a route_id to mean anything")
        return self


class MemberAdd(BaseModel):
    """An address to share a feed with, which may not have an account yet."""

    email: str

    @field_validator("email")
    @classmethod
    def check_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("That is not an email address")
        return v


class ShareOut(BaseModel):
    """What sharing an address did.

    ``kind`` is ``member`` when the address already had a verified account and
    ``invited`` when it did not; the frontend shows ``message`` either way,
    because the difference is exactly what a person needs told.
    """

    kind: str
    message: str


class TrackerRuleWrite(BaseModel):
    """A recurrence rule, as the calendar writes it.

    ``start_time``/``end_time`` are seconds since service midnight, so an
    ``end_time`` past 86400 is a window running into the next calendar day and
    is the only way an overnight trip is expressible. They are never a clock
    time in a zone: the service date the window starts on is what carries the
    date, and that date is the trip's GTFS-RT ``start_date``.

    No weekday is required. A rule with every flag false and a single ``added``
    exception is a one-off assignment, which is a thing the calendar has to be
    able to write.
    """

    trip_id: str
    monday: bool = False
    tuesday: bool = False
    wednesday: bool = False
    thursday: bool = False
    friday: bool = False
    saturday: bool = False
    sunday: bool = False
    start_date: date
    end_date: date | None = None
    start_time: int
    end_time: int

    @field_validator("trip_id")
    @classmethod
    def check_trip_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("A rule needs a trip")
        if len(v) > 256:
            raise ValueError("A trip id is at most 256 characters")
        return v

    @model_validator(mode="after")
    def check_window(self) -> TrackerRuleWrite:
        if self.start_time < 0:
            raise ValueError("A start time cannot be before service midnight")
        if self.end_time <= self.start_time:
            raise ValueError("The window ends before it starts")
        # Two service days is the most the resolver ever evaluates, so a longer
        # window could never be matched in full and is a typo rather than a run.
        if self.end_time > 2 * 86400:
            raise ValueError("A window is at most 48 hours long")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("The rule ends before it starts")
        return self


class RuleExceptionWrite(BaseModel):
    """One service date added to or removed from a rule.

    ``added`` makes the rule run on a date its weekday flags exclude, and
    ``removed`` cancels it on one they include. Neither reaches outside the
    rule's date range, which is what "this rule is over" means.
    """

    date: date
    exception_type: ExceptionType

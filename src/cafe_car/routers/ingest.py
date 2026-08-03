"""Service-to-service ingest API.

Direct HTTP seam for producers that already know their own `trip_id` (Amtrak via
hell-gate-bridge, and simulate_trip.py) — as opposed to the Traccar shim, which
resolves the trip server-side. Writes the *exact* `vehicle:*` record shape that
`gtfs_rt.py` and the vehicle-poser shim use, with the same 60s TTL, so the serving
code needs zero changes.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, model_validator
from railroad_club.models.informed_entity import InformedEntity
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.tracker import Tracker
from sqlmodel import delete, select

from cafe_car.database import get_session
from cafe_car.settings import get_settings

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()

AlertCause = Literal[
    "UNKNOWN_CAUSE",
    "OTHER_CAUSE",
    "TECHNICAL_PROBLEM",
    "STRIKE",
    "DEMONSTRATION",
    "ACCIDENT",
    "HOLIDAY",
    "WEATHER",
    "MAINTENANCE",
    "CONSTRUCTION",
    "POLICE_ACTIVITY",
    "MEDICAL_EMERGENCY",
]
AlertEffect = Literal[
    "NO_SERVICE",
    "REDUCED_SERVICE",
    "SIGNIFICANT_DELAYS",
    "DETOUR",
    "ADDITIONAL_SERVICE",
    "MODIFIED_SERVICE",
    "OTHER_EFFECT",
    "UNKNOWN_EFFECT",
    "STOP_MOVED",
    "NO_EFFECT",
    "ACCESSIBILITY_ISSUE",
]
AlertSeverity = Literal["UNKNOWN_SEVERITY", "INFO", "WARNING", "SEVERE"]

# GTFS-RT VehicleStopStatus, by name. Every one of them names a stop — the
# vehicle is approaching, sitting at, or heading to *that* stop — so a status is
# only meaningful alongside a stop reference (see PositionIngest below).
VehicleStopStatus = Literal["INCOMING_AT", "STOPPED_AT", "IN_TRANSIT_TO"]

# Must match the vehicle-poser shim's TTL — cafe-car serves whatever is live.
POSITION_TTL = 60
# Trip-update predictions live longer than a single fix — a prediction is valid
# even if the next position hasn't landed yet. Still bounded so stale trips age
# out once a producer stops publishing.
TRIP_UPDATE_TTL = 300


class PositionIngest(BaseModel):
    # The secret tracker credential (Tracker.id). Selects the
    # `vehicle:{tracker_id}:*` namespace cafe-car scans; never emitted in a feed.
    tracker_id: str
    # Public per-vehicle identity. One tracker credential can fan out to many
    # concurrent vehicles (e.g. Amtrak's ~53 trains under one credential), so the
    # producer — which knows the real vehicle — supplies its GTFS
    # VehicleDescriptor.id/label here. Absent for single-device producers, which
    # fall back to the tracker nickname at serialisation.
    vehicle_id: str | None = None
    vehicle_label: str | None = None
    trip_id: str
    lat: float
    lon: float
    bearing: float | None = None
    speed: float | None = None  # metres/second (the vehicle:* contract)
    timestamp: int  # epoch seconds
    route_id: str | None = None
    # GTFS-RT service date (YYYYMMDD). A >24h daily trip (Amtrak long-distance)
    # has several instances of the same trip_id en route at once; start_date is
    # what tells them apart — without it they collide on one Redis key.
    start_date: str | None = None
    # Where the vehicle is *now*, along its trip. Without this a consumer can
    # draw the vehicle on a map but cannot place it against the schedule, so a
    # producer that knows its current stop should always send it.
    #
    # `current_status` describes the vehicle's relationship to the stop named by
    # `current_stop_sequence`/`stop_id`, so it is only accepted together with
    # one of them — a bare status names nothing. All three absent is fine: it
    # means "not reported", which is what the feed will then say.
    current_stop_sequence: int | None = None
    stop_id: str | None = None
    current_status: VehicleStopStatus | None = None

    @model_validator(mode="after")
    def _status_needs_a_stop(self) -> PositionIngest:
        if self.current_status is not None and (
            self.current_stop_sequence is None and self.stop_id is None
        ):
            raise ValueError(
                "current_status requires current_stop_sequence or stop_id — "
                "the status describes the vehicle's relationship to that stop"
            )
        return self


class StopTimeUpdateIngest(BaseModel):
    # Identify the stop by id or sequence (at least one; GTFS-RT accepts either).
    stop_id: str | None = None
    stop_sequence: int | None = None
    # Absolute epoch time and/or a delay in seconds, per event. Both are emitted
    # when both are present (see gtfs_rt.py::_fill_stop_time_update).
    arrival_time: int | None = None
    arrival_delay: int | None = None
    departure_time: int | None = None
    departure_delay: int | None = None
    schedule_relationship: str | None = None  # default SCHEDULED


class TripUpdateIngest(BaseModel):
    trip_id: str
    tracker_id: str
    # Public per-vehicle identity (see PositionIngest); emitted as the
    # TripUpdate's VehicleDescriptor.id, falling back to the tracker nickname.
    vehicle_id: str | None = None
    vehicle_label: str | None = None
    timestamp: int  # epoch seconds
    stop_time_updates: list[StopTimeUpdateIngest]
    start_date: str | None = None  # see PositionIngest.start_date


class AlertEntityIngest(BaseModel):
    agency_id: str | None = None
    route_id: str | None = None
    stop_id: str | None = None

    @model_validator(mode="after")
    def _has_specifier(self) -> AlertEntityIngest:
        if not (self.agency_id or self.route_id or self.stop_id):
            raise ValueError(
                "an alert entity needs at least one of agency_id/route_id/stop_id"
            )
        return self


class AlertIngest(BaseModel):
    header_text: str
    description_text: str
    url: str | None = None
    # Restricted to the exact GTFS-RT enum names — gtfs_rt.py's
    # service_alerts serialiser calls Alert.Cause/Effect/SeverityLevel.Value()
    # on these at feed-build time, so an invalid string would 500 the feed
    # instead of failing fast here at ingest.
    cause: AlertCause | None = None
    effect: AlertEffect | None = None
    severity_level: AlertSeverity | None = None
    active_period_start: int | None = None  # epoch seconds
    active_period_end: int | None = None  # epoch seconds
    entities: list[AlertEntityIngest]

    @model_validator(mode="after")
    def _has_entity(self) -> AlertIngest:
        if not self.entities:
            raise ValueError("an alert needs at least one informed entity")
        return self


class AlertsSyncIngest(BaseModel):
    # Same secret tracker credential as position/trip-update ingest; resolved
    # to a feed_id server-side so the producer never needs to know it.
    tracker_id: str
    alerts: list[AlertIngest]


def _vehicle_key(tracker_id: str, trip_id: str, start_date: str | None) -> str:
    # Appending start_date (when present) gives concurrent instances of one
    # long-running daily trip distinct keys. The `vehicle:{tracker_id}:*` scan in
    # gtfs_rt.py still matches.
    slug = f"{trip_id}:{start_date}" if start_date else trip_id
    return f"vehicle:{tracker_id}:{slug}"


def _trip_update_key(trip_id: str, start_date: str | None) -> str:
    if start_date:
        return f"trip_update:{trip_id}:{start_date}"
    return f"trip_update:{trip_id}"


def _check_auth(authorization: str | None) -> None:
    token = get_settings().ingest_api_token
    if not token:
        # No token configured → ingest is closed, not open.
        raise HTTPException(status_code=403, detail="Ingest not configured")
    expected = f"Bearer {token}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=403, detail="Invalid ingest token")


@router.post("/ingest/position")
async def ingest_position(
    body: PositionIngest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _check_auth(authorization)

    # Byte-for-byte the record the vehicle-poser shim writes; keys read by
    # gtfs_rt.py::vehicle_positions (tracker_id, trip_id, lat, lon, bearing,
    # speed, timestamp, optional route_id, optional public vehicle_id/label,
    # optional current_stop_sequence/stop_id/current_status). Producers that
    # predate a key simply omit it — the serialiser reads with .get().
    record: dict[str, object] = {
        "tracker_id": body.tracker_id,
        "trip_id": body.trip_id,
        "lat": body.lat,
        "lon": body.lon,
        "bearing": body.bearing,
        "speed": body.speed,
        "timestamp": body.timestamp,
    }
    if body.route_id:
        record["route_id"] = body.route_id
    if body.start_date:
        record["start_date"] = body.start_date
    if body.vehicle_id:
        record["vehicle_id"] = body.vehicle_id
    if body.vehicle_label:
        record["vehicle_label"] = body.vehicle_label
    # Sequence 0 is a legitimate GTFS stop_sequence, so test presence, not truth.
    if body.current_stop_sequence is not None:
        record["current_stop_sequence"] = body.current_stop_sequence
    if body.stop_id:
        record["stop_id"] = body.stop_id
    if body.current_status:
        record["current_status"] = body.current_status

    key = _vehicle_key(body.tracker_id, body.trip_id, body.start_date)
    await request.app.state.redis.setex(key, POSITION_TTL, json.dumps(record))
    return {"status": "ok"}


@router.post("/ingest/trip-update")
async def ingest_trip_update(
    body: TripUpdateIngest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _check_auth(authorization)

    # Rich, multi-stop record read by gtfs_rt.py::trip_updates. Supersedes the
    # old single-`delay` shape trip-updogger wrote — producers (Amtrak via
    # hell-gate, simulate_trip.py) now supply per-stop predictions directly.
    record = {
        "trip_id": body.trip_id,
        "tracker_id": body.tracker_id,
        "timestamp": body.timestamp,
        "stop_time_updates": [
            stu.model_dump(exclude_none=True) for stu in body.stop_time_updates
        ],
    }
    if body.start_date:
        record["start_date"] = body.start_date
    if body.vehicle_id:
        record["vehicle_id"] = body.vehicle_id
    if body.vehicle_label:
        record["vehicle_label"] = body.vehicle_label
    key = _trip_update_key(body.trip_id, body.start_date)
    await request.app.state.redis.setex(key, TRIP_UPDATE_TTL, json.dumps(record))
    return {"status": "ok"}


@router.post("/ingest/alerts")
async def ingest_alerts(
    body: AlertsSyncIngest,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, str | int]:
    _check_auth(authorization)

    tracker = await session.get(Tracker, body.tracker_id)
    if tracker is None:
        raise HTTPException(status_code=403, detail="Invalid ingest token")
    feed_id = tracker.feed_id

    # Full replace: each sync carries the producer's complete current alert
    # set, so a stale alert (removed upstream) is cleared automatically on
    # the next cycle rather than needing separate expiry logic. No DB-level
    # cascade exists on the FK, so entities must be deleted before alerts.
    existing_ids = (
        await session.exec(
            select(ServiceAlert.id).where(ServiceAlert.feed_id == feed_id)
        )
    ).all()
    if existing_ids:
        await session.exec(
            delete(InformedEntity).where(
                InformedEntity.service_alert_id.in_(existing_ids)  # type: ignore[union-attr]
            )
        )
        await session.exec(delete(ServiceAlert).where(ServiceAlert.feed_id == feed_id))

    for alert_in in body.alerts:
        alert = ServiceAlert(
            feed_id=feed_id,
            header_text=alert_in.header_text,
            description_text=alert_in.description_text,
            url=alert_in.url,
            cause=alert_in.cause,
            effect=alert_in.effect,
            severity_level=alert_in.severity_level,
            active_period_start=(
                datetime.fromtimestamp(alert_in.active_period_start, tz=UTC)
                if alert_in.active_period_start is not None
                else None
            ),
            active_period_end=(
                datetime.fromtimestamp(alert_in.active_period_end, tz=UTC)
                if alert_in.active_period_end is not None
                else None
            ),
        )
        session.add(alert)
        await session.flush()  # populate alert.id for the entities below
        for entity_in in alert_in.entities:
            session.add(
                InformedEntity(
                    service_alert_id=alert.id,
                    agency_id=entity_in.agency_id,
                    route_id=entity_in.route_id,
                    stop_id=entity_in.stop_id,
                )
            )

    await session.commit()
    return {"status": "ok", "count": len(body.alerts)}

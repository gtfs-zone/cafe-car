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
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, model_validator

from cafe_car.settings import get_settings

router = APIRouter()

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

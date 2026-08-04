import json
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from google.protobuf import json_format
from google.transit import gtfs_realtime_pb2
from railroad_club.models.feed import Feed
from railroad_club.models.service_alert import ServiceAlert
from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from cafe_car.alerts import active_alerts, to_utc
from cafe_car.database import get_session

router = APIRouter()

PROTOBUF_CONTENT_TYPE = "application/x-protobuf"
JSON_CONTENT_TYPE = "application/json"


def _public_vehicle_id(
    tracker_nickname: str,
    public_id: str | None,
    trip_id: str | None,
    start_date: str | None,
) -> str:
    """The GTFS-RT `VehicleDescriptor.id` for one vehicle record.

    A producer's own `vehicle_id` is trusted when given, but a producer with no
    concept of a public per-vehicle id (or one that forgets to set it, which bit
    a buswhere feed that ran several devices under one tracker credential) must
    not collapse every such vehicle onto the bare tracker nickname: GTFS-RT
    requires this id "unique per vehicle", and two concurrent vehicles sharing a
    tracker would otherwise share this id too. Folding in the trip instance
    (trip_id + start_date, the same disambiguator used for `entity.id`) restores
    uniqueness for any concurrently-running vehicles, without requiring every
    producer to invent its own scheme.
    """
    if public_id:
        return public_id
    if trip_id:
        instance = f"{trip_id}:{start_date}" if start_date else trip_id
        return f"{tracker_nickname}:{instance}"
    return tracker_nickname


async def get_feed(
    feed_name: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Feed:
    result = await session.exec(select(Feed).where(Feed.feed_name == feed_name))
    feed = result.first()
    if feed is None:
        raise HTTPException(status_code=404, detail=f"Feed '{feed_name}' not found")
    return feed


async def _build_trip_updates_feed(
    feed: Feed, session: AsyncSession, redis: object
) -> gtfs_realtime_pb2.FeedMessage:
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    msg.header.timestamp = int(time.time())

    from railroad_club.models.tracker import Tracker

    result = await session.exec(select(Tracker).where(Tracker.feed_id == feed.id))
    trackers = result.all()

    # A >24h daily trip has several instances of the same trip_id live at once,
    # distinguished by start_date, so dedup on the pair, not trip_id alone.
    seen: set[tuple[str, str | None]] = set()
    for tracker in trackers:
        async for key in redis.scan_iter(f"vehicle:{tracker.id}:*"):
            vehicle_raw = await redis.get(key)
            if vehicle_raw is None:
                continue
            vehicle_data = json.loads(vehicle_raw)
            trip_id = vehicle_data.get("trip_id")
            start_date = vehicle_data.get("start_date")
            if not trip_id or (trip_id, start_date) in seen:
                continue

            tu_key = (
                f"trip_update:{trip_id}:{start_date}"
                if start_date
                else f"trip_update:{trip_id}"
            )
            trip_raw = await redis.get(tu_key)
            if trip_raw is None:
                continue
            trip_data = json.loads(trip_raw)

            seen.add((trip_id, start_date))
            entity = msg.entity.add()
            # Stable per trip-instance across polls; it is exactly the dedup key
            # and carries no secret (unlike a scan-order counter, which reshuffled
            # between polls and was unusable as a focus key).
            entity.id = f"{trip_id}:{start_date}" if start_date else trip_id
            entity.trip_update.trip.trip_id = trip_data["trip_id"]
            entity.trip_update.trip.schedule_relationship = (
                gtfs_realtime_pb2.TripDescriptor.SCHEDULED
            )
            if start_date:
                entity.trip_update.trip.start_date = start_date
            # Public per-vehicle id, never the secret tracker id.
            entity.trip_update.vehicle.id = _public_vehicle_id(
                tracker.nickname, trip_data.get("vehicle_id"), trip_id, start_date
            )
            entity.trip_update.timestamp = trip_data["timestamp"]
            for update in _stop_time_updates(trip_data):
                stu = entity.trip_update.stop_time_update.add()
                _fill_stop_time_update(stu, update)

    return msg


@router.get("/{feed_name}/trip_updates.pb")
async def trip_updates(
    request: Request,
    feed: Feed = Depends(get_feed),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    msg = await _build_trip_updates_feed(feed, session, request.app.state.redis)
    return Response(content=msg.SerializeToString(), media_type=PROTOBUF_CONTENT_TYPE)


@router.get("/{feed_name}/trip_updates.json")
async def trip_updates_json(
    request: Request,
    feed: Feed = Depends(get_feed),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    msg = await _build_trip_updates_feed(feed, session, request.app.state.redis)
    return Response(
        content=json_format.MessageToJson(msg), media_type=JSON_CONTENT_TYPE
    )


_SR_ENUM = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate


def _stop_time_updates(trip_data: dict) -> list[dict]:
    """Return the per-stop update list, tolerating the legacy single-delay shape."""
    updates = trip_data.get("stop_time_updates")
    if updates:
        return updates
    # Backward-compat: the old trip-updogger record carried one flat delay.
    if "delay" in trip_data:
        return [
            {
                "stop_sequence": trip_data.get("stop_sequence"),
                "arrival_delay": trip_data["delay"],
            }
        ]
    return []


def _fill_stop_time_update(stu: object, update: dict) -> None:
    if update.get("stop_id") is not None:
        stu.stop_id = update["stop_id"]
    if update.get("stop_sequence") is not None:
        stu.stop_sequence = update["stop_sequence"]
    # Time and delay are independent: GTFS-RT permits both in one StopTimeEvent,
    # and dropping the delay when a time is present left consumers unable to show
    # lateness at all.
    if update.get("arrival_time") is not None:
        stu.arrival.time = update["arrival_time"]
    if update.get("arrival_delay") is not None:
        stu.arrival.delay = update["arrival_delay"]
    if update.get("departure_time") is not None:
        stu.departure.time = update["departure_time"]
    if update.get("departure_delay") is not None:
        stu.departure.delay = update["departure_delay"]
    sr = update.get("schedule_relationship")
    stu.schedule_relationship = _SR_ENUM.Value(sr) if sr else _SR_ENUM.SCHEDULED


async def _build_vehicle_positions_feed(
    feed: Feed, session: AsyncSession, redis: object
) -> gtfs_realtime_pb2.FeedMessage:
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    msg.header.timestamp = int(time.time())

    from railroad_club.models.tracker import Tracker

    result = await session.exec(select(Tracker).where(Tracker.feed_id == feed.id))
    trackers = result.all()

    for tracker in trackers:
        async for key in redis.scan_iter(f"vehicle:{tracker.id}:*"):
            raw = await redis.get(key)
            if raw is None:
                continue
            data = json.loads(raw)

            trip_id = data.get("trip_id")
            start_date = data.get("start_date")
            entity = msg.entity.add()
            # entity.id must be unique within the message, stable across polls, and
            # free of the secret tracker id, and per GTFS-RT, so must the actual
            # VehicleDescriptor.id below, so both share this derivation. One tracker
            # credential can carry many concurrent vehicles (Amtrak's fleet under
            # one id), so the tracker nickname is only a fallback for single-device
            # producers, folded with the trip instance when there are several.
            public_id = data.get("vehicle_id")
            vehicle_id = _public_vehicle_id(
                tracker.nickname, public_id, trip_id, start_date
            )
            entity.id = vehicle_id
            # Public label only (the tracker id is the secret credential).
            entity.vehicle.vehicle.id = vehicle_id
            entity.vehicle.vehicle.label = (
                data.get("vehicle_label") or public_id or tracker.nickname
            )
            entity.vehicle.position.latitude = data["lat"]
            entity.vehicle.position.longitude = data["lon"]
            if data["bearing"] is not None:
                entity.vehicle.position.bearing = data["bearing"]
            if data["speed"] is not None:
                entity.vehicle.position.speed = data["speed"]
            if trip_id is not None:
                # Only emit a TripDescriptor when the vehicle is tied to a trip.
                # A tracker with no active rule resolves trip_id to None; that is
                # a valid position (GTFS-RT trip is optional) and must not crash
                # the whole feed by assigning None to a protobuf string field.
                entity.vehicle.trip.trip_id = trip_id
                entity.vehicle.trip.schedule_relationship = (
                    gtfs_realtime_pb2.TripDescriptor.SCHEDULED
                )
                # start_date disambiguates concurrent instances of a >24h daily
                # trip (see ingest.py); pass it through so consumers can too.
                if start_date:
                    entity.vehicle.trip.start_date = start_date
                if route_id := data.get("route_id"):
                    entity.vehicle.trip.route_id = route_id
            # Where the vehicle is along its trip. current_status names the stop
            # in current_stop_sequence/stop_id, so all three are emitted together
            # or not at all; this used to hardcode IN_TRANSIT_TO with no stop
            # reference, which says nothing and left consumers unable to place
            # the vehicle against the schedule. A producer that reports no
            # current stop leaves the fields absent: current_status is a proto2
            # field defaulting to IN_TRANSIT_TO, so absent on the wire is the
            # honest "not reported" a presence-checking consumer can see.
            current_stop_sequence = data.get("current_stop_sequence")
            stop_id = data.get("stop_id")
            if current_stop_sequence is not None or stop_id:
                if current_stop_sequence is not None:
                    entity.vehicle.current_stop_sequence = current_stop_sequence
                if stop_id:
                    entity.vehicle.stop_id = stop_id
                if status := data.get("current_status"):
                    entity.vehicle.current_status = (
                        gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus.Value(
                            status
                        )
                    )
            entity.vehicle.timestamp = data["timestamp"]

    return msg


@router.get("/{feed_name}/vehicle_positions.pb")
async def vehicle_positions(
    request: Request,
    feed: Feed = Depends(get_feed),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    msg = await _build_vehicle_positions_feed(feed, session, request.app.state.redis)
    return Response(content=msg.SerializeToString(), media_type=PROTOBUF_CONTENT_TYPE)


@router.get("/{feed_name}/vehicle_positions.json")
async def vehicle_positions_json(
    request: Request,
    feed: Feed = Depends(get_feed),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    msg = await _build_vehicle_positions_feed(feed, session, request.app.state.redis)
    return Response(
        content=json_format.MessageToJson(msg), media_type=JSON_CONTENT_TYPE
    )


async def _build_service_alerts_feed(
    feed: Feed, session: AsyncSession
) -> gtfs_realtime_pb2.FeedMessage:
    now = datetime.now(UTC)

    result = await session.exec(
        select(ServiceAlert)
        .where(ServiceAlert.feed_id == feed.id)
        .options(selectinload(ServiceAlert.entities))
    )
    # Shared with the feed catalog, which reports `has_alerts`; the two must
    # never disagree about what this feed is publishing.
    published = active_alerts(result.all(), now)

    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    msg.header.timestamp = int(now.timestamp())

    for i, alert in enumerate(published, start=1):
        entity = msg.entity.add()
        entity.id = str(i)
        pb_alert = entity.alert

        if alert.active_period_start is not None or alert.active_period_end is not None:
            period = pb_alert.active_period.add()
            if alert.active_period_start is not None:
                period.start = int(to_utc(alert.active_period_start).timestamp())
            if alert.active_period_end is not None:
                period.end = int(to_utc(alert.active_period_end).timestamp())

        for ie in alert.entities:
            selector = pb_alert.informed_entity.add()
            if ie.agency_id:
                selector.agency_id = ie.agency_id
            if ie.route_id:
                selector.route_id = ie.route_id
            if ie.route_type is not None:
                selector.route_type = ie.route_type
            if ie.direction_id is not None:
                selector.direction_id = ie.direction_id
            if ie.stop_id:
                selector.stop_id = ie.stop_id
            if ie.trip_id or ie.trip_route_id:
                if ie.trip_id:
                    selector.trip.trip_id = ie.trip_id
                if ie.trip_route_id:
                    selector.trip.route_id = ie.trip_route_id
                if ie.trip_direction_id is not None:
                    selector.trip.direction_id = ie.trip_direction_id
                if ie.trip_start_time:
                    selector.trip.start_time = ie.trip_start_time
                if ie.trip_start_date:
                    selector.trip.start_date = ie.trip_start_date
                selector.trip.schedule_relationship = (
                    gtfs_realtime_pb2.TripDescriptor.SCHEDULED
                )

        if alert.cause:
            pb_alert.cause = gtfs_realtime_pb2.Alert.Cause.Value(alert.cause)
        if alert.effect:
            pb_alert.effect = gtfs_realtime_pb2.Alert.Effect.Value(alert.effect)
        if alert.severity_level:
            pb_alert.severity_level = gtfs_realtime_pb2.Alert.SeverityLevel.Value(
                alert.severity_level
            )

        pb_alert.header_text.translation.add(text=alert.header_text, language="")
        pb_alert.description_text.translation.add(
            text=alert.description_text, language=""
        )
        if alert.url:
            pb_alert.url.translation.add(text=alert.url, language="")

    return msg


@router.get("/{feed_name}/service_alerts.pb")
async def service_alerts(
    feed: Feed = Depends(get_feed),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    msg = await _build_service_alerts_feed(feed, session)
    return Response(content=msg.SerializeToString(), media_type=PROTOBUF_CONTENT_TYPE)


@router.get("/{feed_name}/service_alerts.json")
async def service_alerts_json(
    feed: Feed = Depends(get_feed),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    msg = await _build_service_alerts_feed(feed, session)
    return Response(
        content=json_format.MessageToJson(msg), media_type=JSON_CONTENT_TYPE
    )

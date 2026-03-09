import json
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from google.transit import gtfs_realtime_pb2
from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.models.feed import Feed
from app.models.service_alert import ServiceAlert
from app.models.trip_alias import TripAlias

router = APIRouter()


async def _alias_map(feed_id: int, session: AsyncSession) -> dict[str, str]:
    result = await session.exec(select(TripAlias).where(TripAlias.feed_id == feed_id))
    return {a.alias: a.trip_id for a in result.all()}

PROTOBUF_CONTENT_TYPE = "application/x-protobuf"


async def get_feed(  # noqa: B008
    feed_name: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Feed:
    result = await session.exec(select(Feed).where(Feed.feed_name == feed_name))
    feed = result.first()
    if feed is None:
        raise HTTPException(status_code=404, detail=f"Feed '{feed_name}' not found")
    return feed


@router.get("/{feed_name}/trip_updates.pb")
async def trip_updates(
    request: Request,
    feed: Feed = Depends(get_feed),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    msg.header.timestamp = int(time.time())

    from app.models.driver import Driver

    result = await session.exec(select(Driver).where(Driver.feed_id == feed.id))
    drivers = result.all()

    redis = request.app.state.redis

    seen_trip_ids: set[str] = set()
    entity_id = 0
    for driver in drivers:
        vehicle_raw = await redis.get(f"vehicle:{driver.username}")
        if vehicle_raw is None:
            continue
        vehicle_data = json.loads(vehicle_raw)
        trip_id = vehicle_data.get("trip_id")
        if not trip_id or trip_id in seen_trip_ids:
            continue

        trip_raw = await redis.get(f"trip_update:{trip_id}")
        if trip_raw is None:
            continue
        trip_data = json.loads(trip_raw)

        seen_trip_ids.add(trip_id)
        entity_id += 1
        entity = msg.entity.add()
        entity.id = str(entity_id)
        entity.trip_update.trip.trip_id = trip_data["trip_id"]
        entity.trip_update.trip.schedule_relationship = (
            gtfs_realtime_pb2.TripDescriptor.SCHEDULED
        )
        entity.trip_update.vehicle.id = trip_data["vehicle_id"]
        entity.trip_update.timestamp = trip_data["timestamp"]
        stu = entity.trip_update.stop_time_update.add()
        stu.stop_sequence = trip_data["stop_sequence"]
        stu.arrival.delay = trip_data["delay"]
        stu.schedule_relationship = (
            gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SCHEDULED
        )

    return Response(content=msg.SerializeToString(), media_type=PROTOBUF_CONTENT_TYPE)


@router.get("/{feed_name}/vehicle_positions.pb")
async def vehicle_positions(
    request: Request,
    feed: Feed = Depends(get_feed),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    msg.header.timestamp = int(time.time())

    from app.models.driver import Driver

    result = await session.exec(select(Driver).where(Driver.feed_id == feed.id))
    drivers = result.all()

    redis = request.app.state.redis
    alias_map = await _alias_map(feed.id, session)

    entity_id = 0
    for driver in drivers:
        key = f"vehicle:{driver.username}"
        raw = await redis.get(key)
        if raw is None:
            continue
        data = json.loads(raw)

        entity_id += 1
        entity = msg.entity.add()
        entity.id = str(entity_id)
        entity.vehicle.vehicle.id = data["driver"]
        entity.vehicle.vehicle.label = data["driver"]
        entity.vehicle.position.latitude = data["lat"]
        entity.vehicle.position.longitude = data["lon"]
        entity.vehicle.position.bearing = data["bearing"]
        entity.vehicle.position.speed = data["speed"]
        entity.vehicle.trip.trip_id = alias_map.get(data["trip_id"], data["trip_id"])
        entity.vehicle.trip.schedule_relationship = gtfs_realtime_pb2.TripDescriptor.SCHEDULED
        if route_id := data.get("route_id"):
            entity.vehicle.trip.route_id = route_id
        entity.vehicle.current_status = gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO
        entity.vehicle.timestamp = data["timestamp"]

    return Response(content=msg.SerializeToString(), media_type=PROTOBUF_CONTENT_TYPE)


@router.get("/{feed_name}/service_alerts.pb")
async def service_alerts(
    feed: Feed = Depends(get_feed),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    now = datetime.now(UTC)

    result = await session.exec(
        select(ServiceAlert)
        .where(ServiceAlert.feed_id == feed.id)
        .options(selectinload(ServiceAlert.entities))
    )
    all_alerts = result.all()

    def _utc(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    active_alerts = [
        a for a in all_alerts
        if (a.active_period_start is None or _utc(a.active_period_start) <= now)
        and (a.active_period_end is None or _utc(a.active_period_end) > now)
    ]

    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    msg.header.timestamp = int(now.timestamp())

    # skip alerts with no informed entities
    active_alerts = [a for a in active_alerts if a.entities]

    for i, alert in enumerate(active_alerts, start=1):
        entity = msg.entity.add()
        entity.id = str(i)
        pb_alert = entity.alert

        if alert.active_period_start is not None or alert.active_period_end is not None:
            period = pb_alert.active_period.add()
            if alert.active_period_start is not None:
                period.start = int(_utc(alert.active_period_start).timestamp())
            if alert.active_period_end is not None:
                period.end = int(_utc(alert.active_period_end).timestamp())

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

    return Response(content=msg.SerializeToString(), media_type=PROTOBUF_CONTENT_TYPE)

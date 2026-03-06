import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from google.transit import gtfs_realtime_pb2
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.models.feed import Feed

router = APIRouter()

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
        entity.trip_update.trip.trip_id = trip_id
        entity.trip_update.delay = trip_data["delay"]
        entity.trip_update.timestamp = trip_data["timestamp"]

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
        entity.vehicle.trip.trip_id = data["trip_id"]
        if route_id := data.get("route_id"):
            entity.vehicle.trip.route_id = route_id
        entity.vehicle.current_status = gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO
        entity.vehicle.timestamp = data["timestamp"]

    return Response(content=msg.SerializeToString(), media_type=PROTOBUF_CONTENT_TYPE)


@router.get("/{feed_name}/service_alerts.pb")
async def service_alerts(feed: Feed = Depends(get_feed)) -> Response:  # noqa: B008
    return Response(content=b"", media_type=PROTOBUF_CONTENT_TYPE)

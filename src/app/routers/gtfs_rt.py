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
async def trip_updates(feed: Feed = Depends(get_feed)) -> Response:  # noqa: B008
    return Response(content=b"", media_type=PROTOBUF_CONTENT_TYPE)


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
    valid_drivers = {d.username for d in result.all()}

    redis = request.app.state.redis
    pattern = f"vehicle:{feed.feed_name}:*"

    entity_id = 0
    async for key in redis.scan_iter(pattern):
        driver_username = key.decode().split(":")[-1]
        if driver_username not in valid_drivers:
            continue
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
        entity.vehicle.trip.route_id = data["route_id"]
        entity.vehicle.current_status = gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO
        entity.vehicle.timestamp = data["timestamp"]

    return Response(content=msg.SerializeToString(), media_type=PROTOBUF_CONTENT_TYPE)


@router.get("/{feed_name}/service_alerts.pb")
async def service_alerts(feed: Feed = Depends(get_feed)) -> Response:  # noqa: B008
    return Response(content=b"", media_type=PROTOBUF_CONTENT_TYPE)

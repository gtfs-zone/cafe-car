from fastapi import APIRouter, Depends, HTTPException
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
async def vehicle_positions(feed: Feed = Depends(get_feed)) -> Response:  # noqa: B008
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    msg.header.timestamp = 0

    entity = msg.entity.add()
    entity.id = "sample-vehicle-1"
    entity.vehicle.vehicle.id = "BUS-001"
    entity.vehicle.vehicle.label = "Bus 001"
    entity.vehicle.position.latitude = 37.7749
    entity.vehicle.position.longitude = -122.4194
    entity.vehicle.position.bearing = 90.0
    entity.vehicle.position.speed = 12.5
    entity.vehicle.trip.trip_id = "sample-trip-1"
    entity.vehicle.trip.route_id = "sample-route-1"
    entity.vehicle.current_status = gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO

    return Response(content=msg.SerializeToString(), media_type=PROTOBUF_CONTENT_TYPE)


@router.get("/{feed_name}/service_alerts.pb")
async def service_alerts(feed: Feed = Depends(get_feed)) -> Response:  # noqa: B008
    return Response(content=b"", media_type=PROTOBUF_CONTENT_TYPE)

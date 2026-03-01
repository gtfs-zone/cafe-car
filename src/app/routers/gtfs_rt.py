from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()

PROTOBUF_CONTENT_TYPE = "application/x-protobuf"


@router.get("/{feed_name}/trip_updates.pb")
async def trip_updates(feed_name: str) -> Response:
    return Response(content=b"", media_type=PROTOBUF_CONTENT_TYPE)


@router.get("/{feed_name}/vehicle_positions.pb")
async def vehicle_positions(feed_name: str) -> Response:
    return Response(content=b"", media_type=PROTOBUF_CONTENT_TYPE)


@router.get("/{feed_name}/service_alerts.pb")
async def service_alerts(feed_name: str) -> Response:
    return Response(content=b"", media_type=PROTOBUF_CONTENT_TYPE)

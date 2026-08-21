"""``GET``/``HEAD /{feed_name}/gtfs.zip`` - a hosted feed's schedule.

Deliberately public and unauthenticated, like the ``.pb`` routes beside it.
This is the URL a feed consumer is given and the only one they ever see: the
storage host stays private, so the bucket can move without a published URL
changing.

Not a redirect to a presigned URL. A redirect would put the storage host in
somebody's address bar, expire, and defeat the conditional GET below, which is
what keeps the cost of proxying the bytes near zero: the ETag is the upload's
sha256, so a consumer polling hourly gets a 304 and the store is never asked.

A url-sourced feed answers 404 here rather than proxying somebody else's
download. Hosting is for zips somebody uploaded to us.
"""

from __future__ import annotations

from datetime import UTC
from email.utils import format_datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from railroad_club.models.feed import Feed
from railroad_club.models.gtfs_upload import GtfsUpload
from railroad_club.object_store import (
    ObjectNotFound,
    ObjectStoreError,
    get_async_object_store,
)
from sqlmodel import select

from cafe_car.database import get_session

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()

ZIP_CONTENT_TYPE = "application/zip"
# The bytes at a given ETag never change - a new upload is a new URL's worth of
# content under the same path - so a consumer may hold it for a while and is
# still told to check.
CACHE_CONTROL = "public, max-age=300"


async def _current_upload(feed_name: str, session: AsyncSession) -> GtfsUpload:
    """The upload this feed serves, or 404.

    One statement rather than a walk from `Feed`: the relationship is lazy and
    this session is closed by the time a response is built.
    """
    upload = (
        await session.exec(
            select(GtfsUpload)
            .join(Feed, Feed.current_upload_id == GtfsUpload.id)
            .where(Feed.feed_name == feed_name)
        )
    ).first()
    if upload is None:
        raise HTTPException(
            status_code=404, detail=f"Feed '{feed_name}' does not host a schedule"
        )
    return upload


def _headers(upload: GtfsUpload) -> dict[str, str]:
    # `uploaded_at` comes back aware from Postgres and naive from a backend
    # that does not keep the offset, and an HTTP date is always GMT, so it is
    # normalized here rather than trusted.
    uploaded_at = upload.uploaded_at
    uploaded_at = (
        uploaded_at.replace(tzinfo=UTC)
        if uploaded_at.tzinfo is None
        else uploaded_at.astimezone(UTC)
    )
    return {
        "ETag": f'"{upload.sha256}"',
        "Last-Modified": format_datetime(uploaded_at, usegmt=True),
        "Cache-Control": CACHE_CONTROL,
        # Names the feed rather than the upload id, so somebody saving it gets
        # a filename that means something.
        "Content-Disposition": 'attachment; filename="gtfs.zip"',
    }


def _not_modified(request: Request, upload: GtfsUpload) -> bool:
    """Whether the caller already has these bytes.

    ``If-None-Match`` may carry a list and may weaken each entry, so the tags
    are compared one by one rather than as a string.
    """
    header = request.headers.get("if-none-match")
    if not header:
        return False
    tags = {tag.strip().removeprefix("W/").strip('"') for tag in header.split(",")}
    return "*" in tags or upload.sha256 in tags


@router.get("/{feed_name}/gtfs.zip")
async def static_feed_zip(
    feed_name: str,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    upload = await _current_upload(feed_name, session)
    headers = _headers(upload)
    if _not_modified(request, upload):
        return Response(status_code=304, headers=headers)

    try:
        body = await get_async_object_store().get(upload.object_key)
    except ObjectNotFound:
        # The row says there are bytes and the store disagrees. Not a 404 the
        # caller can act on: the feed does host a schedule, and this is ours.
        raise HTTPException(
            status_code=500, detail="The stored schedule is missing"
        ) from None
    except ObjectStoreError:
        # Retryable, unlike the above, so it says so.
        raise HTTPException(
            status_code=503, detail="Object storage is unavailable"
        ) from None

    return Response(content=body, media_type=ZIP_CONTENT_TYPE, headers=headers)


@router.head("/{feed_name}/gtfs.zip")
async def static_feed_zip_head(
    feed_name: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """The same headers, without reading the object.

    ``Content-Length`` comes off the row, so a consumer checking size or
    freshness costs one query and no bytes out of the store.
    """
    upload = await _current_upload(feed_name, session)
    return Response(
        status_code=200,
        media_type=ZIP_CONTENT_TYPE,
        headers={**_headers(upload), "Content-Length": str(upload.size_bytes)},
    )

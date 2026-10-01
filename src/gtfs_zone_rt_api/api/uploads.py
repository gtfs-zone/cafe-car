"""Uploading, listing, activating and deleting a feed's schedule zips.

An uploaded feed is *hosted*: the zip lands in object storage, this app serves
it back at a permanent public URL (``routers/static_feed.py``), and
static-importer loads it from the store by key. Every upload is kept, so a bad
one is one click back rather than a re-download of something nobody has any
more.

Gated by ``AccessibleFeed``, not ``OwnedFeed``. A member may already repoint
``static_feed_url``, which is the same power over the same schedule; making
upload owner-only would be a distinction the old admin never drew.

The bytes are validated *before* a single one is stored. A zip that is not a
GTFS feed is a 422 naming ``file``, which is what puts the message under the
drop zone rather than in a toast. The required files are checked at the zip
root because that is where ``gtfs_zone_static_importer.gtfs_loader`` opens them: a feed
nested one directory down parses as empty rather than failing, which is the
worse outcome of the two.

Writes happen object-first: the key contains the upload id, which
``generate_upload_id`` hands out before the insert. An object with no row is
swept by the next retention pass or by the feed delete; a row with no object
would be a rollback target that 500s.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import zipfile
from typing import TYPE_CHECKING

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from gtfs_zone_db_models.models.gtfs_upload import (
    FeedSourceKind,
    GtfsUpload,
    generate_upload_id,
    object_key_for,
)
from gtfs_zone_db_models.object_store import ObjectStoreError, get_async_object_store
from sqlalchemy import select

from gtfs_zone_rt_api.api.deps import AccessibleFeed, CurrentUser, DBSession
from gtfs_zone_rt_api.api.schemas import GtfsUploadOut
from gtfs_zone_rt_api.feed_load import request_feed_load
from gtfs_zone_rt_api.settings import get_settings

if TYPE_CHECKING:
    from gtfs_zone_db_models.models.feed import Feed

router = APIRouter()

# What makes a zip a GTFS feed for the purpose of accepting it. Not a
# validation of the feed's contents: MobilityData's validator is a service, and
# pretending a function call could stand in for it would be worse than saying
# plainly that this only checks the files are there.
REQUIRED_FILES = (
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
)
# A feed needs at least one of these; a feed with neither has no service days.
CALENDAR_FILES = ("calendar.txt", "calendar_dates.txt")

# How much is read at a time while enforcing the cap. `UploadFile` spools to
# disk past its own threshold, so trusting `content-length` would let an
# oversized body through and a single `.read()` would materialize it.
CHUNK_BYTES = 1 << 20


def _reject(message: str) -> None:
    """A 422 shaped like pydantic's, so the form points at the drop zone.

    ``loc`` is what rt-manager's ``fieldErrors`` reads, and it takes the last
    element, so the field has to be named there rather than in the message.
    """
    raise HTTPException(
        status_code=422,
        detail=[{"loc": ["body", "file"], "msg": message, "type": "value_error"}],
    )


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """The whole body, or a 422 as soon as it is one byte too long."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            _reject(f"That file is larger than {max_bytes // (1 << 20)} MB")
        chunks.append(chunk)
    if not total:
        _reject("That file is empty")
    return b"".join(chunks)


def validate_gtfs_zip(data: bytes) -> None:
    """Reject anything that is not a GTFS feed, before it is stored."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        _reject("That file is not a zip archive")
        return

    if nested := [
        n for n in names if "/" in n and n.rsplit("/", 1)[1] in REQUIRED_FILES
    ]:
        directory = nested[0].rsplit("/", 1)[0]
        _reject(
            f"The .txt files are inside '{directory}/'. They have to be at the "
            "top level of the zip"
        )

    if missing := [name for name in REQUIRED_FILES if name not in names]:
        _reject(f"That zip is missing {', '.join(missing)}")

    if not names.intersection(CALENDAR_FILES):
        _reject("That zip has neither calendar.txt nor calendar_dates.txt")


def upload_out(upload: GtfsUpload, feed: Feed) -> GtfsUploadOut:
    return GtfsUploadOut(
        id=upload.id,
        sha256=upload.sha256,
        size_bytes=upload.size_bytes,
        original_filename=upload.original_filename,
        uploaded_by_user_id=upload.uploaded_by_user_id,
        uploaded_at=upload.uploaded_at,
        is_current=feed.current_upload_id == upload.id,
    )


async def _sweep(session: DBSession, feed: Feed) -> None:
    """Drop everything past the newest ``keep_uploads``, current one excepted.

    Runs after a successful upload rather than on a timer: the only thing that
    grows a feed's history is an upload, so that is the only moment the count
    can cross the line.
    """
    keep = get_settings().keep_uploads
    rows = (
        (
            await session.execute(
                select(GtfsUpload)
                .where(GtfsUpload.feed_id == feed.id)
                .order_by(GtfsUpload.uploaded_at.desc(), GtfsUpload.id.desc())
            )
        )
        .scalars()
        .all()
    )
    doomed = [u for u in rows[keep:] if u.id != feed.current_upload_id]
    if not doomed:
        return

    store = get_async_object_store()
    # Suppressed: the upload itself succeeded, and a store that cannot delete
    # must not turn a stored zip into a 503. The next sweep, or the feed
    # delete's prefix sweep, comes back for what is left.
    with contextlib.suppress(ObjectStoreError):
        for upload in doomed:
            await store.delete(upload.object_key)
            await session.delete(upload)
        await session.commit()


@router.post("/feeds/{feed_id}/uploads", status_code=201)
async def create_upload(
    feed: AccessibleFeed,
    user_id: CurrentUser,
    session: DBSession,
    file: UploadFile = File(),  # noqa: B008
) -> GtfsUploadOut:
    """Store a schedule zip, make it the feed's source, and queue the load."""
    settings = get_settings()
    data = await _read_capped(file, settings.max_gtfs_zip_bytes)
    validate_gtfs_zip(data)

    upload_id = generate_upload_id()
    key = object_key_for(feed.id, upload_id)
    try:
        await get_async_object_store().put(key, data)
    except ObjectStoreError as exc:
        # An unhandled one would be a plain-text 500, and a non-JSON answer is
        # how the frontend recognises an expired session; it would reload the
        # page instead of showing what went wrong.
        raise HTTPException(
            status_code=503, detail="Object storage is unavailable"
        ) from exc

    upload = GtfsUpload(
        id=upload_id,
        feed_id=feed.id,
        object_key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        original_filename=(file.filename or "gtfs.zip")[:255],
        uploaded_by_user_id=user_id,
    )
    session.add(upload)
    # The upload is only the feed's source once the row exists, so both halves
    # commit together: a feed pointing at a row that was rolled back would
    # serve a 404 at its own public URL.
    await session.flush()
    feed.source_kind = FeedSourceKind.hosted
    feed.static_feed_url = None
    feed.current_upload_id = upload.id
    session.add(feed)
    await session.commit()
    await session.refresh(upload)
    await session.refresh(feed)

    request_feed_load(feed.id)
    await _sweep(session, feed)

    return upload_out(upload, feed)


@router.get("/feeds/{feed_id}/uploads")
async def list_uploads(feed: AccessibleFeed, session: DBSession) -> list[GtfsUploadOut]:
    """This feed's upload history, newest first."""
    rows = (
        (
            await session.execute(
                select(GtfsUpload)
                .where(GtfsUpload.feed_id == feed.id)
                .order_by(GtfsUpload.uploaded_at.desc(), GtfsUpload.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [upload_out(upload, feed) for upload in rows]


async def _upload_of(session: DBSession, feed: Feed, upload_id: str) -> GtfsUpload:
    upload = await session.scalar(
        select(GtfsUpload).where(
            GtfsUpload.id == upload_id, GtfsUpload.feed_id == feed.id
        )
    )
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    return upload


@router.post("/feeds/{feed_id}/uploads/{upload_id}/activate")
async def activate_upload(
    upload_id: str, feed: AccessibleFeed, session: DBSession
) -> GtfsUploadOut:
    """Roll the feed back to an earlier upload.

    A pointer move *and* a load: the schedule tables still hold whatever the
    bad upload put there, so nothing has actually rolled back until
    static-importer has re-read the object.
    """
    upload = await _upload_of(session, feed, upload_id)
    feed.source_kind = FeedSourceKind.hosted
    feed.static_feed_url = None
    feed.current_upload_id = upload.id
    session.add(feed)
    await session.commit()
    await session.refresh(feed)

    request_feed_load(feed.id)
    return upload_out(upload, feed)


@router.delete("/feeds/{feed_id}/uploads/{upload_id}", status_code=204)
async def delete_upload(
    upload_id: str, feed: AccessibleFeed, session: DBSession
) -> Response:
    """Forget one upload, unless it is the one being served."""
    upload = await _upload_of(session, feed, upload_id)
    if feed.current_upload_id == upload.id:
        raise HTTPException(
            status_code=409, detail="That is the upload this feed is serving"
        )

    # Object first: a row with no object is a rollback target that 500s, and
    # deleting a key that is not there is not an error.
    try:
        await get_async_object_store().delete(upload.object_key)
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503, detail="Object storage is unavailable"
        ) from exc
    await session.delete(upload)
    await session.commit()
    return Response(status_code=204)

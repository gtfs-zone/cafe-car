"""Hosted feeds: the upload endpoints, and the public zip route.

Two apps are exercised here because hosting spans both. The admin app owns
writing an upload and the history around it; the public app owns serving it
back, unauthenticated, to a consumer who knows nothing but the feed name.

The store is faked rather than mocked out: the point of most of these tests is
*what ends up in the bucket* - that a rejected upload writes nothing, that a
deleted feed leaves nothing, that a swept upload takes its object with it - and
a mock that only records calls would let a leak through.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient, Response
from railroad_club.models.feed import Feed
from railroad_club.models.gtfs_upload import GtfsUpload
from railroad_club.object_store import ObjectNotFound
from sqlalchemy import func, select

from tests.factories import PROVIDER, add_member, make_feed, make_user

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlmodel.ext.asyncio.session import AsyncSession


class FakeStore:
    """``AsyncObjectStore``'s surface, over a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, body: bytes, *, content_type: str = "") -> None:
        self.objects[key] = body

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self.objects[key]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        doomed = [k for k in self.objects if k.startswith(prefix)]
        for key in doomed:
            del self.objects[key]
        return len(doomed)

    async def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]


FEED_FILES = (
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "calendar.txt",
)


def gtfs_zip(names: tuple[str, ...] = FEED_FILES, prefix: str = "") -> bytes:
    """A zip that passes the upload's shape check. The contents are not read."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name in names:
            zf.writestr(f"{prefix}{name}", "header\n")
    return buffer.getvalue()


def upload_body(data: bytes, filename: str = "feed.zip") -> dict:
    return {"files": {"file": (filename, data, "application/zip")}}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """One store, shared by every module that reaches for one."""
    fake = FakeStore()
    for module in (
        "cafe_car.api.uploads",
        "cafe_car.api.feeds",
        "cafe_car.routers.static_feed",
    ):
        monkeypatch.setattr(f"{module}.get_async_object_store", lambda: fake)
    return fake


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Feed ids handed to schedule-foamer, in order."""
    queued: list[int] = []
    monkeypatch.setattr(
        "cafe_car.api.uploads.request_feed_load", lambda feed_id: queued.append(feed_id)
    )
    return queued


def _token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _headers(subject: str, email: str) -> dict:
    return {"X-Auth-Request-User": subject, "X-Auth-Request-Email": email}


WRITE = {"X-Yard-Master": "1"}
OWNER = _headers("kc-owner", "owner@example.com")
MEMBER = _headers("kc-member", "member@example.com")
STRANGER = _headers("kc-stranger", "stranger@example.com")


@pytest.fixture
async def client(
    engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused:unused@db/unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused:6379/1")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("OIDC_PROVIDER", PROVIDER)

    import cafe_car.database as database
    from cafe_car.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(database, "_engine", engine)

    from cafe_car.admin_main import create_admin_app

    transport = ASGITransport(app=create_admin_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    get_settings.cache_clear()


@pytest.fixture
async def public(
    engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient]:
    """The public app. Nothing here is authenticated, which is the point."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused:unused@db/unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused:6379/1")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")

    import cafe_car.database as database
    from cafe_car.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(database, "_engine", engine)

    from cafe_car.main import create_public_app

    transport = ASGITransport(app=create_public_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    get_settings.cache_clear()


@pytest.fixture
async def world(session: AsyncSession) -> dict:
    """An owner, a member, a stranger, and one url-sourced feed."""
    owner = await make_user(session, email="owner@example.com", subject="kc-owner")
    member = await make_user(session, email="member@example.com", subject="kc-member")
    stranger = await make_user(
        session, email="stranger@example.com", subject="kc-stranger"
    )
    feed = await make_feed(session, owner, "owner-feed")
    await add_member(session, feed, member, added_by=owner)
    return {"owner": owner, "member": member, "stranger": stranger, "feed": feed}


async def _upload(client: AsyncClient, feed_id: int, data: bytes) -> Response:
    return await client.post(
        f"/api/feeds/{feed_id}/uploads", headers={**OWNER, **WRITE}, **upload_body(data)
    )


class TestUpload:
    async def test_stores_the_zip_and_hosts_the_feed(
        self,
        client: AsyncClient,
        session: AsyncSession,
        world: dict,
        store: FakeStore,
        loads: list[int],
    ) -> None:
        feed = world["feed"]
        response = await _upload(client, feed.id, gtfs_zip())

        assert response.status_code == 201
        body = response.json()
        assert body["original_filename"] == "feed.zip"
        assert body["is_current"] is True
        assert body["uploaded_by_user_id"] == world["owner"].id

        assert await store.list_prefix(f"feeds/{feed.id}/")
        stored = await store.get(f"feeds/{feed.id}/{body['id']}.zip")
        assert stored == gtfs_zip()

        await session.refresh(feed)
        assert feed.source_kind == "hosted"
        assert feed.static_feed_url is None
        assert feed.current_upload_id == body["id"]
        assert loads == [feed.id]

    async def test_a_member_may_upload(
        self, client: AsyncClient, world: dict, store: FakeStore, loads: list[int]
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/uploads",
            headers={**MEMBER, **WRITE},
            **upload_body(gtfs_zip()),
        )

        assert response.status_code == 201

    async def test_a_stranger_cannot(
        self, client: AsyncClient, world: dict, store: FakeStore
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/uploads",
            headers={**STRANGER, **WRITE},
            **upload_body(gtfs_zip()),
        )

        assert response.status_code == 404
        assert store.objects == {}

    async def test_a_file_that_is_not_a_zip_is_refused(
        self, client: AsyncClient, world: dict, store: FakeStore
    ) -> None:
        response = await _upload(client, world["feed"].id, b"this is not a zip")

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "file"]
        assert store.objects == {}

    async def test_a_zip_missing_a_required_file_is_refused(
        self, client: AsyncClient, world: dict, store: FakeStore
    ) -> None:
        without_stops = tuple(n for n in FEED_FILES if n != "stops.txt")
        response = await _upload(client, world["feed"].id, gtfs_zip(without_stops))

        assert response.status_code == 422
        assert "stops.txt" in response.json()["detail"][0]["msg"]
        assert store.objects == {}

    async def test_a_zip_with_neither_calendar_is_refused(
        self, client: AsyncClient, world: dict, store: FakeStore
    ) -> None:
        no_calendar = tuple(n for n in FEED_FILES if n != "calendar.txt")
        response = await _upload(client, world["feed"].id, gtfs_zip(no_calendar))

        assert response.status_code == 422
        assert store.objects == {}

    async def test_calendar_dates_alone_is_enough(
        self, client: AsyncClient, world: dict, store: FakeStore, loads: list[int]
    ) -> None:
        names = tuple(n for n in FEED_FILES if n != "calendar.txt")
        response = await _upload(
            client, world["feed"].id, gtfs_zip((*names, "calendar_dates.txt"))
        )

        assert response.status_code == 201

    async def test_a_nested_feed_is_refused(
        self, client: AsyncClient, world: dict, store: FakeStore
    ) -> None:
        """The loader opens `agency.txt` at the root, so a nested feed would
        load as empty rather than fail, which is the worse of the two."""
        response = await _upload(client, world["feed"].id, gtfs_zip(prefix="my-feed/"))

        assert response.status_code == 422
        assert "top level" in response.json()["detail"][0]["msg"]
        assert store.objects == {}

    async def test_a_file_over_the_cap_is_refused(
        self,
        client: AsyncClient,
        world: dict,
        store: FakeStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cafe_car.settings import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("MAX_GTFS_ZIP_BYTES", "1024")

        response = await _upload(client, world["feed"].id, b"x" * 4096)

        assert response.status_code == 422
        assert store.objects == {}

    async def test_an_empty_file_is_refused(
        self, client: AsyncClient, world: dict, store: FakeStore
    ) -> None:
        response = await _upload(client, world["feed"].id, b"")

        assert response.status_code == 422


class TestHistory:
    async def test_lists_newest_first_and_marks_the_current_one(
        self, client: AsyncClient, world: dict, store: FakeStore, loads: list[int]
    ) -> None:
        feed = world["feed"]
        first = (await _upload(client, feed.id, gtfs_zip())).json()
        second = (await _upload(client, feed.id, gtfs_zip())).json()

        response = await client.get(f"/api/feeds/{feed.id}/uploads", headers=OWNER)

        assert response.status_code == 200
        ids = [u["id"] for u in response.json()]
        assert ids == [second["id"], first["id"]]
        assert [u["is_current"] for u in response.json()] == [True, False]

    async def test_a_stranger_cannot_see_it(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/feeds/{world['feed'].id}/uploads", headers=STRANGER
        )

        assert response.status_code == 404

    async def test_activating_an_earlier_upload_rolls_back_and_reloads(
        self,
        client: AsyncClient,
        session: AsyncSession,
        world: dict,
        store: FakeStore,
        loads: list[int],
    ) -> None:
        feed = world["feed"]
        first = (await _upload(client, feed.id, gtfs_zip())).json()
        await _upload(client, feed.id, gtfs_zip())
        loads.clear()

        response = await client.post(
            f"/api/feeds/{feed.id}/uploads/{first['id']}/activate",
            headers={**OWNER, **WRITE},
        )

        assert response.status_code == 200
        assert response.json()["is_current"] is True
        await session.refresh(feed)
        assert feed.current_upload_id == first["id"]
        # A pointer move alone would leave the bad schedule in the tables.
        assert loads == [feed.id]

    async def test_deleting_the_current_upload_is_refused(
        self, client: AsyncClient, world: dict, store: FakeStore, loads: list[int]
    ) -> None:
        feed = world["feed"]
        current = (await _upload(client, feed.id, gtfs_zip())).json()

        response = await client.delete(
            f"/api/feeds/{feed.id}/uploads/{current['id']}",
            headers={**OWNER, **WRITE},
        )

        assert response.status_code == 409
        assert store.objects

    async def test_deleting_an_old_upload_takes_its_object(
        self,
        client: AsyncClient,
        session: AsyncSession,
        world: dict,
        store: FakeStore,
        loads: list[int],
    ) -> None:
        feed = world["feed"]
        old = (await _upload(client, feed.id, gtfs_zip())).json()
        await _upload(client, feed.id, gtfs_zip())

        response = await client.delete(
            f"/api/feeds/{feed.id}/uploads/{old['id']}", headers={**OWNER, **WRITE}
        )

        assert response.status_code == 204
        assert f"feeds/{feed.id}/{old['id']}.zip" not in store.objects
        assert await session.scalar(select(func.count()).select_from(GtfsUpload)) == 1

    async def test_an_upload_of_another_feed_is_not_found(
        self, client: AsyncClient, session: AsyncSession, world: dict, store: FakeStore
    ) -> None:
        feed = world["feed"]
        other = await make_feed(session, world["owner"], "other-feed")
        mine = (await _upload(client, other.id, gtfs_zip())).json()

        response = await client.delete(
            f"/api/feeds/{feed.id}/uploads/{mine['id']}", headers={**OWNER, **WRITE}
        )

        assert response.status_code == 404


class TestRetention:
    async def test_keeps_the_newest_and_the_current_one(
        self,
        client: AsyncClient,
        session: AsyncSession,
        world: dict,
        store: FakeStore,
        loads: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cafe_car.settings import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("KEEP_UPLOADS", "2")
        feed = world["feed"]

        ids = [
            (await _upload(client, feed.id, gtfs_zip())).json()["id"] for _ in range(4)
        ]

        response = await client.get(f"/api/feeds/{feed.id}/uploads", headers=OWNER)
        kept = [u["id"] for u in response.json()]
        assert kept == ids[-1:-3:-1]
        assert sorted(await store.list_prefix(f"feeds/{feed.id}/")) == sorted(
            f"feeds/{feed.id}/{i}.zip" for i in kept
        )

    async def test_the_current_upload_survives_the_sweep(
        self,
        client: AsyncClient,
        world: dict,
        store: FakeStore,
        loads: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A retention count of zero still leaves the feed something to serve.

        The one configuration where "keep the newest N" and "keep the current
        one" disagree, and the only way to exercise the guard: every ordinary
        sweep follows an upload, which has just made the newest one current.
        """
        from cafe_car.settings import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("KEEP_UPLOADS", "0")
        feed = world["feed"]

        first = (await _upload(client, feed.id, gtfs_zip())).json()["id"]
        second = (await _upload(client, feed.id, gtfs_zip())).json()["id"]

        response = await client.get(f"/api/feeds/{feed.id}/uploads", headers=OWNER)
        assert [u["id"] for u in response.json()] == [second]
        assert list(store.objects) == [f"feeds/{feed.id}/{second}.zip"]
        assert f"feeds/{feed.id}/{first}.zip" not in store.objects


class TestFeedSource:
    async def test_a_hosted_feed_is_created_empty_and_queues_nothing(
        self, client: AsyncClient, session: AsyncSession, world: dict
    ) -> None:
        response = await client.post(
            "/api/feeds",
            headers={**OWNER, **WRITE},
            json={"feed_name": "hosted-feed", "source_kind": "hosted"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["source_kind"] == "hosted"
        assert body["static_feed_url"] is None
        assert body["current_upload"] is None
        assert body["hosted_url"].endswith("/hosted-feed/gtfs.zip")

    async def test_a_linked_feed_still_needs_a_url(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            "/api/feeds", headers={**OWNER, **WRITE}, json={"feed_name": "no-url-feed"}
        )

        assert response.status_code == 422

    async def test_a_hosted_feed_refuses_a_url(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            "/api/feeds",
            headers={**OWNER, **WRITE},
            json={
                "feed_name": "confused-feed",
                "source_kind": "hosted",
                "static_feed_url": "https://example.com/gtfs.zip",
            },
        )

        assert response.status_code == 422

    async def test_the_feed_carries_its_current_upload(
        self, client: AsyncClient, world: dict, store: FakeStore, loads: list[int]
    ) -> None:
        feed = world["feed"]
        upload = (await _upload(client, feed.id, gtfs_zip())).json()

        response = await client.get(f"/api/feeds/{feed.id}", headers=OWNER)

        body = response.json()
        assert body["current_upload"]["id"] == upload["id"]
        assert body["hosted_url"].endswith("/owner-feed/gtfs.zip")
        assert body["static_feed_url"] is None

    async def test_switching_back_to_a_url_reloads(
        self, client: AsyncClient, world: dict, store: FakeStore, loads: list[int]
    ) -> None:
        feed = world["feed"]
        await _upload(client, feed.id, gtfs_zip())

        response = await client.patch(
            f"/api/feeds/{feed.id}",
            headers={**OWNER, **WRITE},
            json={
                "source_kind": "url",
                "static_feed_url": "https://example.com/other.zip",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["source_kind"] == "url"
        assert body["hosted_url"] == "https://example.com/other.zip"

    async def test_hosting_by_patch_alone_is_refused(
        self, client: AsyncClient, world: dict
    ) -> None:
        """Hosting means serving specific bytes, and a PATCH carries none."""
        response = await client.patch(
            f"/api/feeds/{world['feed'].id}",
            headers={**OWNER, **WRITE},
            json={"source_kind": "hosted"},
        )

        assert response.status_code == 422

    async def test_deleting_a_feed_empties_its_prefix(
        self,
        client: AsyncClient,
        session: AsyncSession,
        world: dict,
        store: FakeStore,
        loads: list[int],
    ) -> None:
        feed = world["feed"]
        await _upload(client, feed.id, gtfs_zip())

        response = await client.delete(
            f"/api/feeds/{feed.id}", headers={**OWNER, **WRITE}
        )

        assert response.status_code == 204
        assert store.objects == {}
        assert await session.scalar(select(func.count()).select_from(GtfsUpload)) == 0
        assert await session.scalar(select(func.count()).select_from(Feed)) == 0


class TestPublicZip:
    async def test_serves_the_bytes_unauthenticated(
        self,
        client: AsyncClient,
        public: AsyncClient,
        world: dict,
        store: FakeStore,
        loads: list[int],
    ) -> None:
        feed = world["feed"]
        upload = (await _upload(client, feed.id, gtfs_zip())).json()

        response = await public.get("/owner-feed/gtfs.zip")

        assert response.status_code == 200
        assert response.content == gtfs_zip()
        assert response.headers["etag"] == f'"{upload["sha256"]}"'
        assert response.headers["content-type"] == "application/zip"

    async def test_a_matching_etag_is_a_304(
        self,
        client: AsyncClient,
        public: AsyncClient,
        world: dict,
        store: FakeStore,
        loads: list[int],
    ) -> None:
        upload = (await _upload(client, world["feed"].id, gtfs_zip())).json()

        response = await public.get(
            "/owner-feed/gtfs.zip",
            headers={"If-None-Match": f'W/"{upload["sha256"]}"'},
        )

        assert response.status_code == 304
        assert not response.content

    async def test_a_stale_etag_gets_the_bytes(
        self,
        client: AsyncClient,
        public: AsyncClient,
        world: dict,
        store: FakeStore,
        loads: list[int],
    ) -> None:
        await _upload(client, world["feed"].id, gtfs_zip())

        response = await public.get(
            "/owner-feed/gtfs.zip", headers={"If-None-Match": '"nothing-like-it"'}
        )

        assert response.status_code == 200

    async def test_head_reports_the_size_without_reading_the_object(
        self,
        client: AsyncClient,
        public: AsyncClient,
        world: dict,
        store: FakeStore,
        loads: list[int],
    ) -> None:
        upload = (await _upload(client, world["feed"].id, gtfs_zip())).json()
        store.objects.clear()

        response = await public.head("/owner-feed/gtfs.zip")

        assert response.status_code == 200
        assert response.headers["content-length"] == str(upload["size_bytes"])

    async def test_a_url_sourced_feed_is_a_404(
        self, public: AsyncClient, world: dict, store: FakeStore
    ) -> None:
        response = await public.get("/owner-feed/gtfs.zip")

        assert response.status_code == 404

    async def test_an_unknown_feed_is_a_404(
        self, public: AsyncClient, store: FakeStore
    ) -> None:
        response = await public.get("/no-such-feed/gtfs.zip")

        assert response.status_code == 404

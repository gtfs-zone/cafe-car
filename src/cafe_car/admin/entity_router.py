from pathlib import Path

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from railroad_club.models.feed import Feed
from railroad_club.models.identity import Identity
from railroad_club.models.informed_entity import InformedEntity
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.tracker import Tracker
from railroad_club.models.user import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cafe_car.accounts import choose_absorber, link_candidates, merge_users
from cafe_car.admin.access import accessible_feed_ids
from cafe_car.admin.context import current_user_id_var
from cafe_car.settings import get_settings
from cafe_car.sharing import (
    list_members,
    list_open_invites,
    remove_member,
    revoke_invite,
    share_feed,
    transfer_ownership,
)
from cafe_car.traccar import build_config_url, qr_svg

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(_STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@router.get("/")
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/feed/list")


_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_ROUTE_TYPE_LABELS = {
    0: "Tram / Light Rail",
    1: "Subway / Metro",
    2: "Rail",
    3: "Bus",
    4: "Ferry",
    5: "Cable Tram",
    6: "Aerial Lift",
    7: "Funicular",
    11: "Trolleybus",
    12: "Monorail",
}


async def _current_user_id(request: Request) -> int:
    """Who is calling, according to oauth2-proxy.

    These routes sit outside SQLAdmin, so nothing has run `authenticate` for
    them and the session cookie may be stale or belong to whoever used this
    browser last. The proxy header is the authority: the cached session id is
    used only when it agrees with the header, and otherwise the identity is
    looked up afresh. Returns 0 — which matches no rows anywhere — rather than
    falling back to the cookie.
    """
    subject = request.headers.get("X-Auth-Request-User") or request.headers.get(
        "X-Forwarded-User"
    )
    if not subject:
        return 0
    cached = request.session.get("user_id")
    if cached and request.session.get("subject") == subject:
        return int(cached)
    session: AsyncSession = request.state.session
    user_id = await session.scalar(
        select(Identity.user_id).where(
            Identity.provider == get_settings().oidc_provider,
            Identity.provider_subject == subject,
        )
    )
    return int(user_id or 0)


async def _may_touch_alert(session: AsyncSession, user_id: int, alert_id: int) -> bool:
    result = await session.execute(
        select(ServiceAlert)
        .where(ServiceAlert.feed_id.in_(accessible_feed_ids(user_id)))
        .where(ServiceAlert.id == alert_id)
    )
    return result.scalar_one_or_none() is not None


async def _load_entities(session: AsyncSession, alert_id: int) -> list[InformedEntity]:
    result = await session.execute(
        select(InformedEntity).where(InformedEntity.service_alert_id == alert_id)
    )
    return list(result.scalars().all())


def _render_partial(
    request: Request,
    alert_id: int,
    entities: list[InformedEntity],
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "sqladmin/_entity_partial.html",
        {
            "alert_id": alert_id,
            "entities": entities,
            "error": error,
            "route_type_labels": _ROUTE_TYPE_LABELS,
        },
    )


@router.get("/service-alert/{alert_id}/entity-partial", response_class=HTMLResponse)
async def entity_partial(request: Request, alert_id: int) -> HTMLResponse:
    user_id = await _current_user_id(request)
    session: AsyncSession = request.state.session
    if not await _may_touch_alert(session, user_id, alert_id):
        return HTMLResponse("<p>Not found or access denied.</p>", status_code=403)
    entities = await _load_entities(session, alert_id)
    return _render_partial(request, alert_id, entities)


@router.post("/service-alert/{alert_id}/entity", response_class=HTMLResponse)
async def add_entity(
    request: Request,
    alert_id: int,
    agency_id: str = Form(default=""),
    route_id: str = Form(default=""),
    route_type: str = Form(default=""),
    direction_id: str = Form(default=""),
    stop_id: str = Form(default=""),
    trip_id: str = Form(default=""),
    trip_route_id: str = Form(default=""),
    trip_direction_id: str = Form(default=""),
    trip_start_time: str = Form(default=""),
    trip_start_date: str = Form(default=""),
) -> HTMLResponse:
    user_id = await _current_user_id(request)
    session: AsyncSession = request.state.session
    if not await _may_touch_alert(session, user_id, alert_id):
        return HTMLResponse("<p>Not found or access denied.</p>", status_code=403)

    def to_none(s: str) -> str | None:
        return s.strip() or None

    def to_int_or_none(s: str) -> int | None:
        s = s.strip()
        return int(s) if s else None

    try:
        entity = InformedEntity(
            service_alert_id=alert_id,
            agency_id=to_none(agency_id),
            route_id=to_none(route_id),
            route_type=to_int_or_none(route_type),
            direction_id=to_int_or_none(direction_id),
            stop_id=to_none(stop_id),
            trip_id=to_none(trip_id),
            trip_route_id=to_none(trip_route_id),
            trip_direction_id=to_int_or_none(trip_direction_id),
            trip_start_time=to_none(trip_start_time),
            trip_start_date=to_none(trip_start_date),
        )
    except ValueError as exc:
        entities = await _load_entities(session, alert_id)
        return _render_partial(request, alert_id, entities, error=str(exc))

    session.add(entity)
    await session.commit()
    entities = await _load_entities(session, alert_id)
    return _render_partial(request, alert_id, entities)


async def _load_accessible_tracker(
    session: AsyncSession, user_id: int, tracker_id: str
) -> Tracker | None:
    result = await session.execute(
        select(Tracker)
        .where(Tracker.feed_id.in_(accessible_feed_ids(user_id)))
        .where(Tracker.id == tracker_id)
    )
    return result.scalar_one_or_none()


@router.get("/tracker/{tracker_id}/provisioning-partial", response_class=HTMLResponse)
async def tracker_provisioning_partial(
    request: Request, tracker_id: str
) -> HTMLResponse:
    user_id = await _current_user_id(request)
    session: AsyncSession = request.state.session
    tracker = await _load_accessible_tracker(session, user_id, tracker_id)
    if tracker is None:
        return HTMLResponse("<p>Not found or access denied.</p>", status_code=403)
    config_url = build_config_url(tracker.id)
    return templates.TemplateResponse(
        request,
        "sqladmin/_tracker_provisioning.html",
        {
            "nickname": tracker.nickname,
            "tracker_id": tracker.id,
            "config_url": config_url,
            "qr_svg": qr_svg(config_url),
        },
    )


@router.post("/feeds/{feed_id}/reload")
async def reload_feed(request: Request, feed_id: int) -> RedirectResponse:
    user_id = await _current_user_id(request)
    session: AsyncSession = request.state.session
    result = await session.execute(
        select(Feed)
        .where(Feed.id.in_(accessible_feed_ids(user_id)))
        .where(Feed.id == feed_id)
    )
    if result.scalar_one_or_none() is None:
        return HTMLResponse("Not found or access denied", status_code=403)
    try:
        from cafe_car.celery_client import celery_app

        celery_app.send_task("schedule_foamer.tasks.load_feed", args=[feed_id])
    except Exception:
        pass  # worker unavailable
    return RedirectResponse(url="/feed/list", status_code=303)


@router.delete(
    "/service-alert/{alert_id}/entity/{entity_id}", response_class=HTMLResponse
)
async def delete_entity(
    request: Request, alert_id: int, entity_id: int
) -> HTMLResponse:
    user_id = await _current_user_id(request)
    session: AsyncSession = request.state.session
    if not await _may_touch_alert(session, user_id, alert_id):
        return HTMLResponse("<p>Not found or access denied.</p>", status_code=403)

    result = await session.execute(
        select(InformedEntity).where(
            InformedEntity.id == entity_id,
            InformedEntity.service_alert_id == alert_id,
        )
    )
    entity = result.scalar_one_or_none()
    if entity is not None:
        await session.delete(entity)
        await session.commit()

    entities = await _load_entities(session, alert_id)
    return _render_partial(request, alert_id, entities)


# ── Feed sharing ─────────────────────────────────────────────────────────────
#
# Membership is only ever changed here, never through a SQLAdmin form widget,
# so that "may this caller do this?" is answered server-side on every path.


async def _load_accessible_feed(
    session: AsyncSession, user_id: int, feed_id: int
) -> Feed | None:
    return await session.scalar(
        select(Feed)
        .where(Feed.id.in_(accessible_feed_ids(user_id)))
        .where(Feed.id == feed_id)
    )


async def _load_owned_feed(
    session: AsyncSession, user_id: int, feed_id: int
) -> Feed | None:
    return await session.scalar(
        select(Feed).where(Feed.owner_id == user_id, Feed.id == feed_id)
    )


async def _render_members(
    request: Request,
    session: AsyncSession,
    feed: Feed,
    user_id: int,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    owner = await session.get(User, feed.owner_id)
    return templates.TemplateResponse(
        request,
        "sqladmin/_members_partial.html",
        {
            "feed": feed,
            "owner": owner,
            "members": await list_members(session, feed.id),
            "invites": await list_open_invites(session, feed.id),
            "is_owner": feed.owner_id == user_id,
            "current_user_id": user_id,
            "message": message,
            "error": error,
        },
    )


@router.get("/feeds/{feed_id}/members-partial", response_class=HTMLResponse)
async def members_partial(request: Request, feed_id: int) -> HTMLResponse:
    user_id = await _current_user_id(request)
    session: AsyncSession = request.state.session
    feed = await _load_accessible_feed(session, user_id, feed_id)
    if feed is None:
        return HTMLResponse("<p>Not found or access denied.</p>", status_code=403)
    return await _render_members(request, session, feed, user_id)


@router.post("/feeds/{feed_id}/members", response_class=HTMLResponse)
async def add_member(
    request: Request, feed_id: int, email: str = Form(default="")
) -> HTMLResponse:
    user_id = await _current_user_id(request)
    session: AsyncSession = request.state.session
    feed = await _load_owned_feed(session, user_id, feed_id)
    if feed is None:
        return HTMLResponse(
            "<p>Only the owner can share this feed.</p>", status_code=403
        )
    result = await share_feed(session, feed, email, added_by_user_id=user_id)
    ok = result.kind in ("member", "invited")
    return await _render_members(
        request,
        session,
        feed,
        user_id,
        message=result.message if ok else None,
        error=None if ok else result.message,
    )


@router.delete("/feeds/{feed_id}/members/{member_user_id}", response_class=HTMLResponse)
async def delete_member(
    request: Request, feed_id: int, member_user_id: int
) -> HTMLResponse:
    user_id = await _current_user_id(request)
    session: AsyncSession = request.state.session
    feed = await _load_owned_feed(session, user_id, feed_id)
    if feed is None:
        return HTMLResponse(
            "<p>Only the owner can change access to this feed.</p>", status_code=403
        )
    await remove_member(session, feed_id, member_user_id)
    return await _render_members(
        request, session, feed, user_id, message="Access removed."
    )


@router.delete("/feeds/{feed_id}/invites/{invite_id}", response_class=HTMLResponse)
async def delete_invite(request: Request, feed_id: int, invite_id: int) -> HTMLResponse:
    user_id = await _current_user_id(request)
    session: AsyncSession = request.state.session
    feed = await _load_owned_feed(session, user_id, feed_id)
    if feed is None:
        return HTMLResponse(
            "<p>Only the owner can change access to this feed.</p>", status_code=403
        )
    await revoke_invite(session, feed_id, invite_id)
    return await _render_members(
        request, session, feed, user_id, message="Invitation withdrawn."
    )


# ── Account linking ──────────────────────────────────────────────────────────
#
# The page itself is a SQLAdmin BaseView (admin/account_view.py); the two
# mutating actions live here with the rest of the permission-checked routes.


@router.post("/account/link-confirm")
async def link_confirm(request: Request, candidate_user_id: int = Form()) -> Response:
    user_id = await _current_user_id(request)
    if not user_id:
        return HTMLResponse("Not signed in", status_code=403)
    session: AsyncSession = request.state.session

    # Re-derive the offer rather than trusting the posted id. The form is only
    # ever rendered for a real match, but a merge is irreversible and deletes a
    # user, so the check has to happen where the decision is made.
    candidates = await link_candidates(session, user_id)
    if not any(c.id == candidate_user_id for c in candidates):
        return HTMLResponse(
            "That account does not share a verified email with yours.", status_code=403
        )

    me = await session.get(User, user_id)
    them = await session.get(User, candidate_user_id)
    absorbing, absorbed = choose_absorber(me, them)
    await merge_users(session, absorbing_id=absorbing.id, absorbed_id=absorbed.id)

    # If the caller's own principal was the one absorbed, the session is now
    # holding a deleted id. Their identity row points at the survivor, so the
    # next authenticate would fix it — but not before this response's redirect
    # is served against the stale id.
    request.session["user_id"] = absorbing.id
    current_user_id_var.set(absorbing.id)
    request.session.pop("link_dismissed", None)
    return RedirectResponse(url="/account", status_code=303)


@router.post("/account/link-dismiss")
async def link_dismiss(
    request: Request, candidate_user_id: int = Form()
) -> RedirectResponse:
    dismissed = set(request.session.get("link_dismissed") or [])
    dismissed.add(candidate_user_id)
    request.session["link_dismissed"] = sorted(dismissed)
    return RedirectResponse(url="/account", status_code=303)


@router.post("/feeds/{feed_id}/transfer", response_class=HTMLResponse)
async def transfer_feed(
    request: Request, feed_id: int, new_owner_id: int = Form()
) -> HTMLResponse:
    user_id = await _current_user_id(request)
    session: AsyncSession = request.state.session
    feed = await _load_owned_feed(session, user_id, feed_id)
    if feed is None:
        return HTMLResponse(
            "<p>Only the owner can hand over this feed.</p>", status_code=403
        )
    try:
        await transfer_ownership(session, feed, new_owner_id)
    except PermissionError as exc:
        return await _render_members(request, session, feed, user_id, error=str(exc))
    return await _render_members(
        request,
        session,
        feed,
        user_id,
        message="Ownership transferred. You are now a member of this feed.",
    )

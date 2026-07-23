from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from railroad_club.models.driver import Driver
from railroad_club.models.feed import Feed
from railroad_club.models.informed_entity import InformedEntity
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.user import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _verify_alert_ownership(
    session: AsyncSession, subject: str, alert_id: int
) -> bool:
    result = await session.execute(
        select(ServiceAlert)
        .join(Feed, ServiceAlert.feed_id == Feed.id)
        .join(User, Feed.owner_id == User.id)
        .where(User.provider_subject == subject)
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
    subject = request.session.get("subject", "")
    session: AsyncSession = request.state.session
    if not await _verify_alert_ownership(session, subject, alert_id):
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
    subject = request.session.get("subject", "")
    session: AsyncSession = request.state.session
    if not await _verify_alert_ownership(session, subject, alert_id):
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


async def _load_owned_driver(
    session: AsyncSession, subject: str, driver_id: int
) -> Driver | None:
    result = await session.execute(
        select(Driver)
        .join(Feed, Driver.feed_id == Feed.id)
        .join(User, Feed.owner_id == User.id)
        .where(User.provider_subject == subject)
        .where(Driver.id == driver_id)
    )
    return result.scalar_one_or_none()


@router.get(
    "/driver/{driver_id}/provisioning-partial", response_class=HTMLResponse
)
async def driver_provisioning_partial(
    request: Request, driver_id: int
) -> HTMLResponse:
    subject = request.session.get("subject", "")
    session: AsyncSession = request.state.session
    driver = await _load_owned_driver(session, subject, driver_id)
    if driver is None:
        return HTMLResponse("<p>Not found or access denied.</p>", status_code=403)
    config_url = build_config_url(driver.username)
    return templates.TemplateResponse(
        request,
        "sqladmin/_driver_provisioning.html",
        {
            "username": driver.username,
            "config_url": config_url,
            "qr_svg": qr_svg(config_url),
        },
    )


@router.post("/feeds/{feed_id}/reload")
async def reload_feed(request: Request, feed_id: int) -> RedirectResponse:
    subject = request.session.get("subject", "")
    session: AsyncSession = request.state.session
    result = await session.execute(
        select(Feed)
        .join(User, Feed.owner_id == User.id)
        .where(User.provider_subject == subject)
        .where(Feed.id == feed_id)
    )
    if result.scalar_one_or_none() is None:
        return HTMLResponse("Not found or access denied", status_code=403)
    try:
        from cafe_car.celery_client import celery_app

        celery_app.send_task("worker.tasks.load_feed", args=[feed_id])
    except Exception:
        pass  # worker unavailable
    return RedirectResponse(url="/feed/list", status_code=303)


@router.delete(
    "/service-alert/{alert_id}/entity/{entity_id}", response_class=HTMLResponse
)
async def delete_entity(
    request: Request, alert_id: int, entity_id: int
) -> HTMLResponse:
    subject = request.session.get("subject", "")
    session: AsyncSession = request.state.session
    if not await _verify_alert_ownership(session, subject, alert_id):
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

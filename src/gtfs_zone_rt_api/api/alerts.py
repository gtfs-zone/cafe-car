"""Service alert and informed entity endpoints.

Alerts and entities have no owner of their own: an alert belongs to a feed and
an entity belongs to an alert, so both scope through ``accessible_feed_ids``
rather than growing a second definition of who may read or write them. A member
may publish an alert on a feed they were given, which is the point of being
given it.

An entity is created and deleted, never patched. It is six nullable columns
naming one thing, so editing one is the same act as replacing it, and a PATCH
would only add a second way to arrive at a selector that names nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from gtfs_zone_db_models.models.informed_entity import InformedEntity
from gtfs_zone_db_models.models.service_alert import ServiceAlert
from sqlalchemy import delete, func, select

from gtfs_zone_rt_api.admin.access import accessible_feed_ids
from gtfs_zone_rt_api.api.deps import AccessibleFeed, CurrentUser, DBSession
from gtfs_zone_rt_api.api.schemas import (
    AlertDetailOut,
    AlertOut,
    AlertWrite,
    InformedEntityOut,
    InformedEntityWrite,
)

router = APIRouter()


def _alert_fields(alert: ServiceAlert, entity_count: int) -> dict:
    return {
        "id": alert.id,
        "feed_id": alert.feed_id,
        "header_text": alert.header_text,
        "description_text": alert.description_text,
        "url": alert.url,
        "cause": alert.cause,
        "effect": alert.effect,
        "severity_level": alert.severity_level,
        "active_period_start": alert.active_period_start,
        "active_period_end": alert.active_period_end,
        "entity_count": entity_count,
    }


def _entity_out(entity: InformedEntity) -> InformedEntityOut:
    return InformedEntityOut(
        id=entity.id,
        service_alert_id=entity.service_alert_id,
        agency_id=entity.agency_id,
        route_id=entity.route_id,
        route_type=entity.route_type,
        direction_id=entity.direction_id,
        stop_id=entity.stop_id,
        trip_id=entity.trip_id,
        trip_route_id=entity.trip_route_id,
        trip_direction_id=entity.trip_direction_id,
        trip_start_time=entity.trip_start_time,
        trip_start_date=entity.trip_start_date,
    )


async def _accessible_alert(
    session: DBSession, user_id: int, alert_id: int
) -> ServiceAlert:
    alert = await session.scalar(
        select(ServiceAlert).where(
            ServiceAlert.feed_id.in_(accessible_feed_ids(user_id)),
            ServiceAlert.id == alert_id,
        )
    )
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


async def _load_entities(session: DBSession, alert_id: int) -> list[InformedEntityOut]:
    rows = await session.execute(
        select(InformedEntity)
        .where(InformedEntity.service_alert_id == alert_id)
        .order_by(InformedEntity.id)
    )
    return [_entity_out(e) for e in rows.scalars().all()]


@router.get("/feeds/{feed_id}/alerts")
async def list_alerts(feed: AccessibleFeed, session: DBSession) -> list[AlertOut]:
    # The entity count comes from a grouped subquery rather than
    # `len(alert.entities)`, which would lazy-load one relationship per alert.
    counts = (
        select(
            InformedEntity.service_alert_id.label("alert_id"),
            func.count().label("n"),
        )
        .group_by(InformedEntity.service_alert_id)
        .subquery()
    )
    rows = await session.execute(
        select(ServiceAlert, func.coalesce(counts.c.n, 0))
        .join(counts, counts.c.alert_id == ServiceAlert.id, isouter=True)
        .where(ServiceAlert.feed_id == feed.id)
        .order_by(ServiceAlert.id)
    )
    return [AlertOut(**_alert_fields(alert, n)) for alert, n in rows.all()]


@router.get("/alerts/{alert_id}")
async def read_alert(
    alert_id: int, user_id: CurrentUser, session: DBSession
) -> AlertDetailOut:
    alert = await _accessible_alert(session, user_id, alert_id)
    entities = await _load_entities(session, alert_id)
    return AlertDetailOut(**_alert_fields(alert, len(entities)), entities=entities)


@router.get("/alerts/{alert_id}/entities")
async def list_entities(
    alert_id: int, user_id: CurrentUser, session: DBSession
) -> list[InformedEntityOut]:
    await _accessible_alert(session, user_id, alert_id)
    return await _load_entities(session, alert_id)


@router.post("/feeds/{feed_id}/alerts", status_code=201)
async def create_alert(
    payload: AlertWrite, feed: AccessibleFeed, session: DBSession
) -> AlertDetailOut:
    """Publish a new alert on this feed.

    It starts with no informed entities, which in GTFS-RT means it applies to
    the whole feed. That is a real thing to publish, so the entities are added
    afterwards rather than being required here.
    """
    alert = ServiceAlert(feed_id=feed.id, **payload.model_dump())
    session.add(alert)
    await session.commit()
    await session.refresh(alert)
    return AlertDetailOut(**_alert_fields(alert, 0), entities=[])


@router.patch("/alerts/{alert_id}")
async def update_alert(
    payload: AlertWrite, alert_id: int, user_id: CurrentUser, session: DBSession
) -> AlertDetailOut:
    """Replace the editable half of an alert.

    Every editable field is sent, so an omitted one is a cleared one: the form
    is a whole alert, and "the URL is now blank" has to be expressible.
    """
    alert = await _accessible_alert(session, user_id, alert_id)
    for field, value in payload.model_dump().items():
        setattr(alert, field, value)
    session.add(alert)
    await session.commit()
    await session.refresh(alert)

    entities = await _load_entities(session, alert_id)
    return AlertDetailOut(**_alert_fields(alert, len(entities)), entities=entities)


@router.delete("/alerts/{alert_id}", status_code=204)
async def delete_alert(
    alert_id: int, user_id: CurrentUser, session: DBSession
) -> Response:
    """Delete an alert and its informed entities.

    The entities point at it and do not cascade in the model, so they are
    removed here rather than left to raise a foreign-key error.
    """
    alert = await _accessible_alert(session, user_id, alert_id)
    await session.execute(
        delete(InformedEntity).where(InformedEntity.service_alert_id == alert.id)
    )
    await session.delete(alert)
    await session.commit()
    return Response(status_code=204)


@router.post("/alerts/{alert_id}/entities", status_code=201)
async def create_entity(
    payload: InformedEntityWrite,
    alert_id: int,
    user_id: CurrentUser,
    session: DBSession,
) -> InformedEntityOut:
    """Add one entity selector to an alert.

    Nothing here is checked against the feed's schedule: the zip is parsed in
    the browser, an id can be published before the schedule carrying it is
    loaded, and a selector for a route that does not exist yet is a warning for
    the form to draw rather than a reason to refuse the write.
    """
    await _accessible_alert(session, user_id, alert_id)
    entity = InformedEntity(service_alert_id=alert_id, **payload.model_dump())
    session.add(entity)
    await session.commit()
    await session.refresh(entity)
    return _entity_out(entity)


@router.delete("/alerts/{alert_id}/entities/{entity_id}", status_code=204)
async def delete_entity(
    alert_id: int, entity_id: int, user_id: CurrentUser, session: DBSession
) -> Response:
    """Remove one entity selector.

    Scoped through the alert *and* matched on it, so an entity id belonging to
    somebody else's alert is a 404 rather than a delete.
    """
    await _accessible_alert(session, user_id, alert_id)
    entity = await session.scalar(
        select(InformedEntity).where(
            InformedEntity.id == entity_id,
            InformedEntity.service_alert_id == alert_id,
        )
    )
    if entity is None:
        raise HTTPException(status_code=404, detail="Informed entity not found")
    await session.delete(entity)
    await session.commit()
    return Response(status_code=204)

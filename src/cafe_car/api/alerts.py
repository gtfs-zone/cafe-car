"""Service alert and informed entity endpoints.

Alerts and entities have no owner of their own: an alert belongs to a feed and
an entity belongs to an alert, so both scope through ``accessible_feed_ids``
rather than growing a second definition of who may read them.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from railroad_club.models.informed_entity import InformedEntity
from railroad_club.models.service_alert import ServiceAlert
from sqlalchemy import func, select

from cafe_car.admin.access import accessible_feed_ids
from cafe_car.api.deps import AccessibleFeed, CurrentUser, DBSession
from cafe_car.api.schemas import AlertDetailOut, AlertOut, InformedEntityOut

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

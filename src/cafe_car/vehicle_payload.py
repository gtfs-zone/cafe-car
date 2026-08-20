"""The ``vehicle:*`` keyspace, and the one camelCase view built from it.

Three readers share this module and none of them may re-derive what is here.
``routers/ingest.py`` writes the records and publishes each one as it lands,
``api/positions.py`` reads the whole feed's worth on demand, and
``routers/catalog.py`` walks the keyspace to answer "is anything live". A key
derivation duplicated across those is the failure the surrogate re-key was
supposed to end.

**The view is GTFS-RT camelCase because yard-master's map reads GTFS-RT.** It
is exactly the `VehiclePosition` shape `map-controller.ts` holds, so a payload
goes into `FeedSession.vehicles` with no translation layer, whether it arrived
on the event channel or from the positions endpoint. Those two must never
disagree, which is why one function builds both.

**``key`` is the surrogate, never the nickname.** It is the map feature id, the
key in `FeedSession.vehicles` and the click identity, and nicknames are only
unique within a feed while two trackers sharing one used to collapse onto a
single map feature. One tracker can also carry several concurrent vehicles (one
Redis key per trip instance), so the key carries the trip discriminator too and
``trackerId`` is what says which tracker they all belong to.

``device_key`` appears nowhere in here. A record never held it and a view never
may: these payloads are logged, pushed down a channel and dumped on a page.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from redis.asyncio import Redis

# SCAN's per-call hint. Larger than the default 10 because the vehicle keyspace
# is small and one round trip per ten keys is the slow part.
SCAN_COUNT = 500


def vehicle_key(tracker_id: str, trip_id: str, start_date: str | None) -> str:
    """The identity of one vehicle: a tracker, plus which trip instance it is.

    Appending ``start_date`` when present gives concurrent instances of one
    long-running daily trip distinct keys. This is the Redis key without its
    ``vehicle:`` prefix, so the same string addresses a record and a map
    feature.
    """
    slug = f"{trip_id}:{start_date}" if start_date else trip_id
    return f"{tracker_id}:{slug}"


def redis_key(tracker_id: str, trip_id: str, start_date: str | None) -> str:
    """Where one vehicle's record lives."""
    return f"vehicle:{vehicle_key(tracker_id, trip_id, start_date)}"


def public_vehicle_id(
    tracker_nickname: str,
    public_id: str | None,
    trip_id: str | None,
    start_date: str | None,
) -> str:
    """The GTFS-RT `VehicleDescriptor.id` for one vehicle record.

    A producer's own `vehicle_id` is trusted when given, but a producer with no
    concept of a public per-vehicle id (or one that forgets to set it, which bit
    a buswhere feed that ran several devices under one tracker credential) must
    not collapse every such vehicle onto the bare tracker nickname: GTFS-RT
    requires this id "unique per vehicle", and two concurrent vehicles sharing a
    tracker would otherwise share this id too. Folding in the trip instance
    (trip_id + start_date, the same disambiguator used for `entity.id`) restores
    uniqueness for any concurrently-running vehicles, without requiring every
    producer to invent its own scheme.
    """
    if public_id:
        return public_id
    if trip_id:
        instance = f"{trip_id}:{start_date}" if start_date else trip_id
        return f"{tracker_nickname}:{instance}"
    return tracker_nickname


def _decode(raw: bytes | str) -> str:
    """Redis is opened without ``decode_responses``, so keys arrive as bytes."""
    return raw.decode() if isinstance(raw, bytes) else raw


async def live_vehicle_keys(redis: Redis) -> dict[str, list[str]]:
    """Every live vehicle key on the server, grouped by the tracker that owns it.

    One pass over ``vehicle:*`` for the whole request. Scanning once per tracker
    is fine when a route already knows it is serving one feed's trackers, but a
    caller holding many of them would walk the whole keyspace once each.
    """
    by_tracker: dict[str, list[str]] = {}
    async for raw in redis.scan_iter(match="vehicle:*", count=SCAN_COUNT):
        key = _decode(raw)
        # vehicle:{tracker_id}:{trip_id}[:{start_date}]
        parts = key.split(":")
        if len(parts) < 3:
            continue
        by_tracker.setdefault(parts[1], []).append(key)
    return by_tracker


def vehicle_view(
    tracker_id: str, nickname: str, record: dict[str, Any]
) -> dict[str, Any]:
    """One ``vehicle:*`` record as the camelCase vehicle yard-master's map reads.

    Absent fields are left out rather than sent as null: the map's type has them
    optional, and "the producer did not report a bearing" is not "the bearing is
    zero". ``raw`` carries the record itself, which is what the tracker page
    dumps and the only place a field this view does not know about survives.
    """
    trip_id = record.get("trip_id") or None
    start_date = record.get("start_date") or None
    public_id = record.get("vehicle_id")
    # What a consumer of the published feed sees for this vehicle, derived the
    # same way the `.pb` derives it, so the panel and the feed never disagree.
    vehicle_id = public_vehicle_id(nickname, public_id, trip_id, start_date)

    view: dict[str, Any] = {
        "key": vehicle_key(tracker_id, record.get("trip_id") or "", start_date),
        "trackerId": tracker_id,
        "vehicleId": vehicle_id,
        "entityId": vehicle_id,
        "label": record.get("vehicle_label") or public_id or nickname,
        "lat": record["lat"],
        "lon": record["lon"],
        "raw": record,
    }
    if record.get("bearing") is not None:
        view["bearing"] = record["bearing"]
    if record.get("speed") is not None:
        view["speed"] = record["speed"]
    if trip_id:
        view["tripId"] = trip_id
    if record.get("route_id"):
        view["routeId"] = record["route_id"]
    if start_date:
        view["startDate"] = start_date
    # Sequence 0 is a legitimate GTFS stop_sequence, so test presence, not truth.
    if record.get("current_stop_sequence") is not None:
        view["currentStopSequence"] = record["current_stop_sequence"]
    if record.get("stop_id"):
        view["stopId"] = record["stop_id"]
    if record.get("current_status"):
        view["currentStatus"] = _STOP_STATUS.get(record["current_status"])
    if record.get("timestamp") is not None:
        view["timestamp"] = record["timestamp"]
    return view


# GTFS-RT VehicleStopStatus, by value. The wire form is the enum's number, which
# is what a decoded feed gives a consumer and so what the map's type holds; the
# record stores the name because that is what a producer sends.
_STOP_STATUS = {"INCOMING_AT": 0, "STOPPED_AT": 1, "IN_TRANSIT_TO": 2}


async def feed_vehicles(
    redis: Redis, trackers: Iterable[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Every live vehicle belonging to ``trackers``, as ``(id, nickname)`` pairs.

    Sorted by key so two calls a second apart list the same fleet in the same
    order; scan order is not stable and a panel that reshuffled on every push
    would be unreadable.
    """
    names = dict(trackers)
    if not names:
        return []

    by_tracker = await live_vehicle_keys(redis)
    keys = [key for tracker_id in names for key in by_tracker.get(tracker_id, [])]
    if not keys:
        return []

    views: list[dict[str, Any]] = []
    for key, raw in zip(keys, await redis.mget(keys), strict=True):
        if raw is None:
            # Expired between the scan and the read; its 60s TTL is what makes
            # presence mean freshness, so a gone key is simply not live.
            continue
        tracker_id = key.split(":")[1]
        views.append(vehicle_view(tracker_id, names[tracker_id], json.loads(raw)))
    views.sort(key=lambda v: v["key"])
    return views

"""The ``vehicle:*`` keyspace, and the one camelCase view built from it.

Three readers share this module and none of them may re-derive what is here.
``routers/ingest.py`` writes the records and publishes each one as it lands,
``api/positions.py`` reads the whole feed's worth on demand, and
``routers/catalog.py`` walks the keyspace to answer "is anything live". The key
derivation itself lives one level down, in ``railroad_club.vehicle_keys``, and
is re-exported here: the producers derive the same strings, and two repos
deriving them independently is how ``vehicle:*`` came to mean a device in one
writer and a trip instance in another.

**A vehicle's identity is ``(tracker_id, vehicle_id)``.** ``trip_id`` and
``start_date`` are data on the record, not part of the key, so a vehicle that
finishes one trip and starts another overwrites its own record instead of
leaving the old one to live out its TTL beside the new one. A producer with no
per-vehicle id - a Traccar device is one tracker, one vehicle - holds the bare
tracker id and so exactly one record.

**The view is GTFS-RT camelCase because yard-master's map reads GTFS-RT.** It
is exactly the `VehiclePosition` shape `map-controller.ts` holds, so a payload
goes into `FeedSession.vehicles` with no translation layer, whether it arrived
on the event channel or from the positions endpoint. Those two must never
disagree, which is why one function builds both.

**``key`` is the surrogate, never the nickname.** It is the map feature id, the
key in `FeedSession.vehicles` and the click identity, and nicknames are only
unique within a feed while two trackers sharing one used to collapse onto a
single map feature. It is also the real Redis key for every producer, which is
what lets a view and a record be matched up.

``device_key`` appears nowhere in here. A record never held it and a view never
may: these payloads are logged, pushed down a channel and dumped on a page.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from railroad_club.vehicle_keys import redis_key, split_vehicle_key, vehicle_key

if TYPE_CHECKING:
    from collections.abc import Iterable

    from redis.asyncio import Redis

__all__ = [
    "feed_vehicles",
    "live_vehicle_keys",
    "public_vehicle_id",
    "redis_key",
    "split_vehicle_key",
    "vehicle_key",
    "vehicle_view",
]

# SCAN's per-call hint. Larger than the default 10 because the vehicle keyspace
# is small and one round trip per ten keys is the slow part.
SCAN_COUNT = 500


def public_vehicle_id(tracker_nickname: str, public_id: str | None) -> str:
    """The GTFS-RT `VehicleDescriptor.id` for one vehicle record.

    A producer's own `vehicle_id` is trusted when given; a producer with no
    concept of one runs a single vehicle under its tracker, so the nickname is
    unique per vehicle for it. This is the same pairing the Redis key is built
    from, so two records that share this id would have shared a key.
    """
    return public_id or tracker_nickname


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
        tracker_id, _ = split_vehicle_key(key)
        if not tracker_id:
            continue
        by_tracker.setdefault(tracker_id, []).append(key)
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
    vehicle_id = public_vehicle_id(nickname, public_id)

    view: dict[str, Any] = {
        "key": vehicle_key(tracker_id, public_id),
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
        tracker_id, _ = split_vehicle_key(key)
        views.append(vehicle_view(tracker_id, names[tracker_id], json.loads(raw)))
    views.sort(key=lambda v: v["key"])
    return views

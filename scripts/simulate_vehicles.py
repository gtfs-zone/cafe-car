#!/usr/bin/env python3
"""Simulate vehicle positions in Redis for testing.

Writes a fixed set of test vehicles to Redis every 10 seconds with slight
random movement.  Keys follow the pattern used by the vehicle_positions
endpoint:

    vehicle:{feed_name}:{driver_username}  →  JSON  (TTL 60 s)

Usage:
    # Simulate all built-in test feeds:
    uv run scripts/simulate_vehicles.py

    # Simulate only specific feeds:
    uv run scripts/simulate_vehicles.py --feeds test-feed-sf test-feed-nyc

    # Custom Redis URL:
    uv run scripts/simulate_vehicles.py --redis redis://localhost:6379/1
"""

import argparse
import asyncio
import json
import random
import time

import redis.asyncio as aioredis

REDIS_URL = "redis://localhost:6379/1"

# ---------------------------------------------------------------------------
# Test data — one dict per feed, each with its own drivers and starting area
# ---------------------------------------------------------------------------
TEST_FEEDS: dict[str, list[dict]] = {
    "feedme": [
        {
            "driver": "test-driver-001",
            "trip_id": "test-trip-A1",
            "route_id": "test-route-1",
            "lat": 37.7749,
            "lon": -122.4194,
            "bearing": 45.0,
            "speed": 10.0,
        },
        {
            "driver": "test-driver-002",
            "trip_id": "test-trip-B1",
            "route_id": "test-route-1",
            "lat": 37.7800,
            "lon": -122.4100,
            "bearing": 180.0,
            "speed": 8.0,
        },
        {
            "driver": "test-driver-003",
            "trip_id": "test-trip-C1",
            "route_id": "test-route-2",
            "lat": 37.7700,
            "lon": -122.4300,
            "bearing": 270.0,
            "speed": 14.0,
        },
    ],
    "test-feed-nyc": [
        {
            "driver": "nyc-driver-001",
            "trip_id": "nyc-trip-A1",
            "route_id": "nyc-route-1",
            "lat": 40.7128,
            "lon": -74.0060,
            "bearing": 90.0,
            "speed": 9.0,
        },
        {
            "driver": "nyc-driver-002",
            "trip_id": "nyc-trip-B1",
            "route_id": "nyc-route-1",
            "lat": 40.7200,
            "lon": -73.9950,
            "bearing": 0.0,
            "speed": 12.0,
        },
        {
            "driver": "nyc-driver-003",
            "trip_id": "nyc-trip-C1",
            "route_id": "nyc-route-2",
            "lat": 40.7050,
            "lon": -74.0150,
            "bearing": 225.0,
            "speed": 7.0,
        },
        {
            "driver": "nyc-driver-004",
            "trip_id": "nyc-trip-D1",
            "route_id": "nyc-route-2",
            "lat": 40.7300,
            "lon": -73.9800,
            "bearing": 135.0,
            "speed": 11.0,
        },
    ],
    "test-feed-chi": [
        {
            "driver": "chi-driver-001",
            "trip_id": "chi-trip-A1",
            "route_id": "chi-route-1",
            "lat": 41.8781,
            "lon": -87.6298,
            "bearing": 30.0,
            "speed": 13.0,
        },
        {
            "driver": "chi-driver-002",
            "trip_id": "chi-trip-B1",
            "route_id": "chi-route-1",
            "lat": 41.8850,
            "lon": -87.6200,
            "bearing": 210.0,
            "speed": 9.5,
        },
    ],
}


def _step(vehicle: dict) -> None:
    """Nudge position, bearing, and speed with big random deltas."""
    vehicle["lat"] += random.uniform(-0.1, 0.1)
    vehicle["lon"] += random.uniform(-0.1, 0.1)
    vehicle["bearing"] = (vehicle["bearing"] + random.uniform(-15, 15)) % 360
    vehicle["speed"] = max(2.0, vehicle["speed"] + random.uniform(-2, 2))


async def update_feed(
    pipe: aioredis.client.Pipeline,
    feed_name: str,
    vehicles: list[dict],
    now: int,
) -> None:
    for v in vehicles:
        _step(v)
        key = f"vehicle:{feed_name}:{v['driver']}"
        payload = json.dumps(
            {
                "driver": v["driver"],
                "trip_id": v["trip_id"],
                "route_id": v["route_id"],
                "lat": v["lat"],
                "lon": v["lon"],
                "bearing": v["bearing"],
                "speed": v["speed"],
                "timestamp": now,
            }
        )
        pipe.setex(key, 60, payload)


async def update_all(
    redis: aioredis.Redis,
    feeds: dict[str, list[dict]],
) -> None:
    now = int(time.time())
    async with redis.pipeline(transaction=False) as pipe:
        for feed_name, vehicles in feeds.items():
            await update_feed(pipe, feed_name, vehicles, now)
        await pipe.execute()

    print(f"[{time.strftime('%H:%M:%S')}]")
    for feed_name, vehicles in feeds.items():
        print(f"  feed '{feed_name}' — {len(vehicles)} vehicles")
        for v in vehicles:
            print(
                f"    {v['driver']:<22} trip={v['trip_id']:<14} "
                f"lat={v['lat']:.5f}  lon={v['lon']:.5f}  "
                f"bearing={v['bearing']:5.1f}  speed={v['speed']:.1f} m/s"
            )


async def main(feed_names: list[str], redis_url: str) -> None:
    r = aioredis.from_url(redis_url)
    await r.ping()

    feeds = {
        name: [dict(v) for v in TEST_FEEDS[name]]
        for name in feed_names
    }

    total = sum(len(v) for v in feeds.values())
    print(f"Connected to Redis at {redis_url}")
    print(f"Simulating {total} vehicles across {len(feeds)} feed(s) (Ctrl-C to stop)\n")

    try:
        while True:
            await update_all(r, feeds)
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        pass
    finally:
        await r.aclose()


if __name__ == "__main__":
    all_feed_names = list(TEST_FEEDS.keys())

    parser = argparse.ArgumentParser(description="Simulate GTFS-RT vehicle positions in Redis.")
    parser.add_argument(
        "--feeds",
        nargs="+",
        default=all_feed_names,
        choices=all_feed_names,
        metavar="FEED",
        help=(
            f"Feed name(s) to simulate. Choices: {all_feed_names}. "
            "Defaults to all feeds."
        ),
    )
    parser.add_argument("--redis", default=REDIS_URL, help="Redis URL (default: %(default)s)")
    args = parser.parse_args()

    asyncio.run(main(args.feeds, args.redis))

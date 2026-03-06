#!/usr/bin/env python3
# /// script
# dependencies = ["paho-mqtt>=2.0"]
# ///
"""Simulate a real GTFS trip along its shape, publishing to MQTT in OwnTracks format.

The simulation starts at the position the bus would actually be at right now
according to the GTFS schedule, with a random (or fixed) delay of 5–10 minutes.
Today's date is used so the trip runs in wall-clock sync when --speed 1 is used.

The MQTT topic follows the OwnTracks convention: owntracks/{driver}/{trip_id},
e.g. owntracks/bob/WCCWB. Bus drivers set their OwnTracks device ID to their
trip ID.

Usage:
    # List available trips in the GTFS zip:
    uv run scripts/simulate_trip.py --list-trips

    # Simulate trip WCCWB at 10x speed, publishing every 2s:
    uv run scripts/simulate_trip.py --trip WCCWB

    # Custom driver credentials, speed and interval:
    uv run scripts/simulate_trip.py --driver bob --password bob \\
        --trip ELLSWB --speed 30 --interval 1

    # Custom delay range (seconds):
    uv run scripts/simulate_trip.py --min-delay 30 --max-delay 300 --delay-drift 10
"""

import argparse
import csv
import io
import json
import math
import random
import time
import zipfile

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# GTFS loading
# ---------------------------------------------------------------------------


def load_gtfs(zip_path: str) -> dict:
    z = zipfile.ZipFile(zip_path)

    def read_table(name: str) -> list[dict]:
        data = z.read(name).decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(data)))

    return {
        "trips": read_table("trips.txt"),
        "stop_times": read_table("stop_times.txt"),
        "stops": {r["stop_id"]: r for r in read_table("stops.txt")},
        "shapes": read_table("shapes.txt"),
    }


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def parse_time(s: str) -> int:
    """HH:MM:SS → seconds since midnight (handles >24h GTFS times)."""
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two lat/lon points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def compass_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing (degrees, 0=N) from point 1 → 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ---------------------------------------------------------------------------
# Shape helpers
# ---------------------------------------------------------------------------


def build_shape(shape_rows: list[dict]) -> tuple[list[tuple[float, float]], list[float]]:
    """Return (points, cumulative_distances_m) sorted by sequence."""
    pts = sorted(shape_rows, key=lambda r: int(r["shape_pt_sequence"]))
    points = [(float(r["shape_pt_lat"]), float(r["shape_pt_lon"])) for r in pts]
    dists = [0.0]
    for i in range(1, len(points)):
        dists.append(dists[-1] + haversine(*points[i - 1], *points[i]))
    return points, dists


def position_at_dist(
    points: list[tuple[float, float]],
    dists: list[float],
    target: float,
) -> tuple[float, float, float]:
    """Interpolate (lat, lon, bearing°) at cumulative distance `target` metres."""
    target = max(0.0, min(target, dists[-1]))
    for i in range(1, len(dists)):
        if dists[i] >= target:
            seg = dists[i] - dists[i - 1]
            frac = (target - dists[i - 1]) / seg if seg > 0 else 0.0
            lat = points[i - 1][0] + frac * (points[i][0] - points[i - 1][0])
            lon = points[i - 1][1] + frac * (points[i][1] - points[i - 1][1])
            brg = compass_bearing(*points[i - 1], *points[i])
            return lat, lon, brg
    lat, lon = points[-1]
    brg = compass_bearing(*points[-2], *points[-1])
    return lat, lon, brg



# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------


def map_stops_to_shape(
    stop_times: list[dict],
    stops: dict,
    shape_points: list[tuple[float, float]],
    shape_dists: list[float],
) -> list[tuple[int, float]]:
    """Return [(scheduled_seconds_since_midnight, shape_dist_m)] per stop."""
    result = []
    for st in stop_times:
        stop = stops[st["stop_id"]]
        slat, slon = float(stop["stop_lat"]), float(stop["stop_lon"])
        best_i = min(
            range(len(shape_points)),
            key=lambda i: haversine(slat, slon, *shape_points[i]),
        )
        t = parse_time(st["departure_time"])
        result.append((t, shape_dists[best_i]))
    return result


def shape_dist_at(schedule_elapsed: float, stop_schedule: list[tuple[int, float]]) -> float:
    """Shape distance (m) given seconds elapsed since first stop departure."""
    first_t = stop_schedule[0][0]
    t = first_t + schedule_elapsed

    if t <= stop_schedule[0][0]:
        return stop_schedule[0][1]
    if t >= stop_schedule[-1][0]:
        return stop_schedule[-1][1]

    for i in range(1, len(stop_schedule)):
        if stop_schedule[i][0] >= t:
            prev_t, prev_d = stop_schedule[i - 1]
            next_t, next_d = stop_schedule[i]
            frac = (t - prev_t) / (next_t - prev_t)
            return prev_d + frac * (next_d - prev_d)

    return stop_schedule[-1][1]


def current_stop_index(schedule_elapsed: float, stop_schedule: list[tuple[int, float]]) -> int:
    """Index of the last stop the bus has reached or passed."""
    first_t = stop_schedule[0][0]
    t = first_t + schedule_elapsed
    idx = 0
    for i, (st, _) in enumerate(stop_schedule):
        if st <= t:
            idx = i
    return idx


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Simulate a GTFS trip and publish vehicle positions to MQTT."
    )
    parser.add_argument(
        "--gtfs",
        default="example_data/west_gtfs.zip",
        help="Path to GTFS zip (default: example_data/west_gtfs.zip)",
    )
    parser.add_argument("--trip", help="Trip ID to simulate (default: first trip in zip)")
    parser.add_argument("--driver", default="bob", help="Driver username / OwnTracks user (default: bob)")
    parser.add_argument("--password", default="bob", help="MQTT password (default: bob)")
    parser.add_argument("--broker", default="localhost", help="MQTT broker host (default: localhost)")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port (default: 1883)")
    parser.add_argument(
        "--speed",
        type=float,
        default=10.0,
        help="Simulation speed multiplier — how many scheduled seconds pass per real second (default: 10)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Real-time seconds between MQTT publishes (default: 2)",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="Minimum delay in seconds (default: 60)",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=600.0,
        metavar="SECONDS",
        help="Maximum delay in seconds (default: 600)",
    )
    parser.add_argument(
        "--delay-drift",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Max seconds the delay can drift per tick (default: 5)",
    )
    parser.add_argument(
        "--list-trips",
        action="store_true",
        help="List available trips in the GTFS zip and exit",
    )
    args = parser.parse_args()

    gtfs = load_gtfs(args.gtfs)

    if args.list_trips:
        print(f"Trips in {args.gtfs}:")
        for t in gtfs["trips"]:
            n_stops = sum(1 for st in gtfs["stop_times"] if st["trip_id"] == t["trip_id"])
            stop_times_for_trip = sorted(
                [st for st in gtfs["stop_times"] if st["trip_id"] == t["trip_id"]],
                key=lambda r: int(r["stop_sequence"]),
            )
            time_range = ""
            if stop_times_for_trip:
                time_range = f"  {stop_times_for_trip[0]['departure_time']} → {stop_times_for_trip[-1]['arrival_time']}"
            print(
                f"  {t['trip_id']:<20} route={t['route_id']:<10} "
                f"shape={t['shape_id']:<20} stops={n_stops}{time_range}"
            )
        return 0

    trips_by_id = {t["trip_id"]: t for t in gtfs["trips"]}
    trip_id = args.trip or gtfs["trips"][0]["trip_id"]
    if trip_id not in trips_by_id:
        print(f"Error: trip '{trip_id}' not found. Use --list-trips to see available trips.")
        return 1

    trip = trips_by_id[trip_id]
    shape_id = trip["shape_id"]
    route_id = trip["route_id"]  # for display only

    delay_seconds = random.uniform(args.min_delay, args.max_delay)

    # Build shape polyline
    shape_rows = [r for r in gtfs["shapes"] if r["shape_id"] == shape_id]
    if not shape_rows:
        print(f"Error: no shape points found for shape_id '{shape_id}'")
        return 1
    shape_points, shape_dists = build_shape(shape_rows)

    # Load and sort stop times
    stop_times = sorted(
        [st for st in gtfs["stop_times"] if st["trip_id"] == trip_id],
        key=lambda r: int(r["stop_sequence"]),
    )
    if not stop_times:
        print(f"Error: no stop times found for trip '{trip_id}'")
        return 1

    # Build schedule: (absolute_seconds, shape_dist_m)
    stop_schedule = map_stops_to_shape(stop_times, gtfs["stops"], shape_points, shape_dists)
    trip_duration = stop_schedule[-1][0] - stop_schedule[0][0]

    print(f"Trip:       {trip_id}  (route {route_id})")
    print(f"Shape:      {shape_id}  ({len(shape_points)} pts, {shape_dists[-1] / 1000:.1f} km)")
    print(f"Stops:      {len(stop_times)}")
    print(f"Schedule:   {stop_times[0]['departure_time']} → {stop_times[-1]['arrival_time']}")
    print(f"Duration:   {trip_duration // 60:.0f} min  ({trip_duration}s scheduled)")
    print(f"Delay:      {args.min_delay:.0f}–{args.max_delay:.0f}s (random walk, drift ±{args.delay_drift:.0f}s/tick, starting {delay_seconds:.0f}s)")
    print(f"Speed:      {args.speed}x  →  real runtime ≈ {trip_duration / args.speed / 60:.1f} min")
    print(f"Interval:   {args.interval}s between publishes")
    print(f"MQTT:       {args.broker}:{args.port}  topic=owntracks/{args.driver}/{trip_id}")
    print()

    # Connect MQTT
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"gtfs-sim-{trip_id}",
    )
    client.username_pw_set(args.driver, args.password)

    print(f"Connecting to {args.broker}:{args.port}…")
    client.connect(args.broker, args.port, keepalive=60)
    client.loop_start()
    print("Connected. Starting simulation (Ctrl-C to stop).\n")

    topic = f"owntracks/{args.driver}/{trip_id}"
    real_start = time.time()

    try:
        while True:
            real_elapsed = time.time() - real_start
            schedule_elapsed = real_elapsed * args.speed  # simulated seconds into trip

            if schedule_elapsed >= trip_duration:
                print("Trip complete.")
                break

            # Position
            dist = shape_dist_at(schedule_elapsed, stop_schedule)
            lat, lon, hdg = position_at_dist(shape_points, shape_dists, dist)

            # Scheduled speed: metres per simulated second (from 1s lookahead)
            dist_1s = shape_dist_at(schedule_elapsed + 1, stop_schedule)
            speed_ms = dist_1s - dist  # m/s in scheduled time
            speed_kmh = speed_ms * 3.6

            delay_seconds += random.uniform(-args.delay_drift, args.delay_drift)
            delay_seconds = max(args.min_delay, min(args.max_delay, delay_seconds))

            stop_idx = current_stop_index(schedule_elapsed, stop_schedule)
            pct = schedule_elapsed / trip_duration * 100

            payload = {
                "_type": "location",
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "tst": int(time.time()),
                "vel": round(speed_kmh, 1),
                "cog": round(hdg, 1),
                "acc": 5,
                "tid": args.driver[:2].upper(),
                "t": "t",
            }

            result = client.publish(topic, json.dumps(payload), qos=1)
            status = "✓" if result.rc == mqtt.MQTT_ERR_SUCCESS else f"ERR({result.rc})"

            print(
                f"[{time.strftime('%H:%M:%S')}] {pct:5.1f}%  "
                f"stop {stop_idx + 1}/{len(stop_times)}  "
                f"lat={lat:.5f}  lon={lon:.5f}  "
                f"hdg={hdg:5.1f}°  spd={speed_kmh:5.1f} km/h  "
                f"delay={delay_seconds:+.0f}s  {status}"
            )

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        client.loop_stop()
        client.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

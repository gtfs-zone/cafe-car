#!/usr/bin/env python3
# /// script
# dependencies = ["httpx>=0.27"]
# ///
"""Simulate a real GTFS trip along its shape, POSTing to cafe-car's ingest API.

The simulation starts at the position the bus would actually be at right now
according to the GTFS schedule, with a random (or fixed) delay of 5–10 minutes.
Today's date is used so the trip runs in wall-clock sync when --speed 1 is used.

Positions are POSTed to cafe-car's `/ingest/position` (the direct HTTP ingest
seam), authenticated with a shared bearer token. Each trip reports under
tracker_id=tracker, trip_id=<trip>, so cafe-car's `vehicle:{tracker}:*` scan
picks it up.

Usage:
    # List available routes in the GTFS zip:
    uv run scripts/simulate_trip.py --list-routes

    # List available trips in the GTFS zip:
    uv run scripts/simulate_trip.py --list-trips

    # Simulate trip WCCWB at 10x speed, publishing every 2s:
    uv run scripts/simulate_trip.py --trip WCCWB

    # Simulate two trips in parallel:
    uv run scripts/simulate_trip.py --trip WCCWB --trip ELLSWB

    # Simulate all trips for one or more routes:
    uv run scripts/simulate_trip.py --route 1 --route 2

    # Pick N random trips from the feed:
    uv run scripts/simulate_trip.py --n-trips 10 --speed 20

    # Simulate every trip (load test):
    uv run scripts/simulate_trip.py --all-trips --speed 50 --quiet

    # Custom tracker, ingest endpoint, speed and interval:
    uv run scripts/simulate_trip.py --tracker <tracker-id> \\
        --ingest-url http://localhost:8000 --token dev-ingest-token \\
        --trip ELLSWB --speed 30 --interval 1

    # Custom delay range (seconds):
    uv run scripts/simulate_trip.py --min-delay 30 --max-delay 300 --delay-drift 10

    # Device mode — emulate the Traccar Client app (posts fixes to :5055). The
    # trip is resolved server-side from the tracker's rules, so no trip_id is
    # sent; --trip only selects which shape to drive along. Requires a provisioned
    # Traccar device whose uniqueId == <tracker-id>.
    uv run scripts/simulate_trip.py --mode device --tracker <tracker-id> \\
        --trip WCCWB --speed 30 --real-time

Note: --tracker must equal a provisioned Tracker.id (see scripts/provision_source.py).
The default "bob" writes to a Redis key no feed scans and will not surface.
"""

import argparse
import csv
import datetime
import io
import math
import random
import threading
import time
import zipfile
import zoneinfo

import httpx

# The Traccar Client app carries speed in knots; vehicle-poser converts it back to
# m/s with this same factor. Used only by --mode device.
KNOTS_TO_MS = 0.514444

# ---------------------------------------------------------------------------
# GTFS loading
# ---------------------------------------------------------------------------


def load_gtfs(zip_path: str) -> dict:
    z = zipfile.ZipFile(zip_path)

    def read_table(name: str) -> list[dict]:
        data = z.read(name).decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(data)))

    agency = read_table("agency.txt")
    agency_timezone = agency[0]["agency_timezone"] if agency else "UTC"
    return {
        "trips": read_table("trips.txt"),
        "stop_times": read_table("stop_times.txt"),
        "stops": {r["stop_id"]: r for r in read_table("stops.txt")},
        "shapes": read_table("shapes.txt"),
        "routes": {r["route_id"]: r for r in read_table("routes.txt")},
        "agency_timezone": agency_timezone,
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
        description="Simulate a GTFS trip and publish positions + trip-updates to cafe-car ingest."
    )
    parser.add_argument(
        "--gtfs",
        default="example_data/west_gtfs.zip",
        help="Path to GTFS zip (default: example_data/west_gtfs.zip)",
    )
    parser.add_argument(
        "--trip",
        action="append",
        default=[],
        metavar="TRIP_ID",
        help="Trip ID to simulate (repeatable: --trip A --trip B)",
    )
    parser.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="ROUTE_ID",
        help="Simulate all trips for route (repeatable)",
    )
    parser.add_argument(
        "--n-trips",
        type=int,
        metavar="N",
        help="Pick N random trips from the feed",
    )
    parser.add_argument(
        "--all-trips",
        action="store_true",
        help="Simulate every trip in the GTFS zip",
    )
    parser.add_argument(
        "--tracker",
        default="bob",
        help="Tracker id — must equal a provisioned Tracker.id (default: bob, which "
        "won't surface in any feed). In device mode it is also the Traccar device "
        "uniqueId, so the device must exist (see scripts/provision_source.py).",
    )
    parser.add_argument(
        "--mode",
        choices=("ingest", "device"),
        default="ingest",
        help="ingest: POST explicit trip_id to cafe-car /ingest (default). "
        "device: emulate the Traccar Client app by posting fixes to Traccar :5055; "
        "the trip is resolved server-side from the tracker's rules (no --trip sent).",
    )
    parser.add_argument(
        "--ingest-url",
        default="http://localhost:8000",
        help="cafe-car base URL for the ingest API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--traccar-url",
        default="http://localhost:5055",
        help="Traccar Client endpoint for --mode device (default: http://localhost:5055)",
    )
    parser.add_argument(
        "--token",
        default="dev-ingest-token",
        help="Ingest API bearer token (default: dev-ingest-token)",
    )
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
        help="Real-time seconds between ingest publishes (default: 2)",
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
        "--real-time",
        action="store_true",
        help="Use actual wall-clock time for tst instead of simulated scheduled time + delay",
    )
    parser.add_argument(
        "--list-trips",
        action="store_true",
        help="List available trips in the GTFS zip and exit",
    )
    parser.add_argument(
        "--list-routes",
        action="store_true",
        help="List available routes in the GTFS zip and exit",
    )
    parser.add_argument(
        "--override-trip-id",
        metavar="TRIP_ID",
        help="Publish under this trip ID instead of the GTFS trip ID (geometry still comes from --trip)",
    )
    args = parser.parse_args()

    gtfs = load_gtfs(args.gtfs)

    if args.list_routes:
        print(f"Routes in {args.gtfs}:")
        trips_by_route: dict[str, list] = {}
        for t in gtfs["trips"]:
            trips_by_route.setdefault(t["route_id"], []).append(t["trip_id"])
        for route_id, route in gtfs["routes"].items():
            n_trips = len(trips_by_route.get(route_id, []))
            short = route.get("route_short_name", "")
            long = route.get("route_long_name", "")
            name = f"{short} — {long}" if short and long else short or long
            print(f"  {route_id:<20} {name:<40} trips={n_trips}")
        return 0

    if args.list_trips:
        trips = gtfs["trips"]
        if args.route:
            trips = [t for t in trips if t["route_id"] in args.route]
        print(f"Trips in {args.gtfs}" + (f" (route: {', '.join(args.route)})" if args.route else "") + ":")
        for t in trips:
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

    # Resolve the set of trip IDs to simulate
    trip_ids: set[str] = set()
    for t in args.trip:
        trip_ids.add(t)
    for route in args.route:
        trip_ids |= {t["trip_id"] for t in gtfs["trips"] if t["route_id"] == route}
    if args.all_trips:
        trip_ids = {t["trip_id"] for t in gtfs["trips"]}
    if args.n_trips:
        pool = list({t["trip_id"] for t in gtfs["trips"]} - trip_ids)
        trip_ids |= set(random.sample(pool, min(args.n_trips, len(pool))))
    if not trip_ids:
        # Default: first trip (existing behaviour)
        trip_ids = {gtfs["trips"][0]["trip_id"]}

    # Validate all IDs
    unknown = trip_ids - trips_by_id.keys()
    if unknown:
        print(f"Error: unknown trip IDs: {', '.join(sorted(unknown))}. Use --list-trips to see available trips.")
        return 1

    if args.override_trip_id and len(trip_ids) > 1:
        print("Error: --override-trip-id can only be used with a single --trip.")
        return 1

    if args.override_trip_id and args.mode == "device":
        # In device mode the trip is resolved server-side from the tracker's rules;
        # there is no client-supplied trip_id to override.
        print("Error: --override-trip-id is ingest-only, not valid in device mode.")
        return 1

    threads = []
    for tid in sorted(trip_ids):
        t = threading.Thread(target=run_trip, args=(tid, gtfs, args), daemon=True)
        threads.append(t)
        t.start()

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nInterrupted — waiting for threads to finish.")

    return 0


def run_trip(trip_id: str, gtfs: dict, args: argparse.Namespace) -> None:
    published_trip_id = args.override_trip_id or trip_id
    prefix = f"[{published_trip_id}]"
    trips_by_id = {t["trip_id"]: t for t in gtfs["trips"]}
    trip = trips_by_id[trip_id]
    shape_id = trip["shape_id"]
    route_id = trip["route_id"]

    delay_seconds = random.uniform(args.min_delay, args.max_delay)

    # Build shape polyline
    shape_rows = [r for r in gtfs["shapes"] if r["shape_id"] == shape_id]
    if not shape_rows:
        print(f"{prefix} Error: no shape points found for shape_id '{shape_id}'")
        return
    shape_points, shape_dists = build_shape(shape_rows)

    # Load and sort stop times
    stop_times = sorted(
        [st for st in gtfs["stop_times"] if st["trip_id"] == trip_id],
        key=lambda r: int(r["stop_sequence"]),
    )
    if not stop_times:
        print(f"{prefix} Error: no stop times found for trip '{trip_id}'")
        return

    # Build schedule: (absolute_seconds, shape_dist_m)
    stop_schedule = map_stops_to_shape(stop_times, gtfs["stops"], shape_points, shape_dists)
    trip_duration = stop_schedule[-1][0] - stop_schedule[0][0]

    gtfs_label = f"{trip_id} → {published_trip_id}" if published_trip_id != trip_id else trip_id
    print(f"{prefix} Trip:       {gtfs_label}  (route {route_id})")
    print(f"{prefix} Shape:      {shape_id}  ({len(shape_points)} pts, {shape_dists[-1] / 1000:.1f} km)")
    print(f"{prefix} Stops:      {len(stop_times)}")
    print(f"{prefix} Schedule:   {stop_times[0]['departure_time']} → {stop_times[-1]['arrival_time']}")
    print(f"{prefix} Duration:   {trip_duration // 60:.0f} min  ({trip_duration}s scheduled)")
    print(f"{prefix} Delay:      {args.min_delay:.0f}–{args.max_delay:.0f}s (random walk, drift ±{args.delay_drift:.0f}s/tick, starting {delay_seconds:.0f}s)")
    print(f"{prefix} Speed:      {args.speed}x  →  real runtime ≈ {trip_duration / args.speed / 60:.1f} min")
    if args.mode == "device":
        print(f"{prefix} Device:     {args.traccar_url}?id={args.tracker}  (trip resolved server-side from rules)")
    else:
        print(f"{prefix} Ingest:     {args.ingest_url}/ingest/position  tracker_id={args.tracker}  trip_id={published_trip_id}")
    print()

    position_url = f"{args.ingest_url.rstrip('/')}/ingest/position"
    trip_update_url = f"{args.ingest_url.rstrip('/')}/ingest/trip-update"
    if args.mode == "device":
        client = httpx.Client(timeout=10.0)
    else:
        headers = {"Authorization": f"Bearer {args.token}"}
        client = httpx.Client(headers=headers, timeout=10.0)

    print(f"{prefix} Starting simulation (Ctrl-C to stop).\n")

    real_start = time.time()
    tz = zoneinfo.ZoneInfo(gtfs["agency_timezone"])
    today_midnight = int(datetime.datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    try:
        while True:
            real_elapsed = time.time() - real_start
            schedule_elapsed = real_elapsed * args.speed  # simulated seconds into trip

            if schedule_elapsed >= trip_duration:
                print(f"{prefix} Trip complete.")
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

            if args.real_time:
                tst = int(time.time())
            else:
                tst = today_midnight + int(stop_schedule[0][0] + schedule_elapsed + delay_seconds)

            body = {
                "tracker_id": args.tracker,
                "trip_id": published_trip_id,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "bearing": round(hdg, 1),
                "speed": round(speed_ms, 4),
                "timestamp": tst,
                "route_id": route_id,
            }

            # The sim already knows the current stop and its delay, so in ingest
            # mode it emits the trip-update directly rather than leaving
            # trip-updogger to recompute it. (Device mode sends no trip-update at
            # all — the fix goes to Traccar and trip-updogger derives the
            # prediction from it, which is the path a real driver exercises.)
            current_stop = stop_times[stop_idx]
            trip_update_body = {
                "trip_id": published_trip_id,
                "tracker_id": args.tracker,
                "timestamp": tst,
                "stop_time_updates": [
                    {
                        "stop_id": current_stop["stop_id"],
                        "stop_sequence": int(current_stop["stop_sequence"]),
                        "arrival_delay": int(delay_seconds),
                        "departure_delay": int(delay_seconds),
                    }
                ],
            }

            try:
                if args.mode == "device":
                    # Emulate the Traccar Client app: one location fix to :5055.
                    # No trip_id — vehicle-poser resolves it from the tracker's
                    # rules. Speed is carried in knots (the app's wire unit).
                    client.post(
                        args.traccar_url,
                        params={
                            "id": args.tracker,
                            "lat": round(lat, 6),
                            "lon": round(lon, 6),
                            "bearing": round(hdg, 1),
                            "speed": round(speed_ms / KNOTS_TO_MS, 4),
                            "timestamp": tst,
                        },
                    ).raise_for_status()
                else:
                    client.post(position_url, json=body).raise_for_status()
                    client.post(
                        trip_update_url, json=trip_update_body
                    ).raise_for_status()
                status = "✓"
            except httpx.HTTPError as exc:
                status = f"ERR({exc})"

            print(
                f"{prefix} [{time.strftime('%H:%M:%S')}] {pct:5.1f}%  "
                f"stop {stop_idx + 1}/{len(stop_times)}  "
                f"lat={lat:.5f}  lon={lon:.5f}  "
                f"hdg={hdg:5.1f}°  spd={speed_kmh:5.1f} km/h  "
                f"delay={delay_seconds:+.0f}s  {status}"
            )

            time.sleep(args.interval)

    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

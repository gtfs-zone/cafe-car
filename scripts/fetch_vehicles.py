#!/usr/bin/env python3
"""Fetch and inspect a vehicle_positions.pb GTFS-RT feed."""

import argparse
import sys
import urllib.request
from datetime import datetime, timezone

from google.transit import gtfs_realtime_pb2
from google.protobuf.message import DecodeError

INCREMENTALITY = {
    0: "FULL_DATASET",
    1: "DIFFERENTIAL",
}

VEHICLE_STATUS = {
    0: "INCOMING_AT",
    1: "STOPPED_AT",
    2: "IN_TRANSIT_TO",
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/x-protobuf"})
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def parse(data: bytes) -> gtfs_realtime_pb2.FeedMessage:
    msg = gtfs_realtime_pb2.FeedMessage()
    try:
        msg.ParseFromString(data)
    except DecodeError as e:
        print(f"ERROR: Failed to parse protobuf: {e}", file=sys.stderr)
        sys.exit(1)
    return msg


def fmt_ts(ts: int) -> str:
    if ts == 0:
        return "0 (unset)"
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def print_summary(msg: gtfs_realtime_pb2.FeedMessage) -> None:
    h = msg.header
    print(f"GTFS-RT version : {h.gtfs_realtime_version}")
    print(f"Timestamp       : {fmt_ts(h.timestamp)}")
    print(f"Incrementality  : {INCREMENTALITY.get(h.incrementality, h.incrementality)}")
    print(f"Entities        : {len(msg.entity)}")
    print()

    vehicles = [e for e in msg.entity if e.HasField("vehicle")]
    if not vehicles:
        print("No vehicle entities found.")
        return

    print(f"{'ID':<20} {'Label':<15} {'Trip':<20} {'Route':<15} {'Lat':>10} {'Lon':>11} {'Bear':>6} {'Speed':>7} {'Status':<15}")
    print("-" * 120)
    for e in vehicles:
        v = e.vehicle
        print(
            f"{v.vehicle.id:<20} "
            f"{v.vehicle.label:<15} "
            f"{v.trip.trip_id:<20} "
            f"{v.trip.route_id:<15} "
            f"{v.position.latitude:>10.5f} "
            f"{v.position.longitude:>11.5f} "
            f"{v.position.bearing:>6.1f} "
            f"{v.position.speed:>7.2f} "
            f"{VEHICLE_STATUS.get(v.current_status, str(v.current_status)):<15}"
        )


def print_full(msg: gtfs_realtime_pb2.FeedMessage) -> None:
    print_summary(msg)
    print()
    for e in msg.entity:
        print(f"=== Entity: {e.id} ===")
        print(e)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and inspect a GTFS-RT vehicle_positions.pb feed."
    )
    parser.add_argument(
        "feed",
        help="Feed name (e.g. 'my-feed')",
    )
    parser.add_argument(
        "--backend",
        default="http://localhost:8000",
        help="Base URL of the API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a compact one-line-per-vehicle summary table (default: full protobuf dump)",
    )
    args = parser.parse_args()

    url = f"{args.backend.rstrip('/')}/{args.feed}/vehicle_positions.pb"
    print(f"Fetching: {url}", file=sys.stderr)

    data = fetch(url)
    print(f"Received: {len(data)} bytes", file=sys.stderr)
    print(file=sys.stderr)

    msg = parse(data)

    if args.summary:
        print_summary(msg)
    else:
        print_full(msg)


if __name__ == "__main__":
    main()

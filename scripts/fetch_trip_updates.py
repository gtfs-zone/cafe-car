#!/usr/bin/env python3
"""Fetch and inspect a trip_updates.pb GTFS-RT feed."""

import argparse
import sys
import urllib.request
from datetime import UTC, datetime

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

INCREMENTALITY = {
    0: "FULL_DATASET",
    1: "DIFFERENTIAL",
}

SCHEDULE_RELATIONSHIP = {
    0: "SCHEDULED",
    1: "ADDED",
    2: "UNSCHEDULED",
    3: "CANCELED",
    5: "REPLACEMENT",
    6: "DUPLICATED",
    7: "DELETED",
}

STOP_SCHEDULE_RELATIONSHIP = {
    0: "SCHEDULED",
    1: "SKIPPED",
    2: "NO_DATA",
    3: "UNSCHEDULED",
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
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M:%S")


def fmt_delay(delay: int) -> str:
    if delay == 0:
        return "  0s"
    sign = "+" if delay > 0 else "-"
    return f"{sign}{abs(delay)}s"


def print_summary(msg: gtfs_realtime_pb2.FeedMessage) -> None:
    h = msg.header
    print(f"GTFS-RT version : {h.gtfs_realtime_version}")
    print(f"Timestamp       : {fmt_ts(h.timestamp)}")
    print(f"Incrementality  : {INCREMENTALITY.get(h.incrementality, h.incrementality)}")
    print(f"Entities        : {len(msg.entity)}")
    print()

    updates = [e for e in msg.entity if e.HasField("trip_update")]
    if not updates:
        print("No trip_update entities found.")
        return

    print(
        f"{'Trip ID':<25} {'Route':<12} {'Vehicle':<15} {'Stops':>5} {'Next Stop':<20} {'Arr':>9} {'Dep':>9} {'Delay':>7} {'Sched Rel':<14}"
    )
    print("-" * 125)
    for e in updates:
        tu = e.trip_update
        trip_id = tu.trip.trip_id or "-"
        route_id = tu.trip.route_id or "-"
        vehicle_id = tu.vehicle.id if tu.HasField("vehicle") else "-"
        stop_count = len(tu.stop_time_update)

        if tu.stop_time_update:
            next_stu = tu.stop_time_update[0]
            next_stop = next_stu.stop_id or "-"
            arr = fmt_ts(next_stu.arrival.time) if next_stu.HasField("arrival") else "-"
            dep = (
                fmt_ts(next_stu.departure.time)
                if next_stu.HasField("departure")
                else "-"
            )
            delay_val = (
                next_stu.arrival.delay
                if next_stu.HasField("arrival")
                else next_stu.departure.delay
                if next_stu.HasField("departure")
                else 0
            )
            delay = fmt_delay(delay_val) if delay_val != 0 else "-"
        else:
            next_stop = arr = dep = "-"
            delay = fmt_delay(tu.delay) if tu.delay != 0 else "-"

        sched_rel = SCHEDULE_RELATIONSHIP.get(
            tu.trip.schedule_relationship, str(tu.trip.schedule_relationship)
        )

        print(
            f"{trip_id:<25} "
            f"{route_id:<12} "
            f"{vehicle_id:<15} "
            f"{stop_count:>5} "
            f"{next_stop:<20} "
            f"{arr:>9} "
            f"{dep:>9} "
            f"{delay:>7} "
            f"{sched_rel:<14}"
        )


def print_full(msg: gtfs_realtime_pb2.FeedMessage) -> None:
    print_summary(msg)
    print()
    for e in msg.entity:
        if not e.HasField("trip_update"):
            continue
        tu = e.trip_update
        trip_id = tu.trip.trip_id or "-"
        route_id = tu.trip.route_id or "-"
        sched_rel = SCHEDULE_RELATIONSHIP.get(
            tu.trip.schedule_relationship, str(tu.trip.schedule_relationship)
        )
        print(f"=== Trip: {trip_id}  Route: {route_id}  [{sched_rel}] ===")
        if tu.HasField("vehicle"):
            print(f"  Vehicle: {tu.vehicle.id}")
        if tu.delay != 0:
            print(f"  Delay (top-level): {fmt_delay(tu.delay)}")
        for stu in tu.stop_time_update:
            stop_sched_rel = STOP_SCHEDULE_RELATIONSHIP.get(
                stu.schedule_relationship, str(stu.schedule_relationship)
            )
            arr = (
                f"arr={fmt_ts(stu.arrival.time)} delay={fmt_delay(stu.arrival.delay)}"
                if stu.HasField("arrival")
                else "arr=-"
            )
            dep = (
                f"dep={fmt_ts(stu.departure.time)} delay={fmt_delay(stu.departure.delay)}"
                if stu.HasField("departure")
                else "dep=-"
            )
            print(
                f"  stop={stu.stop_id:<20} seq={stu.stop_sequence:<5} {arr:<35} {dep:<35} [{stop_sched_rel}]"
            )
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and inspect a GTFS-RT trip_updates.pb feed."
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
        help="Print a compact one-line-per-trip summary table (default: full stop-time dump)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also print the raw feed as JSON before the normal output",
    )
    args = parser.parse_args()

    url = f"{args.backend.rstrip('/')}/{args.feed}/trip_updates.pb"
    print(f"Fetching: {url}", file=sys.stderr)

    data = fetch(url)
    print(f"Received: {len(data)} bytes", file=sys.stderr)
    print(file=sys.stderr)

    msg = parse(data)

    if args.verbose:
        print(json_format.MessageToJson(msg, indent=2))
        print()

    if args.summary:
        print_summary(msg)
    else:
        print_full(msg)


if __name__ == "__main__":
    main()

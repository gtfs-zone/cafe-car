#!/usr/bin/env python3
"""Fetch and inspect a service_alerts.pb GTFS-RT feed."""

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

CAUSE_NAMES = {v: k for k, v in gtfs_realtime_pb2.Alert.Cause.items()}
EFFECT_NAMES = {v: k for k, v in gtfs_realtime_pb2.Alert.Effect.items()}
SEVERITY_NAMES = {v: k for k, v in gtfs_realtime_pb2.Alert.SeverityLevel.items()}


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
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_window(alert) -> str:
    periods = alert.active_period
    if not periods:
        return "always"
    parts = []
    for p in periods:
        start = fmt_ts(p.start) if p.start else "*"
        end = fmt_ts(p.end) if p.end else "*"
        parts.append(f"{start} → {end}")
    return "; ".join(parts)


def print_summary(msg: gtfs_realtime_pb2.FeedMessage) -> None:
    h = msg.header
    print(f"GTFS-RT version : {h.gtfs_realtime_version}")
    print(f"Timestamp       : {fmt_ts(h.timestamp)}")
    print(f"Incrementality  : {INCREMENTALITY.get(h.incrementality, h.incrementality)}")
    print(f"Entities        : {len(msg.entity)}")
    print()

    alerts = [e for e in msg.entity if e.HasField("alert")]
    if not alerts:
        print("No service_alert entities found.")
        return

    print(f"{'Cause':<20} {'Effect':<20} {'Header':<45} {'Active Window'}")
    print("-" * 120)
    for e in alerts:
        a = e.alert
        cause = CAUSE_NAMES.get(a.cause, str(a.cause))
        effect = EFFECT_NAMES.get(a.effect, str(a.effect))
        header = ""
        if a.header_text.translation:
            header = a.header_text.translation[0].text
        window = fmt_window(a)
        print(f"{cause:<20} {effect:<20} {header[:44]:<45} {window}")


def print_full(msg: gtfs_realtime_pb2.FeedMessage) -> None:
    print_summary(msg)
    print()
    for e in msg.entity:
        if not e.HasField("alert"):
            continue
        a = e.alert
        cause = CAUSE_NAMES.get(a.cause, str(a.cause))
        effect = EFFECT_NAMES.get(a.effect, str(a.effect))
        severity = SEVERITY_NAMES.get(a.severity_level, str(a.severity_level))
        header = a.header_text.translation[0].text if a.header_text.translation else "-"
        desc = (
            a.description_text.translation[0].text
            if a.description_text.translation
            else "-"
        )
        url = a.url.translation[0].text if a.url.translation else "-"

        print(f"=== Alert ID: {e.id} ===")
        print(f"  Cause         : {cause}")
        print(f"  Effect        : {effect}")
        print(f"  Severity      : {severity}")
        print(f"  Header        : {header}")
        print(f"  Description   : {desc}")
        print(f"  URL           : {url}")
        print(f"  Active window : {fmt_window(a)}")
        for sel in a.informed_entity:
            parts = []
            if sel.agency_id:
                parts.append(f"agency={sel.agency_id}")
            if sel.route_id:
                parts.append(f"route={sel.route_id}")
            if sel.stop_id:
                parts.append(f"stop={sel.stop_id}")
            if sel.HasField("trip") and sel.trip.trip_id:
                parts.append(f"trip={sel.trip.trip_id}")
            print(f"  Informed entity: {', '.join(parts) or '(none)'}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and inspect a GTFS-RT service_alerts.pb feed."
    )
    parser.add_argument("feed", help="Feed name (e.g. 'my-feed')")
    parser.add_argument(
        "--backend",
        default="http://localhost:8000",
        help="Base URL of the API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a compact one-line-per-alert summary table (default: full dump)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also print the raw feed as JSON before the normal output",
    )
    args = parser.parse_args()

    url = f"{args.backend.rstrip('/')}/{args.feed}/service_alerts.pb"
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

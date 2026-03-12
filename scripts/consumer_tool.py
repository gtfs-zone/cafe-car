#!/usr/bin/env python3
"""Unified GTFS-RT consumer tool: fetch, display, follow, and map all feed types."""

import argparse
import contextlib
import json
import os
import queue
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

INCREMENTALITY = {0: "FULL_DATASET", 1: "DIFFERENTIAL"}

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

VEHICLE_STATUS = {
    0: "INCOMING_AT",
    1: "STOPPED_AT",
    2: "IN_TRANSIT_TO",
}

CAUSE_NAMES = {v: k for k, v in gtfs_realtime_pb2.Alert.Cause.items()}
EFFECT_NAMES = {v: k for k, v in gtfs_realtime_pb2.Alert.Effect.items()}
SEVERITY_NAMES = {v: k for k, v in gtfs_realtime_pb2.Alert.SeverityLevel.items()}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def fetch(url: str, timeout: int = 10) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/x-protobuf"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse(data: bytes, label: str) -> gtfs_realtime_pb2.FeedMessage:
    msg = gtfs_realtime_pb2.FeedMessage()
    try:
        msg.ParseFromString(data)
    except DecodeError as e:
        print(f"ERROR: Failed to parse {label} protobuf: {e}", file=sys.stderr)
        sys.exit(1)
    return msg


def parse_trip_updates(data: bytes) -> gtfs_realtime_pb2.FeedMessage:
    return _parse(data, "trip_updates")


def parse_vehicles(data: bytes) -> gtfs_realtime_pb2.FeedMessage:
    return _parse(data, "vehicle_positions")


def parse_service_alerts(data: bytes) -> gtfs_realtime_pb2.FeedMessage:
    return _parse(data, "service_alerts")


def collect(args) -> dict:
    base = args.backend.rstrip("/")
    result = {"trip_updates": None, "vehicles": None, "service_alerts": None}
    if args.trip_updates:
        try:
            data = fetch(f"{base}/{args.feed}/trip_updates.pb", args.timeout)
            result["trip_updates"] = parse_trip_updates(data)
        except Exception as exc:
            print(f"WARN: trip_updates fetch failed: {exc}", file=sys.stderr)
    if args.vehicles:
        try:
            data = fetch(f"{base}/{args.feed}/vehicle_positions.pb", args.timeout)
            result["vehicles"] = parse_vehicles(data)
        except Exception as exc:
            print(f"WARN: vehicle_positions fetch failed: {exc}", file=sys.stderr)
    if args.service_alerts:
        try:
            data = fetch(f"{base}/{args.feed}/service_alerts.pb", args.timeout)
            result["service_alerts"] = parse_service_alerts(data)
        except Exception as exc:
            print(f"WARN: service_alerts fetch failed: {exc}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Trip updates display (from fetch_trip_updates.py)
# ---------------------------------------------------------------------------


def _fmt_ts_tu(ts: int) -> str:
    if ts == 0:
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M:%S")


def _fmt_delay(delay: int) -> str:
    if delay == 0:
        return "  0s"
    sign = "+" if delay > 0 else "-"
    return f"{sign}{abs(delay)}s"


def print_trip_updates(
    msg: gtfs_realtime_pb2.FeedMessage, summary: bool, verbose: bool
) -> None:
    if verbose:
        print(json_format.MessageToJson(msg, indent=2))
        print()

    h = msg.header
    print(f"GTFS-RT version : {h.gtfs_realtime_version}")
    print(f"Timestamp       : {_fmt_ts_tu(h.timestamp)}")
    print(f"Incrementality  : {INCREMENTALITY.get(h.incrementality, h.incrementality)}")
    print(f"Entities        : {len(msg.entity)}")
    print()

    updates = [e for e in msg.entity if e.HasField("trip_update")]
    if not updates:
        print("No trip_update entities found.")
        return

    print(
        f"{'Trip ID':<25} {'Route':<12} {'Vehicle':<15} {'Stops':>5} "
        f"{'Next Stop':<20} {'Arr':>9} {'Dep':>9} {'Delay':>7} {'Sched Rel':<14}"
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
            has_arr = next_stu.HasField("arrival")
            has_dep = next_stu.HasField("departure")
            arr = _fmt_ts_tu(next_stu.arrival.time) if has_arr else "-"
            dep = _fmt_ts_tu(next_stu.departure.time) if has_dep else "-"
            delay_val = (
                next_stu.arrival.delay
                if has_arr
                else next_stu.departure.delay
                if has_dep
                else 0
            )
            delay = _fmt_delay(delay_val) if delay_val != 0 else "-"
        else:
            next_stop = arr = dep = "-"
            delay = _fmt_delay(tu.delay) if tu.delay != 0 else "-"

        sched_rel = SCHEDULE_RELATIONSHIP.get(
            tu.trip.schedule_relationship, str(tu.trip.schedule_relationship)
        )

        if summary:
            print(
                f"{trip_id:<25} {route_id:<12} {vehicle_id:<15} {stop_count:>5} "
                f"{next_stop:<20} {arr:>9} {dep:>9} {delay:>7} {sched_rel:<14}"
            )
        else:
            sched_rel_full = SCHEDULE_RELATIONSHIP.get(
                tu.trip.schedule_relationship, str(tu.trip.schedule_relationship)
            )
            print(f"=== Trip: {trip_id}  Route: {route_id}  [{sched_rel_full}] ===")
            if tu.HasField("vehicle"):
                print(f"  Vehicle: {tu.vehicle.id}")
            if tu.delay != 0:
                print(f"  Delay (top-level): {_fmt_delay(tu.delay)}")
            for stu in tu.stop_time_update:
                stop_sched_rel = STOP_SCHEDULE_RELATIONSHIP.get(
                    stu.schedule_relationship, str(stu.schedule_relationship)
                )
                if stu.HasField("arrival"):
                    at = _fmt_ts_tu(stu.arrival.time)
                    ad = _fmt_delay(stu.arrival.delay)
                    arr_s = f"arr={at} delay={ad}"
                else:
                    arr_s = "arr=-"
                if stu.HasField("departure"):
                    dt = _fmt_ts_tu(stu.departure.time)
                    dd = _fmt_delay(stu.departure.delay)
                    dep_s = f"dep={dt} delay={dd}"
                else:
                    dep_s = "dep=-"
                print(
                    f"  stop={stu.stop_id:<20} seq={stu.stop_sequence:<5} "
                    f"{arr_s:<35} {dep_s:<35} [{stop_sched_rel}]"
                )
            print()


# ---------------------------------------------------------------------------
# Vehicle positions display (from fetch_vehicles.py)
# ---------------------------------------------------------------------------


def _fmt_ts_vp(ts: int) -> str:
    if ts == 0:
        return "0 (unset)"
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def print_vehicles(msg: gtfs_realtime_pb2.FeedMessage, summary: bool) -> None:
    h = msg.header
    print(f"GTFS-RT version : {h.gtfs_realtime_version}")
    print(f"Timestamp       : {_fmt_ts_vp(h.timestamp)}")
    print(f"Incrementality  : {INCREMENTALITY.get(h.incrementality, h.incrementality)}")
    print(f"Entities        : {len(msg.entity)}")
    print()

    vehicles = [e for e in msg.entity if e.HasField("vehicle")]
    if not vehicles:
        print("No vehicle entities found.")
        return

    if summary:
        print(
            f"{'ID':<20} {'Label':<15} {'Trip':<20} {'Route':<15} "
            f"{'Lat':>10} {'Lon':>11} {'Bear':>6} {'Speed':>7} {'Status':<15}"
        )
        print("-" * 120)
        for e in vehicles:
            v = e.vehicle
            print(
                f"{v.vehicle.id:<20} {v.vehicle.label:<15} {v.trip.trip_id:<20} "
                f"{v.trip.route_id:<15} {v.position.latitude:>10.5f} "
                f"{v.position.longitude:>11.5f} {v.position.bearing:>6.1f} "
                f"{v.position.speed:>7.2f} "
                f"{VEHICLE_STATUS.get(v.current_status, str(v.current_status)):<15}"
            )
    else:
        print(
            f"{'ID':<20} {'Label':<15} {'Trip':<20} {'Route':<15} "
            f"{'Lat':>10} {'Lon':>11} {'Bear':>6} {'Speed':>7} {'Status':<15}"
        )
        print("-" * 120)
        for e in vehicles:
            v = e.vehicle
            print(
                f"{v.vehicle.id:<20} {v.vehicle.label:<15} {v.trip.trip_id:<20} "
                f"{v.trip.route_id:<15} {v.position.latitude:>10.5f} "
                f"{v.position.longitude:>11.5f} {v.position.bearing:>6.1f} "
                f"{v.position.speed:>7.2f} "
                f"{VEHICLE_STATUS.get(v.current_status, str(v.current_status)):<15}"
            )
        print()
        for e in msg.entity:
            print(f"=== Entity: {e.id} ===")
            print(e)


# ---------------------------------------------------------------------------
# Service alerts display (from fetch_service_alerts.py)
# ---------------------------------------------------------------------------


def _fmt_ts_sa(ts: int) -> str:
    if ts == 0:
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_window(alert) -> str:
    periods = alert.active_period
    if not periods:
        return "always"
    parts = []
    for p in periods:
        start = _fmt_ts_sa(p.start) if p.start else "*"
        end = _fmt_ts_sa(p.end) if p.end else "*"
        parts.append(f"{start} → {end}")
    return "; ".join(parts)


def print_service_alerts(
    msg: gtfs_realtime_pb2.FeedMessage, summary: bool, verbose: bool
) -> None:
    if verbose:
        print(json_format.MessageToJson(msg, indent=2))
        print()

    h = msg.header
    print(f"GTFS-RT version : {h.gtfs_realtime_version}")
    print(f"Timestamp       : {_fmt_ts_sa(h.timestamp)}")
    print(f"Incrementality  : {INCREMENTALITY.get(h.incrementality, h.incrementality)}")
    print(f"Entities        : {len(msg.entity)}")
    print()

    alerts = [e for e in msg.entity if e.HasField("alert")]
    if not alerts:
        print("No service_alert entities found.")
        return

    if summary:
        print(f"{'Cause':<20} {'Effect':<20} {'Header':<45} {'Active Window'}")
        print("-" * 120)
        for e in alerts:
            a = e.alert
            cause = CAUSE_NAMES.get(a.cause, str(a.cause))
            effect = EFFECT_NAMES.get(a.effect, str(a.effect))
            header = (
                a.header_text.translation[0].text if a.header_text.translation else ""
            )
            window = _fmt_window(a)
            print(f"{cause:<20} {effect:<20} {header[:44]:<45} {window}")
    else:
        for e in alerts:
            a = e.alert
            cause = CAUSE_NAMES.get(a.cause, str(a.cause))
            effect = EFFECT_NAMES.get(a.effect, str(a.effect))
            severity = SEVERITY_NAMES.get(a.severity_level, str(a.severity_level))
            header = (
                a.header_text.translation[0].text if a.header_text.translation else "-"
            )
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
            print(f"  Active window : {_fmt_window(a)}")
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


# ---------------------------------------------------------------------------
# Combined print
# ---------------------------------------------------------------------------


def print_all(args, data: dict) -> None:
    if args.trip_updates and data["trip_updates"] is not None:
        print("=== TRIP UPDATES ===")
        print_trip_updates(data["trip_updates"], args.summary, args.verbose)
        print()
    if args.vehicles and data["vehicles"] is not None:
        print("=== VEHICLE POSITIONS ===")
        print_vehicles(data["vehicles"], args.summary)
        print()
    if args.service_alerts and data["service_alerts"] is not None:
        print("=== SERVICE ALERTS ===")
        print_service_alerts(data["service_alerts"], args.summary, args.verbose)
        print()


# ---------------------------------------------------------------------------
# Follow mode
# ---------------------------------------------------------------------------


def run_follow(args) -> None:
    try:
        while True:
            data = collect(args)
            if args.clear:
                os.system("clear")
            print_all(args, data)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Map vis — SSE server + Leaflet HTML
# ---------------------------------------------------------------------------

MAP_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>GTFS-RT Live Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin:0; padding:0; height:100%; }
  body { display:flex; height:100vh; }
  #map { flex:1; min-width:0; }
  #sidebar {
    width:300px; height:100%; overflow-y:auto;
    background:#1e1e1e; color:#ddd; font:13px/1.5 monospace;
    padding:8px; box-sizing:border-box; flex-shrink:0;
  }
  #sidebar h3 { margin:4px 0 8px; color:#7ec8e3; font-size:14px; }
  .vehicle-item, .alert-item {
    border-bottom:1px solid #333; padding:4px 0; font-size:12px;
  }
  .vehicle-item span.label { font-weight:bold; color:#fff; }
  .alert-item .ah { color:#f0c060; }
  #statusbar {
    position:absolute; bottom:8px; left:8px; z-index:9999;
    background:rgba(0,0,0,0.65); color:#fff; padding:4px 10px;
    border-radius:4px; font:12px monospace; pointer-events:none;
  }
</style>
</head>
<body>
<div id="map"></div>
<div id="sidebar">
  <h3>Vehicles</h3>
  <div id="vlist"></div>
  <h3 style="margin-top:12px">Service Alerts</h3>
  <div id="alist"></div>
</div>
<div id="statusbar">Connecting…</div>
<script>
const map = L.map('map').setView([0, 0], 2);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors', maxZoom: 19
}).addTo(map);

const markers = {};
let firstData = true;

function arrowIcon(bearing, color) {
  const rot = bearing || 0;
  const ns = 'http://www.w3.org/2000/svg';
  const pts = '0,-10 6,8 0,4 -6,8';
  const svg = `<svg xmlns="${ns}" width="28" height="28"`
    + ` viewBox="-14 -14 28 28"><g transform="rotate(${rot})">`
    + `<polygon points="${pts}" fill="${color}"`
    + ` stroke="#fff" stroke-width="1.5"/></g></svg>`;
  return L.divIcon({
    html: svg, className: '', iconSize: [28,28], iconAnchor: [14,14]
  });
}

const evtSource = new EventSource('/stream');
evtSource.onmessage = function(e) {
  const d = JSON.parse(e.data);
  const now = new Date(d.timestamp);
  const timeStr = now.toTimeString().slice(0,8);

  // vehicles
  const seen = new Set();
  (d.vehicles || []).forEach(v => {
    const key = v.trip_id || v.id;
    seen.add(key);
    const icon = arrowIcon(v.bearing, '#2980b9');
    const popup = `<b>${v.label || v.id}</b><br>
      trip: ${v.trip_id || '-'}<br>
      route: ${v.route_id || '-'}<br>
      speed: ${v.speed != null ? v.speed.toFixed(1)+' m/s' : '-'}<br>
      bearing: ${v.bearing != null ? v.bearing.toFixed(0)+'°' : '-'}<br>
      status: ${v.status || '-'}`;
    if (markers[key]) {
      markers[key].setLatLng([v.lat, v.lon]).setIcon(icon).setPopupContent(popup);
    } else {
      markers[key] = L.marker([v.lat, v.lon], {icon}).bindPopup(popup).addTo(map);
    }
  });
  // remove stale
  Object.keys(markers).forEach(id => {
    if (!seen.has(id)) { markers[id].remove(); delete markers[id]; }
  });

  // auto-fit on first data
  if (firstData && d.vehicles && d.vehicles.length > 0) {
    firstData = false;
    const latlngs = d.vehicles.map(v => [v.lat, v.lon]);
    map.fitBounds(L.latLngBounds(latlngs).pad(0.2));
  }

  // sidebar vehicles
  const vlist = document.getElementById('vlist');
  vlist.innerHTML = (d.vehicles || []).map(v =>
    `<div class="vehicle-item"><span class="label">${v.label || v.id}</span>
     trip=${v.trip_id||'-'} rt=${v.route_id||'-'}
     ${v.speed!=null?v.speed.toFixed(1)+'m/s':''}</div>`
  ).join('') || '<div style="color:#888">None</div>';

  // sidebar alerts
  const alist = document.getElementById('alist');
  alist.innerHTML = (d.service_alerts || []).map(a =>
    `<div class="alert-item"><span class="ah">${a.header||'(no header)'}</span>
     <br>${a.cause||''} / ${a.effect||''}</div>`
  ).join('') || '<div style="color:#888">None</div>';

  // status bar
  const nv = (d.vehicles||[]).length;
  const na = (d.service_alerts||[]).length;
  document.getElementById('statusbar').textContent =
    `Updated ${timeStr} · ${nv} vehicle${nv!==1?'s':''} · ${na} alert${na!==1?'s':''}`;
};
evtSource.onerror = function() {
  document.getElementById('statusbar').textContent = 'Connection lost — reconnecting…';
};
</script>
</body>
</html>
"""

_client_queues: list[queue.Queue] = []
_client_queues_lock = threading.Lock()


def push_to_map(data: dict) -> None:
    def _veh(msg):
        out = []
        if msg is None:
            return out
        for e in msg.entity:
            if not e.HasField("vehicle"):
                continue
            v = e.vehicle
            out.append(
                {
                    "id": v.vehicle.id,
                    "label": v.vehicle.label,
                    "lat": v.position.latitude,
                    "lon": v.position.longitude,
                    "bearing": v.position.bearing,
                    "speed": v.position.speed,
                    "trip_id": v.trip.trip_id,
                    "route_id": v.trip.route_id,
                    "status": VEHICLE_STATUS.get(
                        v.current_status, str(v.current_status)
                    ),
                }
            )
        return out

    def _tu(msg):
        out = []
        if msg is None:
            return out
        for e in msg.entity:
            if not e.HasField("trip_update"):
                continue
            tu = e.trip_update
            next_stop = tu.stop_time_update[0].stop_id if tu.stop_time_update else None
            delay = None
            if tu.stop_time_update:
                stu = tu.stop_time_update[0]
                if stu.HasField("arrival"):
                    delay = stu.arrival.delay
                elif stu.HasField("departure"):
                    delay = stu.departure.delay
            out.append(
                {
                    "trip_id": tu.trip.trip_id,
                    "route_id": tu.trip.route_id,
                    "vehicle_id": tu.vehicle.id if tu.HasField("vehicle") else None,
                    "stop_count": len(tu.stop_time_update),
                    "next_stop": next_stop,
                    "delay": delay,
                }
            )
        return out

    def _sa(msg):
        out = []
        if msg is None:
            return out
        for e in msg.entity:
            if not e.HasField("alert"):
                continue
            a = e.alert
            header = (
                a.header_text.translation[0].text if a.header_text.translation else ""
            )
            out.append(
                {
                    "header": header,
                    "cause": CAUSE_NAMES.get(a.cause, str(a.cause)),
                    "effect": EFFECT_NAMES.get(a.effect, str(a.effect)),
                    "active_window": _fmt_window(a),
                }
            )
        return out

    payload = json.dumps(
        {
            "vehicles": _veh(data["vehicles"]),
            "trip_updates": _tu(data["trip_updates"]),
            "service_alerts": _sa(data["service_alerts"]),
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
    )

    with _client_queues_lock:
        for q in list(_client_queues):
            with contextlib.suppress(queue.Full):
                q.put_nowait(payload)


class MapHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/":
            body = MAP_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=10)
            with _client_queues_lock:
                _client_queues.append(q)
            try:
                while True:
                    try:
                        payload = q.get(timeout=30)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _client_queues_lock:
                    _client_queues.remove(q)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        pass


def run_map(args) -> None:
    server = HTTPServer(("localhost", args.map_port), MapHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"Map: http://localhost:{args.map_port}", file=sys.stderr)
    try:
        while True:
            data = collect(args)
            push_to_map(data)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Unified GTFS-RT consumer: fetch, display, follow, and map.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("feed", help="Feed name (e.g. 'my-feed')")

    feeds = p.add_argument_group("feed toggles (all enabled by default)")
    feeds.add_argument("--no-trip-updates", dest="trip_updates", action="store_false",
                       help="Skip trip updates")
    feeds.add_argument("--no-vehicles", dest="vehicles", action="store_false",
                       help="Skip vehicle positions")
    feeds.add_argument(
        "--no-service-alerts", dest="service_alerts", action="store_false",
        help="Skip service alerts",
    )

    output = p.add_argument_group("output")
    output.add_argument("--summary", action="store_true",
                        help="Compact table output (default: full)")
    output.add_argument("--verbose", action="store_true",
                        help="Also print raw JSON")

    follow = p.add_argument_group("follow mode")
    follow.add_argument("-f", "--follow", action="store_true",
                        help="Poll continuously")
    follow.add_argument("--interval", type=float, default=2,
                        metavar="N", help="Seconds between polls (default: 2)")
    follow.add_argument("--no-clear", dest="clear", action="store_false",
                        help="Don't clear terminal between polls")
    follow.add_argument("--timeout", type=int, default=10,
                        metavar="N", help="HTTP timeout in seconds (default: 10)")

    mapg = p.add_argument_group("map")
    mapg.add_argument("--browser", action="store_true",
                      help="Host a Leaflet map at http://localhost:<map-port>")
    mapg.add_argument("--map-port", type=int, default=8765,
                      metavar="N", help="Port for map server (default: 8765)")

    conn = p.add_argument_group("connection")
    conn.add_argument("--backend", default="http://localhost:8000",
                      help="Base URL (default: http://localhost:8000)")

    p.set_defaults(trip_updates=True, vehicles=True, service_alerts=True, clear=True)
    return p


def main() -> None:
    args = build_parser().parse_args()

    if args.browser:
        run_map(args)
    elif args.follow:
        run_follow(args)
    else:
        data = collect(args)
        print_all(args, data)


if __name__ == "__main__":
    main()

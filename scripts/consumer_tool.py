#!/usr/bin/env python3
"""Unified GTFS-RT consumer tool: fetch, display, follow, and map all feed types."""

import argparse
import contextlib
import csv
import io
import json
import os
import queue
import sys
import threading
import time
import urllib.request
import zipfile
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
# Map vis: SSE server + Leaflet HTML
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
    width:380px; height:100%; overflow-y:auto;
    background:#1e1e1e; color:#ddd; font:13px/1.5 monospace;
    padding:8px; box-sizing:border-box; flex-shrink:0;
  }
  #sidebar h3 { margin:12px 0 2px; color:#7ec8e3; font-size:14px; }
  #sidebar h3:first-child { margin-top:4px; }
  .feedhdr { color:#888; font-size:11px; margin-bottom:6px; }
  .item { border-bottom:1px solid #333; padding:4px 0; font-size:12px; }
  .item .label { font-weight:bold; color:#fff; }
  .item .ah { color:#f0c060; }
  .item .dim { color:#888; }
  .none { color:#888; font-size:12px; }
  details summary {
    cursor:pointer; color:#7a9; font-size:11px; outline:none; padding:1px 0;
  }
  details pre {
    margin:2px 0 4px; padding:6px; background:#141414; border-radius:3px;
    font-size:11px; line-height:1.35; overflow-x:auto; white-space:pre;
    color:#c8c8c8;
  }
  /* Whole trips can run to dozens of stops, so scroll rather than grow a popup
     taller than the map. */
  .stopwrap { max-height:220px; overflow-y:auto; margin-top:4px; }
  table.stops { border-collapse:collapse; font-size:11px; }
  table.stops th, table.stops td { padding:1px 6px 1px 0; text-align:left; }
  table.stops th {
    color:#0b6fa4; font-weight:normal; position:sticky; top:0;
    background:#fff; text-align:left;
  }
  table.stops tr.passed td { opacity:0.45; }
  .late { color:#e06c60; }
  .early { color:#6ba7e0; }
  .ontime { color:#79c17a; }
  /* Popups sit on white, so the sidebar's light-on-dark palette needs darker
     variants to stay readable. */
  .leaflet-popup-content .late { color:#c0392b; }
  .leaflet-popup-content .early { color:#1f6fb2; }
  .leaflet-popup-content .ontime { color:#2e7d32; }
  .leaflet-popup-content .dim { color:#777; }
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
  <div class="feedhdr" id="vhdr"></div>
  <div id="vlist"></div>
  <h3>Trip Updates</h3>
  <div class="feedhdr" id="thdr"></div>
  <div id="tlist"></div>
  <h3>Service Alerts</h3>
  <div class="feedhdr" id="ahdr"></div>
  <div id="alist"></div>
</div>
<div id="statusbar">Connecting…</div>
<script>
// route_id -> display name, injected from a GTFS routes.txt when --gtfs is
// given (GTFS-RT carries only route_id, not the name). Empty otherwise.
const ROUTE_NAMES = __ROUTE_NAMES__;
function routeName(id) {
  if (!id) return '-';
  return ROUTE_NAMES[id] || id;
}

const map = L.map('map').setView([0, 0], 2);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors', maxZoom: 19
}).addTo(map);

const markers = {};
let firstData = true;

// MessageToDict omits scalars equal to their default, so `speed: 0` and
// `delay: 0` arrive as undefined rather than 0, so always supply a fallback.
function num(v, dflt) { return (v === undefined || v === null) ? dflt : Number(v); }
function esc(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function fmtTime(t) {
  if (t === undefined || t === null) return '-';
  return new Date(Number(t) * 1000).toTimeString().slice(0, 8);
}
function fmtDelay(d) {
  if (d === undefined || d === null) return {text: '-', cls: 'dim'};
  const n = Number(d);
  if (n === 0) return {text: 'on time', cls: 'ontime'};
  return {text: (n > 0 ? '+' : '') + n + 's', cls: n > 0 ? 'late' : 'early'};
}
// Same as fmtDelay but spells out h/m/s, used in map popups only; the sidebar
// stays raw seconds.
function fmtDelayHuman(d) {
  if (d === undefined || d === null) return {text: '-', cls: 'dim'};
  const n = Number(d);
  if (n === 0) return {text: 'on time', cls: 'ontime'};
  const cls = n > 0 ? 'late' : 'early';
  let s = Math.abs(n);
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60);   s -= m * 60;
  const parts = [];
  if (h) parts.push(h + 'h');
  if (m) parts.push(m + 'm');
  if (s || !parts.length) parts.push(s + 's');
  return {text: (n > 0 ? '+' : '-') + parts.join(''), cls};
}
// Prefer arrival, fall back to departure; matches how the feed fills these in.
function stuDelay(stu) {
  if (!stu) return undefined;
  if (stu.arrival && stu.arrival.delay !== undefined) return stu.arrival.delay;
  if (stu.departure && stu.departure.delay !== undefined) return stu.departure.delay;
  return undefined;
}
function stuTime(stu) {
  if (!stu) return undefined;
  if (stu.arrival && stu.arrival.time !== undefined) return stu.arrival.time;
  if (stu.departure && stu.departure.time !== undefined) return stu.departure.time;
  return undefined;
}
// The next few stops: those still in the future, else just the head of the list.
function nextStops(tu, n) {
  const stus = (tu && tu.stop_time_update) || [];
  const now = Date.now() / 1000;
  const future = stus.filter(s => {
    const t = stuTime(s);
    return t !== undefined && Number(t) >= now;
  });
  return (future.length ? future : stus).slice(0, n);
}

// One sidebar row: readable summary line + the entity's complete JSON.
function entityBlock(id, summaryHtml, obj) {
  return `<div class="item">${summaryHtml}
    <details data-eid="${esc(id)}"><summary>raw</summary>
    <pre>${esc(JSON.stringify(obj, null, 2))}</pre></details></div>`;
}

function feedHeader(feed) {
  const h = (feed && feed.header) || {};
  const n = ((feed && feed.entity) || []).length;
  return `v${h.gtfs_realtime_version || '?'} · `
    + `${h.incrementality || 'FULL_DATASET'} · `
    + `${fmtTime(h.timestamp)} · ${n} entit${n === 1 ? 'y' : 'ies'}`;
}

// Re-rendering innerHTML every tick would slam shut any <details> the user
// opened, so remember which ones were open and restore them afterwards.
function openIds() {
  const s = new Set();
  document.querySelectorAll('#sidebar details[open]').forEach(
    d => s.add(d.dataset.eid));
  return s;
}
function restoreOpen(s) {
  document.querySelectorAll('#sidebar details').forEach(d => {
    if (s.has(d.dataset.eid)) d.open = true;
  });
}

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
  const timeStr = new Date(d.timestamp).toTimeString().slice(0,8);
  const wasOpen = openIds();

  const vEnts = (d.vehicles || {}).entity || [];
  const tEnts = (d.trip_updates || {}).entity || [];
  const aEnts = (d.service_alerts || {}).entity || [];

  // A >24h daily trip has several instances of one trip_id live at once, told
  // apart by start_date, so join on (trip_id, start_date), not trip_id alone.
  function tripKey(trip) {
    const tid = trip && trip.trip_id;
    if (!tid) return null;
    const sd = trip && trip.start_date;
    return sd ? tid + '\\x1f' + sd : tid;
  }

  // Join vehicles to trip updates. vehicle_positions rewrites trip_id through
  // the feed's alias map while trip_updates does not, so the vehicle id is the
  // more dependable key, so index both and try the trip instance first.
  const tuByTrip = {}, tuByVehicle = {};
  tEnts.forEach(e => {
    const tu = e.trip_update || {};
    const k = tripKey(tu.trip);
    if (k) tuByTrip[k] = tu;
    if (tu.vehicle && tu.vehicle.id) tuByVehicle[tu.vehicle.id] = tu;
  });
  function tripUpdateFor(v) {
    const k = tripKey(v.trip);
    const vid = v.vehicle && v.vehicle.id;
    return (k && tuByTrip[k]) || (vid && tuByVehicle[vid]) || null;
  }

  // The whole trip, not just the next few; stops already passed are dimmed so
  // the upcoming ones still read first.
  function stopsTable(tu) {
    if (!tu) return '<div class="dim">no trip update for this vehicle</div>';
    const stops = (tu.stop_time_update) || [];
    if (!stops.length) return '<div class="dim">no stop time updates</div>';
    const now = Date.now() / 1000;
    const rows = stops.map(s => {
      const dl = fmtDelayHuman(stuDelay(s));
      const t = stuTime(s);
      const passed = t !== undefined && Number(t) < now;
      return `<tr class="${passed ? 'passed' : ''}">
        <td>${num(s.stop_sequence, '-')}</td>
        <td>${esc(s.stop_id || '-')}</td>
        <td>${fmtTime(s.arrival && s.arrival.time)}</td>
        <td>${fmtTime(s.departure && s.departure.time)}</td>
        <td class="${dl.cls}">${dl.text}</td></tr>`;
    }).join('');
    return `<div class="stopwrap"><table class="stops">
      <tr><th>seq</th><th>stop</th><th>arr</th><th>dep</th><th>delay</th></tr>
      ${rows}</table></div>`;
  }

  // --- map markers ---
  // setPopupContent below rebuilds the popup DOM each tick, which would reset
  // the scroll of a stop table the user is reading. Leaflet keeps at most one
  // popup open, so remember its scroll and restore it afterwards.
  let popupScroll = null;
  for (const [k, m] of Object.entries(markers)) {
    if (m.isPopupOpen && m.isPopupOpen()) {
      const wrap = m.getPopup().getElement()?.querySelector('.stopwrap');
      popupScroll = { key: k, top: wrap ? wrap.scrollTop : 0 };
    }
  }
  const seen = new Set();
  vEnts.forEach(ent => {
    const v = ent.vehicle || {};
    const pos = v.position || {};
    const vid = (v.vehicle && v.vehicle.id) || ent.id;
    const tid = v.trip && v.trip.trip_id;
    const sd = v.trip && v.trip.start_date;
    // Key per trip instance: concurrent instances of one >24h trip share a
    // trip_id and differ only by start_date, so keying on trip_id alone would
    // collapse them back into a single marker.
    const key = tripKey(v.trip) || vid;
    seen.add(key);
    const lat = num(pos.latitude, 0), lon = num(pos.longitude, 0);
    const icon = arrowIcon(num(pos.bearing, 0), '#2980b9');
    // One producer can report many trains under a single vehicle id (Amtrak
    // publishes every train as "amtrakdriver"), so lead with the trip.
    const title = tid ? `${tid} · ${(v.vehicle && v.vehicle.label) || vid}`
                      : ((v.vehicle && v.vehicle.label) || vid);
    const popup = `<b>${esc(title)}</b><br>
      trip: ${esc(tid || '-')}<br>
      ${sd ? `start: ${esc(sd)}<br>` : ''}
      route: ${esc(routeName(v.trip && v.trip.route_id))}<br>
      speed: ${num(pos.speed, 0).toFixed(1)} m/s<br>
      bearing: ${num(pos.bearing, 0).toFixed(0)}°<br>
      status: ${esc(v.current_status || '-')}<br>
      updated: ${fmtTime(v.timestamp)}
      ${stopsTable(tripUpdateFor(v))}`;
    if (markers[key]) {
      markers[key].setLatLng([lat, lon]).setIcon(icon).setPopupContent(popup);
    } else {
      markers[key] = L.marker([lat, lon], {icon}).bindPopup(popup).addTo(map);
    }
  });
  Object.keys(markers).forEach(id => {
    if (!seen.has(id)) { markers[id].remove(); delete markers[id]; }
  });

  if (popupScroll && markers[popupScroll.key]) {
    const wrap = markers[popupScroll.key].getPopup().getElement()
      ?.querySelector('.stopwrap');
    if (wrap) wrap.scrollTop = popupScroll.top;
  }

  if (firstData && vEnts.length > 0) {
    firstData = false;
    const latlngs = vEnts.map(ent => {
      const p = (ent.vehicle || {}).position || {};
      return [num(p.latitude, 0), num(p.longitude, 0)];
    });
    map.fitBounds(L.latLngBounds(latlngs).pad(0.2));
  }

  // --- sidebar: vehicles ---
  document.getElementById('vhdr').textContent = feedHeader(d.vehicles);
  document.getElementById('vlist').innerHTML = vEnts.map(ent => {
    const v = ent.vehicle || {};
    const pos = v.position || {};
    const vid = (v.vehicle && v.vehicle.id) || ent.id;
    const tid = v.trip && v.trip.trip_id;
    const sd = v.trip && v.trip.start_date;
    const summary = `<span class="label">${esc(tid
      || (v.vehicle && v.vehicle.label) || vid)}</span>
      ${sd ? `<span class="dim">start=</span>${esc(sd)} ` : ''}
      <span class="dim">veh=</span>${esc((v.vehicle && v.vehicle.label) || vid)}
      <span class="dim">rt=</span>${esc((v.trip && v.trip.route_id) || '-')}
      ${num(pos.speed, 0).toFixed(1)}m/s
      <span class="dim">${esc(v.current_status || '')}</span>`;
    // Entity ids are renumbered on every request, so key the open/closed state
    // on something stable across polls: the trip instance (trip_id+start_date).
    return entityBlock('v' + (tripKey(v.trip) || vid), summary, ent);
  }).join('') || '<div class="none">None</div>';

  // --- sidebar: trip updates ---
  document.getElementById('thdr').textContent = feedHeader(d.trip_updates);
  document.getElementById('tlist').innerHTML = tEnts.map(ent => {
    const tu = ent.trip_update || {};
    const stus = tu.stop_time_update || [];
    const nxt = nextStops(tu, 1)[0];
    const dl = fmtDelay(stuDelay(nxt));
    const nextStr = nxt
      ? `next=${esc(nxt.stop_id || '?')} @ ${fmtTime(stuTime(nxt))}
         <span class="${dl.cls}">${dl.text}</span>`
      : '<span class="dim">no stops</span>';
    const sd = tu.trip && tu.trip.start_date;
    const summary = `<span class="label">${esc((tu.trip && tu.trip.trip_id)
      || ent.id)}</span>
      ${sd ? `<span class="dim">start=</span>${esc(sd)} ` : ''}
      <span class="dim">veh=</span>${esc((tu.vehicle && tu.vehicle.id) || '-')}
      ${stus.length} stop${stus.length === 1 ? '' : 's'}<br>${nextStr}`;
    return entityBlock('t' + (tripKey(tu.trip) || ent.id), summary, ent);
  }).join('') || '<div class="none">None</div>';

  // --- sidebar: service alerts ---
  document.getElementById('ahdr').textContent = feedHeader(d.service_alerts);
  document.getElementById('alist').innerHTML = aEnts.map(ent => {
    const a = ent.alert || {};
    const tr = f => (a[f] && a[f].translation && a[f].translation[0])
      ? a[f].translation[0].text : '';
    const activeWindow = (a.active_period || []).map(p =>
      `${p.start ? fmtTime(p.start) : '*'} → ${p.end ? fmtTime(p.end) : '*'}`
    ).join('; ') || 'always';
    const desc = tr('description_text');
    const url = tr('url');
    const ies = (a.informed_entity || []).map(ie => {
      const parts = [];
      if (ie.agency_id) parts.push('agency=' + ie.agency_id);
      if (ie.route_id) parts.push('route=' + ie.route_id);
      if (ie.route_type !== undefined) parts.push('route_type=' + ie.route_type);
      if (ie.direction_id !== undefined) parts.push('dir=' + ie.direction_id);
      if (ie.stop_id) parts.push('stop=' + ie.stop_id);
      if (ie.trip && ie.trip.trip_id) parts.push('trip=' + ie.trip.trip_id);
      if (ie.trip && ie.trip.start_date) parts.push('date=' + ie.trip.start_date);
      if (ie.trip && ie.trip.start_time) parts.push('time=' + ie.trip.start_time);
      return esc(parts.join(', ') || '(all)');
    }).join('<br>');
    const summary = `<span class="ah">${esc(tr('header_text')
      || '(no header)')}</span><br>
      ${esc(a.cause || 'UNKNOWN_CAUSE')} / ${esc(a.effect || 'UNKNOWN_EFFECT')}
      / ${esc(a.severity_level || 'UNKNOWN_SEVERITY')}<br>
      <span class="dim">active:</span> ${esc(activeWindow)}
      ${desc ? '<br>' + esc(desc) : ''}
      ${url ? `<br><a href="${esc(url)}" target="_blank">${esc(url)}</a>` : ''}
      ${ies ? '<br><span class="dim">informed:</span><br>' + ies : ''}`;
    return entityBlock('a' + (tr('header_text') || ent.id), summary, ent);
  }).join('') || '<div class="none">None</div>';

  restoreOpen(wasOpen);

  document.getElementById('statusbar').textContent =
    `Updated ${timeStr} · ${vEnts.length} vehicle${vEnts.length!==1?'s':''}`
    + ` · ${tEnts.length} trip update${tEnts.length!==1?'s':''}`
    + ` · ${aEnts.length} alert${aEnts.length!==1?'s':''}`;
};
evtSource.onerror = function() {
  document.getElementById('statusbar').textContent = 'Connection lost, reconnecting…';
};
</script>
</body>
</html>
"""

_client_queues: list[queue.Queue] = []
_client_queues_lock = threading.Lock()

# route_id -> display name, populated from --gtfs and injected into the map HTML.
_route_names: dict[str, str] = {}


def load_route_names(source: str) -> dict[str, str]:
    """route_id -> name from a GTFS zip's routes.txt (local path or http URL)."""
    if source.startswith(("http://", "https://")):
        raw = fetch(source, timeout=60)
    else:
        with open(source, "rb") as f:
            raw = f.read()
    names: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        text = zf.read("routes.txt").decode("utf-8-sig")
    for row in csv.DictReader(io.StringIO(text)):
        rid = row.get("route_id")
        if not rid:
            continue
        name = row.get("route_long_name") or row.get("route_short_name") or rid
        names[rid] = name.strip()
    return names


def push_to_map(data: dict) -> None:
    def _dump(msg):
        """Whole FeedMessage as JSON: nothing hand-picked, nothing dropped."""
        if msg is None:
            return {"header": None, "entity": []}
        return json_format.MessageToDict(msg, preserving_proto_field_name=True)

    payload = json.dumps(
        {
            "vehicles": _dump(data["vehicles"]),
            "trip_updates": _dump(data["trip_updates"]),
            "service_alerts": _dump(data["service_alerts"]),
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
    )

    with _client_queues_lock:
        for q in list(_client_queues):
            with contextlib.suppress(queue.Full):
                q.put_nowait(payload)


class MapHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = MAP_HTML.replace(
                "__ROUTE_NAMES__", json.dumps(_route_names)
            ).encode()
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

    def log_message(self, format, *args):
        pass


def run_map(args) -> None:
    if args.gtfs:
        try:
            _route_names.update(load_route_names(args.gtfs))
            print(f"Loaded {len(_route_names)} route names from {args.gtfs}",
                  file=sys.stderr)
        except Exception as exc:
            print(f"WARN: could not load routes from {args.gtfs}: {exc}",
                  file=sys.stderr)

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
    mapg.add_argument("--gtfs", metavar="PATH_OR_URL",
                      help="GTFS zip (local path or http URL) to resolve "
                           "route_id → route name in map popups")

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

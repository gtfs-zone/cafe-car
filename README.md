# gtfs-zone-rt-api

Core API for [GTFS.Zone](https://gtfs.zone): serves GTFS-RT feeds and the JSON API [rt-manager](https://github.com/gtfs-zone/gtfs-zone-rt-manager) uses to manage feeds, trackers and service alerts. rt-api has no UI of its own; rt-manager is the UI.

Part of a larger stack; see [gtfs-zone-infra](https://github.com/gtfs-zone/gtfs-zone-infra) for the full deployment.

### How it fits together

```
GitHub / Google / GitLab
    └─> Keycloak (OIDC provider, brokers all three onto one account)
            └─> oauth2-proxy (ForwardAuth)
                    └─> Traefik
                            ├─> rt-manager + admin app (manage.rt.<domain>), auth-gated
                            └─> Public API (rt.<domain>), no auth
                                    ├─> PostgreSQL (feeds, trackers, alerts, users)
                                    └─> Redis DB 1 (vehicle positions, trip updates)

Traccar Client app (phone) / GPS unit
    └─> Traccar (/osmand)
            └─> rt-traccar-receiver (HTTP forward)  → Redis DB 1 (vehicle:{tracker_id}:* keys)

rt-pollers (Amtrak, Columbia County)
    └─> POST /ingest/positions, /ingest/trip-updates (batch)  → Redis DB 1

rt-delay-estimator
    └─> sweeps vehicle:* + scheduled stop_times → Redis DB 1 (trip_update:{tracker_id}:{trip_id} keys)
```

There is no MQTT broker and no OwnTracks path any more: positions arrive over HTTP, either through [rt-traccar-receiver](https://github.com/gtfs-zone/gtfs-zone-rt-traccar-receiver) (Traccar's forwarder) or directly on this service's `/ingest` API. Trip delays are written by [rt-delay-estimator](https://github.com/gtfs-zone/gtfs-zone-rt-delay-estimator) and by upstream pollers.

---

## Trackers, not drivers

A `Tracker` is one producer's credential. Its `id` is a uuid4 hex surrogate: the Redis key namespace and the `tracker_id` a producer posts under. Its `device_key` is a secret pet-name (e.g. `gently-tender-oyster`) that doubles as the Traccar `uniqueId`; there is no password. Creating a tracker in rt-manager auto-creates the matching Traccar device and renders a provisioning QR for the Traccar Client app.

Neither id is emitted in a feed. A vehicle is labelled with the producer's `vehicle_id`, falling back to the tracker's public `nickname`.

A `TrackerRule` binds a tracker to a `trip_id` on a day-of-week and time window, which is how rt-traccar-receiver resolves an incoming position to a trip server-side.

---

## Public API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /{feed_name}/vehicle_positions.pb` | Live vehicle positions (GTFS-RT protobuf) |
| `GET /{feed_name}/trip_updates.pb` | Trip updates from Redis (GTFS-RT protobuf) |
| `GET /{feed_name}/service_alerts.pb` | Service alerts from Postgres (GTFS-RT protobuf) |
| `GET /{feed_name}/*.json` | The same three feeds as JSON, for browsers and debugging |
| `GET /feeds` | Public feed catalog: every feed, its four URLs, and whether each realtime endpoint currently has anything in it |
| `GET /health` | Liveness check (pings Redis + Postgres) |

All of the above are unauthenticated. Feeds are configured in rt-manager.

### Ingest API

Service-to-service, guarded by a shared bearer token (`INGEST_API_TOKEN`), for producers that already know their own `trip_id`:

| Endpoint | Description |
|----------|-------------|
| `POST /ingest/position` | One vehicle position, written as a `vehicle:{tracker_id}:{vehicle_id}` record with a 60s TTL |
| `POST /ingest/positions` | A batch of positions (`{"positions": [...]}`), one poll cycle in one request |
| `POST /ingest/trip-update` | One trip's delay/stop-time predictions, 300s TTL |
| `POST /ingest/trip-updates` | A batch of trip updates (`{"trip_updates": [...]}`) |
| `POST /ingest/alerts` | Replace a feed's producer-published service alerts |

`GET /feed_urls` is internal-only: it refuses any request carrying `X-Forwarded-For`.

---

## Local development

Requires a `.env` file:

```
DATABASE_URL=postgresql+asyncpg://postgres:mysecretpassword@localhost:5432/postgres
REDIS_URL=redis://localhost:6379/1
SESSION_SECRET_KEY=some-random-secret-key
```

Run Postgres and Redis externally (e.g. via the deployment stack), then:

```bash
uv sync
uv run fastapi dev src/gtfs_zone_rt_api/main.py        # public API → http://localhost:8000
uv run fastapi dev src/gtfs_zone_rt_api/admin_main.py  # admin app  → http://localhost:8001
```

The admin app has no UI of its own; it serves rt-manager's JSON API at `/api`. To simulate oauth2-proxy headers locally:

```bash
curl -H "X-Auth-Request-User: alice" -H "X-Auth-Request-Email: alice@example.com" \
     http://localhost:8001/api/me
```

`X-Auth-Request-User` is the OIDC subject and is the only thing identifying the caller; the header alone creates the `User` and `Identity` on first use. Paths that need a *verified* email (invite claiming, account linking) also want a token: see CLAUDE.md for the unsigned-JWT recipe under `DEBUG=true`.

---

## Testing a feed

Helper scripts live under `scripts/`.

### `simulate_trip.py`

Simulates real GTFS trips along their shapes, POSTing positions to `/ingest/position`. The simulation starts where the vehicle would actually be right now according to the schedule, with a random delay.

```bash
# List what is in the GTFS zip:
uv run scripts/simulate_trip.py --list-routes
uv run scripts/simulate_trip.py --list-trips

# Simulate trip WCCWB at 10x speed, publishing every 2s:
uv run scripts/simulate_trip.py --trip WCCWB

# Every trip on a route, or N random trips, or the whole feed:
uv run scripts/simulate_trip.py --route 1 --route 2
uv run scripts/simulate_trip.py --n-trips 10 --speed 20
uv run scripts/simulate_trip.py --all-trips --speed 50 --quiet

# Custom tracker, ingest endpoint, speed and interval:
uv run scripts/simulate_trip.py --tracker <tracker-id> \
    --ingest-url http://localhost:8000 --token dev-ingest-token \
    --trip ELLSWB --speed 30 --interval 1
```

The tracker must exist in the database (created via rt-manager), and the token must match `INGEST_API_TOKEN`, for the positions to appear in the feed.

### `provision_source.py`

Creates the row chain a producer needs (a `Feed` owned by an existing `User`, plus a `Tracker`), creates the matching Traccar device, and prints the tracker `id` to paste into the producer's env. Idempotent.

### Feed inspectors

`fetch_vehicles.py`, `fetch_trip_updates.py` and `fetch_service_alerts.py` each fetch one `.pb` endpoint and pretty-print it; `consumer_tool.py` does all three, and can follow a feed and map it.

```bash
uv run scripts/fetch_vehicles.py <feed_name>            # full protobuf dump
uv run scripts/fetch_vehicles.py <feed_name> --summary  # one line per vehicle
uv run scripts/fetch_vehicles.py <feed_name> --backend http://localhost:8000
```

---

## Development commands

```bash
# Install git hooks (required once per clone)
uv run pre-commit install

uv run ruff check src/          # lint
uv run ruff check --fix src/    # lint + autofix
uv run pytest                   # run tests

# Apply migrations. Models and Alembic revisions live in gtfs-zone-db-models, which
# ships the migrator as a console script; this repo has no alembic.ini.
uv run gtfs-zone-db-models-migrate
```

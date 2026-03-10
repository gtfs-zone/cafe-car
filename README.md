# redis-gtfs-rt-api

Core API for [GTFS.Zone](https://gtfs.zone) — serves GTFS-RT protobuf feeds and provides an admin UI for managing feeds and drivers.

Part of a larger stack; see [deploy-gtfs-rt](https://git.kcfam.us/gtfs.zone/deploy-gtfs-rt) for the full deployment.

### How it fits together

```
GitHub OAuth
    └─> Dex (OIDC)
            └─> oauth2-proxy (ForwardAuth)
                    └─> Traefik
                            ├─> Admin app  (manage.rt.<domain>) — auth-gated
                            └─> Public API (rt.<domain>)        — no auth
                                    ├─> PostgreSQL (feeds, drivers, users)
                                    └─> Redis DB 1 (vehicle positions)

OwnTracks app (phone)
    └─> NanoMQ (MQTT broker, bundled)
            ├─> vehicle-poser         → Redis DB 1 (vehicle:{username} keys)
            └─> trip-updogger        → Redis DB 1 (trip_update:{trip_id} keys)
```

Vehicle positions (`vehicle:{username}`) are published by [vehicle-poser](https://git.kcfam.us/gtfs.zone/vehicle-poser) and trip delay data (`trip_update:{trip_id}`) by [trip-updogger](https://git.kcfam.us/gtfs.zone/trip-updogger), both via the NanoMQ MQTT broker bundled in this stack.

---

## NanoMQ broker

A [NanoMQ](https://nanomq.io/) MQTT broker runs on port `1883` (configured via the deployment stack). Anonymous connections are disabled; authentication is delegated via HTTP POST to `/mqtt/auth` and ACL is checked via `/mqtt/acl` (both implemented by this service).

ACL rules:
- Users may only **publish** to `owntracks/{their_username}/#`
- All clients may **subscribe** to `owntracks/#`

---

## Public API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /{feed_name}/vehicle_positions.pb` | Live vehicle positions (GTFS-RT protobuf) |
| `GET /{feed_name}/trip_updates.pb` | Trip updates from Redis (GTFS-RT protobuf) |
| `GET /{feed_name}/service_alerts.pb` | Service alerts (stub — returns empty response) |
| `POST /mqtt/auth` | MQTT broker auth hook (validates driver credentials) |
| `GET /health` | Liveness check (pings Redis + Postgres) |

All endpoints are unauthenticated. Feed names are configured via the admin UI.

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
uv run fastapi dev src/app/main.py    # public API  → http://localhost:8000
uv run fastapi dev src/app/admin_main.py  # admin app → http://localhost:8001
```

To simulate oauth2-proxy headers locally:

```bash
curl -H "X-Auth-Request-User: alice" -H "X-Auth-Request-Email: alice@example.com" \
     http://localhost:8001/
```

---

## Testing vehicle positions

Two helper scripts are provided under `scripts/`:

### `simulate_trip.py`

Simulates a real GTFS trip along its shape, publishing vehicle positions to MQTT in OwnTracks format. The simulation starts at the position the bus would actually be at right now according to the GTFS schedule, with a random delay. The MQTT topic follows the OwnTracks convention: `owntracks/{driver}/{trip_id}`.

```bash
# List available trips in the GTFS zip:
uv run scripts/simulate_trip.py --list-trips

# Simulate trip WCCWB at 10x speed (default), publishing every 2s:
uv run scripts/simulate_trip.py --trip WCCWB

# Custom driver credentials, speed and interval:
uv run scripts/simulate_trip.py --driver bob --password bob \
    --trip ELLSWB --speed 30 --interval 1

# Custom delay range (seconds):
uv run scripts/simulate_trip.py --min-delay 30 --max-delay 300 --delay-drift 10
```

The driver must exist in the database (created via the admin UI) and have a matching MQTT password for their positions to appear in the feed.

### `fetch_vehicles.py`

Fetches a `vehicle_positions.pb` endpoint and pretty-prints the result.

```bash
# Full protobuf dump (default)
uv run scripts/fetch_vehicles.py <feed_name>

# Compact one-line-per-vehicle table
uv run scripts/fetch_vehicles.py <feed_name> --summary

# Custom API base URL
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

# Alembic migrations
uv run alembic revision --autogenerate -m "describe change"
uv run alembic upgrade head
uv run alembic downgrade -1
```

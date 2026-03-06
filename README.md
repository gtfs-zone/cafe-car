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
```

Vehicle positions are published to Redis by [owntrack-redis-bridge](https://git.kcfam.us/gtfs.zone/owntrack-redis-bridge) via MQTT.

---

## Public API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /{feed_name}/vehicle_positions.pb` | Live vehicle positions (GTFS-RT protobuf) |
| `GET /{feed_name}/trip_updates.pb` | Trip updates (stub — returns empty feed) |
| `GET /{feed_name}/service_alerts.pb` | Service alerts (stub — returns empty feed) |
| `POST /mqtt/auth` | MQTT broker auth hook (validates driver credentials) |
| `GET /health` | Liveness check (pings Redis + Postgres) |

All endpoints are unauthenticated. Feed names are configured via the admin UI.

---

## Local development

Starts Postgres, Redis, the public API, admin app, Dex, and oauth2-proxy:

```bash
docker compose up --build
```

| Service | URL |
|---------|-----|
| Public API | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Admin UI (via oauth2-proxy) | http://localhost:4180 |
| Admin UI (direct, no auth) | http://localhost:8001 |

**Dev login credentials** (Dex static passwords — log in with email):

| Email | Password |
|-------|----------|
| alice@local | password |
| bob@local | password |

---

## Testing vehicle positions

Two helper scripts are provided under `scripts/`:

### `simulate_vehicles.py`

Writes fake vehicle positions to Redis every 10 seconds with slight random movement, simulating what owntrack-redis-bridge would publish in production. Includes a built-in set of test drivers across SF, NYC, and Chicago.

```bash
# Simulate all built-in test drivers
uv run scripts/simulate_vehicles.py

# Simulate specific drivers only
uv run scripts/simulate_vehicles.py --drivers test-driver-001 nyc-driver-001

# Custom Redis URL
uv run scripts/simulate_vehicles.py --redis redis://localhost:6379/1
```

The drivers must exist in the database (created via the admin UI) for their positions to appear in the feed. Each key has a 60-second TTL — vehicles stop appearing in the feed if the simulator is stopped.

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
uv run ruff check src/          # lint
uv run ruff check --fix src/    # lint + autofix
uv run pytest                   # run tests

# Alembic migrations
uv run alembic revision --autogenerate -m "describe change"
uv run alembic upgrade head
uv run alembic downgrade -1
```

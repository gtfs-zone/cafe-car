# redis-gtfs-rt-api

## Project Overview

FastAPI service that:
- Sits behind Authelia forward auth (Traefik middleware) using Dex for OAuth logins
- Manages config data (Feeds, Drivers) in PostgreSQL via SQLModel + Alembic
- Exposes stub GTFS-RT protobuf endpoints (`/<feed_name>/*.pb`) for trip updates, vehicle positions, and service alerts
- Provides a scoped SQLAdmin interface at `/admin` where every authenticated user can only see their own Feeds and associated Drivers

## Architecture

```
Traefik (+ Authelia middleware)
    └─> FastAPI (this service)
            ├─> PostgreSQL (SQLModel models, Alembic migrations)
            └─> Redis DB 1  (cache / RT data)
```

## Authentication

Authelia injects headers on every authenticated request:
- `Remote-User` — username (used as primary identity)
- `Remote-Email` — email address
- `Remote-Name` — display name
- `Remote-Groups` — group memberships

No passwords are stored for web users — Authelia owns credentials. The `User` record is auto-created/updated from headers on first request.

`Driver` records have their own `password` field (hashed) for GTFS-RT feed access.

## Redis DB Allocation

- DB 1: This FastAPI service (cache and real-time data)
  - `REDIS_URL=redis://redis:6379/1`

## Alembic Workflow

```bash
# Generate a new migration after model changes
uv run alembic revision --autogenerate -m "describe change"

# Apply all pending migrations
uv run alembic upgrade head

# Downgrade one step
uv run alembic downgrade -1
```

## Running Locally

Requires a `.env` file with:
```
DATABASE_URL=postgresql+asyncpg://fastapi:password@localhost:5432/fastapi
REDIS_URL=redis://localhost:6379/1
SESSION_SECRET_KEY=some-random-secret-key
```

```bash
uv sync
uv run fastapi dev src/app/main.py
```

API docs: http://localhost:8000/docs
Admin:    http://localhost:8000/admin

To simulate Authelia headers locally:
```bash
curl -H "Remote-User: alice" -H "Remote-Email: alice@example.com" \
     http://localhost:8000/myfeed/trip_updates.pb
```

## Important Rules

- Never create a stop_time with null departure and arrival
- Admin views must always scope queries to the authenticated user — never expose another user's Feeds or Drivers
- Use `uv` for all package management (never `pip install` directly)
- Run `uv run ruff check src/` before committing

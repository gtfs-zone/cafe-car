# redis-gtfs-rt-api

## Project Overview

FastAPI service that:
- Sits behind oauth2-proxy forward auth (Traefik middleware) using Dex as the OIDC provider (GitHub OAuth)
- Manages config data (Feeds, Drivers) in PostgreSQL via SQLModel + Alembic
- Exposes GTFS-RT protobuf endpoints (`/<feed_name>/*.pb`) for trip updates, vehicle positions, and service alerts
- Provides a scoped SQLAdmin interface at `/admin` where every authenticated user can only see their own Feeds and associated Drivers

## Architecture

```
GitHub OAuth
    └─> Dex (OIDC provider, dex.gtfs.zone)
            └─> oauth2-proxy (ForwardAuth middleware, auth.gtfs.zone)
                    └─> Traefik
                            ├─> FastAPI admin app (manage.rt.gtfs.zone) — protected by oauth2-proxy
                            └─> FastAPI public API (rt.gtfs.zone)    — no auth required
                                    ├─> PostgreSQL (SQLModel models, Alembic migrations)
                                    └─> Redis DB 1  (cache / RT data)
```

## Authentication

oauth2-proxy injects headers on every authenticated request to the admin interface:
- `X-Auth-Request-User` — OIDC `sub` claim (GitHub username); used as primary identity
- `X-Auth-Request-Email` — email address
- `X-Auth-Request-Access-Token` — OIDC access token

No passwords are stored for web users — Dex/GitHub owns credentials. The `User` record is auto-created on first request using `(provider="dex", provider_subject=<X-Auth-Request-User>)` as the lookup key.

The public GTFS-RT endpoints (`rt.gtfs.zone`) have **no authentication middleware** — they are publicly accessible.

`Driver` records have their own `password` field (hashed) for GTFS-RT feed access.

## Redis DB Allocation

- DB 0: oauth2-proxy session storage (managed by deploy-gtfs-rt)
- DB 1: This FastAPI service (cache and real-time data)
  - `REDIS_URL=redis://redis:6379/1`
- DB 2: Bridge pub/sub messages (OwnTrack Redis Bridge)

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
DATABASE_URL=postgresql+asyncpg://postgres:mysecretpassword@localhost:5432/postgres
REDIS_URL=redis://localhost:6379/1
SESSION_SECRET_KEY=some-random-secret-key
```

```bash
uv sync
uv run fastapi dev src/app/main.py
```

API docs: http://localhost:8000/docs
Admin:    http://localhost:8000/admin

Or use Docker Compose (includes Postgres and Redis):
```bash
docker compose up --build
```

To simulate oauth2-proxy headers locally:
```bash
curl -H "X-Auth-Request-User: alice" -H "X-Auth-Request-Email: alice@example.com" \
     http://localhost:8000/admin
```

## Important Rules

- Never create a stop_time with null departure and arrival
- Admin views must always scope queries to the authenticated user — never expose another user's Feeds or Drivers
- Use `uv` for all package management (never `pip install` directly)
- Run `uv run ruff check src/` before committing

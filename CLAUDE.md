# cafe-car — Claude Guide

## Project Overview

FastAPI service that:
- Sits behind oauth2-proxy forward auth (Traefik middleware) using Dex as the OIDC provider (GitHub OAuth)
- Manages config data (Feeds, Drivers) in PostgreSQL via SQLModel + Alembic
- Exposes GTFS-RT protobuf endpoints (`/<feed_name>/*.pb`) for trip updates, vehicle positions, and service alerts
- Provides a scoped SQLAdmin interface at `/admin` where every authenticated user can only see their own Feeds and associated Drivers

## Commands

```bash
uv sync              # install dependencies
ruff check .         # lint
ruff format .        # format
pre-commit install   # install git hooks
```

```bash
uv run ruff check src/          # lint
uv run ruff check --fix src/    # lint + autofix
uv run pytest                   # run all tests
uv run pytest tests/test_foo.py::test_bar  # single test
```

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
- DB 2: Bridge pub/sub messages (vehicle-poser)

## Two-App Architecture

There are two separate FastAPI apps sharing the same DB/Redis:

- `src/app/main.py` → **public API** (`app = create_public_app()`): GTFS-RT protobuf endpoints (`/{feed_name}/trip_updates.pb`, `vehicle_positions.pb`, `service_alerts.pb`) + MQTT auth (`POST /mqtt/auth`). Run with `uv run fastapi dev src/app/main.py`.
- `src/app/admin_main.py` → **admin app** (`app = create_admin_app()`): SQLAdmin interface mounted at `/`. Uses `OIDCAuthBackend`, `SessionMiddleware`, `DBSessionMiddleware`, and `SubjectMiddleware`. Run with `uv run fastapi dev src/app/admin_main.py`.

The current user identity flows via `request.session["subject"]` (set in `OIDCAuthBackend.authenticate`) and also via `current_subject_var` (`ContextVar`) for use in `DriverAdmin.scaffold_form` where `request` is unavailable.

## Redis Data Format

Vehicle positions are stored at key `vehicle:{driver.username}` as JSON with fields: `driver`, `lat`, `lon`, `bearing`, `speed`, `trip_id`, `route_id` (optional), `timestamp`.

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

To simulate oauth2-proxy headers locally:
```bash
curl -H "X-Auth-Request-User: alice" -H "X-Auth-Request-Email: alice@example.com" \
     http://localhost:8000/admin
```

## Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string, e.g. `postgresql+asyncpg://postgres:password@localhost:5432/cafe-car` |
| `REDIS_URL` | Redis connection string, e.g. `redis://localhost:6379/1` |
| `SESSION_SECRET_KEY` | Secret key for signing sessions |

## Rules

- Never include `Co-Authored-By: Claude ...` trailers in commit messages.
- Only read files within this repo's directory. Do not access parent directories or sibling repos.
- Never create a stop_time with null departure and arrival
- Admin views must always scope queries to the authenticated user — never expose another user's Feeds or Drivers
- Use `uv` for all package management (never `pip install` directly)
- Run `uv run ruff check src/` before committing

## Related Repos

| Repo | Description | URL |
|---|---|---|
| cafe-car | GTFS-RT HTTP API serving real-time feeds | https://git.kcfam.us/gtfs.zone/cafe-car |
| vehicle-poser | Worker that tracks and posts vehicle positions | https://git.kcfam.us/gtfs.zone/vehicle-poser |
| trip-updogger | Worker that generates trip update predictions | https://git.kcfam.us/gtfs.zone/trip-updogger |
| schedule-foamer | Worker that ingests and processes GTFS schedule data | https://git.kcfam.us/gtfs.zone/schedule-foamer |
| railroad-club | Shared Python library for GTFS types and utilities | https://git.kcfam.us/gtfs.zone/railroad-club |
| music-student | Orchestration repo for deployments and infra | https://git.kcfam.us/gtfs.zone/music-student |
| landing-zone | Static marketing/status site | https://git.kcfam.us/gtfs.zone/landing-zone |

## Forgejo Workflow

This project uses an offline-first workflow. Claude reads/writes `CURRENT_PLAN.md` locally and only touches Forgejo when explicitly asked.

### Making a plan (triggered by "make a plan for issue #N" or "let's plan X")

1. If the user said "fetch issue #N", use `mcp__forgejo__get_issue_by_index` with `owner: "gtfs.zone"`, `repo: "cafe-car"` to retrieve the issue body; otherwise work from the context provided
2. Explore the codebase as needed
3. Ask clarifying questions inline; wait for answers before writing
4. Write the plan to `CURRENT_PLAN.md` in the repo root (format: Summary, Relevant Context, numbered Phases each with prose + checklist + gotchas)
5. Do not start implementation

### Completing a phase (triggered by "complete phase N" or "do phase N")

1. Read `CURRENT_PLAN.md` directly — do not fetch from Forgejo
2. Implement everything in the phase; commit as you go with conventional commits
3. After completing, update `CURRENT_PLAN.md`: check off completed items, append discoveries to that phase's prose
4. Do not update the Forgejo issue; do not start the next phase; stop for user review

### Updating Forgejo (triggered by "update issue #N")

1. Use `mcp__forgejo__update_issue` to overwrite the issue body with the current contents of `CURRENT_PLAN.md`

### Creating a PR (triggered by "make a PR closing #N")

1. Use `mcp__forgejo__create_pull_request` with `owner: "gtfs.zone"`, `repo: "cafe-car"`, current branch as `head`, `main` as `base`, issue title as PR title, `Closes #N` as body

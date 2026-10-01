# AGENTS.md

FastAPI service: the public GTFS-RT feeds at `rt.gtfs.zone`, the HTTP ingest
seam the producers post to, and rt-manager's authenticated JSON API at
`manage.rt.gtfs.zone/api`. Pushing to `main` publishes the image.

## Commands

```bash
uv run fastapi dev src/gtfs_zone_rt_api/main.py        # public API, :8000
uv run fastapi dev src/gtfs_zone_rt_api/admin_main.py  # admin app, :8001
uv run gtfs-zone-db-models-migrate                     # apply migrations
```

## Architecture

Two FastAPI apps share one Postgres and Redis DB 1: `main.py` (public, no auth)
and `admin_main.py` (behind Keycloak -> oauth2-proxy -> Traefik; no HTML UI,
rt-manager is the UI). The routes, the auth model, the Redis key format, local
auth simulation and env vars are in [docs/architecture.md](docs/architecture.md).
`scripts/` holds the trip simulator and feed inspectors, see [README.md](README.md).

- **Migrations live in gtfs-zone-db-models**: a model change is a revision there,
  a tag, then a dependency bump here. There is no `alembic.ini` in this repo.
- **A person is not a credential**: `User` is the principal, `Identity` one row
  per `(provider, provider_subject)`. Scope on `user_id`, never on the raw header.
- **`accessible_feed_ids`** (`admin/access.py`) is the one definition of "may
  touch this feed". Every `/api` feed-scoped route depends on
  `api/deps.py::accessible_feed`, and an invisible feed is **404, not 403**.
- **Never match an invite or link accounts on an unverified email**: that is an
  account-takeover primitive. A new credential with another user's verified
  email gets its own principal and `/account` offers the merge.
- **`/api` responses** are built from explicit models in `api/schemas.py`, never
  a dumped ORM object. Only the tracker detail and provisioning endpoints may
  return `Tracker.id`. `require_csrf` guards the whole router.
- **`Tracker.device_key`** is the secret Traccar credential, with no password
  behind it. Neither it nor `Tracker.id` appears in a public feed; vehicles are
  labelled by the producer's `vehicle_id`, else the tracker's `nickname`.
- **A vehicle's identity is `(tracker_id, vehicle_id)`**: `trip_id` is data on
  the record, never part of the key. Keys come from
  `gtfs_zone_db_models.vehicle_keys`.
- `GET /api/feeds/{id}/schedule.zip` is the only URL rt-manager fetches a
  schedule from, for either source kind.
- `admin/entity_router.py` routes take the proxy header as authoritative and
  fall back to user id `0`, never the session cookie.
- Never create a stop_time with null departure and arrival.

## Conventions

- **Commits**: Conventional Commits, enforced by the `commit-msg` hook. Never add
  Co-Authored-By trailers. Setup and release are in [CONTRIBUTING.md](CONTRIBUTING.md).
- **Verification**: no Playwright or other browser automation; the user tests UI
  changes by hand.
- **Logging**: module loggers are named `log`, never `logger`.
- **Plans**: write plans to `CURRENT_PLAN.md` at the repo root as a
  checklist (`- [ ]`), ticked off as work lands. It is neither tracked nor
  gitignored: never stage or commit it.

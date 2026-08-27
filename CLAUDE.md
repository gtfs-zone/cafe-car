# cafe-car - Claude Guide

## Project Overview

FastAPI service that:
- Sits behind oauth2-proxy forward auth (Traefik middleware) using Keycloak as the OIDC provider, which brokers GitHub / Google / GitLab
- Manages config data (Feeds, Trackers) in PostgreSQL via SQLModel; the models and their Alembic revisions come from railroad-club
- Exposes GTFS-RT protobuf endpoints (`/<feed_name>/*.pb`) for trip updates, vehicle positions, and service alerts
- Serves [yard-master](https://git.kcfam.us/gtfs.zone/yard-master)'s JSON API at `/api`, scoped to the Feeds a user owns or has been given access to, and the Trackers beneath them. yard-master, a static SPA, is the UI for this; cafe-car itself has none

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
GitHub / Google / GitLab OAuth
    └─> Keycloak (OIDC provider, brokers the above; links them to one account)
            └─> oauth2-proxy (ForwardAuth middleware, auth.gtfs.zone)
                    └─> Traefik
                            ├─> nginx serving yard-master (manage.rt.gtfs.zone), protected by oauth2-proxy
                            │       └─> FastAPI admin app, same host, /api
                            └─> FastAPI public API (rt.gtfs.zone), no auth required
                                    ├─> PostgreSQL (railroad-club models + migrations)
                                    └─> Redis DB 1  (cache / RT data)
```

## Authentication

oauth2-proxy injects headers on every authenticated request to the admin interface:
- `X-Auth-Request-User`: OIDC `sub` claim (a Keycloak UUID); identifies the credential
- `X-Auth-Request-Email`: email address
- `X-Auth-Request-Access-Token`: OIDC access token

No passwords are stored for web users; Keycloak owns credentials, and brokers GitHub/Google/GitLab behind them.

**A person is not a credential.** `User` is the principal that everything else (feeds, memberships) points at; `Identity` is one row per `(provider, provider_subject)` pair, many-to-one back to `User`. Signing in with GitHub and with Google gives one user and two identities. `cafe_car/accounts.py::resolve_login` resolves a login to a `User`, creating both rows the first time a credential is seen.

Every scoped query filters on `user_id`, never on the raw header. `request.session["user_id"]` and `current_user_id_var` carry it; `subject` is kept for display only. `cafe_car/admin/access.py::accessible_feed_ids` is the single definition of "may touch this feed" (owner **or** member); trackers, tracker rules, alerts and informed entities all scope through it.

A new credential whose *verified* email already belongs to another user never merges silently. It gets its own principal, and `/account` offers the merge, which the user confirms. `merge_users` is in `accounts.py`.

The public GTFS-RT endpoints (`rt.gtfs.zone`) have **no authentication middleware**; they are publicly accessible.

`Tracker` records have a secret pet-name `id` (e.g. `gently-tender-oyster`) that serves as the Traccar `uniqueId` / QR provisioning credential. There is **no password**. The `id` is a secret and is never exposed in a public GTFS-RT feed; feeds show the tracker's public `nickname` instead.

## Redis DB Allocation

- DB 0: oauth2-proxy session storage (managed by deploy-gtfs-rt)
- DB 1: This FastAPI service (cache and real-time data)
  - `REDIS_URL=redis://redis:6379/1`
- DB 2: Bridge pub/sub messages (vehicle-poser)

## Two-App Architecture

There are two separate FastAPI apps sharing the same DB/Redis:

- `src/cafe_car/main.py` → **public API** (`app = create_public_app()`): GTFS-RT endpoints (`/{feed_name}/trip_updates.pb`, `vehicle_positions.pb`, `service_alerts.pb`, plus a `.json` twin of each), the public feed catalog (`GET /feeds`) and the HTTP ingest seam (`POST /ingest/position`, `/ingest/trip-update`, `/ingest/alerts`). Run with `uv run fastapi dev src/cafe_car/main.py`.
- `src/cafe_car/admin_main.py` → **admin app** (`app = create_admin_app()`): [yard-master](https://git.kcfam.us/gtfs.zone/yard-master)'s JSON API, mounted at `/api`, plus `admin/entity_router.py`'s hand-written routes (sharing, account linking). No SQLAdmin any more — this app has no HTML UI of its own; yard-master, a separate static SPA, is that UI now. Uses `SessionMiddleware`, `DBSessionMiddleware`, and `SubjectMiddleware`. Run with `uv run fastapi dev src/cafe_car/admin_main.py`.

Rules for anything added under `/api`:

- Every response is built from an explicit model in `api/schemas.py`, never by
  dumping an ORM object. `TrackerOut` has no `id`; `TrackerDetailOut` does, and
  only the tracker detail and provisioning endpoints may return it.
- Every feed-scoped route depends on `api/deps.py::accessible_feed`, and a feed
  the caller cannot see answers **404, not 403**, so no id is confirmed.
- `require_csrf` is a dependency of the whole router, so every mutation carries
  `X-Yard-Master` without a route having to remember.
- `GET /api/feeds` scopes through `personal_feed_ids`, which does not apply the
  admin bypass; `?all=1` is how an admin opts in, and it is refused to everyone
  else.
- `GET /api/feeds/{id}/schedule.zip` is the one URL yard-master downloads a
  schedule from, whichever source kind the feed is: a hosted feed streams the
  current upload (the same body-and-headers helper as the public
  `/{feed_name}/gtfs.zip`), a linked feed is fetched here from
  `static_feed_url` and streamed back with no ETag. The browser never fetches
  the public URL itself: that URL is prod-only and a linked feed's CORS
  policy would refuse it anyway; this endpoint is same-origin and reads
  whatever the feed row already points at.

The current user id flows via `request.session["user_id"]` and via
`current_user_id_var` (`ContextVar`). `admin/auth.py::resolve_request_user_id`
is the fast, read-only lookup every request goes through first; when a
subject has no `Identity` row yet, `ensure_identity` (same file) creates one,
claims any invites waiting on its verified email, and primes the session —
the provisioning that `OIDCAuthBackend.authenticate` used to do before
SQLAdmin was removed. `OIDCAuthBackend` itself is unused now but not yet
deleted; see the note on the `sqladmin` pin in `pyproject.toml`.

`admin/entity_router.py` holds hand-written routes (sharing, account linking,
and some htmx partials left from the retired SQLAdmin pages). Nothing runs
`resolve_request_user_id`'s slow path for them automatically; they take the
proxy header as authoritative and fall back to user id `0`, never to the
session cookie, which may belong to whoever used the browser last.

## Redis Data Format

Vehicle positions are stored at key `vehicle:{tracker.id}:{slug}`, one key per concurrent vehicle under that tracker, and read back with a `vehicle:{tracker.id}:*` scan. The slug is `{trip_id}` or `{trip_id}:{start_date}`, which is what keeps concurrent instances of one long-running daily trip apart. Each value is JSON with fields: `tracker_id`, `lat`, `lon`, `bearing`, `speed`, `trip_id`, `timestamp`, plus optional `route_id`, `start_date`, `vehicle_id`, `vehicle_label`, `current_stop_sequence`, `stop_id` and `current_status`; the serialiser reads every optional one with `.get()`, so a producer that predates a key just omits it. The `tracker_id` is the secret credential and is only a Redis-internal identifier; feeds label vehicles by the producer's public `vehicle_id`, falling back to the tracker's `nickname` from the DB.

Trip updates are stored at `trip_update:{trip_id}` or `trip_update:{trip_id}:{start_date}`. Positions carry a 60s TTL, trip updates 300s: a prediction stays valid for longer than the fix that produced it.

## Migrations

Models and Alembic revisions live in **railroad-club**, not here; this repo has
no `alembic.ini`. Apply them with the console script railroad-club ships, which
is also what the cluster's PreSync hook runs:

```bash
uv run railroad-club-migrate
```

A model change means a new revision in railroad-club, then a dependency bump
here.

## Running Locally

Requires a `.env` file with:
```
DATABASE_URL=postgresql+asyncpg://postgres:mysecretpassword@localhost:5432/postgres
REDIS_URL=redis://localhost:6379/1
SESSION_SECRET_KEY=some-random-secret-key
```

```bash
uv sync
uv run fastapi dev src/cafe_car/main.py        # public API → :8000
uv run fastapi dev src/cafe_car/admin_main.py  # admin app  → :8001
```

API docs: http://localhost:8000/docs
Admin app: http://localhost:8001/api (no UI of its own; yard-master is the UI, run separately)

To simulate oauth2-proxy headers locally:
```bash
curl -H "X-Auth-Request-User: alice" -H "X-Auth-Request-Email: alice@example.com" \
     http://localhost:8001/api/me
```

`X-Auth-Request-User` is the OIDC subject and is the only thing that identifies the caller; the header alone creates the `User` and `Identity` on first use. To simulate a *verified* email (needed for invite claiming and account linking, both of which refuse unverified addresses), set `DEBUG=true` and pass an unsigned JWT whose `sub` matches the header:

```bash
TOKEN=$(python3 -c "
import base64, json
b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')
print(b64({'alg':'none'}) + '.' + b64({'sub':'alice','email':'alice@example.com','email_verified':True}) + '.x')")
curl -H "X-Auth-Request-User: alice" -H "Authorization: Bearer $TOKEN" http://localhost:8001/account
```

## Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string, e.g. `postgresql+asyncpg://postgres:password@localhost:5432/cafe-car` |
| `REDIS_URL` | Redis connection string, e.g. `redis://localhost:6379/1` |
| `SESSION_SECRET_KEY` | Secret key for signing sessions |
| `OIDC_PROVIDER` | Namespaces an `Identity`'s `provider_subject`. Defaults to `keycloak` |
| `KEYCLOAK_ACCOUNT_URL` | Keycloak's Account Console, linked from `/account`. Empty hides the link |

## Rules

- Never include `Co-Authored-By: Claude ...` trailers in commit messages.
- Do not use Playwright / the browser automation tools. The user tests UI changes manually.
- Never create a stop_time with null departure and arrival
- `/api` routes must always scope queries through `accessible_feed_ids`; never expose a Feed or Tracker the caller neither owns nor is a member of
- Never match an invite or link two accounts on an **unverified** email; that is an account-takeover primitive
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

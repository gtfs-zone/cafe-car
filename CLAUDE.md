# gtfs-zone-rt-api - Claude Guide

## Project Overview

FastAPI service that:
- Sits behind oauth2-proxy forward auth (Traefik middleware) using Keycloak as the OIDC provider, which brokers GitHub / Google / GitLab
- Manages config data (Feeds, Trackers) in PostgreSQL via SQLModel; the models and their Alembic revisions come from gtfs-zone-db-models
- Exposes GTFS-RT protobuf endpoints (`/<feed_name>/*.pb`) for trip updates, vehicle positions, and service alerts
- Serves [rt-manager](https://github.com/gtfs-zone/gtfs-zone-rt-manager)'s JSON API at `/api`, scoped to the Feeds a user owns or has been given access to, and the Trackers beneath them. rt-manager, a static SPA, is the UI for this; rt-api itself has none

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
                            ├─> nginx serving rt-manager (manage.rt.gtfs.zone), protected by oauth2-proxy
                            │       └─> FastAPI admin app, same host, /api
                            └─> FastAPI public API (rt.gtfs.zone), no auth required
                                    ├─> PostgreSQL (gtfs-zone-db-models models + migrations)
                                    └─> Redis DB 1  (cache / RT data)
```

## Authentication

oauth2-proxy injects headers on every authenticated request to the admin interface:
- `X-Auth-Request-User`: OIDC `sub` claim (a Keycloak UUID); identifies the credential
- `X-Auth-Request-Email`: email address
- `X-Auth-Request-Access-Token`: OIDC access token

No passwords are stored for web users; Keycloak owns credentials, and brokers GitHub/Google/GitLab behind them.

**A person is not a credential.** `User` is the principal that everything else (feeds, memberships) points at; `Identity` is one row per `(provider, provider_subject)` pair, many-to-one back to `User`. Signing in with GitHub and with Google gives one user and two identities. `gtfs_zone_rt_api/accounts.py::resolve_login` resolves a login to a `User`, creating both rows the first time a credential is seen.

Every scoped query filters on `user_id`, never on the raw header. `request.session["user_id"]` and `current_user_id_var` carry it; `subject` is kept for display only. `gtfs_zone_rt_api/admin/access.py::accessible_feed_ids` is the single definition of "may touch this feed" (owner **or** member); trackers, tracker rules, alerts and informed entities all scope through it.

A new credential whose *verified* email already belongs to another user never merges silently. It gets its own principal, and `/account` offers the merge, which the user confirms. `merge_users` is in `accounts.py`.

The public GTFS-RT endpoints (`rt.gtfs.zone`) have **no authentication middleware**; they are publicly accessible.

A `Tracker` has two ids. `id` is a uuid4 hex surrogate, the primary key and the Redis key namespace; it is not a secret. `device_key` is the secret pet-name (e.g. `gently-tender-oyster`) that serves as the Traccar `uniqueId` / QR provisioning credential, and there is **no password** behind it. Neither is exposed in a public GTFS-RT feed: feeds label a vehicle with the producer's `vehicle_id`, falling back to the tracker's public `nickname`.

## Redis DB Allocation

- DB 0: oauth2-proxy session storage (managed by gtfs-zone-infra)
- DB 1: This FastAPI service (cache and real-time data)
  - `REDIS_URL=redis://redis:6379/1`
- DB 2: Bridge pub/sub messages (rt-traccar-receiver)

## Two-App Architecture

There are two separate FastAPI apps sharing the same DB/Redis:

- `src/gtfs_zone_rt_api/main.py` → **public API** (`app = create_public_app()`): GTFS-RT endpoints (`/{feed_name}/trip_updates.pb`, `vehicle_positions.pb`, `service_alerts.pb`, plus a `.json` twin of each), the public feed catalog (`GET /feeds`) and the HTTP ingest seam (`POST /ingest/position`, `/ingest/trip-update`, their `/ingest/positions` and `/ingest/trip-updates` batch twins, and `/ingest/alerts`). Run with `uv run fastapi dev src/gtfs_zone_rt_api/main.py`.
- `src/gtfs_zone_rt_api/admin_main.py` → **admin app** (`app = create_admin_app()`): [rt-manager](https://github.com/gtfs-zone/gtfs-zone-rt-manager)'s JSON API, mounted at `/api`, plus `admin/entity_router.py`'s hand-written routes (sharing, account linking). No SQLAdmin any more — this app has no HTML UI of its own; rt-manager, a separate static SPA, is that UI now. Uses `SessionMiddleware`, `DBSessionMiddleware`, and `SubjectMiddleware`. Run with `uv run fastapi dev src/gtfs_zone_rt_api/admin_main.py`.

Rules for anything added under `/api`:

- Every response is built from an explicit model in `api/schemas.py`, never by
  dumping an ORM object. `TrackerOut` has no `id`; `TrackerDetailOut` does, and
  only the tracker detail and provisioning endpoints may return it.
- Every feed-scoped route depends on `api/deps.py::accessible_feed`, and a feed
  the caller cannot see answers **404, not 403**, so no id is confirmed.
- `require_csrf` is a dependency of the whole router, so every mutation carries
  `X-RT-Manager` without a route having to remember.
- `GET /api/feeds` scopes through `personal_feed_ids`, which does not apply the
  admin bypass; `?all=1` is how an admin opts in, and it is refused to everyone
  else.
- `GET /api/feeds/{id}/schedule.zip` is the one URL rt-manager downloads a
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

Vehicle positions are stored at key `vehicle:{tracker.id}:{vehicle_id}`, one key per real-world vehicle under that tracker. A producer with no per-vehicle id - a Traccar device is one tracker, one vehicle - holds the bare key `vehicle:{tracker.id}` and so exactly one record. **A vehicle's identity is `(tracker_id, vehicle_id)`**: `trip_id` and `start_date` are data on the record, never part of the key, so a vehicle that finishes one trip and starts another overwrites its own record instead of leaving the old one to live out its TTL beside the new one. Each value is JSON with fields: `tracker_id`, `lat`, `lon`, `bearing`, `speed`, `trip_id`, `timestamp`, plus optional `route_id`, `start_date`, `vehicle_id`, `vehicle_label`, `current_stop_sequence`, `stop_id` and `current_status`; the serialiser reads every optional one with `.get()`, so a producer that predates a key just omits it. Key derivation lives in `gtfs_zone_db_models.vehicle_keys` and is re-exported by `vehicle_payload.py`; a tracker id never contains `:`, which is what lets `split_vehicle_key` hand back a `vehicle_id` that does, such as Amtrak's `449:20260921`. That rule is enforced in one place, `Tracker.validate_id` in gtfs-zone-db-models, and nothing downstream re-checks it.

Trip updates are stored at `trip_update:{tracker.id}:{trip_id}` or `trip_update:{tracker.id}:{trip_id}:{start_date}`. The keyspace is scoped by tracker so two feeds whose GTFS share a `trip_id` string do not overwrite each other's predictions. Positions carry a 60s TTL, trip updates 300s: a prediction stays valid for longer than the fix that produced it.

## Migrations

Models and Alembic revisions live in **gtfs-zone-db-models**, not here; this repo has
no `alembic.ini`. Apply them with the console script gtfs-zone-db-models ships, which
is also what the cluster's PreSync hook runs:

```bash
uv run gtfs-zone-db-models-migrate
```

A model change means a new revision in gtfs-zone-db-models, then a dependency bump
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
uv run fastapi dev src/gtfs_zone_rt_api/main.py        # public API → :8000
uv run fastapi dev src/gtfs_zone_rt_api/admin_main.py  # admin app  → :8001
```

API docs: http://localhost:8000/docs
Admin app: http://localhost:8001/api (no UI of its own; rt-manager is the UI, run separately)

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
| `DATABASE_URL` | PostgreSQL connection string, e.g. `postgresql+asyncpg://postgres:password@localhost:5432/rt-api` |
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
- Module loggers are named `log`, never `logger`: `log = logging.getLogger(__name__)`

## Related Repos

| Repo | Description | URL |
|---|---|---|
| rt-api | GTFS-RT HTTP API serving real-time feeds | https://github.com/gtfs-zone/gtfs-zone-rt-api |
| rt-traccar-receiver | Worker that tracks and posts vehicle positions | https://github.com/gtfs-zone/gtfs-zone-rt-traccar-receiver |
| rt-delay-estimator | Worker that generates trip update predictions | https://github.com/gtfs-zone/gtfs-zone-rt-delay-estimator |
| static-importer | Worker that ingests and processes GTFS schedule data | https://github.com/gtfs-zone/gtfs-zone-static-importer |
| gtfs-zone-db-models | Shared Python library for GTFS types and utilities | https://github.com/gtfs-zone/gtfs-zone-db-models |
| dev-stack | Orchestration repo for deployments and infra | https://github.com/gtfs-zone/gtfs-zone-dev-stack |
| homepage | Static marketing/status site | https://github.com/gtfs-zone/gtfs-zone-homepage |

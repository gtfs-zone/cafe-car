# Architecture

## Two apps

There are two separate FastAPI apps sharing the same DB/Redis:

- `src/gtfs_zone_rt_api/main.py` -> **public API** (`app = create_public_app()`): GTFS-RT endpoints (`/{feed_name}/trip_updates.pb`, `vehicle_positions.pb`, `service_alerts.pb`, plus a `.json` twin of each), the public feed catalog (`GET /feeds`) and the HTTP ingest seam (`POST /ingest/position`, `/ingest/trip-update`, their `/ingest/positions` and `/ingest/trip-updates` batch twins, and `/ingest/alerts`). Run with `uv run fastapi dev src/gtfs_zone_rt_api/main.py`.
- `src/gtfs_zone_rt_api/admin_main.py` -> **admin app** (`app = create_admin_app()`): [rt-manager](https://github.com/gtfs-zone/gtfs-zone-rt-manager)'s JSON API, mounted at `/api`, plus `admin/entity_router.py`'s hand-written routes (sharing, account linking). No SQLAdmin any more: this app has no HTML UI of its own; rt-manager, a separate static SPA, is that UI now. Uses `SessionMiddleware`, `DBSessionMiddleware`, and `SubjectMiddleware`. Run with `uv run fastapi dev src/gtfs_zone_rt_api/admin_main.py`.

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
claims any invites waiting on its verified email, and primes the session : 
the provisioning that `OIDCAuthBackend.authenticate` used to do before
SQLAdmin was removed.

`admin/entity_router.py` holds hand-written routes (sharing, account linking,
and some htmx partials left from the retired SQLAdmin pages). Nothing runs
`resolve_request_user_id`'s slow path for them automatically; they take the
proxy header as authoritative and fall back to user id `0`, never to the
session cookie, which may belong to whoever used the browser last.

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

### Simulating oauth2-proxy locally

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

## Redis data format

Vehicle positions are stored at key `vehicle:{tracker.id}:{vehicle_id}`, one key per real-world vehicle under that tracker. A producer with no per-vehicle id (a Traccar device is one tracker, one vehicle) holds the bare key `vehicle:{tracker.id}` and so exactly one record. **A vehicle's identity is `(tracker_id, vehicle_id)`**: `trip_id` and `start_date` are data on the record, never part of the key, so a vehicle that finishes one trip and starts another overwrites its own record instead of leaving the old one to live out its TTL beside the new one. Each value is JSON with fields: `tracker_id`, `lat`, `lon`, `bearing`, `speed`, `trip_id`, `timestamp`, plus optional `route_id`, `start_date`, `vehicle_id`, `vehicle_label`, `current_stop_sequence`, `stop_id` and `current_status`; the serialiser reads every optional one with `.get()`, so a producer that predates a key just omits it. Key derivation lives in `gtfs_zone_db_models.vehicle_keys` and is re-exported by `vehicle_payload.py`; a tracker id never contains `:`, which is what lets `split_vehicle_key` hand back a `vehicle_id` that does, such as Amtrak's `449:20260921`. That rule is enforced in one place, `Tracker.validate_id` in gtfs-zone-db-models, and nothing downstream re-checks it.

Trip updates are stored at `trip_update:{tracker.id}:{trip_id}` or `trip_update:{tracker.id}:{trip_id}:{start_date}`. The keyspace is scoped by tracker so two feeds whose GTFS share a `trip_id` string do not overwrite each other's predictions. Positions carry a 60s TTL, trip updates 300s: a prediction stays valid for longer than the fix that produced it.

## Environment variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string, e.g. `postgresql+asyncpg://postgres:password@localhost:5432/rt-api` |
| `REDIS_URL` | Redis connection string, e.g. `redis://localhost:6379/1` |
| `SESSION_SECRET_KEY` | Secret key for signing sessions |
| `OIDC_PROVIDER` | Namespaces an `Identity`'s `provider_subject`. Defaults to `keycloak` |
| `KEYCLOAK_ACCOUNT_URL` | Keycloak's Account Console, linked from `/account`. Empty hides the link |

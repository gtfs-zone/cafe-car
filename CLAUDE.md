# cafe-car — Claude Guide

## Project Overview

FastAPI service that:
- Sits behind oauth2-proxy forward auth (Traefik middleware) using Keycloak as the OIDC provider, which brokers GitHub / Google / GitLab
- Manages config data (Feeds, Trackers) in PostgreSQL via SQLModel + Alembic
- Exposes GTFS-RT protobuf endpoints (`/<feed_name>/*.pb`) for trip updates, vehicle positions, and service alerts
- Provides a scoped SQLAdmin interface at `/admin` where a user sees only the Feeds they own or have been given access to, and the Trackers beneath them

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
                            ├─> FastAPI admin app (manage.rt.gtfs.zone) — protected by oauth2-proxy
                            └─> FastAPI public API (rt.gtfs.zone)    — no auth required
                                    ├─> PostgreSQL (SQLModel models, Alembic migrations)
                                    └─> Redis DB 1  (cache / RT data)
```

## Authentication

oauth2-proxy injects headers on every authenticated request to the admin interface:
- `X-Auth-Request-User` — OIDC `sub` claim (a Keycloak UUID); identifies the credential
- `X-Auth-Request-Email` — email address
- `X-Auth-Request-Access-Token` — OIDC access token

No passwords are stored for web users — Keycloak owns credentials, and brokers GitHub/Google/GitLab behind them.

**A person is not a credential.** `User` is the principal that everything else (feeds, memberships) points at; `Identity` is one row per `(provider, provider_subject)` pair, many-to-one back to `User`. Signing in with GitHub and with Google gives one user and two identities. `cafe_car/accounts.py::resolve_login` resolves a login to a `User`, creating both rows the first time a credential is seen.

Every scoped query filters on `user_id`, never on the raw header. `request.session["user_id"]` and `current_user_id_var` carry it; `subject` is kept for display only. `cafe_car/admin/access.py::accessible_feed_ids` is the single definition of "may touch this feed" (owner **or** member) — trackers, tracker rules, alerts and informed entities all scope through it.

A new credential whose *verified* email already belongs to another user never merges silently. It gets its own principal, and `/account` offers the merge, which the user confirms. `merge_users` is in `accounts.py`.

The public GTFS-RT endpoints (`rt.gtfs.zone`) have **no authentication middleware** — they are publicly accessible.

`Tracker` records have a secret pet-name `id` (e.g. `gently-tender-oyster`) that serves as the Traccar `uniqueId` / QR provisioning credential. There is **no password**. The `id` is a secret and is never exposed in a public GTFS-RT feed — feeds show the tracker's public `nickname` instead.

## Redis DB Allocation

- DB 0: oauth2-proxy session storage (managed by deploy-gtfs-rt)
- DB 1: This FastAPI service (cache and real-time data)
  - `REDIS_URL=redis://redis:6379/1`
- DB 2: Bridge pub/sub messages (vehicle-poser)

## Two-App Architecture

There are two separate FastAPI apps sharing the same DB/Redis:

- `src/app/main.py` → **public API** (`app = create_public_app()`): GTFS-RT protobuf endpoints (`/{feed_name}/trip_updates.pb`, `vehicle_positions.pb`, `service_alerts.pb`) + the HTTP ingest seam (`POST /ingest/position`, `POST /ingest/trip-update`). Run with `uv run fastapi dev src/app/main.py`.
- `src/app/admin_main.py` → **admin app** (`app = create_admin_app()`): SQLAdmin interface mounted at `/`. Uses `OIDCAuthBackend`, `SessionMiddleware`, `DBSessionMiddleware`, and `SubjectMiddleware`. Run with `uv run fastapi dev src/app/admin_main.py`.

The current user id flows via `request.session["user_id"]` and via `current_user_id_var` (`ContextVar`) for use in `scaffold_form`, where `request` is unavailable. The ContextVar is set inside `authenticate`, not in the middleware — middleware runs *before* authentication, so it would otherwise lag a request behind and hand a switched-over browser the previous user's data.

**There are no details pages.** Every view subclasses `ScopedModelView`, which
sets `can_view_details = False`, so `/{identity}/details/{pk}` returns 403. The
edit page is the only page for an object and shows non-editable fields read-only;
`/feed/edit/{id}` is the hub, linking to the feed's trackers, alerts and people.
`templates/sqladmin/list.html` is a **fork** of the pinned sqladmin's copy (row
actions moved right and reduced to delete; relation cells link to `admin:edit`,
since `admin:details` now 403s) — re-check it whenever the `sqladmin` pin moves.

htmx is vendored at `admin/static/htmx.min.js`, served from `/vendor/htmx.min.js`
and loaded once in `base.html`. Do not add per-template CDN `<script>` tags: a
page that forgets one leaves its panels reading "Loading…" forever, which is
exactly how the sharing UI shipped broken.

`admin/entity_router.py` holds the routes that sit **outside** SQLAdmin (sharing, account linking, htmx partials). Nothing runs `authenticate` for them, so they take the proxy header as authoritative and fall back to user id `0` — never to the session cookie, which may belong to whoever used the browser last. They are registered *before* `Admin` mounts at `/`, or the mount swallows them.

## Redis Data Format

Vehicle positions are stored at key `vehicle:{tracker.id}` as JSON with fields: `tracker_id`, `lat`, `lon`, `bearing`, `speed`, `trip_id`, `route_id` (optional), `timestamp`. The `tracker_id` is the secret credential and is only a Redis-internal identifier — feeds label vehicles by the tracker's public `nickname`, resolved from the DB.

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

`X-Auth-Request-User` is the OIDC subject and is the only thing that identifies the caller — the header alone creates the `User` and `Identity` on first use. To simulate a *verified* email (needed for invite claiming and account linking, both of which refuse unverified addresses), set `DEBUG=true` and pass an unsigned JWT whose `sub` matches the header:

```bash
TOKEN=$(python3 -c "
import base64, json
b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')
print(b64({'alg':'none'}) + '.' + b64({'sub':'alice','email':'alice@example.com','email_verified':True}) + '.x')")
curl -H "X-Auth-Request-User: alice" -H "Authorization: Bearer $TOKEN" http://localhost:8000/account
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
- Admin views must always scope queries through `accessible_feed_ids` — never expose a Feed or Tracker the caller neither owns nor is a member of
- Never add a relationship to `Feed` without also excluding it from `FeedAdmin.form_excluded_columns`. WTForms walks every attribute and lazy-loads it on a detached instance, which raises `DetachedInstanceError` and breaks the edit form. This has now happened twice (`members`, `invites`)
- Anything a `*_edit.html` template touches must be eager-loaded in that view's `form_edit_query`. SQLAdmin's `_run_query` closes its session before rendering, so a bare relationship access is a `DetachedInstanceError`, not a slow query
- Never interpolate model text into `Markup(...)` in a `column_formatters` lambda — use `_link()` or `escape()`. `nickname`, `header_text` and `trip_id` are free text and `Tracker.id` is caller-supplied, so unescaped interpolation is stored XSS against everyone a feed is shared with
- Never match an invite or link two accounts on an **unverified** email — that is an account-takeover primitive
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

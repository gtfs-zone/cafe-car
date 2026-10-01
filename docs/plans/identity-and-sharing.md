# Plan: Robust identity (multi-OAuth account linking) + feed sharing

## Summary

Two related pieces of work:

1. **Identity.** Replace Dex with **Keycloak** so a person can sign in with GitHub
   *or* Google and land on the same account. Keycloak handles brokering, the
   "an account with this email already exists, link it?" first-login prompt,
   and a self-serve Account Console where a logged-in user adds another
   provider. rt-api additionally grows a canonical `user` + `identity` model
   so the app is not permanently married to one IdP's `sub` semantics.
2. **Sharing.** A `Feed` gets one **owner** and any number of **members** with
   equivalent access, except that members may not transfer/delete ownership or
   manage the member list. Backed by an explicit `feed_member` association *row*
   (own PK, own timestamps) plus a `feed_invite` table for people who have never
   logged in, deliberately not a SQLModel `link_model` M2M, for reasons in
   Phase 4.

Decisions already made (do not relitigate):

- Keycloak, replacing Dex, not "keep Dex + app-side linking".
- App-side `identity` table anyway, even though Keycloak links upstream.
- Email match → **suggest and require confirmation**, never silent auto-merge.
- Flat owner + members. No viewer/editor tiers.
- Invite-by-email rows, claimed on first login with a matching verified email.

## Relevant Context

### Where things live

| Thing | Repo / path |
|---|---|
| `User`, `Feed`, `Tracker`, … SQLModel models | `gtfs-zone-db-models` → `src/gtfs_zone_db_models/models/` |
| Alembic migrations (`gtfs-zone-db-models-migrate`) | `gtfs-zone-db-models` → `src/gtfs_zone_db_models/alembic/versions/` |
| Admin app, auth backend, scoped views | `rt-api` → `src/gtfs_zone_rt_api/admin/` |
| Dev docker-compose, Dex config | `dev-stack` → `docker-compose.yml`, `dev/dex/config.yaml` |
| Prod k8s (Dex Deployment, config, SOPS secrets) | `gtfs-zone-infra` → `gtfs/dex/` |

### How identity works today

- oauth2-proxy sets `X-Auth-Request-User`; `OIDCAuthBackend.authenticate`
  (`src/gtfs_zone_rt_api/admin/auth.py:37`) reads it, upserts a `User` row keyed on
  `(provider="dex", provider_subject=<header>)`, and stashes the raw string in
  `request.session["subject"]` and `current_subject_var`.
- **Every** scoped query is `… .join(User, Feed.owner_id == User.id).where(User.provider_subject == subject)`.
  It appears ~20 times across five ModelViews in `src/gtfs_zone_rt_api/admin/views.py`
  and four helpers in `src/gtfs_zone_rt_api/admin/entity_router.py`.
- `Feed.owner_id` is a plain FK to `user.id` (`gtfs_zone_db_models/models/feed.py`).

### Two landmines in the current setup

1. **Identity is keyed on whatever oauth2-proxy happens to pass.** Dev set
   `OAUTH2_PROXY_USER_ID_CLAIM: name`, so `provider_subject` was the *name*
   claim, mutable, and not unique across connectors. (Older dev rows carry
   Dex's opaque per-connector `sub` instead, from before that setting; the dev
   DB has both, which is itself the duplicate-account problem in miniature.)
   Either way nothing survives the switch to Keycloak, so every existing `user`
   row needs remapping (Phase 7).
2. **Dex cannot link accounts.** Each connector mints a distinct opaque `sub`
   (base64 of `{userID, connID}`), and Dex has no notion of one user with two
   connectors. This is the whole reason for the swap; it is not a config gap.

### Blast radius outside rt-api

- **Traccar** authenticates *its* manager users against Dex directly as the
  `traccar` static client (`gtfs-zone-infra/gtfs/dex/config.yaml`). It must move
  to Keycloak in the same cutover or it breaks.
- **Redis DB 0** holds oauth2-proxy sessions. They reference Dex tokens and must
  be flushed at cutover.
- Public GTFS-RT endpoints (`rt.gtfs.zone`) have no auth and are **untouched** by
  all of this.

---

## Phase 1: Keycloak in the dev compose stack

Stand Keycloak up next to Dex in `dev-stack` first, on a different port, so
both can run while the app is ported. Configure it declaratively (realm JSON
imported at boot) rather than by clicking, so dev and prod stay identical and
the config is reviewable in git.

The realm needs: a `gtfs` realm; an `oauth2-proxy` confidential client; a
`traccar` confidential client; GitHub and Google identity providers; and the
**first broker login** flow configured as *Detect Existing Broker User* →
*Confirm Link Existing Account* → *Verify Existing Account By Email*. That
built-in chain is precisely the "we found an account with this email, confirm to
link" behavior we want, and it does **not** auto-link on an unverified email.

- [x] Add a `keycloak` service to `dev-stack/docker-compose.yml`
      (`quay.io/keycloak/keycloak:26.4`, `start-dev --import-realm`), backed by
      the existing `db` service with its own `keycloak` database.
- [x] Write `dev-stack/dev/keycloak/gtfs-realm.json`: realm, clients, IdPs,
      and two dev users (alice/bob) at parity with the old static passwords.
- [x] Wire the GitHub/Google IdPs, **as two fake Keycloak realms**, see below.
- [x] Repoint `oauth2-proxy` at Keycloak
      (`http://keycloak:8090/realms/gtfs`), and enable PKCE while there.
- [x] **Set `OAUTH2_PROXY_USER_ID_CLAIM: sub`** (drop the `name` override).
- [x] Verify `X-Auth-Request-User` is now a stable Keycloak UUID.
- [x] Add Keycloak docs to README / CLAUDE.md / startup-guide.md.

**Discoveries**

- **No custom flow JSON was needed.** Keycloak's stock `first broker login`
  flow already is exactly the chain the plan called for, verified against the
  running server: Review Profile → *Create User If Unique* | *Handle Existing
  Account* → Confirm link existing account → (Verify by Email | Verify by
  Re-authentication). So the realm just points both IdPs at `first broker
  login` and sets `trustEmail: false`.
- **Env-var placeholders in the realm JSON were sidestepped.** Instead of real
  GitHub/Google credentials in dev, the stack brokers to two *fake* realms in
  the same Keycloak (`fake-github`, `fake-google`) via generic `oidc` IdPs.
  Fully offline, and it covers all three cases: new user (carol), existing
  email (alice via github), second provider (alice via google).
- **Mailpit was added** (port 8025) and wired as the realm's SMTP server. It is
  not optional garnish: an account created through a broker has no password, so
  the "Verify by Re-authentication" branch cannot complete for it and email
  verification is the only working confirmation path.
- **Port 8090 inside *and* outside the container**, with a `keycloak` →
  127.0.0.1 `/etc/hosts` entry (the same requirement Dex already had). The
  issuer is baked into tokens, so browser and containers must use one identical
  URL. Verified: discovery reports `issuer: http://keycloak:8090/realms/gtfs`.
- **Verified end to end** by driving the login with curl: both IdP buttons
  render, and a full login through oauth2-proxy created
  `user(provider=dex, provider_subject=d60d01c0-8846-…)` (a Keycloak UUID),
  alongside the pre-existing Dex row for the same person. Two rows, one human:
  precisely what Phases 2–3 and the Phase 7 remap exist to fix.

**Gotchas**

- Realm import is **create-only**: an existing realm is not updated on restart.
  Iterating on the realm JSON means dropping the `keycloak` database. Documented
  in the README; it will bite otherwise.
- Dex is still running and still serves Traccar. It moves in Phase 7.
- `provider` is still hardcoded to the `oidc_provider` setting (`"dex"`) in
  rt-api, hence the misleading `provider=dex` on the Keycloak row above.
  Phase 3 changes that string; Phase 7 rewrites the rows.

## Phase 2: Canonical `user` + `identity` schema in gtfs-zone-db-models

Split "the person" from "the credential". `User` becomes the identity-agnostic
principal that everything else FKs to; `Identity` is one row per (provider,
subject) pair, many-to-one back to `User`.

```
User      id, primary_email, display_name, created_at
Identity  id, user_id FK, provider, provider_subject, email, email_verified,
          linked_at        UNIQUE(provider, provider_subject)
```

`User.provider` / `User.provider_subject` go away. `Feed.owner_id` keeps
pointing at `user.id`, so no FK churn elsewhere.

- [x] Add `src/gtfs_zone_db_models/models/identity.py`; rework `models/user.py`.
- [x] Alembic migration `a4b5c6d7e8f9`: create `identity`, backfill one row per
      existing user, drop the old columns and `uq_user_provider_subject`.
- [x] Lossy downgrade that collapses the earliest identity back, documented as
      such in the migration docstring.
- [x] Re-lock rt-api against the new gtfs-zone-db-models.

**Discoveries**

- The unique constraint is named `uq_user_provider_subject` (from migration
  `a1b2c3d4e5f6`), not the Postgres default; dropping it by the default name
  would have failed on a real database.
- Verified up → down → up against the dev database: `user.id` is preserved in
  both directions, so feed ownership survives.
- `alembic check` reports no drift from the new models. (It does report one
  pre-existing drift, `ix_tracker_rule_tracker_id`, unrelated to this work.)
- `email_verified` backfills as **false**, not true: nothing in the old schema
  recorded whether an address was ever proven, and account linking keys off
  this flag. Guessing true would have handed it a forged match to trust.

**Gotchas**

- The backfilled `provider_subject` values are the *old Dex name-claim* strings,
  which will not match anything Keycloak sends. That is expected and is resolved
  by the remap in Phase 7; the backfill exists to preserve `user.id` (and
  therefore feed ownership), not to keep anyone logged in.
- Do **not** make `identity.email` unique. Two identities can legitimately carry
  the same email; that is the whole point.
- `primary_email` is a denormalized convenience for display and invite matching.
  Keep it a plain nullable column refreshed on login, not a computed property;
  invite claiming (Phase 5) needs to query it.

## Phase 3: Rewrite the auth backend around user ids

`OIDCAuthBackend.authenticate` stops upserting a `User` keyed on the header and
instead: look up `Identity` by `(provider, subject)` → if found, use its user;
if not, run the "does a verified email match an existing user?" check (Phase 6)
before creating anything. Session then carries `user_id`, and *that* is what
every scoped query uses.

The mechanical part is replacing the `.join(User, …).where(User.provider_subject == …)`
pattern everywhere. Rather than search-and-replace 20 call sites into a new
2-table join, introduce one helper and have every view call it; this also makes
Phase 4's membership rules a single-place change.

```python
# gtfs_zone_rt_api/admin/access.py
def accessible_feed_ids(user_id: int) -> Select:      # owner OR member
def owned_feed_ids(user_id: int) -> Select:           # owner only
```

- [x] Rewrite `admin/auth.py` around `Identity`, via a new
      `gtfs_zone_rt_api/accounts.py::resolve_login`; keeps the `claims["sub"] ==
      subject` guard and now reads `email_verified`.
- [x] Store `request.session["user_id"]`; `subject` kept for display only.
- [x] `current_subject_var` → `current_user_id_var`.
- [x] Add `gtfs_zone_rt_api/admin/access.py`.
- [x] Convert all five ModelViews (18 scoping queries) and the four
      `entity_router.py` helpers.
- [x] Update `_macros.html`: the `subject` fallback is now a UUID, not a name,
      so it was dropped rather than displayed.
- [x] `oidc_provider` default `dex` → `keycloak`.
- [x] PermissionError → 403 via a handler on SQLAdmin's sub-app (a handler on
      the parent FastAPI app would not catch it, since the mount has its own
      exception middleware).

**Discoveries**

- Verified against the dev database with simulated proxy headers: two logins
  create two users with correct identities and `email_verified` from the token;
  alice's feed is invisible in bob's list; bob gets 404 on
  `/feed/details/{id}` and `/feed/edit/{id}`, and 403 on delete and reload,
  with the row still present afterwards.

**Gotchas**

- `scaffold_form` in four views reads the ContextVar because `request` is not
  available there (`views.py:246,418,545,667`). It must keep working; the
  middleware sets the var per-request, so just change the type.
- Session values survive a deploy. A stale session carrying only `subject` must
  fail closed: treat a missing `user_id` as unauthenticated and re-run
  `authenticate`, don't fall back to the old lookup.
- `PermissionError` from `_get_owned_*` currently surfaces as a 500. Since
  members will now hit these paths legitimately, convert to a 403/redirect with
  a flash message while touching this code.

## Phase 4: Feed membership (owner + members)

```
FeedMember  id, feed_id FK, user_id FK, added_by_user_id FK, created_at
            UNIQUE(feed_id, user_id)
```

**Why an explicit row and not a `link_model` M2M.** SQLModel/SQLAdmin M2M pain
is real and this is where it bites: a `link_model` relationship renders in
SQLAdmin as an unscoped multi-select of *every user in the database* (an
enumeration leak), gives no place for `added_by`/`created_at`, and async
lazy-loads of the collection blow up with `MissingGreenlet` in templates. So:
a first-class table with its own PK, never exposed as a SQLAdmin `ModelView`
with a relationship widget, always mutated through explicit routes (Phase 5)
that check permission server-side.

Ownership stays as `Feed.owner_id`. Owner is *not* also a `FeedMember` row;
one representation per fact, so "is owner" is never ambiguous.

- [x] Add `models/feed_member.py` + `Feed.members` / `User.memberships`.
- [x] Alembic migration `b5c6d7e8f9a0`, round-tripped against the dev database.
- [x] `accessible_feed_ids` = owned OR member.
- [x] Owner-only `delete_model`; `update_model` allows members but pops
      `owner_id` from the submitted data.
- [x] `insert_model` sets `owner_id` to the creator (and no longer re-queries
      the user, since the session already carries the id).
- [x] "owner" / "shared with me" column on the feed list.
- [ ] Member add/remove routes → owner only *(Phase 5, where they are written)*.

**Discoveries: two bugs the new relationship exposed**

- **`DetachedInstanceError` on the edit form.** WTForms' `process()` calls
  `hasattr(obj, name)` across every attribute, which lazy-loads `Feed.members`
  on a detached instance. Fixed by excluding `members` from the form, the same
  reason `trackers` and `alerts` were already excluded. This is the M2M-adjacent
  trap the plan predicted, and it fired immediately.
- **The current-user ContextVar lagged one request behind.** `SubjectMiddleware`
  set it from the session, but middleware runs *before* `authenticate`. On the
  first request of a session it was 0; on a browser that switched users it still
  held the previous user, and `scaffold_form` builds its feed dropdown from it,
  so user B's first request would have listed user A's feed names. Now set in
  `authenticate` once identity is known, with the middleware left as the
  fallback for the non-SQLAdmin `entity_router` routes. This predates the
  current work; the sharing badge is simply what made it visible.
- Verified: a member sees the feed, opens details and edit, and an edit POST
  carrying `owner_id` changes the URL but leaves ownership intact; delete is
  403. A member's tracker-create dropdown lists the shared feed; an unrelated
  user's lists nothing.

**Gotchas**

- Trackers, tracker rules, service alerts and informed entities all scope
  *through* the feed, so they inherit membership for free once
  `accessible_feed_ids` is in place; verify each of the five views, including
  `scaffold_form`'s feed dropdown, so a member can actually attach a tracker to
  a shared feed.
- **Members can see tracker `id`s, which are the secret Traccar credentials.**
  That follows from "equivalent access" and is the intended semantics, but it
  means adding a member is a credential-sharing act. Say so in the confirm UI.
- Deleting a user must not orphan feeds. Decide now: block deletion while the
  user owns feeds. (There is no user-delete path today; add the guard when one
  appears.)
- Never allow a `FeedMember` row where `user_id == feed.owner_id`.

## Phase 5: Sharing UI and invites

```
FeedInvite  id, feed_id FK, email (stored lowercased), invited_by_user_id FK,
            token, created_at, claimed_at, claimed_user_id
            UNIQUE(feed_id, email) WHERE claimed_at IS NULL
```

A "Members" panel on the feed detail page: current owner, member list with
remove buttons, an add-by-email form, and pending invites. Adding by an email
that matches an existing user's verified identity creates a `FeedMember`
immediately; otherwise it creates a `FeedInvite`. Invites are claimed at login
by matching a *verified* email.

- [x] `models/feed_invite.py` + migration `c6d7e8f9a0b1`.
- [x] Extend `admin/entity_router.py` with owner-only routes:
      `POST /feeds/{id}/members`, `DELETE /feeds/{id}/members/{user_id}`,
      `DELETE /feeds/{id}/invites/{invite_id}`, `POST /feeds/{id}/transfer`.
- [x] Members panel partial (`_members_partial.html`), included from
      `templates/sqladmin/feed_detail.html`.
- [x] `claim_invites(user)` called from `authenticate` after identity
      resolution; converts matching unclaimed invites into `FeedMember` rows in
      one transaction.
- [x] Ownership transfer: owner picks an existing member; old owner becomes a
      `FeedMember`; single transaction.

**Discoveries**

- Three real bugs surfaced and were fixed: `Feed.members` broke the edit form
  with `DetachedInstanceError` (the M2M trap Phase 4 predicted); the
  current-user ContextVar was set by middleware *before* `authenticate`, so it
  lagged a request behind and would have shown a switched-over browser the
  previous user's feed names; and `entity_router` sits outside SQLAdmin, so
  nothing ran `authenticate` for it and it trusted the session cookie outright,
  and a cookie belonging to a different user than the proxy headers said would
  have been authorised as the cookie's owner. The header is now the authority.
- **Deviation:** the plan says the add-by-email form must not reveal whether an
  address belongs to a registered user. Not implemented, deliberately: the
  panel necessarily shows "member" vs "invited" straight afterwards, so hiding
  it in the immediate response would be theatre. The owner learns only whether
  an address has a gtfs.zone account, and only for addresses they already chose
  to share with.

**Gotchas**

- Only ever match invites on **verified** emails, and match case-insensitively
  on a stored-lowercase column. An unverified-email match is an account
  takeover primitive.
- The add-by-email form must not leak whether an email belongs to a registered
  user; return the same "invited" response either way.
- Load members with `selectinload` in `details_query`; the async session will
  raise on lazy-load inside the template otherwise.
- Route registration order matters: `entity_router` is included **before**
  `Admin` mounts at `/` (`admin_main.py`), or the mount swallows these paths.

## Phase 6: Linked accounts + the email-match prompt

Keycloak owns the actual linking, so rt-api's job is (a) show the user what is
linked, (b) send them to the Keycloak account console to link more, and (c)
handle the case where a *new* identity arrives whose verified email matches an
existing rt-api user.

Case (c) should be rare: Keycloak's first-broker-login flow normally catches it
upstream and links there. But it happens whenever the same person exists in
Keycloak twice (two accounts, different emails at Keycloak but the same email
later verified), so rt-api must not silently create a duplicate principal.
Behavior: create the new `Identity` attached to a **new** user, then show a
one-time interstitial ("an existing account uses this email; link them?"),
which on confirm runs a merge.

The merge is the sharp edge and must be one transaction:
reassign the absorbed user's `Feed.owner_id`, `FeedMember` rows (skipping ones
that would violate the unique constraint or make the owner a member of their own
feed), `FeedInvite.invited_by_user_id`, and `Identity.user_id`; then delete the
absorbed `User`.

- [x] `POST /account/link-confirm` + `/account/link-dismiss` in
      `entity_router.py`; the offer is recomputed live rather than stashed.
- [x] `merge_users(absorbing_id, absorbed_id)` in `gtfs_zone_rt_api/accounts.py`,
      written test-first, with tests for every constraint-collision case.
- [x] `/account` page: identity list (provider, email, verified, linked_at) + a
      "Manage linked accounts" link to
      `{keycloak}/realms/gtfs/account/#/account-security/linked-accounts`.
- [x] `keycloak_account_url` setting in `gtfs_zone_rt_api/settings.py` alongside the
      existing `oauth2_proxy_logout_url`.

**Discoveries**

- **The session cannot carry the suggestion.** `authenticate` calls
  `request.session.clear()` on *every* request, so a stashed suggestion would
  be gone by the time the user could click it. `link_candidates()` recomputes
  it live instead, which is also self-healing: once merged, the offer simply
  stops appearing. Only the *dismissal* is carried across the clear, and only
  for the same user id; a browser that switches users must not inherit the
  previous one's "don't ask me again". No token table was needed.
- The page is a SQLAdmin `BaseView` with `@expose`, not an `entity_router`
  route, so it renders inside the admin chrome, gets a nav entry, and is
  covered by `login_required`. The two mutating actions stayed in
  `entity_router` with the rest of the permission-checked routes.
- `user_with_verified_email` moved from `sharing.py` into `accounts.py` and now
  compares case-insensitively on *both* sides. `Identity.email` is stored
  exactly as the provider sent it (unlike `FeedInvite.email`, which has a
  lowercasing validator), so the old exact comparison could miss a real match.
- Verified against the dev stack end to end: a second verified `alice@local`
  credential raised the offer, confirming it left one user holding both
  identities with feed ownership untouched, a crafted `candidate_user_id` got
  403, and a dismissal survived the session clear.

**Gotchas**

- Never merge on an unverified email, and never merge without an authenticated
  confirmation from a session that holds *one* of the two identities.
- Merge direction: keep the **older** user as the absorber, so the longer-lived
  `user.id` (referenced by feeds) survives.
- After the identity list changes in Keycloak, rt-api's `identity` rows are
  stale until the next login on that provider. Accept that: the app-side table
  is a cache of what has actually been seen, and the page should say so rather
  than claiming to mirror Keycloak.

## Phase 7: Production cutover in gtfs-zone-infra

- [ ] Keycloak Deployment/Service/Ingress under `gtfs/keycloak/`, replacing
      `gtfs/dex/`; CNPG database `keycloak` alongside the existing `dex` one.
- [ ] Realm config: prefer the same import JSON as dev, secrets via SOPS
      (`gtfs-app-secrets`), GitHub + Google + GitLab IdPs carried over.
- [ ] Repoint oauth2-proxy (issuer, client id/secret, `USER_ID_CLAIM: sub`).
- [ ] Repoint **Traccar's** OIDC client at Keycloak; it is a separate Dex
      client today and will break silently otherwise.
- [x] Remap existing prod `identity` rows: for each, find the Keycloak user with
      the same GitHub identity and rewrite `(provider, provider_subject)` to
      `("keycloak", <kc sub>)`. Written as a one-shot script, not a migration,
      since it needs to call the Keycloak admin API:
      `scripts/remap_identities_to_keycloak.py`.
- [ ] Flush Redis DB 0 (oauth2-proxy sessions) at cutover.
- [ ] Keep the Dex manifests in git for one release as a rollback path.
- [ ] Update `rt-api/CLAUDE.md` (the auth section describes Dex throughout)
      and the diagram in `dev-stack`.

**Discoveries**

- **Keycloak 26.4 does not expand `${env.VAR}` during `--import-realm`.**
  Verified empirically against a throwaway container: the literal string
  `${env.PROBE_CLIENT_SECRET}` is what ends up stored as the client secret, and
  the legacy `-Dkeycloak.migration.replace-placeholders=true` system property
  changes nothing. So the prod realm JSON **must** go through an init container
  that `envsubst`s the ConfigMap into an emptyDir before Keycloak reads it. Dev
  sidestepped this by using fake realms with no real secrets, which is why it
  never came up before.
- The remap script's matching is two-tier: the strong match is a Keycloak
  **federated identity** whose upstream user id or username equals the row's
  `provider_subject` (Dex's GitHub connector and Keycloak's GitHub IdP key on
  the same GitHub account), falling back to an unambiguous **verified** email.
  It refuses to write if anything is unmatched, or if two rows would map to one
  Keycloak subject; that second case is two rt-api principals for one human,
  which is a merge decision, not a remap. `--dry-run` is the default.
- Exercised against the dev stack, where it correctly refused: three test users
  exist only in rt-api, and four identities across three users all resolve to
  the same Keycloak subject.

**Gotchas**

- The user count is small enough to remap by hand if the script is fiddly, but
  do it deliberately, because a missed row means someone silently gets a fresh
  empty account while their feeds stay attached to the orphaned user id.
- Prod will hit the collision check if anyone has duplicate principals. Merge
  them via `/account` (or `merge_users` directly) *before* the cutover, not
  after; once after, the second row is unreachable.
- Import GitHub users into Keycloak *before* cutover if possible, so the first
  post-cutover login links rather than creates.
- `DEX_ISSUER` appears in more places than the dex dir; grep the whole repo.

## Phase 8: Tests

Done **before** Phase 6, so `merge_users` could be written test-first rather
than verified by another scratchpad script. 77 tests, `uv run pytest`.

- [x] `merge_users`: collision cases, ownership preservation, absorbed-user
      cleanup.
- [x] `accessible_feed_ids`: owner sees own, member sees shared, stranger sees
      neither, across all five entity types.
- [x] Ownership cannot be transferred by a member via a crafted `owner_id` POST.
- [x] Invite claiming ignores unverified emails and is case-insensitive.
- [x] Auth backend: unknown identity → new user; known identity → existing user;
      stale session without `user_id` → re-authenticates.

**Discoveries**

- **SQLite in memory, via `aiosqlite`**: the models are dialect-agnostic (no
  JSONB, arrays or enums), so `SQLModel.metadata.create_all` reproduces the
  schema and the suite needs no running service. Two things this does not
  cover, and which stay hand-verified against the dev database: the Alembic
  migrations, and Postgres-specific constraint behaviour.
- Two harness traps, both silent if missed: a plain `sqlite://` URL gives every
  connection its *own* empty database, so `StaticPool` is required or the
  tables vanish between statements; and SQLite ignores foreign keys unless
  `PRAGMA foreign_keys=ON`, without which every cascade assertion passes
  vacuously.
- **The suite immediately found a real bug.** Adding `Feed.invites` in Phase 5
  reintroduced the exact trap Phase 4 hit with `Feed.members`. WTForms'
  `process()` calls `hasattr()` across every attribute and lazy-loads it on a
  detached instance. The feed edit form had been raising
  `DetachedInstanceError` ever since. Fixed by excluding `invites` too.
- The crafted-`owner_id` case needed **two** tests. `owner_id` is in
  `form_excluded_columns`, so WTForms never builds the field and the HTTP test
  passes whether or not `update_model` still pops the key; it was verified
  vacuous by deleting the pop and watching the test still pass. A second test
  drives `update_model` directly with `owner_id` in the data; removing the pop
  fails that one, as it should.
- `.venv/bin/*` console scripts carried a stale shebang from the repo's old
  name (`redis-gtfs-rt-api`), so `uv run pytest` was silently executing a
  different interpreter with no `gtfs_zone_db_models` on it. Recreating the venv
  fixed it; worth knowing if it recurs after a rename.

---

## Open items

- Whether to keep a `provider` string on `Identity` now that Keycloak is the only
  issuer. Keeping it (`"keycloak"`, and later possibly `"github"` directly)
  costs nothing and preserves the option of dropping oauth2-proxy someday.
- Whether members should be able to see the feed's Traccar QR codes. Currently
  implied yes by "equivalent access"; revisit if that feels too broad.

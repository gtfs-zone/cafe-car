# Plan: Cross-app links (admin → viz, viz/admin → editor)

## Summary
Add per-feed deep links between the three gtfs.zone frontends, hardcoding prod
hostnames for simplicity:
- **cafe-car admin** → **viz.rt.gtfs.zone** (test-track): feed list + feed detail page
- **cafe-car admin feed page** → **edit.gtfs.zone** (coloring-book)
- **test-track navbar** → **edit.gtfs.zone** (for the currently loaded feed)

No settings/env vars, no music-student or deploy-gtfs-rt changes. `cors=s,r` on viz links.

## Relevant Context
- Public RT endpoints are mounted at the root of `rt.gtfs.zone`:
  - `https://rt.gtfs.zone/<feed_name>/vehicle_positions.pb`
  - `https://rt.gtfs.zone/<feed_name>/trip_updates.pb`
  - `https://rt.gtfs.zone/<feed_name>/service_alerts.pb`
  (see `src/cafe_car/routers/gtfs_rt.py:32,138,232`, mounted at root in `main.py:47`)
- Feeds are keyed by `Feed.feed_name` (unique slug); `Feed.static_feed_url` holds the
  static GTFS zip URL.
- `FeedAdmin` lives in `src/cafe_car/admin/views.py:85`. List view already renders
  virtual columns via `column_formatters` (`views.py:112`). Detail page currently uses
  stock `sqladmin/details.html` (no override).
- Custom templates shadow stock ones from `src/cafe_car/admin/templates/sqladmin/`
  (`base.html` already overridden; `TrackerAdmin` uses `details_template`).

### URL schemes (verified)
- **Viz** (`../test-track/src/modules/feed-url.ts:19`):
  `https://viz.rt.gtfs.zone/#static=<enc>&rt_vp=<enc>&rt_tu=<enc>&rt_al=<enc>&cors=s,r`
- **Editor** (`../coloring-book/src/modules/page-state-integration.ts:75`):
  `https://edit.gtfs.zone/#load=<enc static_url>`

## Phase 1 — cafe-car admin viz + editor links
Add hardcoded prod bases and helpers, a list-view viz column, and a feed detail
template with viz + editor buttons.

- [x] Add `src/cafe_car/admin/links.py` with `VIZ_BASE`, `EDITOR_BASE`,
      `PUBLIC_RT_BASE` constants and `viz_url(feed)` / `editor_url(feed)` helpers
      (using `urllib.parse`).
- [x] List view: add `"viz_link"` to `FeedAdmin.column_list` + `column_labels` +
      `column_formatters` lambda rendering a `Map ↗` link (`target="_blank"`).
- [x] Detail page: set `FeedAdmin.details_template = "sqladmin/feed_detail.html"`;
      create that template extending `sqladmin/details.html` with "Open in viz ↗"
      and "Open in editor ↗" buttons. Helpers registered as Jinja globals
      (`viz_url`, `editor_url`) in `admin_main.py`.
- [x] Editor button disabled when `static_feed_url` is empty.
- [x] Verify: `uv run ruff check src/` passes; URL builders produce the expected
      scheme; admin app constructs cleanly.

Discoveries: SQLAdmin detail template has direct access to `model`; extra helpers
are cleanest exposed via `admin.templates.env.globals`. No tests dir exists in the
repo yet, so verification was manual (URL builder + app construction).

## Phase 2 — test-track editor link
- [ ] `../test-track/src/index.html` navbar-end: add `Edit ↗` anchor
      (`id="edit-feed-btn"`, hidden by default, styled like `reload-feed-btn`).
- [ ] Wire visibility in app-state/shell: when a `static.kind === 'url'` feed loads,
      set `href = https://edit.gtfs.zone/#load=<enc static url>` and unhide (reuse the
      `reload-feed-btn` visibility trigger). Add an `EDITOR_BASE` const.
- [ ] Verify: build test-track, load a feed locally, confirm button appears/opens editor.

Gotchas: file-backed static sources have no URL → keep hidden (same rule as
`isReproducible`).

## Phase 3 — local testing + deploy sanity
- [ ] Local: run cafe-car admin + test-track; confirm links open prod frontends.
- [ ] Note: local feeds won't exist in prod viz (expected trade-off).
- [ ] Deploy: none required (hardcoded prod URLs). Future option: add
      `viz.rt.gtfs.zone` to `cors_allowed_origins` to drop `cors=r`.

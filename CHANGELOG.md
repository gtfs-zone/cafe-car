## v0.2.0 (2026-10-01)

### BREAKING CHANGE

- the module is now gtfs_zone_rt_api

### Feat

- **ingest**: add batch position and trip-update routes
- **trip-updates**: scope the trip_update keyspace by tracker
- **vehicles**: key live positions on the vehicle, not the trip
- **admin**: retire SQLAdmin, now that yard-master is the UI
- **api**: add GET /api/feeds/{id}/schedule.zip
- **alerts**: accept SPECIAL_EVENT as an alert cause
- **uploads**: hosted feeds, the upload endpoints and the public zip route
- **dev**: a seeded dev stack, and provisioning shared with it
- **api**: rule and exception writes for the assignments calendar
- **positions**: the tracker positions endpoint and the position push
- **api**: the per-feed SSE channel
- **api**: the write half of the yard-master API
- **api**: create and reload a feed
- **tracker**: address trackers by surrogate id, credential moves to device_key
- **api**: the authenticated read-only JSON API for yard-master
- **traccar**: put every provisioned device in one group
- **admin**: gtfs-admins members get full owner powers on every feed
- **account**: show linked providers live from Keycloak
- **api**: add JSON output for GTFS-RT feeds alongside protobuf
- **ingest**: add POST /ingest/alerts for producer-published service alerts
- **api**: publish a public feed catalog at GET /feeds
- **scripts**: add the Dex to Keycloak identity remap
- **admin**: add an /account page that can merge two accounts
- **accounts**: surface a link suggestion when a verified email collides
- **admin**: share feeds with members and pending invites
- **admin**: let feed members work on a shared feed
- **admin**: scope access by user id, not by OIDC subject
- **admin**: link feeds to viz and editor frontends
- **gtfs-rt**: carry the vehicle's current stop through ingest
- add device-sim mode to simulate_trip (emulate Traccar Client via :5055)
- add provision_source.py to provision Feed + Tracker (+ Traccar device)
- remove TripAlias admin view and feed remapping
- label feeds by Tracker nickname, keep secret id out of feeds
- resolve route_id to route name in map popups via --gtfs
- disambiguate concurrent trip instances by start_date
- simulate_trip.py POSTs trip-updates to /ingest/trip-update
- rich trip-update ingest + multi-stop serving; drop NanoMQ passwd writer
- add /ingest/position API; port simulate_trip.py to HTTP ingest
- auto-create Traccar device + QR provisioning per driver
- add DriverRule admin view, CORS support, and simulate_trip improvements
- favicon added
- celery limits
- add trip aliases
- use celery for async feed load
- integrate informed entities into service alerts
- add simple service alerts
- better trip sim and fetch updates
- integrate mqtt server and better trip simulation
- support trip updates and simulate
- add get_feeds api
- add user info to navbar
- add healthcheck to rt-api

### Fix

- **ingest**: accept a device_key as tracker_id and log unknown ones
- **provision**: look up owner by Identity, not removed User columns
- **rt**: guarantee VehicleDescriptor.id uniqueness per feed, not per producer
- **accounts**: make an unverified address visible instead of silent
- **admin**: exclude Feed.invites from the feed form
- **gtfs-rt**: give each vehicle its own identity, not the tracker nickname
- **admin**: cache /statics assets and skip DB/session middleware for them
- editable id
- consumer map popup scroll, humanized delays, per-instance markers
- keep stop-time delays, show whole trip in consumer map
- omit TripDescriptor from vehicle positions with no trip_id
- emit org.traccar.client://config deep link for provisioning URL
- write pw file instead of mqtt http auth
- land on feed page
- feed last loaded in browser tz
- rm aliases from feed edit
- support better error handling of feeds
- add static feed model
- specify schedule relationship
- add mqtt acl endpoint
- debug mode for local testing with email and name
- parse email and name claims from jwt
- add field validation for admin

### Refactor

- rename the package to gtfs-zone-rt-api
- remove em-dashes from sqladmin templates
- **admin**: make the edit page the only page for an object
- shift source tree from src/app to src/cafe_car
- rename to cafe-car
- use railroad-club
- remove docker compose

## v0.1.0 (2026-03-03)

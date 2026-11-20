# LTA Political Calendar

An interactive calendar for letstalkalberta.com covering elections, by-elections, council meetings and party events (AGMs, conventions, nomination meetings) for every party, plus:

- **Event details**: candidates, results, the party's governance documents, and the source link with the date we last checked it
- **Google Calendar**: one-click "add" on every event, plus subscription feeds (everything, by level, or by party) that update by themselves
- **Sharing**: Facebook, X, WhatsApp, text, email, copy link, a ready-made invite message, and the phone's own share sheet. Shared links show a proper preview card
- **Ride board**: people offer seats or ask for a ride to each event. Contact details stay private (see below)
- **Watcher with an AI reader**: checks council systems, party sites, Elections Alberta and newspapers. Anything the AI finds waits for a person to approve it
- **Ready for the LTA app**: phones register with the server and get push notifications for events they follow

**LTA Watcher** (Windows app for LTA PCs that do the website checks and reviews): see `desktop/README.md`.

```
desktop/   LTA Watcher Windows app + installer + build script
web/       calendar page (template + built versions)
data/      events.json (published), parties.json, pending.json (review queue)
watcher/   sources.json (sites to watch), watch.py, review.py
server/    server.py (API, feeds, rides, admin), push.py (app notifications)
deploy/    Dockerfile, env.example
build_page.py   bakes the current data into the page
```

## For Juanjo: putting it on NationBuilder

**Step 1 (works today, no server needed).** Create a page at `/political-calendar` (a Basic page, or a custom template with no sidebar). Paste the whole of `web/lta-political-calendar.nationbuilder.html` into the page's source/HTML view. Everything is scoped under `#lta-cal`, so it won't clash with the theme. In this mode the page uses the events built into it, the share buttons and "＋ Google Calendar" work, and the ride board runs in preview mode (nothing is saved).

**Step 2 (after the server is up).** Add this line just above the pasted block (or edit `CONFIG` at the top of the script):

```html
<script>window.LTA_CAL_CONFIG={apiBase:"https://calendar-api.letstalkalberta.com",feedBase:"https://calendar-api.letstalkalberta.com/feeds",pageUrl:"https://www.letstalkalberta.com/political-calendar"};</script>
```

The page then loads live events, saves rides and offers subscription feeds. If the server is ever down, the page falls back to its built-in copy.

NationBuilder notes: some themes strip `<script>` from the page body. If that happens, put the block in a custom page template (`pages_show_basic.html` copy) instead of the content editor. Liquid tags aren't used, so no escaping is needed.

## The server

Python 3.10+, standard library only (push.py also needs `pip install google-auth requests`). Any small Linux host works (a $6–12/month VPS, Render, Fly.io, Railway). Point `calendar-api.letstalkalberta.com` at it with HTTPS.

```bash
docker build -f deploy/Dockerfile -t lta-calendar .
docker run -d --env-file .env -v $PWD/data:/app/data -p 8080:8080 lta-calendar
```

Or without Docker: `python3 server/server.py`, plus cron:

```
15 */6 * * *  cd /srv/lta-calendar && python3 watcher/watch.py --stale-hours 24 && python3 build_page.py   # backup: only sites no PC has checked in a day
*/15 * * * *  cd /srv/lta-calendar && python3 server/push.py
```

Settings are in `deploy/env.example`. Keep `data/` on a persistent disk: it holds the published events, the review queue, and the SQLite database for rides and devices.

## The watcher and the AI reader

`watcher/sources.json` lists every site. Two kinds of reader:

| Reader | Used for | Goes to |
|---|---|---|
| `escribe`, `ics`, `wp_tribe`, `jsonld` | Council meeting systems (Calgary, Edmonton), calendar feeds, WordPress event calendars | Published straight away: the site is the official source |
| `ai_page`, `ai_news` | Party sites, Elections Alberta / Elections Canada pages, newspapers | **Review queue** until someone approves it |

How the AI reader stays honest:
1. It only runs when a page's text has changed since the last check, which keeps cost down.
2. It has to copy the exact sentence that states the date. If that sentence isn't on the page word for word, the item is thrown away.
3. It is told never to guess a date, time, place or candidate. Empty beats wrong.
4. Nothing it finds goes public until a person approves it at `https://calendar-api…/admin/review?key=ADMIN_KEY` (or `python3 watcher/review.py list|approve|reject`).
5. Newspaper items keep only the facts and a link, never article text, and are marked as secondary sources.

To add a site, copy an entry in `sources.json`. A council that uses eSCRIBE needs only its `pub-<city>.escribemeetings.com` address. For anything else, use `ai_page`.

**Not yet confirmed against the live sites** (this was built in a sandbox that couldn't reach them; entries are marked `"status": "needs_check"`): the eSCRIBE meeting feed for Calgary and Edmonton, and whether the UCP site exposes the WordPress events API (if not, it falls back to the AI reader on its own). The first real run's `data/watch_report.json` and the admin page list any source that failed.

## Ride board privacy

- The public board shows only first name, town, departure time and seats.
- A rider's contact goes only to the driver, through the driver's private link, and only after the rider asks for a seat.
- All ride records, contacts included, are deleted one day after the event.
- Rate limits, length limits and a spam trap are built in. LTA does not screen drivers or riders, and the page says so.
- With SMTP set, drivers get an email when someone asks for a seat, and riders get one when they're accepted.

## The phone app (later)

The app needs only these calls:

| Call | Purpose |
|---|---|
| `GET /api/events`, `GET /api/parties` | Calendar data |
| `GET /api/rides?event=…`, `POST /api/rides/offers`, `POST /api/rides/needs`, `POST /api/rides/offers/{id}/requests` | Ride board |
| `POST /api/devices` `{token, platform, follows:{levels, parties, types, regions, events}}` | Register for push (Firebase token) and say what to follow |
| `DELETE /api/devices/{token}` | Stop notifications |

`server/push.py` sends: new events that match what a phone follows, changes to date/time/place, and reminders 24 hours and 1 hour before. Each is sent once. It uses Firebase Cloud Messaging, which also covers iPhones. It has been tested in dry-run mode only, because there's no Firebase project yet.

## Data rules

- Every event carries `source` (name, URL, primary or secondary, date checked).
- All times are Alberta time, stored with an explicit `-06:00` offset (Alberta has been on UTC−6 year-round since 2026). The page displays times in fixed UTC−6 so phones with outdated time-zone data still show the right time.
- Hand-entered events go in `data/events.json`. Run `python3 build_page.py` afterwards to refresh the built-in copy.
- Governance documents in `data/parties.json` link to each party's own published copy. Where none was found, the entry says so.

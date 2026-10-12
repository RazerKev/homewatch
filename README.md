# HomeWatch — cloud layer

The cloud side of a home-monitoring service built on Home Assistant and Zigbee sensors.
Each home runs a small **agent** on its Raspberry Pi that forwards sensor events and sends
a heartbeat. The **cloud** stores the events and runs a **watchdog** that notices when a
home goes quiet — a power cut, an internet outage, a dead hub — and sends an alert.

> **Design rule:** the watchdog never runs on the Pi. A home that has lost power or
> internet can't report its own failure, so something outside the home has to notice the
> silence.

This is **Phase 1**: everything runs locally with Docker Compose. Later phases add tests
and CI (2), AWS deployment (3), fault-injection testing (4), and anomaly detection (5).

```
  HOME  (Raspberry Pi, Home Assistant OS)            CLOUD  (Docker Compose now, AWS in Phase 3)
 +-----------------------------------------+        +---------------------------------------------+
 |  Zigbee sensors --> Home Assistant       |        |   FastAPI  /v1/events  ------> PostgreSQL    |
 |                        |  WebSocket      |  HTTP  |            /v1/heartbeat        homes         |
 |                        v                 | -----> |                                 heartbeats    |
 |  HomeWatch agent (Home Assistant app)    |  API   |                                 events        |
 |    SQLite outbox: nothing lost offline   |  key   |   Watchdog, every 30 s:  -----> alerts        |
 +-----------------------------------------+        |   "which homes went quiet?"  --> ntfy (SNS)  |
                                                     +---------------------------------------------+
```

---

## Repository layout

```
api/                  cloud service — one Docker image, three jobs
  app/main.py           FastAPI endpoints + per-home API key auth
  app/models.py         SQLAlchemy tables (homes, heartbeats, events, alerts)
  app/health.py         pure "is this home stale?" logic (unit-test target)
  app/watchdog.py       offline/degraded detection + alert delivery
  app/alerts.py         alert wording, log + ntfy channels
  app/cli.py            operator CLI: create homes, rotate keys, list status/alerts
  app/logging_setup.py  JSON logs with no personal data
  migrations/           Alembic schema migrations
agent/                the Pi side — also a Home Assistant app folder
  homewatch_agent/      HA WebSocket client, SQLite outbox, uploader, heartbeat
  config.yaml, Dockerfile, DOCS.md   Home Assistant app packaging
tools/fake_ha.py      a fake Home Assistant, for testing without a Pi
docker-compose.yml    Postgres + migrations + API + watchdog
```

---

## Quick start (Windows)

You need Docker Desktop (running — whale icon in the taskbar) and Python 3.11 or newer.
Commands below are for **PowerShell**, the terminal Windows uses.

### New to the terminal?

- A **terminal** is a window where you type commands instead of clicking. The program
  reading what you type is the **shell**: PowerShell on Windows, bash on Linux, macOS and
  your Raspberry Pi.
- Open one **in this folder**: in VS Code, *File > Open Folder* (pick `homewatch`), then
  *Terminal > New Terminal*. Or right-click the `homewatch` folder in File Explorer and
  choose *Open in Terminal*.
- Type (or paste) one line, press **Enter**, wait for it to finish, then the next line.
- Anything after `#` is a note for you — you don't need to type it.
- **Ctrl+C** stops a program that keeps running (like the agent). The **+** in VS Code's
  terminal panel opens another terminal when a step says "Terminal 2".

### 1. Create your settings file

```powershell
copy .env.example .env      # your private copy of the settings template
notepad .env                # or: code .env
```

In `.env`, replace `change-me` after `POSTGRES_PASSWORD=` with a long random password.
To make one: `python -c "import secrets; print(secrets.token_hex(24))"`. Save and close.

`.env.example` is the template that goes on GitHub. `.env` holds your real secrets and
is listed in `.gitignore`, so git never uploads it.

### 2. Start the cloud stack

```powershell
docker compose up -d --build   # build the API image, then start everything in the background
docker compose ps -a           # db and api "healthy", watchdog "Up", migrate "Exited (0)"
```

The first run downloads Python and Postgres images, so give it a few minutes. Then check
it in your browser: http://localhost:8000/healthz should show `{"ok":true,...}`, and
http://localhost:8000/docs has interactive API docs.

### 3. Register a home

```powershell
docker compose run --rm api python -m app.cli create-home home-001
```

This runs the admin tool inside the API container (`--rm` deletes that temporary
container afterwards). It prints an API key starting with `hwk_` **once**. Save it
somewhere safe, like a password manager. Only a hash is stored, so a lost key can't be
recovered; issue a new one with
`docker compose run --rm api python -m app.cli rotate-key home-001`.

Labels are short lowercase codes (`home-001`). Keep names and addresses out of them: the
telemetry database is designed never to know who lives where.

### 4. Try it without the Pi

**Terminal 1** — a fake Home Assistant with a few sensors (plus two phone sensors the
agent must ignore). Leave it running:

```powershell
cd agent                               # move into the agent folder
python -m venv .venv                   # one-time: a private Python for this project
.\.venv\Scripts\Activate.ps1           # use it — the prompt now starts with (.venv)
pip install -r requirements.txt        # one-time: install the agent's library (aiohttp)
python ..\tools\fake_ha.py serve
```

**Terminal 2** — the agent. Leave it running too:

```powershell
cd agent
.\.venv\Scripts\Activate.ps1
copy .env.example .env                 # then open agent\.env, paste your key after HOMEWATCH_API_KEY=
python -m homewatch_agent
```

You should see `home assistant connected`, `uploaded events accepted=6`, and `heartbeat ok`.

**Terminal 3** — make something happen, then look for it in the database:

```powershell
python tools\fake_ha.py set binary_sensor.water_heater_leak on
docker compose exec db psql -U homewatch -c "select entity_id, state, received_at from events order by id desc limit 5;"
docker compose run --rm api python -m app.cli list-homes
```

### Stopping and starting later

```powershell
docker compose stop            # stop the cloud stack (your data is kept)
docker compose up -d           # start it again
docker compose logs -f api     # watch a service's log live; Ctrl+C stops watching, not the service
docker compose down -v         # wipe the local database and start over (deletes all its data)
```

The database lives in a Docker **volume** (`homewatch_pgdata`), which is stored by Docker,
not in this folder, so deleting the folder doesn't delete it. Postgres only reads
`POSTGRES_PASSWORD` the first time it creates that volume: if you change the password
afterwards, either change it back or reset with `docker compose down -v`.

Stop the agent and the fake Home Assistant with **Ctrl+C** in their terminals. Next time,
just `cd agent`, run `.\.venv\Scripts\Activate.ps1`, and start them again — the one-time
lines don't need repeating.

### If something goes wrong

| Message | Fix |
|---|---|
| `service "migrate" didn't complete successfully: exit 1` | Run `docker compose logs migrate` to see why. If it says the database rejected the password, it was created with an earlier password: run `docker compose down -v`, then `docker compose up -d --build`. |
| `running scripts is disabled on this system` (when activating) | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, answer `Y`, try again. |
| `python` opens the Microsoft Store or isn't found | Use `py` instead of `python`. |
| `Cannot connect to the Docker daemon` / `error during connect` | Docker Desktop isn't running. Start it and wait for the whale icon to settle. |
| Port 8000 or 5432 "already in use" | Another program uses it. Stop it, or change the left-hand number in `docker-compose.yml` (e.g. `"8001:8000"`). |

### 5. Install the agent on the Pi

The agent is packaged as a local Home Assistant **app** (Home Assistant's new name for add-ons).

1. From the App store, install **Samba share** (or **Terminal & SSH**) to reach the Pi's `addons` folder.
2. Copy the `agent` folder there as `homewatch_agent`, so `addons/homewatch_agent/config.yaml` exists.
3. **Settings > Apps > App store**, then the three-dot menu > **Check for updates**. A
   **Local apps** section appears with **HomeWatch Agent**. Install it (it builds on the
   Pi; give it a few minutes).
4. **Configuration** tab: `api_url` = `http://<your PC's IP>:8000`, `api_key` = your key. Save.
   (Find the IP by running `ipconfig` in PowerShell: the *IPv4 Address* under your Wi-Fi
   or Ethernet adapter, e.g. `192.168.1.50`.)
5. **Info** tab: turn on **Start on boot** and **Watchdog**, then **Start**. The **Log** tab
   should show `home assistant connected` and `heartbeat ok`.

If the Pi can't reach your PC, allow inbound TCP 8000 in Windows Defender Firewall. To
test, run this **on the Pi** (in Home Assistant's Terminal app, which uses bash):
`curl http://<your PC's IP>:8000/healthz`.

> In Phase 1 your PC *is* the cloud: when it's off, nothing is watching. That's expected
> until Phase 3 moves the stack to AWS. (A spare Pi running Docker also works as an
> always-on stand-in.)

---

## Phase 1 acceptance checklist

Shorten the timings for testing, then run each check:

```powershell
docker compose run --rm api python -m app.cli set-offline-after home-001 10   # alert after 10 s of silence
# in .env set WATCHDOG_INTERVAL_S=1, save, then run:  docker compose up -d watchdog
# in agent\.env set HOMEWATCH_HEARTBEAT_S=2, save, then restart the agent (Ctrl+C, run it again)
```

In the table, `fake_ha.py ...` and `set ...` are short for `python tools\fake_ha.py ...`
in Terminal 3, and `app.cli alerts` for `docker compose run --rm api python -m app.cli alerts`.

| # | Check | How | Expected | Reference run (localhost, fake HA) |
|---|---|---|---|---|
| 1 | Live event | `fake_ha.py set binary_sensor.water_heater_leak on` | row in `events` | 0.2 s |
| 2 | Only allowlisted data leaves | `set sensor.phone_geocoded_location "somewhere"`; `set sensor.water_heater_leak_lqi 90` | no rows for phone or LQI sensors | 0 rows |
| 3 | Attribute-only change ignored | `set sensor.basement_temperature 64.2 --attr friendly_name=X` | no new row | 0 rows |
| 4 | New sensor picked up | `set sensor.attic_temperature 71 --attr device_class=temperature` | row appears | yes |
| 5 | Power cut | stop the agent (close Terminal 2) | one **OFFLINE** alert (`app.cli alerts`) | 10.7 s |
| 6 | Recovery | start the agent again | one **recovered** alert with the gap length | 0.8 s |
| 7 | Restart re-sends nothing new | watch the agent log after restart | `accepted=0 duplicates=N` | `duplicates=7` |
| 8 | Home Assistant down | `fake_ha.py down` | **DEGRADED** alert; heartbeats continue with `ha_ok=false` | 9.6 s |
| 9 | Change during HA outage | while down: `set binary_sensor.front_door on`; then `fake_ha.py up` | door event arrives after reconnect | under 3 s after up |
| 10 | Cloud down | `docker compose stop api`; change 4 sensors; `docker compose start api` | events buffer on the Pi, then drain in order, no duplicates | 4 buffered, drained in 2.4 s |
| 11 | Poison event | insert a malformed row into the agent's `outbox` table | it moves to `dead_letter`; good events keep flowing; heartbeat `dead_letters=1` | yes |
| 12 | Nothing personal in logs | `docker compose logs api watchdog` | no sensor values, entity names, IPs, or keys | clean |

For check 10: while the API is stopped, heartbeats can't arrive either, so the watchdog
also flags the home offline and then recovered — from the cloud's side a dead API and a
dead home look the same. For check 11, run this from the `agent` folder while the agent
is running:

```powershell
python -c "import sqlite3,json; db=sqlite3.connect('homewatch_outbox.db'); db.execute('insert into outbox (event_id, payload) values (?, ?)', ('poison-1', json.dumps({'event_id': 'poison-1', 'entity_id': 'Not Valid', 'state': 'x', 'occurred_at': '2026-01-01T00:00:00Z'}))); db.commit()"
```

With the production defaults (heartbeat 60 s, offline after 300 s, watchdog every 30 s), a
dead home is flagged roughly 4–5½ minutes after it goes quiet. Phase 4 turns these checks
into automated tests with measured thresholds.

---

## API

All `/v1` routes need `Authorization: Bearer <hwk_ key>`.

**`POST /v1/heartbeat`** — proof of life.

```json
{"sent_at": "2026-09-27T17:00:00Z", "agent_version": "0.1.0", "ha_ok": true, "outbox_depth": 0, "dead_letters": 0}
```
→ `200 {"status": "ok", "server_time": "..."}` (the agent uses `server_time` to warn about a wrong Pi clock)

**`POST /v1/events`** — up to 500 state changes per request.

```json
{"events": [{"event_id": "3f1c...", "entity_id": "binary_sensor.water_heater_leak", "state": "on",
             "device_class": "moisture", "unit": null, "occurred_at": "2026-09-27T17:00:00.123456+00:00"}]}
```
→ `200 {"accepted": 1, "duplicates": 0, "rejected": []}`. Invalid events come back in
`rejected` with their index; the rest are stored. Re-sending an event is harmless.

**`GET /healthz`** — `200` when the API and database are up, `503` otherwise. No auth, no data.

---

## Design decisions (and why)

**The watchdog runs off the Pi.** Detection has to live somewhere that survives the
failure it's detecting. Locally it's its own container; in Phase 3 it's a scheduled
Lambda. *Who watches the watchdog?* In Phase 3, a CloudWatch alarm on the Lambda's
errors and missed runs.

**Only the server's clock counts.** A Raspberry Pi has no real-time clock and can boot
with the wrong time. Staleness is computed from `received_at` (database time), never
from the agent's timestamps. An event with a 1970 timestamp is still accepted — a leak
is still a leak.

**Offline vs degraded.** Each heartbeat says whether Home Assistant answered a ping.
*No heartbeats* means power/internet/hub failure; *heartbeats but HA silent* means the
Pi is fine but nothing is being monitored. Different problems, different fixes, different
alerts. Using the same window for both absorbs Home Assistant's own restarts during updates.

**Alerts fire on transitions, safely, at least once.** The watchdog changes a home's
status with a compare-and-set that also checks the exact heartbeat timestamps it based
its decision on — if a heartbeat lands mid-check, the update matches nothing and there's
no false alarm. The alert row is written in the same transaction (a transactional
outbox), then delivered separately with `SELECT ... FOR UPDATE SKIP LOCKED`, so a crash
can't lose an alert and two watchdogs can't double-send one. Verified with four
watchdogs running at once: exactly one alert per transition.

**A fourth table.** The spec named three (homes, heartbeats, events); `alerts` is that
outbox, and doubles as the audit trail of what was sent and when — which matters for a
monitoring business.

**Nothing is lost when the internet drops.** The agent commits every event to a SQLite
outbox before uploading, drains it in order with exponential backoff and jitter, and caps
its size so a weeks-long outage can't fill the disk.

**Idempotency end to end.** An event's ID is a UUIDv5 of *(entity, state, HA's
last_changed)*. The same change seen twice gets the same ID, so the outbox ignores it
locally and Postgres ignores it via `UNIQUE (home_id, event_id)` + `ON CONFLICT DO NOTHING`.
Retries, restarts and reconnect snapshots are all safe to repeat.

**Subscribe first, then snapshot.** On every (re)connect the agent subscribes to state
changes *before* reading all current states, so a change made while it was disconnected
is caught and nothing slips between the two. Duplicates are free, thanks to the above.

**Partial batches and poison isolation.** Each event in a batch is validated on its own
(the way SQS `SendMessageBatch` and Kinesis `PutRecords` work): good ones are stored, bad
ones come back by index and the agent sets them aside in `dead_letter`. If a whole request
is refused, the agent halves the batch until it isolates the culprit. One bad event can
never block a home's pipeline.

**Tolerant reader.** The API ignores unknown fields, because agents in customers' homes
update on their own schedule; a newer agent must not break against an older server.

**API keys.** 192 random bits, shown once, stored as SHA-256 and looked up by hash.
A slow password hash (bcrypt/argon2) protects guessable human passwords; a random key
can't be guessed, so a fast one-way hash is the standard choice for API tokens. The
`hwk_` prefix (the same idea as GitHub's `ghp_` tokens) makes a leaked key easy to spot
and to catch with secret scanning.

**Privacy by design.** Only sensors from the home's own sensor network (ZHA by default)
with an allowlisted device class are forwarded, and only five fields each — no friendly
names, no phone sensors. The database never learns who a home belongs to. Logs carry
counts and opaque IDs only: no bodies, no query strings, no client IPs, no sensor values.
SQLAlchemy's `hide_parameters` keeps row values out of error messages, and 422 responses
don't echo the rejected input.

**Plain, testable pieces.** `health.py` and `agent/homewatch_agent/events.py` are pure
functions — the natural first targets for Phase 2 unit tests. Sync SQLAlchemy in
FastAPI's threadpool keeps the code simple, and it moves to Lambda unchanged.

---

## Secrets and privacy checklist

- `.env` and `agent/.env` are git-ignored; commit only the `.env.example` files.
- API keys: shown once by the CLI, stored hashed, rotatable (`rotate-key`) and revocable (`deactivate`).
- On the Pi, the key lives in the app's configuration (Home Assistant's storage), not in files you edit.
- Postgres is bound to `127.0.0.1` only; the API is the one thing exposed to your network.
- Containers run as a non-root user (the Pi app runs as root inside its container because `/data` is root-owned).
- `.gitattributes` forces LF line endings so Windows checkouts don't break the Pi build.

## Known limits (and which phase fixes them)

- **Plain HTTP on your home network.** Fine for Phase 1 on a LAN; Phase 3 is HTTPS only.
- **No rate limiting or request-size cap** on the local API — API Gateway provides both (Phase 3).
- **No data retention yet.** At a 60 s heartbeat, each home adds ~1,440 heartbeat rows a
  day. Phase 3 archives raw data to S3 and prunes old rows.
- **The cloud doesn't alert on sensor events** (a leak, say). Home Assistant's local
  automations own that fast path; the cloud owns "this home went quiet".
- **No flap damping.** A home on a very flaky connection could alternate offline/recovered.
- **One request per event when things are quiet.** Fine locally; before Phase 3 (where
  requests cost money) the uploader gets a short linger so bursts share one request.
- **Cost heads-up for Phase 3:** RDS is the one piece with an always-on hourly price;
  everything else here is effectively free at this scale. The billing alarm comes first.

## Roadmap

| Phase | What | Skills |
|---|---|---|
| 1 ✅ | Local API, schema, agent, watchdog | FastAPI, PostgreSQL, SQLAlchemy/Alembic, Docker Compose, asyncio |
| 2 | Unit + integration tests, GitHub Actions (test on push, build image) | pytest, CI/CD |
| 3 | AWS: API on Lambda + API Gateway (cheapest at this scale), RDS, EventBridge watchdog, SNS alerts, S3 archive, CloudWatch | AWS Lambda, S3, CloudWatch, IAM |
| 4 | Automated fault injection: time-to-alert under a threshold, malformed and duplicate data | test automation, reliability engineering |
| 5 | Anomaly detection: freezer drift, unusual water use | time-series analysis, ML |

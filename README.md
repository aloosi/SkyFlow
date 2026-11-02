# SkyFlow API

SkyFlow is an event-driven catering broker for round-the-clock flight operations. When a delay, cancellation, or passenger-count change arrives, the API accepts it immediately, recalculates meal requirements in the background, commits the update in one database transaction, and serves a live kitchen queue ordered by the closest departure.

Built for a schedule of 300 or more flights a day. The service does not cook meals. It keeps the inventory numbers the kitchen works from.

## Core features

- **Disruption intake.** `POST /api/v1/events/disruption` validates the payload and returns `202 Accepted` before meal math finishes.
- **Background recalculation.** An asyncio task updates the flight, the passenger manifest, every meal row, and an audit log together. A failure rolls the whole change back and writes a `RECALC_FAILED` record.
- **Meal rules.** One meal per passenger and crew member, split across Standard, Halal, Vegetarian, Vegan, and Special Dietary. A cumulative delay of 60 minutes or more adds a 5% safety buffer. Cancelled flights drop to zero meals.
- **Kitchen queue.** `GET /api/v1/kitchen/queue` ranks upcoming flights by departure time and remaining meals. Priority is CRITICAL within 60 minutes, HIGH within 3 hours, otherwise NORMAL.
- **Operational snapshot.** Seed data loads 300 flights, manifests, inventories, and audit rows. An optional simulator posts a random delay or passenger surge every 15–30 seconds.
- **Integrity checks.** SQLite foreign keys are enforced. Concurrent events for the same flight are serialized so two updates cannot overwrite each other.

## Technical highlights

| Area | Choice |
| --- | --- |
| Language | Python 3.10+ (image uses 3.12) |
| HTTP | FastAPI, served by Uvicorn (one worker) |
| Validation | Pydantic v2 |
| Persistence | SQLAlchemy 2.0 async engine, SQLite via aiosqlite, WAL, foreign keys on |
| Background work | `asyncio` tasks behind `schedule_event` / `execute_event`, so a queue such as Celery can replace the dispatcher later |
| Seed data | pandas and Faker |
| Tests | pytest, pytest-asyncio, httpx ASGI client |
| Runtime | Multi-stage Docker image, non-root user, Compose healthcheck |

Layout:

```text
app/
  main.py            FastAPI app, lifespan, /health
  config.py          Environment settings
  database.py        Async engine, sessions, SQLite pragmas
  models/            flights, manifests, meal inventories, audit logs
  schemas/           Request and response contracts
  api/v1/            flights, disruption events, kitchen queue and logs
  services/          Meal engine and background tasks
  simulator/         HTTP client that posts live disruptions
scripts/seed_db.py   Initial 300-flight load
tests/               API and engine tests, one SQLite file per test
```

Routes:

| Method and path | Behavior |
| --- | --- |
| `GET /health` | Liveness probe |
| `GET /api/v1/flights/` | Paginated list. Filters: `status`, `limit` (1–200), `offset` |
| `GET /api/v1/flights/{flight_id}` | Flight, manifest, and meal inventory |
| `PATCH /api/v1/flights/{flight_id}/status` | Status change and meal recalculation in the same transaction |
| `POST /api/v1/events/disruption` | Accepts an event with `202`. Processing continues in the background |
| `GET /api/v1/kitchen/queue` | Prep queue. Query: `hours_ahead`, `limit` |
| `GET /api/v1/kitchen/logs` | Audit log, newest first. Filters: `flight_id`, `event_type` |

Interactive docs: <http://localhost:8000/docs>

Example disruption body:

```json
{"flight_id": "QR1050", "event_type": "FLIGHT_DELAY", "delay_minutes": 90}
```

An unknown flight returns `404`. A payload that does not match its event type returns `422`.

## Prerequisites

- **Docker path:** Docker Desktop 4.x with Compose v2. On Windows, wait until the engine status says running, then open a new terminal so `docker` is on `PATH`.
- **Local Python path:** Python 3.10 or newer and `pip`. The commands below use a virtual environment.
- Git is required only if you are cloning the repository.

## Local setup

### Docker Compose

From the repository root:

```bash
docker compose up --build
```

Compose builds the image, runs `scripts/seed_db.py` once, and starts the API only after that job exits successfully. Both containers share the `skyflow-data` volume, so the API sees the seeded database.

- API: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>

Live disruptions, in a second terminal:

```bash
docker compose --profile simulator up
```

Stop the stack with `Ctrl+C`, then `docker compose down`. The database volume remains. `docker compose down -v` deletes it. The next `up` seeds again.

A second `up` against an existing volume skips seeding and prints that flights are already present.

### Python virtual environment

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python scripts/seed_db.py
uvicorn app.main:app --reload
```

macOS and Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/seed_db.py
uvicorn app.main:app --reload
```

Open <http://localhost:8000/docs>.

Useful seed flags:

```bash
python scripts/seed_db.py --count 300 --seed 42
python scripts/seed_db.py --force
```

`--force` deletes existing flights, manifests, inventories, and audit rows, then loads a new set in one transaction.

Optional simulator, from a second terminal with the same virtual environment active:

```bash
python -m app.simulator.flight_simulator --base-url http://localhost:8000
```

Shorter demo interval: `--min-interval 1 --max-interval 3`.

Run Uvicorn as a single worker. Meal tasks and per-flight locks live in that process. The Docker image already does this.

## Environment variables

Copy `.env.example` to `.env` for a local run. `app/config.py` loads that file on startup through `python-dotenv`. Variables already set in the process, in Docker, or in CI are kept.

The Docker image sets `DATABASE_URL` itself (`sqlite+aiosqlite:////data/skyflow.db` on the Compose volume). You do not need a `.env` file inside the container. To override the API container, add an `environment` entry on the `api` service.

Restart the process after changing variables. Settings are read once at startup.

| Variable | Default | Used by |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./skyflow.db` | API. Docker overrides this to `/data/skyflow.db` |
| `LONG_DELAY_THRESHOLD_MINUTES` | `60` | API. Cumulative delay that triggers the safety buffer |
| `SAFETY_BUFFER_PCT` | `0.05` | API. Extra meal fraction for a long delay |
| `SHUTDOWN_GRACE_SECONDS` | `10` | API. How long shutdown waits for in-flight recalculations |
| `SKYFLOW_API_URL` | `http://localhost:8000` | Simulator only |
| `SIM_MIN_INTERVAL` | `15` | Simulator only. Minimum seconds between events |
| `SIM_MAX_INTERVAL` | `30` | Simulator only. Maximum seconds between events |

`.env` is gitignored. Commit changes to `.env.example` only.

## Tests

From an activated virtual environment:

```bash
pip install -r requirements.txt
pytest
```

`pytest.ini` sets `asyncio_mode = auto`. No database server and no `.env` file are required. Each test builds its own SQLite file and overrides the FastAPI session dependencies.

What the suite covers:

- Meal split totals, the delay buffer, cancellation, and meal status transitions
- Endpoint status codes, query validation, and unknown-flight `404`s
- `202` returned before a blocked recalculation finishes
- Queue order, audit log order, and foreign-key rejection
- A mid-update failure leaves the flight, manifest, and inventories unchanged

Verbose output:

```bash
pytest -v
```

## Operational limits

- Background tasks are in-process. A hard kill can drop a task that has not committed. Graceful shutdown waits up to `SHUTDOWN_GRACE_SECONDS`.
- The `202` response includes an event id for the caller. That id is not stored. Poll the flight or `GET /api/v1/kitchen/logs` to see the result.
- SQLite is the database shipped here. The engine is created from `DATABASE_URL`, so another SQLAlchemy URL can be supplied, but schema creation is `create_all` on startup rather than a migration tool.
- Meal mix percentages are fixed in configuration (50 / 25 / 12 / 5 / 8). They are not per-passenger special-meal orders.
- `flights.delay_minutes` stores cumulative delay so several delay events share one buffer threshold.

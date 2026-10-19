# AI Client Engagement (ACE)

## Overview

AI-drafted, human-reviewed, personalized client outreach

The first use case is re-engaging dormant clients over email. The system reads client features, drafts a tailored win-back email with an LLM, routes it to a person for review and approval, and only then sends it, recording every step along the way. It is designed so other channels (such as SMS or WhatsApp) and other use cases can be added later without rework.

### Design principles

- No client personal data (names, contact details, identifiers) is ever sent to the LLM. Personal data lives in a separate `pii_vault` table that a restricted DB role owns. The model-facing path runs under a safe role that can read only `llm_client_context`, an allow-listed view of tiers and buckets.
- Every action that writes data is recorded in an audit trail.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) for dependency management
- Docker, to run a local PostgreSQL database with the pgvector extension

## Installation

### Clone the repository

```bash
git clone https://github.com/NestaNdereva23/Ai-Client-Engagement.git
cd Ai-Client-Engagement
```

### Install dependencies

```bash
uv sync    # creates the virtual environment and installs dependencies
```

### Set up environment variables

Copy `.env.example` to `.env` in the project root and fill it in. The variables you need first are:

```bash
APP_ENV=   
APP_NAME=AI Client Engagement
LOG_LEVEL=INFO
ADMIN_USERNAME=   
ADMIN_PASSWORD=
ADMIN_SECRET_KEY=

#Data 
CYTONN_API_BASE_URL=
CYTONN_API_ACTIVE_URL=
CYTONN_API_KEY=

#DB
DATABASE_URL=
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_DB=
POSTGRES_PORT=
DB_POOL_SIZE=
DB_MAX_OVERFLOW=
DB_ASYNC_POOL_SIZE=       # async pool, defaults to DB_POOL_SIZE
DB_ASYNC_MAX_OVERFLOW=    # async overflow, defaults to DB_MAX_OVERFLOW
WORKER_THREADS=
SLOW_REQUEST_MS=10000   #logging only

#Ticketing&CORS
AI_OUTREACH_JWT_SECRET=
CORS_ALLOW_ORIGINS=
CONSOLE_BASE_URL=
CONSOLE_SESSION_SECRET_KEY=
FORWARDED_ALLOW_IPS=

#CTA  Guardrail Check
PUBLIC_CONTACT_NUMBERS=

# ---Live  Delivery mode ---   
DELIVERY_MODE=           #live or test
LIVE_CAMPAIGN_MAX_CLIENTS=
LIVE_SMS_TO_TEST_LIST=
CAMPAIGN_COOLDOWN_DAYS=7

TEST_CLIENT_FIRST_NAME=Test
TEST_SUBJECT_PREFIX=Test:
# Test campaigns enroll at most this many clients.
#TEST_CAMPAIGN_MAX_CLIENTS=5
#TEST_CAMPAIGN_MAX_CLIENTS=10

DISPATCH_DIRECT_SEND=false
DISPATCH_DIRECT_SEND_MAX_BATCH=50  #Max dispatch to ticketing per request

# --- Outgoing mail through Ticketing ---
MAIL_TRANSPORT=
TICKETING_BASE_URL=
TICKETING_TIMEOUT_SECONDS=

# --- Outgoing SMS  ---
SMS_TRANSPORT=

LLM_PROVIDER=
LLM_MODEL=
ANTHROPIC_API_KEY= 
PROMPT_CONFIG_SOURCE=     #db AI Prompts/Instructions for Knowledge base

RAG_ENABLED=   #Pull Cytonn Weekly Rate
RAG_RETRIEVAL_K=
RAG_MIN_SCORE=

```

`.env.example` lists every setting with a short note. Never commit real secrets. `.env` is ignored by git.

### Start the database

```bash
docker compose up -d    # starts Postgres 16 + pgvector (needs POSTGRES_* in .env)
```

### Database setup

```bash
# Apply migrations to the main database
uv run alembic upgrade head

# Apply migrations to the test database (adjust host/port to match .env)
DATABASE_URL=postgresql+psycopg://ace:ace@localhost:5432/ace_test uv run alembic upgrade head
```

The test suite never runs against the database used for real ingested data. `tests/conftest.py` redirects `DATABASE_URL` to a database named `<database>_test` automatically, or to `TEST_DATABASE_URL` if set. A fresh `docker compose up -d` creates both databases on first boot (`docker/init-test-db.sh`). An existing container needs the one-off `CREATE DATABASE <name>_test` (through `psql` or any equivalent) before the second `alembic upgrade head` will connect.

## Running the development server

```bash
uv run fastapi dev src/app/main.py
```

## Running tests and lint

```bash
uv run pytest                  # run all tests
uv run ruff check .            # lint (add --fix to autofix)
uv run ruff format .           # format
```

## Branches

`main` is what production runs. `staging` is where every change is checked first(sometimes :).

1. Open each pull request against `staging`.
2. When staging looks good, open one pull request from `staging` to `main`.
3. Merge `staging` into `main`


## Scheduled jobs (cron)

Three jobs run on the production droplet from `/etc/cron.d`. Each one is a small wrapper in `deploy/cron/` that runs a script inside the already running `ace` container with `docker compose exec -T`.

| Job | Side | Script it runs | When (UTC) | When (Nairobi) | Log |
| --- | --- | --- | --- | --- | --- |
| `ace-inactive-pipeline` | Inactive (dormant) | `scripts/inactive/run_pipeline.py` | 01:00 every day | 04:00 | `/var/log/ace/inactive-pipeline.log` |
| `ace-risk-detect` | Active | `scripts/risk/detect.py` | 03:00 Monday to Friday | 06:00 | `/var/log/ace/risk-detect.log` |
| `ace-batch-ingest` | Campaigns | `scripts/inactive/batch_ingest_tick.py` | 04:30 every day | 07:30 | `/var/log/ace/batch-ingest.log` |

The droplet clock is UTC and its cron ignores a timezone set inside a crontab, so every time in the `.cron` files is in UTC. Kenya has no daylight saving, so Nairobi time is always UTC plus 3.

### What each job does

- **Inactive pipeline.** Pulls the inactive-clients feed, transforms it into the normal tables, and works out each client's indicators. Pass `--fast` to fetch pages at the same time instead of one by one.
- **Active risk detection.** Pulls the active-clients feed, fetches complaints and advisor assignments, transforms, scores and routes clients, and saves the snapshot. It then assigns each client to the active or inactive side, runs the nightly agent, builds the morning digest, and sends the digest emails. A failed run can be picked up again with `--run-id <id>`.
- **Batch ingest.** Collects the finished drafts of any campaign generation batch that is still in flight.

The inactive job runs two hours before risk detection on purpose. The droplet has one CPU, so the two big pulls should not compete for it or for the database pool.

### Overlap and failure

Every wrapper takes a lock with `flock` before it starts. If the last run is still going, the new one skips itself and writes a line to the log. A job that fails exits with a non zero code and writes the code to the log. Logs rotate weekly and keep eight copies. Right after a rotation the `.log` file can be empty, so read `.log.1`.

### Install on the droplet

A `git pull` never adds a cron file. Installing one is a manual step on the host, done once per job. Run this from `/opt/Ai-Outreach/Ai-Client-Engagement`, using the job name from the table (here `inactive-pipeline`):

```bash
mkdir -p /var/log/ace
chmod +x deploy/cron/inactive-pipeline.sh

install -m 644 deploy/cron/ace-inactive-pipeline.cron /etc/cron.d/ace-inactive-pipeline
install -m 644 deploy/cron/ace-inactive-pipeline.logrotate /etc/logrotate.d/ace-inactive-pipeline

systemctl restart cron
```

For the other two, swap in `risk-detect` or `batch-ingest`. Then confirm the file is there with `ls /etc/cron.d/`. Files in `/etc/cron.d` must be owned by root and must not be writable by group or others, or cron ignores them without saying so.

### Settings the jobs need

`.env.prod` must have `CY_API_BASE_URL` and `CY_API_KEY`, or the inactive and active runs exit before they call anything. Risk detection also needs `CY_ACTIVE_CLIENTS_URL` (the older name `CYTONN_API_ACTIVE_URL` also works). The digest emails only reach advisors when the `SMTP_*` settings are filled in.

### Checking and running by hand

```bash
tail -50 /var/log/ace/risk-detect.log            # latest output
grep ace-risk-detect /var/log/syslog | tail      # did cron start it?

/opt/Ai-Outreach/Ai-Client-Engagement/deploy/cron/risk-detect.sh        # run now
/opt/Ai-Outreach/Ai-Client-Engagement/deploy/cron/inactive-pipeline.sh --fast
```

Extra arguments after the wrapper name go straight to the script. More detail is in `docs/scheduled-inactive-pipeline.md` and `docs/README2.md`.


## Tracing (Langfuse) - Not Configured Yet

`docker compose up -d` also starts a self-hosted Langfuse (web on `localhost:3000`, its own worker, Postgres, ClickHouse, Redis and MinIO, fully isolated from the app's own database). It is optional: a generation run behaves identically without it.

See the `LANGFUSE_*` block in `.env.example` for the compose bootstrap variables (all `CHANGEME` defaults, dev only) and for `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`, the three the app itself reads. Until all three are set, every generation runs through a no-op tracer.

### Morning digest emails

Each advisor chosen for the digest gets one email after the nightly run has recomputed the groups. It is the same brief the Daily Brief screen shows.

- Under "Today at a glance" it lists what happened since yesterday: large withdrawals, what is on the calendar today, balances that fell below the capital preservation line, overdue calls, and deposits.
- Then comes "Where to start": up to three groups of clients in order of urgency, each with a count, the money at risk (each balance weighted by its risk score, not the full balance), the most common warning signs, one specific fact such as how many took out more than half of their balance in the last 30 days, and a fixed next step.
- The subject names the large withdrawals or overdue calls first, if there are any, then how many clients are in the groups to review.
- The email carries counts and money only, never a client name.
- It links to the Daily Brief in Ticketing, built from `TICKETING_BASE_URL` as `/dashboard?view=ai-outreach&section=at-risk`. The link is left out when that setting is blank.

The same brief is built by `GET /api/v1/fa-action/{fa_id}/brief`. The client list takes `sort=brief` to list clients in the same order, with `brief_group` to show one group.

#### Who owns each client

Each client belongs to the account manager Cytonn names for them. The active feed sends `fa_name` and `fa_email` with every client, and the nightly run matches `fa_email` to the email of an advisor in User Access (ignoring case).

- A match makes that manager the owner. If Cytonn moves a client to another manager, the next run follows.
- A client with no manager in the feed, or one whose manager is not an active advisor, keeps their current owner or is shared out to the manager with the lightest load.
- The audit entry for each run says how many clients were placed by the feed and how many did not match.


## Which database session to use

There are two ways into the database, and they run side by side.

| Session | Use it for | Endpoint style |
| --- | --- | --- |
| `get_session` (blocking) | Almost everything. Every endpoint in the app works this way today. | Plain `def` with `Depends(get_session)`. FastAPI runs it in a worker thread, so it does not hold up anything else. |
| `get_async_session` (async) | Requests that stay open a long time: a live stream, a long agent run, anything that waits and waits. | `async def` with `Depends(get_async_session)`. It waits without holding a thread at all. |

Never mix them. An `async def` that touches the blocking session stops the event loop for every other request for as long as the database call takes. `tests/test_async_route_discipline.py` walks every route and fails the build if that ever appears.

### Pools and settings

- The two paths have separate pools, sized by `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` and `DB_ASYNC_POOL_SIZE` / `DB_ASYNC_MAX_OVERFLOW` (the async pair defaults to the blocking pair).
- Separate caps mean a burst on one path cannot take every connection and leave the other waiting. Both together have to stay under the Postgres connection limit.
- Both paths ask for `DB_TIMEZONE` as a connection startup option, so a timestamp does not depend on which path wrote it.

### Helpers used on both sides

Where a helper is needed on both sides, add an async twin next to the blocking one rather than rewriting it. `app.services.agent_runs` is the example.

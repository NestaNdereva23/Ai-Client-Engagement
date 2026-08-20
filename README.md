# AI Client Engagement (ACE)

AI-drafted, human-reviewed, personalized client outreach for Cytonn Investments, built by Vunoh Global.

The first use case is re-engaging dormant clients over email. The system reads client data from Cytonn's API, drafts a tailored win-back email with an LLM, routes it to a person for review and approval, and only then sends it, recording every step along the way. It is designed so other channels (such as SMS or WhatsApp) and other use cases can be added later without rework.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/) for dependency management
- Docker, to run a local PostgreSQL database with the pgvector extension

## Getting started

```bash
uv sync                                   # create the virtual environment and install dependencies
docker compose up -d                      # start Postgres 16 + pgvector (needs POSTGRES_* in .env)
uv run alembic upgrade head                # apply migrations to the main database
DATABASE_URL=postgresql+psycopg://ace:ace@localhost:5432/ace_test uv run alembic upgrade head
                                           # apply migrations to the test database (adjust host/port to match .env)
```

The test suite never runs against the database used for real ingested data: `tests/conftest.py` redirects `DATABASE_URL` to a database named `<database>_test` automatically (or to `TEST_DATABASE_URL` if set). A fresh `docker compose up -d` creates both databases on first boot (`docker/init-test-db.sh`); an existing container needs the one-off `CREATE DATABASE <name>_test` above, or an equivalent `psql`/`CREATE DATABASE` command, before the second `alembic upgrade head` will connect.

## Tracing (Langfuse)

`docker compose up -d` also starts a self-hosted Langfuse (web on `localhost:3000`, its own worker, Postgres, ClickHouse, Redis and MinIO, fully isolated from the app's own database). It is optional: a generation run behaves identically without it. See the `LANGFUSE_*` block in `.env.example` for the compose bootstrap variables (all `CHANGEME` defaults, dev-only) and for `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`, the three the app itself reads. Until all three are set, every generation runs through a no-op tracer.

## Outgoing mail

Everything that sends mail goes through `app.delivery.mailer.get_mailer()`, which reads the `SMTP_*` and `EMAIL_SENDER` settings and returns either an SMTP sender or a recording no-op. An unset `SMTP_HOST` (or a blank `EMAIL_SENDER`) selects the no-op: it records what it was asked to send and sends nothing, so a half-configured environment goes quiet instead of failing mid-run.

`docker compose up -d` also starts Mailpit, a local mail server that accepts everything and delivers nothing. Point the app at it with `SMTP_HOST=localhost`, `SMTP_PORT=1025` and any `EMAIL_SENDER`; no username, password or TLS is needed. Development mail lands in the web inbox at `http://localhost:8025`. No test ever opens a socket: the suite runs green with no mail server present.

### Morning digest emails

Each account manager on `ACE_FA_ROSTER` gets one email at the end of the nightly risk run: how many clients are at risk, what is at stake, and what is driving it, plus a link to the full call list in the console. The email carries counts and money only, never a client name, so it sends the moment the risk run commits rather than waiting on the AI narration warm-up. `CONSOLE_BASE_URL` is where the link points; leave it blank and the email renders without one. Summary counts come from the run's whole snapshot population, not the capped digest lines, so the email never understates the morning.

Each advisor's `daily_capacity` (the fourth field in `ACE_FA_ROSTER`) is the real cap on their call queue. A client over that cap is first offered to another advisor with room for the night; if nobody has room, that client's line moves to the watchlist instead of piling onto an advisor's already-full list. Ownership never changes either way -- only the loan, or the move to the watchlist, is for that one night.

A marker table records each advisor's send for each digest run, so a re-run or a retry cannot mail the same person twice. One advisor's send failing leaves the others sent, and audits.

Send or preview them by hand, without re-running the nightly job:

```bash
uv run python scripts/digest/send_email.py --digest-run-id 42
uv run python scripts/digest/send_email.py --digest-run-id 42 --fa-id 3 --dry-run
```

### Dormant-client outreach send

`POST /campaigns/{campaign_id}/send` sends every approved, not-yet-sent touch in a campaign through the same `get_mailer()` as the digest email above: Mailpit in development, the recording no-op wherever SMTP is not configured. The recipient is the client's `contact_email` on file in `pii_vault`; a message with no personalized content, or a client with no contact_email, blocks that one touch rather than sending it. Real client sends still need a verified sender domain and provider credentials, and Cytonn contact data for the whole dormant book -- both outside this repo's control.

## Phase 2: Account Manager Intelligence

Phase 2 adds risk scoring for the active client book, on top of the same ingestion and PII boundary as Phase 1. Three new packages, scaffolded and empty until their milestones land:

- `risk/` — the six dormancy risk signals, score composition, and routing into queues.
- `digest/` — assembles the morning digest from the latest risk snapshot.
- `briefing/` — a deterministic, on-demand briefing for one client, plus an optional model-narrated version (off by default, `AI_BRIEFING_ENABLED`) that falls back to the deterministic text on any doubt. An accepted narration is stored against a hash of the facts it was written from, so it is served instantly next time and dropped the moment those facts change. The nightly run pre-drafts one for the clients the digest surfaced (`BRIEFING_PREWARM_LIMIT`); every other client is narrated on request.

They follow existing conventions: `db/models/` gets one file per new table group (`active_clients.py`, `risk.py`, `fa_assignment.py`, `complaints.py`, `digest.py`), and `api/routers/` plus `schemas/` get one file per new domain (`risk.py`, `digest.py`, `briefing.py`), mounted the same way the Phase 1 routers are.

## Design principles

- No client personal data (names, contact details, identifiers) is ever sent to the LLM. Personal data lives in a separate `pii_vault` table that a restricted DB role owns; the model-facing path runs under a safe role that can read only `llm_client_context`, an allow-listed view of tiers and buckets.
- No message is delivered without human review and approval.
- Every action that writes data is recorded in an audit trail.

# AI Client Engagement (ACE)

AI-drafted, human-reviewed, personalized client outreach for Cytonn Investments, built by Vunoh Global.

The first use case is re-engaging dormant clients over email. The system reads client data from Cytonn's API, drafts a tailored win-back email with an LLM, routes it to a person for review and approval, and only then sends it, recording every step along the way. It is designed so other channels (such as SMS or WhatsApp) and other use cases can be added later without rework.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/) for dependency management
- Docker, to run a local PostgreSQL database with the pgvector extension

## Getting started

```bash
uv sync                 # create the virtual environment and install dependencies
```

## Design principles

- No client personal data (names, contact details, identifiers) is ever sent to the LLM. Personal data lives in a separate `pii_vault` table that a restricted DB role owns; the model-facing path runs under a safe role that can read only `llm_client_context`, an allow-listed view of tiers and buckets.
- No message is delivered without human review and approval.
- Every action that writes data is recorded in an audit trail.

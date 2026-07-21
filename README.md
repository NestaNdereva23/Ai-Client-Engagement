# AI Client Engagement (ACE)

AI-drafted, human-reviewed, personalized client outreach for Cytonn Investments, built by Vunoh Global.

The first use case is re-engaging dormant clients over email. The system reads client data from Cytonn's API, drafts a tailored win-back email with an LLM, routes it to a person for review and approval, and only then sends it, recording every step along the way. It is designed so other channels (such as SMS or WhatsApp) and other use cases can be added later without rework.

## Status

Early setup. Project metadata, the pinned dependency lock, and the environment template are in place and the environment builds. Application code is added next.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/) for dependency management
- Docker, to run a local PostgreSQL database with the pgvector extension

## Getting started

```bash
uv sync                 # create the virtual environment and install dependencies
cp .env.example .env    # then fill in local values
```

## Design principles

- No client personal data (names, contact details, identifiers) is ever sent to the LLM.
- No message is delivered without human review and approval.
- Every action that writes data is recorded in an audit trail.

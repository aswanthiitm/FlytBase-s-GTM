# FlytBase GTM Intelligence System

An agentic system over the Book of Business. This repo currently contains **L1 —
ingest, reconciliation, and the change feed**: the substrate every reasoning
engine sits on.

## The design decision that shapes everything

Documents are stored as **versioned, hash-addressed records**, and evidence is
stored as **claims that point at exactly one source document and one verbatim
quote**.

That has three consequences the rest of the system depends on:

1. **Re-running is cheap.** An unchanged document is skipped by hash, so a poll
   over an unchanged portfolio costs nothing and touches nothing.
2. **A new document re-reasons one account, not fourteen.** The delta names the
   accounts that actually moved.
3. **A deleted document withdraws its conclusions.** Claims sourced from a
   removed document are *retracted*, not deleted — the account gets re-scored
   without that evidence, and the retraction is itself a visible event.

## Layers

| Layer | Status | What it does |
|---|---|---|
| L1 ingest | **built** | fetch → hash → diff → tombstone → change feed. No LLM. |
| L2 extract | scaffolded | document → `Claim[]`, quote-containment enforced at write time |
| L3 synthesize | scaffolded | claims + deterministic metrics → account dossier |
| L4 portfolio | scaffolded | next-best-action queue, renewal forecast, expansion register |
| L5 serve | not started | dashboard reads the store; no LLM at request time |
| L6 change feed | **built** | append-only, user-visible record of what the system noticed |

`claims`, `dossiers`, and `portfolio_snapshots` tables already exist, so the
reasoning engines slot in without a migration.

## Running it

No install step — flat layout, `python -m gtm` from the repo root.

```bash
uv venv --python 3.12 && uv pip install httpx pydantic python-dotenv typer rich pytest
```

Three passes that demonstrate the whole loop:

```bash
uv run python -m gtm ingest --source fixture --fixture-dir fixtures/snapshot_a
```

```bash
uv run python -m gtm ingest --source fixture --fixture-dir fixtures/snapshot_a
```

```bash
uv run python -m gtm ingest --source fixture --fixture-dir fixtures/snapshot_b
```

Pass 2 reports `no changes` — the idempotency gate. Pass 3 simulates the update
batch: one new document, one edited, **one removed**, a new month of usage, and a
lifecycle-stage change.

Then inspect what happened:

```bash
uv run python -m gtm feed --limit 30
```

```bash
uv run python -m gtm status
```

### Against the live API

```bash
uv run python -m gtm probe --url <base-url> --key <api-key>
```

`probe` dumps the real response shapes so the adapter's field mapping is
confirmed rather than guessed. Then set `GTM_SOURCE=flytbase` in `.env` and:

```bash
uv run python -m gtm poll --every 300
```

## Tests

```bash
uv run pytest -q
```

Nine tests, each mapped to a way this could silently fail in production:

- a second identical run is a no-op (otherwise the change feed becomes noise)
- a removed document retracts its claims, and keeps them as retracted rows
- a restored document reinstates claims **only if the text is byte-identical**
- an edited document invalidates claims extracted from the previous revision
- **a partial upstream fetch never tombstones anything** — the failure mode where
  a transient 502 reads as "every document was deleted" and wipes the portfolio
- a claim whose quote does not appear in its source document is rejected

## What is deliberately not automated

Deterministic code owns anything a language model would be worse at: usage
slope, days-since-contact, renewal countdown, ARR arithmetic, content hashing,
delta computation. The LLM is confined to judgment — role inference, sentiment,
risk narrative, opportunity classification — and every judgment it emits has to
carry a quote that exists in the source or it does not get written.

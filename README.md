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
| L1.5 metrics | **built** | usage slope, renewal countdown, contact recency, divergences. No LLM. |
| L2 extract | **built** | document → `Claim[]`, hash-cached, quote-containment enforced |
| L3 synthesize | scaffolded | claims + metrics → account dossier |
| L4 portfolio | scaffolded | next-best-action queue, renewal forecast, expansion register |
| L5 serve | not started | dashboard reads the store; no LLM at request time |
| L6 change feed | **built** | append-only, user-visible record of what the system noticed |

`dossiers` and `portfolio_snapshots` tables already exist, so the remaining
reasoning engines slot in without a migration.

## The metrics layer, and why it has no LLM in it

For a drone-operations vendor, **flight hours are the ground truth of account
health; everything else is a lagging indicator.** A customer that stopped flying
has already left — the CSM's green label just hasn't caught up.

`gtm/metrics.py` computes trend slope, renewal countdown, contact recency, and
ARR at risk in plain Python, then hands them to the reasoning layers as *given
facts*. It also runs the divergence checks — the one that matters most compares
the CRM's health label against what the aircraft actually did:

```
Northwind Utilities — CRM health is 'green' but flight hours are declining:
down 33% — 96h in 2026-03, peak 210.5h in 2026-01.
```

Trend classification is normalized against each account's own scale, so a
10 h/month drop is correctly read as noise at 1000h and as a crisis at 30h.

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
uv run python -m gtm metrics
```

```bash
uv run python -m gtm status
```

### Extraction

Needs an Anthropic credential — set `ANTHROPIC_API_KEY` in `.env`, or run
`ant auth login`. Without one, `gtm extract` reports how many documents are
pending and exits; every other command works unaffected.

```bash
uv run python -m gtm extract
```

```bash
uv run python -m gtm claims acct_demo_01
```

Extraction is hash-cached per document, so a second run over an unchanged
portfolio reads nothing and costs nothing. It defaults to `claude-opus-5`;
override with `GTM_MODEL` if you want to trade capability for cost on this
high-volume step.

### Against the live API

The Book of Business is **not a REST API** — it is an MCP server speaking
JSON-RPC 2.0 over HTTP POST at a single endpoint. Two things cost real time to
establish and are worth writing down:

- **The API key already contains the `Bearer ` prefix.** Sending
  `Authorization: Bearer <key>` produces `Bearer Bearer …` and a 401. The
  adapter passes it through as-is unless it looks bare.
- **The server is stateless** — `tools/call` works with no `initialize`
  handshake and returns no session id.

Tools the adapter uses: `list_accounts`, `list_account_documents`,
`get_account_document`, `get_account_usage`.

```bash
uv run python -m gtm setkey --name FLYTBASE_API_KEY
```

```bash
uv run python -m gtm ingest --source flytbase
```

A full portfolio pull is 14 accounts / 87 documents / 70 usage months in about
three seconds. A second run reports `no changes`.

```bash
uv run python -m gtm poll --every 300
```

### Setting keys

Input is hidden, `.env` is gitignored and written mode 600, so a key pasted here
reaches neither the repo nor your shell history.

```bash
uv run python -m gtm setkey
```

```bash
uv run python -m gtm llmcheck
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

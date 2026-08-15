# Deploying to Railway

## Why the first build failed

Railway built commit `b5dfc09a` from `main`, which at that point contained only
PR #1 — no `Dockerfile` and no `railway.toml`. Railway fell back to Nixpacks,
which sees `pyproject.toml` and runs `pip install .`. That fails here on
purpose: this is a flat layout with **no build backend**, chosen so `python -m
gtm` works with no install step.

Two fixes, both now in the repo:

1. `Dockerfile` + `railway.toml` (`builder = "dockerfile"`) so Railway stops
   guessing.
2. `requirements.txt`, so that even if a host does fall back to Nixpacks,
   `pip install -r requirements.txt` succeeds.

**The build will keep failing until PR #2 is merged into `main`.**

## Steps

### 1. Merge PR #2

Railway deploys `main`. The build files live in PR #2.

### 2. Set the service variables

The failed deploy showed **0 Variables** — a poller with no credentials builds
fine and then does nothing, which is the worst outcome here because it still
looks alive.

| Variable | Value |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` — reference the Postgres service |
| `FLYTBASE_BASE_URL` | the MCP endpoint, ending `/api/mcp` |
| `FLYTBASE_API_KEY` | the key **including** its `Bearer ` prefix |
| `GROQ_API_KEY` | `gsk_…` |
| `GTM_SOURCE` | `flytbase` |
| `GTM_MODEL` | `llama-3.3-70b-versatile` |

### 3. Start command

The image already defaults to the poller, so no start command is required. To
set one explicitly:

```
python -m gtm poll --every 300 --source flytbase
```

### 4. Confirm it is actually running

```
poller source=flytbase every=300s store=postgres
13:44:39 quiet
```

`store=postgres` is the line that matters. If it says `store=sqlite`, the
service is writing to a container filesystem that is erased on the next
redeploy — the change feed, and the evidence the system updated itself, go with
it.

## Internal vs public database URL

Railway exposes Postgres twice:

- `DATABASE_URL` — private `postgres.railway.internal` host. Correct and faster
  **inside** Railway; unresolvable anywhere else.
- `DATABASE_PUBLIC_URL` — TCP proxy, reachable from a laptop.

`gtm.config` prefers the internal URL only when `RAILWAY_ENVIRONMENT` is set, so
one `.env` works in both places. To run against the deployed database locally,
copy `DATABASE_PUBLIC_URL` from the Postgres service into `.env`:

```bash
uv run python -m gtm setkey --name DATABASE_PUBLIC_URL
```

That also un-skips the Postgres round-trip test:

```bash
uv run pytest -q
```

## Verifying

```bash
uv run python -m gtm dbcheck
```

```bash
uv run python -m gtm status
```

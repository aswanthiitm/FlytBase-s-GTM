from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from gtm import config
from gtm.changefeed import recent
from gtm.db import connect
from gtm.ingest import ingest
from gtm.sources.fixture import FixtureAdapter

app = typer.Typer(add_completion=False, help="FlytBase GTM intelligence system")

# When stdout is piped (CI, a captured demo, `| less`) Rich falls back to 80
# columns and mangles the wide tables. Use a readable fixed width off-tty.
console = Console(width=None if sys.stdout.isatty() else 150)


def _adapter(source: str | None = None, fixture_dir: Path | None = None):
    src = source or config.SOURCE
    if src == "fixture":
        return FixtureAdapter(fixture_dir or config.FIXTURE_DIR)
    if src == "flytbase":
        from gtm.sources.flytbase import FlytBaseAdapter

        return FlytBaseAdapter(config.FLYTBASE_BASE_URL, config.FLYTBASE_API_KEY,
                               config.HTTP_TIMEOUT)
    raise typer.BadParameter(f"unknown source: {src}")


@app.command()
def init():
    """Create the database and schema."""
    connect(config.DB_TARGET)
    console.print(f"[green]ready[/] {config.DB_PATH}")


ENV_PATH = config.ROOT / ".env"

_KEY_HELP = {
    "GROQ_API_KEY": "Groq API key — https://console.groq.com/keys (starts with gsk_)",
    "FLYTBASE_API_KEY": "FlytBase Book of Business API key",
    "FLYTBASE_BASE_URL": "FlytBase API origin only, e.g. https://example.com (no path)",
}


def _write_env(updates: dict[str, str]) -> None:
    """Upsert keys into .env, preserving everything else and the file's order."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        name = line.split("=", 1)[0].strip().lstrip("#").strip() if "=" in line else ""
        if name in remaining:
            out.append(f"{name}={remaining.pop(name)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    ENV_PATH.write_text("\n".join(out).rstrip() + "\n")
    ENV_PATH.chmod(0o600)


@app.command()
def setkey(
    name: str = typer.Option("GROQ_API_KEY", help="which key to set"),
    value: str = typer.Option(None, help="skip the prompt and set it directly"),
):
    """Paste an API key into .env.

    Input is hidden and never echoed, .env is gitignored, and the file is
    chmod 600 after writing — so a key pasted here does not reach the repo or
    your shell history.
    """
    name = name.upper()
    if name in _KEY_HELP:
        console.print(f"[dim]{_KEY_HELP[name]}[/]")

    secret = value or typer.prompt(f"{name}", hide_input=not name.endswith("URL"))
    secret = secret.strip().strip('"').strip("'")
    if not secret:
        console.print("[red]empty — nothing written[/]")
        raise typer.Exit(1)

    _write_env({name: secret})
    shown = secret if name.endswith("URL") else f"{secret[:6]}…{secret[-4:]}"
    console.print(f"[green]wrote[/] {name}={shown} → {ENV_PATH} (mode 600, gitignored)")

    if name == "GROQ_API_KEY":
        console.print("\nVerify it with: [bold]python -m gtm llmcheck[/]")


@app.command()
def dbcheck():
    """Confirm the store is reachable and report which dialect is in use."""
    from gtm.db import connect, is_postgres_url

    target = config.DB_TARGET
    shown = target
    if is_postgres_url(target):  # never print the password
        import re

        shown = re.sub(r"://([^:]+):[^@]+@", r"://\1:****@", target)
    console.print(f"target: [bold]{shown}[/]")

    try:
        conn = connect(target)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]cannot connect:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc

    console.print(f"dialect: [bold]{conn.dialect}[/]")
    if conn.dialect == "sqlite":
        console.print("[yellow]This is a local file.[/] On a deployed host the "
                      "filesystem is wiped on redeploy, taking the change feed with "
                      "it. Set DATABASE_URL to a postgres:// URL for the live store.")

    counts = conn.execute("""
        SELECT (SELECT COUNT(*) FROM accounts) AS accounts,
               (SELECT COUNT(*) FROM documents WHERE status='active') AS documents,
               (SELECT COUNT(*) FROM change_events) AS events,
               (SELECT COUNT(*) FROM ingest_runs) AS runs
    """).fetchone()
    console.print(f"[green]connected[/] — " + ", ".join(f"{k}={counts[k]}" for k in counts.keys()))


@app.command()
def llmcheck():
    """Confirm the Groq key works, and show which models it can reach."""
    from gtm.llm import DEFAULT_MODEL, LLMUnavailable, credentials_available, list_models

    if not credentials_available():
        console.print("[red]GROQ_API_KEY is not set.[/] Run [bold]python -m gtm setkey[/].")
        raise typer.Exit(1)
    try:
        models = list_models()
    except LLMUnavailable as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Groq rejected the key:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]key works[/] — {len(models)} model(s) reachable\n")
    for m in models:
        marker = "  [green]<- GTM_MODEL[/]" if m == DEFAULT_MODEL else ""
        console.print(f"  {m}{marker}")

    if DEFAULT_MODEL not in models:
        console.print(f"\n[yellow]GTM_MODEL is '{DEFAULT_MODEL}', which is not in that "
                      "list.[/] Set GTM_MODEL in .env to one of the above.")


@app.command("ingest")
def ingest_cmd(
    source: str = typer.Option(None, help="fixture | flytbase"),
    fixture_dir: Path = typer.Option(None, help="override fixture snapshot dir"),
    trigger: str = typer.Option("manual", help="manual | poll | cron"),
):
    """Run one reconciliation pass against the source."""
    conn = connect(config.DB_TARGET)
    delta = ingest(conn, _adapter(source, fixture_dir), trigger=trigger)
    if delta.is_empty:
        console.print("[dim]no changes[/]")
    else:
        console.print(f"[green]delta[/] {delta.summary()}")
        for label, items in (("new", delta.new_docs), ("changed", delta.changed_docs),
                             ("removed", delta.removed_docs), ("restored", delta.restored_docs)):
            if items:
                console.print(f"  {label}: {', '.join(items)}")
    if delta.incomplete_accounts:
        console.print(f"[yellow]partial:[/] {', '.join(delta.incomplete_accounts)} "
                      "(removal detection skipped)")


@app.command()
def poll(
    every: int = typer.Option(None, help="seconds between passes"),
    source: str = typer.Option(None),
    once: bool = typer.Option(False, help="run a single pass and exit"),
):
    """Continuously reconcile. This is the self-update loop.

    Runs server-side, unattended. Every pass writes to ingest_runs whether or
    not anything changed, so 'the poller is alive but the data is quiet' is
    distinguishable from 'the poller died'.
    """
    interval = every or config.POLL_SECONDS
    conn = connect(config.DB_TARGET)
    adapter = _adapter(source)
    console.print(f"[cyan]poller[/] source={adapter.name} every={interval}s db={config.DB_PATH}")
    while True:
        try:
            delta = ingest(conn, adapter, trigger="poll")
            if not delta.is_empty:
                console.print(f"[green]{time.strftime('%H:%M:%S')}[/] {delta.summary()}")
            else:
                console.print(f"[dim]{time.strftime('%H:%M:%S')} quiet[/]")
        except Exception as exc:  # noqa: BLE001 - a poller must never die
            console.print(f"[red]{time.strftime('%H:%M:%S')} poll failed:[/] {exc}")
        if once:
            return
        time.sleep(interval)


@app.command()
def status():
    """Portfolio and pipeline state."""
    conn = connect(config.DB_TARGET)
    run = conn.execute("SELECT * FROM ingest_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if run:
        colour = {"ok": "green", "partial": "yellow", "failed": "red"}.get(run["status"], "white")
        console.print(f"last run #{run['run_id']} [{colour}]{run['status']}[/] "
                      f"{run['finished_at']} trigger={run['trigger']} source={run['source']}")
    counts = conn.execute("""
        SELECT (SELECT COUNT(*) FROM accounts) AS accounts,
               (SELECT COUNT(*) FROM documents WHERE status='active') AS docs_active,
               (SELECT COUNT(*) FROM documents WHERE status='removed') AS docs_removed,
               (SELECT COUNT(*) FROM claims WHERE status='active') AS claims_active,
               (SELECT COUNT(*) FROM claims WHERE status='retracted') AS claims_retracted,
               (SELECT COUNT(*) FROM usage_periods) AS usage_rows,
               (SELECT COUNT(*) FROM change_events) AS events
    """).fetchone()
    t = Table(show_header=False, box=None)
    for k in counts.keys():
        t.add_row(k, str(counts[k]))
    console.print(t)


@app.command()
def feed(limit: int = 25, account: str = typer.Option(None)):
    """The change feed -- what the system noticed, and when."""
    conn = connect(config.DB_TARGET)
    rows = recent(conn, limit=limit, account_id=account)
    if not rows:
        console.print("[dim]no events yet[/]")
        return
    t = Table("when", "account", "event", "summary")
    for r in reversed(rows):
        t.add_row(r["ts"][11:19], r["account_id"] or "-", r["event_type"], r["summary"])
    console.print(t)


@app.command()
def accounts():
    """One line per account with document and usage counts."""
    conn = connect(config.DB_TARGET)
    rows = conn.execute("""
        SELECT a.account_id, a.name, a.stage,
               (SELECT COUNT(*) FROM documents d WHERE d.account_id=a.account_id AND d.status='active') AS docs,
               (SELECT COUNT(*) FROM documents d WHERE d.account_id=a.account_id AND d.status='removed') AS gone,
               (SELECT COUNT(*) FROM usage_periods u WHERE u.account_id=a.account_id) AS months
        FROM accounts a ORDER BY a.stage, a.name
    """).fetchall()
    t = Table("account_id", "name", "stage", "docs", "removed", "usage months")
    for r in rows:
        t.add_row(r["account_id"], r["name"], r["stage"] or "-", str(r["docs"]),
                  str(r["gone"]) if r["gone"] else "", str(r["months"]))
    console.print(t)


@app.command()
def extract(
    account: str = typer.Option(None, help="limit to one account"),
    workers: int = typer.Option(6, help="parallel extraction workers"),
):
    """L2: read pending documents and persist evidence-backed claims.

    Only documents whose content hash has no recorded extraction are read, so
    re-running after a quiet poll costs nothing.
    """
    from gtm.extract import groq_extractor, pending_documents, run_extraction
    from gtm.llm import DEFAULT_MODEL, LLMUnavailable, credentials_available

    conn = connect(config.DB_TARGET)
    pending = pending_documents(conn, {account} if account else None)
    if not pending:
        console.print("[dim]nothing to extract — every active document is already "
                      "read at its current hash[/]")
        return

    if not credentials_available():
        console.print(f"[yellow]{len(pending)} document(s) pending, but GROQ_API_KEY "
                      "is not set.[/]\nPaste your key into [bold].env[/] — run "
                      "[bold]python -m gtm setkey[/] to do it interactively.")
        raise typer.Exit(1)

    try:
        extractor = groq_extractor()
    except LLMUnavailable as exc:
        console.print(f"[red]extraction unavailable:[/] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"extracting {len(pending)} document(s) with {DEFAULT_MODEL}…")
    report = run_extraction(conn, extractor, {account} if account else None,
                            max_workers=workers, model_label=DEFAULT_MODEL)
    console.print(f"[green]{report.summary()}[/]")

    if report.rejections:
        console.print("\n[yellow]rejected claims (evidence not found in source):[/]")
        for claim_type, reason in report.rejections[:10]:
            console.print(f"  {claim_type}: {reason}")
    for doc_id, err in report.errors[:10]:
        console.print(f"[red]failed[/] {doc_id}: {err}")


@app.command()
def claims(account: str, limit: int = 40):
    """Every active claim for an account, with the quote behind it."""
    from gtm.claims import active_claims

    conn = connect(config.DB_TARGET)
    rows = active_claims(conn, account)
    if not rows:
        console.print("[dim]no active claims[/]")
        return
    t = Table("type", "subject", "claim", "evidence", "source")
    for r in rows[:limit]:
        t.add_row(r["claim_type"], r["subject"] or "-", r["value"],
                  f'"{r["verbatim_quote"][:70]}"', f'{r["source_title"]} ({r["doc_date"]})')
    console.print(t)


_ARROW = {"growing": "[green]^[/]", "declining": "[red]v[/]", "stable": "[yellow]-[/]",
          "dormant": "[red]X[/]", "insufficient_data": "[dim]?[/]", "no_data": "[dim].[/]"}


@app.command()
def metrics(account: str = typer.Option(None, help="limit to one account")):
    """Deterministic metrics: usage trend, renewal countdown, divergences.

    No LLM involved. These are the facts the reasoning layers are handed.
    """
    from gtm.metrics import compute, compute_all

    conn = connect(config.DB_TARGET)
    rows = [compute(conn, account)] if account else compute_all(conn)

    t = Table("account", "stage", "ARR", "health", "usage", "trend", "renewal", "last touch",
              "flags")
    for m in rows:
        renewal = f"{m.days_to_renewal}d" if m.days_to_renewal is not None else "-"
        touch = f"{m.days_since_last_touch}d ago" if m.days_since_last_touch is not None else "-"
        flags = ", ".join(
            f"[red]{d.kind}[/]" if d.severity == "high" else d.kind for d in m.divergences
        ) or ""
        t.add_row(m.name, m.stage or "-", f"{m.arr:,.0f}" if m.arr else "-",
                  m.health_label or "-",
                  f"{m.usage.latest_hours:g}h" if m.usage.months_observed else "-",
                  _ARROW.get(m.usage.trend, "?"), renewal, touch, flags)
    console.print(t)

    flagged = [(m, d) for m in rows for d in m.divergences if d.severity == "high"]
    if flagged:
        console.print("\n[bold red]High-severity divergences[/]")
        for m, d in flagged:
            console.print(f"  [bold]{m.name}[/] — {d.detail}")

    at_risk = sum(m.arr_at_risk for m in rows)
    if at_risk:
        console.print(f"\nARR at risk (weighted by severity): [bold]{at_risk:,.0f}[/]")


@app.command()
def doc(doc_id: str):
    """Show a stored document and its revision history."""
    conn = connect(config.DB_TARGET)
    d = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    if not d:
        raise typer.Exit(f"no such document: {doc_id}")
    console.print(f"[bold]{d['title']}[/] ({d['doc_type']}, {d['doc_date']}) "
                  f"status={d['status']} r{d['revision']} account={d['account_id']}")
    console.print(d["body"])
    versions = conn.execute(
        "SELECT revision, captured_at, length(body) AS n FROM document_versions "
        "WHERE doc_id=? ORDER BY revision", (doc_id,)).fetchall()
    if len(versions) > 1:
        console.print("\n[dim]revisions:[/] " +
                      ", ".join(f"r{v['revision']} @ {v['captured_at']} ({v['n']} chars)"
                                for v in versions))


@app.command()
def probe(url: str = typer.Option(None), key: str = typer.Option(None)):
    """Recon: dump the live API's real shape so the adapter maps it correctly.

    Prints raw JSON for the account list, one account's documents, and one
    account's usage -- plus whether a document detail endpoint is needed.
    """
    import httpx

    from gtm.sources.flytbase import ACCOUNTS_PATH

    base = (url or config.FLYTBASE_BASE_URL).rstrip("/")
    api_key = key or config.FLYTBASE_API_KEY
    if not base:
        raise typer.BadParameter("pass --url or set FLYTBASE_BASE_URL")

    masked = f"{api_key[:4]}...{api_key[-4:]} (len {len(api_key)})" if api_key else "(none)"
    console.print(f"base_url = [bold]{base}[/]\nkey      = {masked}\n")

    def show(label: str, r: "httpx.Response") -> None:
        ctype = r.headers.get("content-type", "?")
        colour = "green" if r.status_code == 200 else "red"
        console.print(f"[cyan]{label}[/] -> [{colour}]{r.status_code}[/] ({ctype})")
        body = r.text
        # Always show the body. An error body is the diagnostic -- servers say
        # "missing api key" or "unknown account" in it, and swallowing that is
        # what made the first probe useless.
        snippet = body[:1200].strip()
        if snippet:
            console.print(f"[dim]{snippet}[/]")
            if len(body) > 1200:
                console.print(f"[dim]... {len(body)} bytes total[/]")
        console.print()

    # Step 1 -- is auth the problem? Compare header styles, including none.
    console.print("[bold]— auth style —[/]")
    variants: dict[str, dict[str, str]] = {"no auth": {}}
    if api_key:
        variants |= {
            "Bearer": {"Authorization": f"Bearer {api_key}"},
            "X-API-Key": {"X-API-Key": api_key},
            "both": {"Authorization": f"Bearer {api_key}", "X-API-Key": api_key},
            "?api_key=": {},
        }
    for label, hdrs in variants.items():
        try:
            with httpx.Client(base_url=base, timeout=30, follow_redirects=True) as c:
                params = {"api_key": api_key} if label == "?api_key=" else None
                r = c.get(ACCOUNTS_PATH, headers={"Accept": "application/json", **hdrs},
                          params=params)
            show(f"{ACCOUNTS_PATH} [{label}]", r)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]{label} failed:[/] {type(exc).__name__}: {exc}\n")

    # Step 2 -- path discovery. Root first: a 500 on *every* path usually means
    # the base URL is wrong, not that all the paths are.
    console.print("[bold]— path discovery —[/]")
    headers = {"Accept": "application/json"}
    if api_key:
        headers |= {"Authorization": f"Bearer {api_key}", "X-API-Key": api_key}
    candidates = ["/", "/api", "/api/accounts", "/accounts", "/api/v1/accounts",
                  "/api/book-of-business", "/api/bob", "/api/portfolio",
                  "/api/docs", "/openapi.json", "/api/openapi.json"]
    ok_paths: list[str] = []
    with httpx.Client(base_url=base, headers=headers, timeout=30, follow_redirects=True) as c:
        for path in candidates:
            try:
                r = c.get(path)
                if r.status_code == 200:
                    ok_paths.append(path)
                    show(f"GET {path}", r)
                else:
                    console.print(f"[cyan]GET {path}[/] -> [red]{r.status_code}[/] "
                                  f"[dim]{r.text[:200].strip()}[/]")
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]GET {path} failed:[/] {type(exc).__name__}: {exc}")

    console.print()
    if ok_paths:
        console.print(f"[green]reachable:[/] {', '.join(ok_paths)}")
    else:
        console.print("[yellow]nothing returned 200.[/] Most likely causes, in order: "
                      "the base URL is not the API host (check for a /api prefix already "
                      "baked into it, or a different subdomain); the key belongs in a "
                      "header this probe did not try; or the endpoint needs a trailing "
                      "slash. Send me the Book of Business page URL and I will read the "
                      "documented shape instead of guessing.")


@app.command()
def export(out: Path = typer.Option(Path("data/export.json"))):
    """Dump the whole store as JSON -- feeds the dashboard and makes the state
    inspectable without a SQLite client."""
    conn = connect(config.DB_TARGET)
    payload = {
        table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        for table in ("accounts", "documents", "usage_periods", "claims", "change_events",
                      "ingest_runs")
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    console.print(f"[green]wrote[/] {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    app()

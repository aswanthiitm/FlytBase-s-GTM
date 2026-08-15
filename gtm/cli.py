from __future__ import annotations

import json
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
console = Console()


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
    connect(config.DB_PATH)
    console.print(f"[green]ready[/] {config.DB_PATH}")


@app.command("ingest")
def ingest_cmd(
    source: str = typer.Option(None, help="fixture | flytbase"),
    fixture_dir: Path = typer.Option(None, help="override fixture snapshot dir"),
    trigger: str = typer.Option("manual", help="manual | poll | cron"),
):
    """Run one reconciliation pass against the source."""
    conn = connect(config.DB_PATH)
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
    conn = connect(config.DB_PATH)
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
    conn = connect(config.DB_PATH)
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
    conn = connect(config.DB_PATH)
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
    conn = connect(config.DB_PATH)
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
def doc(doc_id: str):
    """Show a stored document and its revision history."""
    conn = connect(config.DB_PATH)
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

    base = (url or config.FLYTBASE_BASE_URL).rstrip("/")
    api_key = key or config.FLYTBASE_API_KEY
    if not base:
        raise typer.BadParameter("pass --url or set FLYTBASE_BASE_URL")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-Key"] = api_key

    with httpx.Client(base_url=base, headers=headers, timeout=30, follow_redirects=True) as c:
        candidates = ["/api/accounts", "/accounts", "/api/v1/accounts",
                      "/api/book-of-business", "/api"]
        for path in candidates:
            try:
                r = c.get(path)
                console.print(f"[cyan]GET {path}[/] -> {r.status_code} "
                              f"({r.headers.get('content-type', '?')})")
                if r.status_code == 200:
                    body = r.text
                    console.print(body[:2500])
                    if len(body) > 2500:
                        console.print(f"[dim]... {len(body)} bytes total[/]")
                    break
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]GET {path} failed:[/] {exc}")


@app.command()
def export(out: Path = typer.Option(Path("data/export.json"))):
    """Dump the whole store as JSON -- feeds the dashboard and makes the state
    inspectable without a SQLite client."""
    conn = connect(config.DB_PATH)
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

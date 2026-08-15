"""L5 -- serve. Reads the store and renders it. No LLM at request time.

Every number on these pages was computed before the request arrived: metrics by
deterministic code, claims by the extraction layer. A page load is a few SELECTs,
so the dashboard cannot be slow, cannot be non-deterministic, and cannot cost
money to look at.

Stdlib only, deliberately. A dashboard that reads four tables does not need a
web framework, and fewer dependencies means a faster container build and less to
break on a deadline.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from gtm import config
from gtm.changefeed import recent
from gtm.db import connect
from gtm.metrics import compute, compute_all

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font:14px/1.55 ui-sans-serif,-apple-system,'Segoe UI',sans-serif;padding:0 0 60px}
a{color:#58a6ff;text-decoration:none}a:hover{text-decoration:underline}
header{background:#161b22;border-bottom:1px solid #30363d;padding:14px 28px;position:sticky;top:0;z-index:5}
header h1{font-size:15px;font-weight:600;display:inline-block;margin-right:22px;color:#e6edf3}
nav a{margin-right:16px;font-size:13px;color:#8b949e}nav a.on{color:#58a6ff;font-weight:600}
main{max-width:1500px;margin:0 auto;padding:26px 28px}
h2{font-size:16px;margin:26px 0 12px;color:#e6edf3;font-weight:600}
h2:first-child{margin-top:0}
.sub{color:#8b949e;font-size:12.5px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:13px;background:#0d1117}
th{text-align:left;padding:8px 10px;border-bottom:1px solid #30363d;color:#8b949e;font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
td{padding:9px 10px;border-bottom:1px solid #21262d;vertical-align:top}
tr:hover td{background:#161b22}
.num{text-align:right;font-variant-numeric:tabular-nums}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.card{background:#161b22;border:1px solid #30363d;border-radius:7px;padding:14px 16px}
.card .k{color:#8b949e;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
.card .v{font-size:23px;font-weight:600;color:#e6edf3;margin-top:5px;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 8px;border-radius:11px;font-size:11.5px;border:1px solid}
.hi{background:#3d1519;border-color:#8b2a30;color:#ff9492}
.med{background:#3a2a12;border-color:#8a6420;color:#e3b341}
.lo{background:#1c2733;border-color:#30506e;color:#79c0ff}
.ok{background:#12261e;border-color:#2b5f43;color:#7ee787}
.up{color:#7ee787}.down{color:#ff7b72}.flat{color:#8b949e}.dead{color:#ff7b72;font-weight:600}
.quote{border-left:2px solid #30363d;padding:3px 0 3px 11px;color:#8b949e;font-style:italic;margin-top:4px;font-size:12.5px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.muted{color:#8b949e}
.warn{background:#3d1519;border:1px solid #8b2a30;border-radius:7px;padding:12px 15px;margin-bottom:18px;color:#ffb3b0}
.evt{border-bottom:1px solid #21262d;padding:9px 0;display:flex;gap:13px;align-items:baseline}
.evt time{color:#8b949e;font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums}
.tag{font-size:10.5px;padding:1px 6px;border-radius:4px;background:#21262d;color:#8b949e;white-space:nowrap}
.tag.new{background:#12261e;color:#7ee787}.tag.rm{background:#3d1519;color:#ff9492}
.tag.ch{background:#3a2a12;color:#e3b341}
footer{max-width:1500px;margin:30px auto 0;padding:0 28px;color:#8b949e;font-size:12px}
"""


def _fmt_money(v):
    return f"${v:,.0f}" if v else "—"


def _trend_cell(u):
    arrow = {"growing": ("▲", "up"), "declining": ("▼", "down"), "stable": ("—", "flat"),
             "dormant": ("✕", "dead"), "insufficient_data": ("·", "muted"),
             "no_data": ("·", "muted")}
    a, cls = arrow.get(u.trend, ("·", "muted"))
    return f'<span class="{cls}">{a} {escape(u.trend.replace("_", " "))}</span>'


def _sparkline(series, w=132, h=30):
    """Inline SVG. The usage series is the single most important thing on the
    page — a shape communicates a trajectory faster than eight numbers."""
    if len(series) < 2:
        return '<span class="muted">—</span>'
    vals = [p["flight_hours"] for p in series]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = w / (len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{h - 3 - ((v - lo) / span) * (h - 8):.1f}"
                   for i, v in enumerate(vals))
    colour = "#7ee787" if vals[-1] >= vals[0] else "#ff7b72"
    last_x, last_y = pts.split(" ")[-1].split(",")
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="1.6"/>'
            f'<circle cx="{last_x}" cy="{last_y}" r="2.4" fill="{colour}"/></svg>')


def _page(title, body, active=""):
    def nav(href, label):
        cls = ' class="on"' if active == label.lower() else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style></head><body>
<header><h1>FlytBase GTM</h1><nav>{nav('/', 'Portfolio')}{nav('/actions', 'Priority queue')}{nav('/feed', 'Change feed')}</nav></header>
<main>{body}</main>
<footer>Read-only view of the store. Every figure was computed before this request —
no model runs at page load.</footer></body></html>"""


# --------------------------------------------------------------------------


def render_portfolio(conn) -> str:
    rows = compute_all(conn)
    run = conn.execute(
        "SELECT * FROM ingest_runs ORDER BY run_id DESC LIMIT 1").fetchone()

    total_arr = sum(m.arr or 0 for m in rows)
    at_risk = sum(m.arr_at_risk for m in rows)
    flagged = [(m, d) for m in rows for d in m.divergences if d.severity == "high"]
    claims = conn.execute(
        "SELECT COUNT(*) n FROM claims WHERE status='active'").fetchone()["n"]

    cards = f"""<div class="cards">
<div class="card"><div class="k">Accounts</div><div class="v">{len(rows)}</div></div>
<div class="card"><div class="k">Portfolio ARR</div><div class="v">{_fmt_money(total_arr)}</div></div>
<div class="card"><div class="k">ARR at risk</div><div class="v" style="color:#ff7b72">{_fmt_money(at_risk)}</div></div>
<div class="card"><div class="k">High-severity flags</div><div class="v">{len(flagged)}</div></div>
<div class="card"><div class="k">Evidence claims</div><div class="v">{claims}</div></div>
</div>"""

    banner = ""
    if flagged:
        items = "".join(
            f'<div style="margin-top:7px"><a href="/account/{escape(m.account_id)}">'
            f'<b>{escape(m.name)}</b></a> — {escape(d.detail)}</div>'
            for m, d in flagged)
        banner = (f'<div class="warn"><b>{len(flagged)} account(s) where the CRM label '
                  f'disagrees with the aircraft.</b> Usage is the leading indicator; the '
                  f'label lags.{items}</div>')

    body = ['<h2>Portfolio</h2>']
    if run:
        body.append(f'<div class="sub">Last sync #{run["run_id"]} · {escape(str(run["status"]))} '
                    f'· {escape(str(run["finished_at"] or ""))} · trigger '
                    f'{escape(str(run["trigger"]))}</div>')
    body.append(cards)
    body.append(banner)
    body.append("<table><tr><th>Account</th><th>Stage</th><th class='num'>ARR</th>"
                "<th>CRM health</th><th>Flight hours</th><th>Trend</th>"
                "<th class='num'>Last contact</th><th>Flags</th></tr>")

    for m in sorted(rows, key=lambda x: (-x.arr_at_risk, -(x.arr or 0))):
        sev = {"high": "hi", "medium": "med"}
        flags = "".join(
            f'<span class="pill {sev.get(d.severity, "lo")}">'
            f'{escape(d.kind.replace("_", " "))}</span> '
            for d in m.divergences) or '<span class="muted">—</span>'
        hours = (f'{m.usage.latest_hours:g}h <span class="muted">'
                 f'{escape(str(m.usage.latest_period or ""))}</span>'
                 if m.usage.months_observed else '<span class="muted">no usage</span>')
        touch = (f'{m.days_since_last_touch}d' if m.days_since_last_touch is not None
                 else '<span class="muted">—</span>')
        body.append(
            f'<tr><td><a href="/account/{escape(m.account_id)}"><b>{escape(m.name)}</b></a>'
            f'<br><span class="muted mono">{escape(m.account_id)}</span></td>'
            f'<td>{escape(str(m.stage or "—"))}</td>'
            f'<td class="num">{_fmt_money(m.arr)}</td>'
            f'<td>{escape(str(m.health_label or "—"))}</td>'
            f'<td>{hours}<br>{_sparkline(m.usage.series)}</td>'
            f'<td>{_trend_cell(m.usage)}</td>'
            f'<td class="num">{touch}</td><td>{flags}</td></tr>')
    body.append("</table>")
    return _page("Portfolio — FlytBase GTM", "".join(body), "portfolio")


def render_account(conn, account_id: str) -> str:
    try:
        m = compute(conn, account_id)
    except KeyError:
        return _page("Not found", "<h2>No such account</h2>"
                     "<p><a href='/'>Back to portfolio</a></p>")

    body = [f'<div class="sub"><a href="/">← Portfolio</a></div>',
            f'<h2>{escape(m.name)}</h2>',
            f'<div class="sub mono">{escape(m.account_id)} · {escape(str(m.stage or "—"))}'
            f' · owner {escape(str(m.owner or "—"))}</div>']

    body.append(f"""<div class="cards">
<div class="card"><div class="k">ARR</div><div class="v">{_fmt_money(m.arr)}</div></div>
<div class="card"><div class="k">CRM health</div><div class="v" style="font-size:17px">{escape(str(m.health_label or "—"))}</div></div>
<div class="card"><div class="k">Usage trend</div><div class="v" style="font-size:17px">{_trend_cell(m.usage)}</div></div>
<div class="card"><div class="k">Last contact</div><div class="v">{m.days_since_last_touch if m.days_since_last_touch is not None else "—"}<span style="font-size:13px" class="muted"> days</span></div></div>
</div>""")

    if m.divergences:
        body.append("<h2>Divergences</h2><div class='sub'>Where one source disagrees with "
                    "another. These are surfaced, not reconciled.</div>")
        for d in m.divergences:
            cls = {"high": "hi", "medium": "med"}.get(d.severity, "lo")
            body.append(f'<div class="warn" style="background:#161b22;border-color:#30363d;'
                        f'color:#c9d1d9"><span class="pill {cls}">{escape(d.severity)}</span> '
                        f'<b>{escape(d.kind.replace("_", " "))}</b><br>{escape(d.detail)}</div>')

    if m.usage.months_observed:
        body.append("<h2>Flight activity</h2>")
        body.append(f'<div class="sub">{escape(m.usage.headline())}</div>')
        body.append(_sparkline(m.usage.series, w=460, h=64))
        body.append("<table style='margin-top:12px'><tr><th>Month</th>"
                    "<th class='num'>Flight hours</th><th class='num'>Missions</th></tr>")
        for p in m.usage.series:
            body.append(f'<tr><td class="mono">{escape(p["period"])}</td>'
                        f'<td class="num">{p["flight_hours"]:g}</td>'
                        f'<td class="num">{p["missions"]}</td></tr>')
        body.append("</table>")

    rows = conn.execute(
        """SELECT c.*, d.title AS source_title FROM claims c
           JOIN documents d ON d.doc_id = c.source_doc_id
           WHERE c.account_id=? AND c.status='active'
           ORDER BY c.claim_type, c.doc_date DESC""", (account_id,)).fetchall()
    body.append(f"<h2>Evidence — {len(rows)} claim(s)</h2>")
    if rows:
        body.append("<div class='sub'>Every claim carries a quote that was verified to "
                    "exist in its source document before it was stored.</div>")
        body.append("<table><tr><th>Type</th><th>Subject</th><th>Claim</th>"
                    "<th>Evidence</th></tr>")
        for r in rows:
            body.append(
                f'<tr><td><span class="pill lo">{escape(r["claim_type"])}</span></td>'
                f'<td>{escape(str(r["subject"] or "—"))}</td>'
                f'<td>{escape(r["value"])}</td>'
                f'<td><div class="quote">“{escape(r["verbatim_quote"])}”</div>'
                f'<div class="muted mono" style="margin-top:3px">{escape(str(r["source_title"]))}'
                f' · {escape(str(r["doc_date"] or ""))}</div></td></tr>')
        body.append("</table>")
    else:
        body.append("<div class='sub'>No claims extracted yet for this account.</div>")

    docs = conn.execute(
        "SELECT title, doc_type, doc_date, status, revision FROM documents "
        "WHERE account_id=? ORDER BY status, doc_date", (account_id,)).fetchall()
    body.append(f"<h2>Source documents — {len(docs)}</h2><table>"
                "<tr><th>Title</th><th>Type</th><th>Date</th><th>State</th></tr>")
    for d in docs:
        state = ('<span class="pill hi">removed upstream</span>'
                 if d["status"] == "removed"
                 else (f'<span class="pill med">revised r{d["revision"]}</span>'
                       if d["revision"] > 1 else '<span class="muted">—</span>'))
        body.append(f'<tr><td>{escape(str(d["title"]))}</td>'
                    f'<td>{escape(str(d["doc_type"]))}</td>'
                    f'<td class="mono">{escape(str(d["doc_date"] or "—"))}</td>'
                    f'<td>{state}</td></tr>')
    body.append("</table>")

    events = recent(conn, limit=25, account_id=account_id)
    if events:
        body.append("<h2>What changed here</h2>")
        for e in events:
            body.append(f'<div class="evt"><time>{escape(e["ts"][:19].replace("T"," "))}</time>'
                        f'<span class="tag">{escape(e["event_type"])}</span>'
                        f'<span>{escape(e["summary"])}</span></div>')

    return _page(f"{m.name} — FlytBase GTM", "".join(body))



def render_actions(conn) -> str:
    from gtm.portfolio import expansion_register, next_best_actions, renewal_picture

    actions = [a for a in next_best_actions(conn) if a.play != "monitor"]
    pic = renewal_picture(conn)
    reg = expansion_register(conn)

    body = ["<h2>What to do next</h2>",
            "<div class='sub'>Ranked by a weighted score whose components are shown on every "
            "row — a ranking you cannot argue with is one you can only believe. No model runs "
            "in the ranking path.</div>"]

    for i, a in enumerate(actions, 1):
        cls = "hi" if a.score >= 50 else "med" if a.score >= 20 else "lo"
        comps = " · ".join(f"{k.replace('_',' ')} {v:.0f}" for k, v in
                           sorted(a.components.items(), key=lambda kv: -kv[1]))
        reasons = "".join(f"<div class='muted' style='margin-top:2px'>· {escape(r)}</div>"
                          for r in a.reasons)
        ev = "".join(f'<div class="quote">\u201c{escape(e["verbatim_quote"][:200])}\u201d</div>'
                     for e in a.evidence[:2])
        body.append(
            f'<div style="border-bottom:1px solid #21262d;padding:13px 0">'
            f'<span class="pill {cls}">{a.score:.0f}</span> '
            f'<b style="font-size:15px">{i}. <a href="/account/{escape(a.account_id)}">'
            f'{escape(a.name)}</a></b> '
            f'<span class="pill lo">{escape(a.play.replace("_"," "))}</span>'
            + (f' <span class="muted">${a.arr:,.0f}</span>' if a.arr else "")
            + f'<div style="margin-top:5px">{escape(a.headline)}</div>{reasons}{ev}'
            f'<div class="muted mono" style="margin-top:5px;font-size:11.5px">'
            f'score: {escape(comps)}</div></div>')

    body.append("<h2>Renewal &amp; revenue picture</h2>")
    body.append("<table><tr><th>Bucket</th><th class='num'>Accounts</th>"
                "<th class='num'>ARR</th></tr>")
    labels = {"secure": ("secure", "ok"), "watch": ("watch", "lo"),
              "at_risk": ("at risk", "med"), "lost": ("lost", "hi"),
              "pipeline": ("pipeline (no ARR yet)", "lo")}
    for k, (label, cls) in labels.items():
        body.append(f'<tr><td><span class="pill {cls}">{label}</span></td>'
                    f'<td class="num">{len(pic["buckets"][k])}</td>'
                    f'<td class="num">${pic["totals"][k]:,.0f}</td></tr>')
    body.append("</table>")
    body.append(f'<div class="sub" style="margin-top:10px">Live ARR '
                f'<b>${pic["live_arr"]:,.0f}</b> · at-risk share '
                f'<b style="color:#ff7b72">{pic["at_risk_share"]:.0%}</b></div>')
    for c in pic["caveats"]:
        body.append(f'<div class="warn" style="background:#3a2a12;border-color:#8a6420;'
                    f'color:#e3b341">{escape(c)}</div>')

    body.append("<h2>Expansion register</h2>")
    body.append("<div class='sub'>Signals worth acting on, and signals whose account "
                "fundamentals contradict them.</div>")
    if reg["real"]:
        for e in reg["real"]:
            latent = (' <span class="pill lo">latent — growing usage nobody has acted on</span>'
                      if e["latent"] else "")
            body.append(f'<div class="evt"><span class="tag new">real</span>'
                        f'<span><a href="/account/{escape(e["account_id"])}">'
                        f'{escape(e["name"])}</a>{latent}</span></div>')
    else:
        body.append("<div class='sub'>No qualified expansion signals yet.</div>")
    for e in reg["traps"]:
        body.append(f'<div class="evt"><span class="tag rm">trap</span>'
                    f'<span><a href="/account/{escape(e["account_id"])}">'
                    f'{escape(e["name"])}</a> — {escape("; ".join(e["disqualifiers"]))}</span></div>')

    return _page("Priority queue — FlytBase GTM", "".join(body), "priority queue")


def render_feed(conn, limit=180) -> str:
    events = recent(conn, limit=limit)
    runs = conn.execute(
        "SELECT * FROM ingest_runs ORDER BY run_id DESC LIMIT 12").fetchall()

    body = ["<h2>Change feed</h2>",
            "<div class='sub'>Append-only. This is the record of what the system noticed "
            "on its own — nobody re-imported anything to produce these entries.</div>"]

    if runs:
        body.append("<table style='margin-bottom:26px'><tr><th>Run</th><th>Finished</th>"
                    "<th>Trigger</th><th>Status</th><th class='num'>New</th>"
                    "<th class='num'>Changed</th><th class='num'>Removed</th>"
                    "<th class='num'>Accounts</th></tr>")
        for r in runs:
            colour = {"ok": "ok", "partial": "med", "failed": "hi"}.get(r["status"], "lo")
            body.append(f'<tr><td class="mono">#{r["run_id"]}</td>'
                        f'<td class="mono">{escape(str(r["finished_at"] or "running"))[:19].replace("T"," ")}</td>'
                        f'<td>{escape(str(r["trigger"]))}</td>'
                        f'<td><span class="pill {colour}">{escape(str(r["status"]))}</span></td>'
                        f'<td class="num">{r["docs_new"]}</td>'
                        f'<td class="num">{r["docs_changed"]}</td>'
                        f'<td class="num">{r["docs_removed"]}</td>'
                        f'<td class="num">{r["accounts_touched"]}</td></tr>')
        body.append("</table>")

    tagcls = {"doc_new": "new", "account_new": "new", "usage_new": "new",
              "doc_removed": "rm", "claims_retracted": "rm",
              "doc_changed": "ch", "account_changed": "ch", "usage_changed": "ch"}
    for e in events:
        cls = tagcls.get(e["event_type"], "")
        acct = (f'<a href="/account/{escape(e["account_id"])}">{escape(e["account_id"])}</a>'
                if e["account_id"] else '<span class="muted">—</span>')
        body.append(f'<div class="evt"><time>{escape(e["ts"][:19].replace("T"," "))}</time>'
                    f'<span class="tag {cls}">{escape(e["event_type"])}</span>'
                    f'<span class="mono muted" style="min-width:170px">{acct}</span>'
                    f'<span>{escape(e["summary"])}</span></div>')
    if not events:
        body.append("<div class='sub'>No events recorded yet.</div>")
    return _page("Change feed — FlytBase GTM", "".join(body), "change feed")


# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "gtm"

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = unquote(urlparse(self.path).path).rstrip("/") or "/"
        conn = None
        try:
            if path == "/healthz":
                conn = connect(config.DB_TARGET)
                run = conn.execute(
                    "SELECT run_id, finished_at, status FROM ingest_runs "
                    "ORDER BY run_id DESC LIMIT 1").fetchone()
                payload = {"ok": True, "store": conn.dialect,
                           "last_run": dict(run) if run else None}
                return self._send(200, json.dumps(payload).encode(), "application/json")

            conn = connect(config.DB_TARGET)
            if path == "/":
                return self._send(200, render_portfolio(conn).encode())
            if path == "/actions":
                return self._send(200, render_actions(conn).encode())
            if path == "/feed":
                return self._send(200, render_feed(conn).encode())
            if path.startswith("/account/"):
                return self._send(200, render_account(conn, path[len("/account/"):]).encode())
            if path == "/api/portfolio":
                data = [m.to_dict() for m in compute_all(conn)]
                return self._send(200, json.dumps(data, default=str).encode(),
                                  "application/json")
            if path == "/api/actions":
                from gtm.portfolio import next_best_actions
                return self._send(200, json.dumps(
                    [a.to_dict() for a in next_best_actions(conn)], default=str).encode(),
                    "application/json")
            if path == "/api/feed":
                return self._send(200, json.dumps(recent(conn, 200), default=str).encode(),
                                  "application/json")
            self._send(404, b"<h1>404</h1><p><a href='/'>Portfolio</a></p>")
        except Exception as exc:  # noqa: BLE001
            self._send(500, f"<h1>500</h1><pre>{escape(repr(exc))}</pre>".encode())
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def log_message(self, fmt, *args):
        pass  # the poller's output is the interesting log; requests are noise


def background_poller(interval: int, source: str) -> threading.Thread:
    """Optional in-process poller, for hosts where a separate worker is not
    available (or not free). The dedicated `gtm poll` service is preferred when
    you can have one — a crash there is visible, whereas a dead thread inside a
    healthy web process is not."""
    from gtm.ingest import ingest
    from gtm.sources.fixture import FixtureAdapter

    def loop():
        while True:
            try:
                conn = connect(config.DB_TARGET)
                if source == "flytbase":
                    from gtm.sources.flytbase import FlytBaseAdapter
                    adapter = FlytBaseAdapter(config.FLYTBASE_BASE_URL,
                                              config.FLYTBASE_API_KEY, config.HTTP_TIMEOUT)
                else:
                    adapter = FixtureAdapter(config.FIXTURE_DIR)
                delta = ingest(conn, adapter, trigger="poll")
                print(f"[poller] {time.strftime('%H:%M:%S')} "
                      f"{delta.summary() if not delta.is_empty else 'quiet'}", flush=True)
                conn.close()
            except Exception as exc:  # noqa: BLE001
                print(f"[poller] failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name="poller")
    t.start()
    return t


def serve(host="0.0.0.0", port=8000, poll_interval=0, source="flytbase") -> None:
    if poll_interval:
        background_poller(poll_interval, source)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"dashboard on http://{host}:{port}  store={config.DB_TARGET.split('@')[-1]}",
          flush=True)
    httpd.serve_forever()

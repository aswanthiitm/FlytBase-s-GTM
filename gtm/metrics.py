"""Deterministic account metrics. No LLM in this file, on purpose.

Everything here is arithmetic a language model would be *worse* at: trend slope,
day counts, renewal countdowns, revenue at risk. The LLM gets these as given
facts and is asked for judgment on top of them, never to compute them.

The thesis this encodes: for a drone-operations vendor, **flight hours are the
ground truth of account health and everything else is a lagging indicator.** A
customer that stopped flying has already left; the CSM's green label just has
not caught up. `divergences()` is what finds those.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

# --------------------------------------------------------------------------
# Tolerant field access. The CRM export mixes naming conventions and we do not
# control it; candidate lists keep that mess in one place.
# --------------------------------------------------------------------------

ARR_KEYS = ["arr", "annual_recurring_revenue", "annualRecurringRevenue", "acv",
            "contract_value", "contractValue", "value", "mrr_annualized"]
HEALTH_KEYS = ["health", "health_label", "healthLabel", "health_status", "account_health",
               "health_score", "status_label"]
RENEWAL_KEYS = ["renewal_date", "renewalDate", "renewal", "contract_end", "contract_end_date",
                "contractEndDate", "next_renewal", "term_end"]
OWNER_KEYS = ["owner", "csm", "account_owner", "accountOwner", "assigned_to", "am"]

POSITIVE_HEALTH = {"green", "healthy", "good", "strong", "on track", "on-track", "excellent"}
WARNING_HEALTH = {"yellow", "amber", "at risk", "at-risk", "watch", "neutral", "monitor"}
NEGATIVE_HEALTH = {"red", "critical", "churn risk", "churn-risk", "poor", "escalated"}


def _first(d: dict, keys: list[str], default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return default


def parse_date(value: Any) -> date | None:
    """Accept the date formats a messy export actually contains."""
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%b %d, %Y",
                "%d %B %Y", "%Y-%m", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s[: len(fmt) + 6] if "T" in fmt else s, fmt).date()
        except ValueError:
            continue
    try:  # last resort: ISO-ish prefix
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _money(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace(",", "").replace("$", "").replace("USD", "").strip()
    mult = 1.0
    if cleaned and cleaned[-1].lower() == "k":
        mult, cleaned = 1_000.0, cleaned[:-1]
    elif cleaned and cleaned[-1].lower() == "m":
        mult, cleaned = 1_000_000.0, cleaned[:-1]
    try:
        return float(cleaned) * mult
    except ValueError:
        return None


def classify_health_label(label: Any) -> Literal["positive", "warning", "negative", "unknown"]:
    if label in (None, ""):
        return "unknown"
    s = str(label).strip().lower()
    if s in POSITIVE_HEALTH:
        return "positive"
    if s in WARNING_HEALTH:
        return "warning"
    if s in NEGATIVE_HEALTH:
        return "negative"
    # Numeric scores: assume 0-100, higher is better.
    try:
        n = float(s)
    except ValueError:
        return "unknown"
    return "positive" if n >= 70 else "warning" if n >= 40 else "negative"


# --------------------------------------------------------------------------
# Usage trend
# --------------------------------------------------------------------------

TrendLabel = Literal["growing", "stable", "declining", "dormant", "insufficient_data", "no_data"]

# Slope is normalized against the account's own mean, so a 5 h/month drop reads
# differently on a 200-hour base than on a 20-hour one. 5% of mean per month.
_TREND_BAND = 0.05


@dataclass
class UsageTrend:
    months_observed: int = 0
    first_period: str | None = None
    latest_period: str | None = None
    latest_hours: float = 0.0
    latest_missions: int = 0
    peak_hours: float = 0.0
    peak_period: str | None = None
    mean_hours: float = 0.0
    slope_hours_per_month: float = 0.0
    normalized_slope: float = 0.0
    pct_change_recent: float = 0.0  # last half vs prior half
    mom_change_pct: float = 0.0
    consecutive_declines: int = 0
    zero_streak: int = 0
    pct_off_peak: float = 0.0
    trend: TrendLabel = "no_data"
    series: list[dict] = field(default_factory=list)

    def headline(self) -> str:
        if self.trend in ("no_data", "insufficient_data"):
            return "no usage history"
        if self.trend == "dormant":
            return f"dormant — {self.zero_streak} month(s) at zero flight hours"
        direction = {"growing": "up", "declining": "down", "stable": "flat"}[self.trend]
        return (f"{direction} {abs(self.pct_change_recent):.0f}% — "
                f"{self.latest_hours:g}h in {self.latest_period}, "
                f"peak {self.peak_hours:g}h in {self.peak_period}")


def _slope(ys: list[float]) -> float:
    """Least-squares slope over evenly spaced periods."""
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def usage_trend(rows: list[dict]) -> UsageTrend:
    """rows: [{period, flight_hours, missions}], any order."""
    rows = sorted((r for r in rows if r.get("period")), key=lambda r: r["period"])
    if not rows:
        return UsageTrend(trend="no_data")

    hours = [float(r.get("flight_hours") or 0) for r in rows]
    periods = [r["period"] for r in rows]
    missions = [int(r.get("missions") or 0) for r in rows]

    t = UsageTrend(
        months_observed=len(rows),
        first_period=periods[0],
        latest_period=periods[-1],
        latest_hours=hours[-1],
        latest_missions=missions[-1],
        peak_hours=max(hours),
        peak_period=periods[hours.index(max(hours))],
        mean_hours=sum(hours) / len(hours),
        series=[{"period": p, "flight_hours": h, "missions": m}
                for p, h, m in zip(periods, hours, missions)],
    )

    # Trailing zeros -- the strongest churn signal there is.
    for h in reversed(hours):
        if h == 0:
            t.zero_streak += 1
        else:
            break

    for a, b in zip(reversed(hours[:-1]), reversed(hours[1:])):
        if b < a:
            t.consecutive_declines += 1
        else:
            break

    if len(hours) >= 2:
        prev = hours[-2]
        t.mom_change_pct = ((hours[-1] - prev) / prev * 100) if prev else 0.0
    if t.peak_hours:
        t.pct_off_peak = (t.peak_hours - hours[-1]) / t.peak_hours * 100

    if len(hours) < 2:
        t.trend = "insufficient_data"
        return t

    t.slope_hours_per_month = _slope(hours)
    t.normalized_slope = t.slope_hours_per_month / t.mean_hours if t.mean_hours else 0.0

    half = len(hours) // 2
    recent, prior = hours[half:], hours[:half] or hours[:1]
    ra, pa = sum(recent) / len(recent), sum(prior) / len(prior)
    t.pct_change_recent = ((ra - pa) / pa * 100) if pa else 0.0

    if t.zero_streak >= 2 or (t.zero_streak >= 1 and t.months_observed <= 2):
        t.trend = "dormant"
    elif t.normalized_slope < -_TREND_BAND:
        t.trend = "declining"
    elif t.normalized_slope > _TREND_BAND:
        t.trend = "growing"
    else:
        t.trend = "stable"
    return t


# --------------------------------------------------------------------------
# Divergence -- the label says one thing, the aircraft say another
# --------------------------------------------------------------------------


@dataclass
class Divergence:
    kind: str
    severity: Literal["high", "medium", "low"]
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


def divergences(m: "AccountMetrics") -> list[Divergence]:
    out: list[Divergence] = []
    cls = classify_health_label(m.health_label)
    u = m.usage

    if cls == "positive" and u.trend in ("declining", "dormant"):
        sev = "high" if (u.trend == "dormant" or u.pct_off_peak >= 40) else "medium"
        out.append(Divergence(
            kind="health_label_overstates_usage",
            severity=sev,
            detail=(f"CRM health is '{m.health_label}' but flight hours are {u.trend}: "
                    f"{u.headline()}. Usage is the leading indicator; the label is lagging."),
            evidence={"health_label": m.health_label, "trend": u.trend,
                      "normalized_slope": round(u.normalized_slope, 3),
                      "pct_off_peak": round(u.pct_off_peak, 1),
                      "series": u.series[-6:]},
        ))

    if cls in ("negative", "warning") and u.trend == "growing":
        out.append(Divergence(
            kind="health_label_understates_usage",
            severity="low",
            detail=(f"CRM health is '{m.health_label}' but flight hours are growing: "
                    f"{u.headline()}. Possible recovery nobody has re-scored."),
            evidence={"health_label": m.health_label, "trend": u.trend,
                      "pct_change_recent": round(u.pct_change_recent, 1)},
        ))

    # Renewal approaching with no recent human contact.
    if m.days_to_renewal is not None and 0 <= m.days_to_renewal <= 90:
        if m.days_since_last_touch is not None and m.days_since_last_touch >= 30:
            out.append(Divergence(
                kind="renewal_near_with_stale_contact",
                severity="high" if m.days_to_renewal <= 45 else "medium",
                detail=(f"Renewal in {m.days_to_renewal} days but no recorded contact for "
                        f"{m.days_since_last_touch} days."),
                evidence={"days_to_renewal": m.days_to_renewal,
                          "days_since_last_touch": m.days_since_last_touch,
                          "arr": m.arr},
            ))

    # Flying hard but nobody has talked to them -- silent, and usually fine
    # right up until it is not.
    if (u.trend in ("growing", "stable") and m.days_since_last_touch is not None
            and m.days_since_last_touch >= 60):
        out.append(Divergence(
            kind="active_but_unengaged",
            severity="low",
            detail=(f"Account is still flying ({u.headline()}) but has had no recorded "
                    f"contact in {m.days_since_last_touch} days."),
            evidence={"days_since_last_touch": m.days_since_last_touch, "trend": u.trend},
        ))

    return out


# --------------------------------------------------------------------------
# Per-account rollup
# --------------------------------------------------------------------------


@dataclass
class AccountMetrics:
    account_id: str
    name: str
    stage: str | None = None
    arr: float | None = None
    health_label: str | None = None
    owner: str | None = None
    renewal_date: str | None = None
    days_to_renewal: int | None = None
    days_since_last_touch: int | None = None
    last_touch_doc_id: str | None = None
    last_touch_date: str | None = None
    doc_counts: dict[str, int] = field(default_factory=dict)
    active_docs: int = 0
    removed_docs: int = 0
    active_claims: int = 0
    retracted_claims: int = 0
    usage: UsageTrend = field(default_factory=UsageTrend)
    divergences: list[Divergence] = field(default_factory=list)
    arr_at_risk: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["usage_headline"] = self.usage.headline()
        return d


def compute(conn: sqlite3.Connection, account_id: str, today: date | None = None) -> AccountMetrics:
    today = today or date.today()
    row = conn.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown account: {account_id}")
    raw = json.loads(row["raw_json"] or "{}")

    m = AccountMetrics(
        account_id=account_id,
        name=row["name"],
        stage=row["stage"],
        arr=_money(_first(raw, ARR_KEYS)),
        health_label=_first(raw, HEALTH_KEYS),
        owner=_first(raw, OWNER_KEYS),
    )

    rd = parse_date(_first(raw, RENEWAL_KEYS))
    if rd:
        m.renewal_date = rd.isoformat()
        m.days_to_renewal = (rd - today).days

    usage_rows = [dict(r) for r in conn.execute(
        "SELECT period, flight_hours, missions FROM usage_periods WHERE account_id=? "
        "ORDER BY period", (account_id,)).fetchall()]
    m.usage = usage_trend(usage_rows)

    for r in conn.execute(
        "SELECT doc_type, COUNT(*) n FROM documents WHERE account_id=? AND status='active' "
        "GROUP BY doc_type", (account_id,)).fetchall():
        m.doc_counts[r["doc_type"]] = r["n"]
    m.active_docs = sum(m.doc_counts.values())
    m.removed_docs = conn.execute(
        "SELECT COUNT(*) n FROM documents WHERE account_id=? AND status='removed'",
        (account_id,)).fetchone()["n"]

    m.active_claims = conn.execute(
        "SELECT COUNT(*) n FROM claims WHERE account_id=? AND status='active'",
        (account_id,)).fetchone()["n"]
    m.retracted_claims = conn.execute(
        "SELECT COUNT(*) n FROM claims WHERE account_id=? AND status='retracted'",
        (account_id,)).fetchone()["n"]

    # Last human touch. Internal notes are excluded deliberately: a CSM writing
    # a note to themselves is not contact with the customer.
    for d in conn.execute(
        "SELECT doc_id, doc_date FROM documents WHERE account_id=? AND status='active' "
        "AND doc_type IN ('transcript','email') AND doc_date IS NOT NULL "
        "ORDER BY doc_date DESC", (account_id,)).fetchall():
        when = parse_date(d["doc_date"])
        if when:
            m.last_touch_doc_id = d["doc_id"]
            m.last_touch_date = when.isoformat()
            m.days_since_last_touch = (today - when).days
            break

    m.divergences = divergences(m)

    # ARR at risk: only counted where there is a concrete reason, so the number
    # means something when it is summed across the portfolio.
    if m.arr:
        worst = max((d.severity for d in m.divergences),
                    key=lambda s: {"low": 1, "medium": 2, "high": 3}.get(s, 0), default=None)
        weight = {"high": 1.0, "medium": 0.5, "low": 0.15}.get(worst or "", 0.0)
        if m.usage.trend == "dormant":
            weight = 1.0
        m.arr_at_risk = round(m.arr * weight, 2)

    return m


def compute_all(conn: sqlite3.Connection, today: date | None = None) -> list[AccountMetrics]:
    ids = [r["account_id"] for r in conn.execute(
        "SELECT account_id FROM accounts WHERE status='active' ORDER BY account_id").fetchall()]
    return [compute(conn, aid, today=today) for aid in ids]

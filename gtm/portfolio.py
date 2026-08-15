"""L4 -- portfolio-level reasoning. No LLM in the ranking path.

"Ranked by what actually matters" is only meaningful if you can say what
mattered. So the score is a weighted sum of named components, and every action
carries the components that produced it. A reader can disagree with a weight —
which is the point. A model that emits a ranked list with a confident paragraph
cannot be argued with, only believed.

Three outputs:
  next_best_actions  -- what to do, for which account, why, in order
  renewal_picture    -- secure / at risk / lost, and the honest forecast
  expansion_register -- real opportunities, and the ones that are traps
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from gtm.db import Connection
from gtm.metrics import AccountMetrics, classify_health_label, compute_all

Play = Literal[
    "confirm_churn", "save_play", "reengage", "renewal_outreach",
    "win_back", "expansion_conversation", "onboarding_check", "advance_deal", "monitor",
]

# Weights are declared here rather than buried in the scorer so they can be
# argued with. They are judgement calls, not measurements.
WEIGHTS = {
    "arr_at_risk": 40.0,      # money actually exposed, normalised across portfolio
    "usage_collapse": 25.0,   # how far off peak the account has fallen
    "divergence": 20.0,       # CRM says one thing, the aircraft say another
    "silence": 10.0,          # nobody has spoken to them
    "renewal_proximity": 15.0,
    "recoverability": 12.0,   # churned, but the evidence says winnable
}


@dataclass
class Action:
    account_id: str
    name: str
    play: Play
    headline: str
    score: float
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    arr: float | None = None
    evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id, "name": self.name, "play": self.play,
            "headline": self.headline, "score": round(self.score, 1),
            "components": {k: round(v, 1) for k, v in self.components.items()},
            "reasons": self.reasons, "arr": self.arr, "evidence": self.evidence,
        }


def _claims(conn: Connection, account_id: str, types: tuple[str, ...]) -> list[dict]:
    marks = ",".join("?" * len(types))
    rows = conn.execute(
        f"""SELECT c.claim_type, c.subject, c.value, c.verbatim_quote, c.doc_date,
                   d.title AS source_title
            FROM claims c JOIN documents d ON d.doc_id = c.source_doc_id
            WHERE c.account_id=? AND c.status='active' AND c.claim_type IN ({marks})
            ORDER BY c.doc_date DESC""",
        [account_id, *types],
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------


def _score(m: AccountMetrics, max_arr: float, recoverable: bool) -> tuple[float, dict, list[str]]:
    comp: dict[str, float] = {}
    why: list[str] = []
    u = m.usage

    if m.arr_at_risk and max_arr:
        comp["arr_at_risk"] = WEIGHTS["arr_at_risk"] * (m.arr_at_risk / max_arr)
        why.append(f"${m.arr_at_risk:,.0f} ARR exposed")

    if u.pct_off_peak > 0 and u.trend in ("declining", "dormant"):
        comp["usage_collapse"] = WEIGHTS["usage_collapse"] * min(u.pct_off_peak / 100, 1.0)
        why.append(f"flying {u.pct_off_peak:.0f}% below peak "
                   f"({u.latest_hours:g}h vs {u.peak_hours:g}h)")

    worst = max((d.severity for d in m.divergences),
                key=lambda s: {"low": 1, "medium": 2, "high": 3}.get(s, 0), default=None)
    if worst:
        comp["divergence"] = WEIGHTS["divergence"] * {"high": 1.0, "medium": .6, "low": .25}[worst]
        why.append(f"{len(m.divergences)} divergence(s), worst = {worst}")

    # Silence only counts once it is unusual. Under a month is normal cadence.
    if m.days_since_last_touch is not None and m.days_since_last_touch > 30:
        comp["silence"] = WEIGHTS["silence"] * min((m.days_since_last_touch - 30) / 120, 1.0)
        why.append(f"no recorded contact for {m.days_since_last_touch} days")

    if m.days_to_renewal is not None and 0 <= m.days_to_renewal <= 120:
        comp["renewal_proximity"] = WEIGHTS["renewal_proximity"] * (1 - m.days_to_renewal / 120)
        why.append(f"renewal in {m.days_to_renewal} days")

    if recoverable:
        comp["recoverability"] = WEIGHTS["recoverability"]
        why.append("churn evidence points to a recoverable cause")

    return sum(comp.values()), comp, why


def _recoverable_churn(claims: list[dict]) -> tuple[bool, list[dict]]:
    """Distinguish a churn we caused from one that was structural.

    Deliberately keyword-driven over the *verbatim quotes*, not an LLM call:
    the decision is auditable, and the quote that triggered it travels with the
    verdict so a human can overrule it.
    """
    ours = ("fell through the cracks", "deprioriti", "our side", "follow-up",
            "follow up", "no one reached", "never got back", "dropped the ball",
            "one more nudge", "lapsed")
    intent = ("wanting to add", "delayed, not cancelled", "not cancelled",
              "second dock", "interested in", "would consider", "revisit")
    hits = [c for c in claims
            if any(k in (c["verbatim_quote"] or "").lower() for k in ours + intent)]
    return bool(hits), hits[:4]


def _pick_play(m: AccountMetrics, recoverable: bool) -> tuple[Play, str]:
    stage = (m.stage or "").lower()
    u = m.usage

    if "churn" in stage:
        if recoverable:
            return "win_back", ("Churned for a cause we control, with expansion intent "
                                "on record — worth one deliberate re-approach.")
        return "confirm_churn", "Churned; evidence suggests the cause was structural."

    if u.trend == "dormant":
        return "save_play", ("Flying has stopped entirely. Treat as an active save, not "
                             "a check-in — the account has effectively already left.")

    if u.trend == "declining" and any(d.severity == "high" for d in m.divergences):
        return "reengage", ("Usage is falling while the CRM still reads healthy. Find out "
                            "why before the renewal conversation, not during it.")

    if m.days_to_renewal is not None and 0 <= m.days_to_renewal <= 90:
        return "renewal_outreach", "Renewal is close enough to need a plan."

    if "pre-sale" in stage or "prospect" in stage or "negotiat" in stage:
        return "advance_deal", "Open opportunity — the next step is commercial, not technical."

    if "onboard" in stage or "newly-sold" in stage:
        return "onboarding_check", ("Recently sold. Early usage is the leading indicator of "
                                    "whether this renews.")

    if u.trend == "growing" and (m.days_since_last_touch or 0) > 30:
        return "expansion_conversation", ("Flying more and nobody has asked why. Growing usage "
                                          "with no conversation is an unclaimed expansion.")

    return "monitor", "No action indicated by the current evidence."


def next_best_actions(conn: Connection, today: date | None = None,
                      limit: int | None = None) -> list[Action]:
    metrics = compute_all(conn, today=today)
    max_arr = max((m.arr_at_risk for m in metrics), default=0.0) or 1.0

    actions: list[Action] = []
    for m in metrics:
        churn_claims = _claims(conn, m.account_id,
                               ("churn_reason", "opportunity", "renewal_signal", "blocker"))
        recoverable, hits = _recoverable_churn(churn_claims) if "churn" in (m.stage or "").lower() \
            else (False, [])

        score, comp, why = _score(m, max_arr, recoverable)
        play, headline = _pick_play(m, recoverable)

        # "monitor" is not an action; keep it out of a queue meant to be worked
        # top-down, but leave it visible in the API for completeness.
        actions.append(Action(
            account_id=m.account_id, name=m.name, play=play, headline=headline,
            score=score, components=comp, reasons=why, arr=m.arr,
            evidence=hits or [c for c in churn_claims[:2]],
        ))

    actions.sort(key=lambda a: (-a.score, -(a.arr or 0)))
    return actions[:limit] if limit else actions


# --------------------------------------------------------------------------


def renewal_picture(conn: Connection, today: date | None = None) -> dict[str, Any]:
    """Secure / at risk / lost, plus an honest note on what is not yet known.

    The forecast states its own blind spot rather than implying completeness:
    renewal dates live inside the renewal-tracker documents, so until extraction
    reaches those files the timing half of this is incomplete.
    """
    metrics = compute_all(conn, today=today)
    buckets: dict[str, list[dict]] = {"lost": [], "at_risk": [], "watch": [], "secure": [],
                                      "pipeline": []}

    for m in metrics:
        stage = (m.stage or "").lower()
        entry = {"account_id": m.account_id, "name": m.name, "arr": m.arr or 0,
                 "trend": m.usage.trend, "health_label": m.health_label,
                 "days_to_renewal": m.days_to_renewal}

        if "churn" in stage:
            buckets["lost"].append(entry)
        elif not m.arr:
            buckets["pipeline"].append(entry)
        elif m.usage.trend == "dormant" or any(d.severity == "high" for d in m.divergences):
            buckets["at_risk"].append(entry)
        elif m.usage.trend == "declining" or (m.days_since_last_touch or 0) > 60:
            buckets["watch"].append(entry)
        else:
            buckets["secure"].append(entry)

    totals = {k: sum(e["arr"] for e in v) for k, v in buckets.items()}
    live = totals["secure"] + totals["watch"] + totals["at_risk"]

    missing_dates = sum(1 for m in metrics if m.arr and m.days_to_renewal is None)
    caveats = []
    if missing_dates:
        caveats.append(
            f"{missing_dates} paying account(s) have no renewal date in the CRM record — "
            "the dates live inside the renewal-tracker documents, so timing is incomplete "
            "until extraction covers those files.")
    unextracted = conn.execute(
        """SELECT COUNT(*) n FROM accounts a WHERE NOT EXISTS
           (SELECT 1 FROM claims c WHERE c.account_id=a.account_id AND c.status='active')"""
    ).fetchone()["n"]
    if unextracted:
        caveats.append(f"{unextracted} account(s) have no extracted claims yet, so this view "
                       "rests on CRM fields and usage alone for them.")

    return {
        "buckets": buckets, "totals": totals,
        "live_arr": live,
        "at_risk_share": (totals["at_risk"] / live) if live else 0.0,
        "caveats": caveats,
    }


def expansion_register(conn: Connection, today: date | None = None) -> dict[str, list[dict]]:
    """Real opportunities versus the ones that only look like opportunities.

    A trap is an opportunity signal on an account whose fundamentals contradict
    it — the account has stopped flying, or already churned, or the CRM health
    label is the only thing holding the story up.
    """
    metrics = {m.account_id: m for m in compute_all(conn, today=today)}
    real: list[dict] = []
    traps: list[dict] = []

    for aid, m in metrics.items():
        opps = _claims(conn, aid, ("opportunity",))
        blockers = _claims(conn, aid, ("blocker", "objection", "risk"))
        stage = (m.stage or "").lower()

        disqualifiers: list[str] = []
        if "churn" in stage:
            disqualifiers.append("account is already churned")
        if m.usage.trend == "dormant":
            disqualifiers.append("no flight activity — the account has stopped flying")
        if m.usage.trend == "declining" and any(d.severity == "high" for d in m.divergences):
            disqualifiers.append(
                f"usage is {m.usage.pct_off_peak:.0f}% off peak while the CRM still reads "
                f"'{m.health_label}'")

        # An account with no opportunity claim but healthy growth is still worth
        # listing: growing usage nobody has acted on is an unclaimed opportunity.
        latent = (not opps and m.usage.trend == "growing"
                  and (m.days_since_last_touch or 0) > 30)

        if not opps and not latent:
            continue

        entry = {
            "account_id": aid, "name": m.name, "arr": m.arr,
            "stage": m.stage, "trend": m.usage.trend,
            "signals": opps[:3],
            "latent": latent,
            "blockers": [b["value"] for b in blockers[:3]],
            "disqualifiers": disqualifiers,
        }
        (traps if disqualifiers else real).append(entry)

    real.sort(key=lambda e: -(e["arr"] or 0))
    traps.sort(key=lambda e: -(e["arr"] or 0))
    return {"real": real, "traps": traps}

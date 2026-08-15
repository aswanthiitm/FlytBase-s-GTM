"""Tests for the deterministic metrics layer.

The divergence tests matter most: they are the mechanism that catches an account
labelled healthy while its aircraft sit on the ground.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gtm.db import connect
from gtm.ingest import ingest
from gtm.metrics import (
    _money,
    classify_health_label,
    compute,
    parse_date,
    usage_trend,
)
from gtm.sources.fixture import FixtureAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def series(*pairs):
    return [{"period": p, "flight_hours": h, "missions": int(h)} for p, h in pairs]


# ----------------------------------------------------------------- trends


def test_no_data_and_single_month():
    assert usage_trend([]).trend == "no_data"
    assert usage_trend(series(("2026-01", 100))).trend == "insufficient_data"


def test_declining_is_detected():
    t = usage_trend(series(("2026-01", 210), ("2026-02", 188), ("2026-03", 96)))
    assert t.trend == "declining"
    assert t.consecutive_declines == 2
    assert t.latest_hours == 96
    assert t.peak_hours == 210
    assert t.pct_off_peak == pytest.approx(54.3, abs=0.5)


def test_growing_is_detected():
    t = usage_trend(series(("2026-01", 40), ("2026-02", 65), ("2026-03", 110)))
    assert t.trend == "growing"
    assert t.pct_change_recent > 0


def test_flat_usage_is_stable_not_a_trend():
    t = usage_trend(series(("2026-01", 100), ("2026-02", 101), ("2026-03", 99)))
    assert t.trend == "stable"


def test_dormant_beats_declining():
    """Two months at zero is not a slope, it is a stopped customer."""
    t = usage_trend(series(("2026-01", 120), ("2026-02", 0), ("2026-03", 0)))
    assert t.trend == "dormant"
    assert t.zero_streak == 2
    assert "dormant" in t.headline()


def test_slope_is_normalized_to_account_scale():
    """A 10 h/month drop is noise at 1000h and a crisis at 30h. Both series fall
    by the same absolute amount per month; only the small one is 'declining'."""
    big = usage_trend(series(("2026-01", 1010), ("2026-02", 1000), ("2026-03", 990)))
    small = usage_trend(series(("2026-01", 50), ("2026-02", 40), ("2026-03", 30)))
    assert big.trend == "stable"
    assert small.trend == "declining"


# ----------------------------------------------------------------- parsing


@pytest.mark.parametrize("raw,expected", [
    ("2026-03-14", date(2026, 3, 14)),
    ("2026/03/14", date(2026, 3, 14)),
    ("14-Mar-2026", date(2026, 3, 14)),
    ("2026-03-14T09:30:00Z", date(2026, 3, 14)),
    ("", None),
    (None, None),
    ("not a date", None),
])
def test_parse_date_handles_export_variety(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    (120000, 120000.0), ("120000", 120000.0), ("$120,000", 120000.0),
    ("120k", 120000.0), ("1.2M", 1200000.0), ("", None), (None, None),
])
def test_money_parsing(raw, expected):
    assert _money(raw) == expected


@pytest.mark.parametrize("label,expected", [
    ("green", "positive"), ("Healthy", "positive"), ("at risk", "warning"),
    ("red", "negative"), ("85", "positive"), ("20", "negative"),
    (None, "unknown"), ("mauve", "unknown"),
])
def test_health_label_classification(label, expected):
    assert classify_health_label(label) == expected


# ----------------------------------------------------------------- rollup


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "m.db")
    yield c
    c.close()


def test_divergence_flags_green_label_over_declining_usage(conn):
    """T4, the headline finding: the label says green, the aircraft say otherwise."""
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_b"))  # adds the 96h March

    m = compute(conn, "acct_demo_01", today=date(2026, 4, 1))
    assert m.health_label == "green"
    assert m.usage.trend == "declining"

    kinds = {d.kind for d in m.divergences}
    assert "health_label_overstates_usage" in kinds

    d = next(d for d in m.divergences if d.kind == "health_label_overstates_usage")
    assert d.severity == "high"  # >40% off peak
    assert d.evidence["series"]  # evidence travels with the finding


def test_no_divergence_when_label_and_usage_agree(conn):
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    m = compute(conn, "acct_demo_01", today=date(2026, 3, 1))
    # snapshot_a alone is 210 -> 188, a mild decline on a green label.
    assert not any(d.severity == "high" for d in m.divergences)


def test_last_touch_ignores_internal_notes(conn):
    """A CSM writing a note to themselves is not contact with the customer."""
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    m = compute(conn, "acct_demo_01", today=date(2026, 3, 20))
    # Internal note is 2026-03-05; latest real contact is the Feb 11 transcript.
    assert m.last_touch_date == "2026-02-11"
    assert m.days_since_last_touch == 37


def test_metrics_read_crm_fields_from_raw_payload(conn):
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    m = compute(conn, "acct_demo_01", today=date(2026, 3, 1))
    assert m.arr == 120000.0
    assert m.health_label == "green"
    assert m.doc_counts == {"transcript": 1, "ticket": 1, "note": 1}


def test_renewal_countdown_and_stale_contact(conn):
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    conn.execute(
        "UPDATE accounts SET raw_json=? WHERE account_id='acct_demo_01'",
        (json.dumps({"arr": 120000, "health": "green", "renewal_date": "2026-04-15"}),),
    )
    m = compute(conn, "acct_demo_01", today=date(2026, 3, 20))
    assert m.days_to_renewal == 26
    kinds = {d.kind for d in m.divergences}
    assert "renewal_near_with_stale_contact" in kinds
    assert m.arr_at_risk > 0


def test_removed_document_shows_in_metrics(conn):
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_a"))
    ingest(conn, FixtureAdapter(FIXTURES / "snapshot_b"))
    m = compute(conn, "acct_demo_01", today=date(2026, 4, 1))
    assert m.removed_docs == 1

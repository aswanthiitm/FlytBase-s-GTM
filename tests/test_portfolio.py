"""L4 tests.

The ranking is deterministic on purpose, so it can be asserted rather than
eyeballed. These tests pin the judgements that would be embarrassing to get
wrong in front of a reader: that money and evidence outrank tidiness, and that
two churned accounts are not treated as the same problem.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gtm.claims import persist_claims
from gtm.db import connect
from gtm.ingest import ingest
from gtm.models import Claim
from gtm.portfolio import expansion_register, next_best_actions, renewal_picture
from gtm.sources.fixture import FixtureAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TODAY = date(2026, 4, 1)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "p.db")
    ingest(c, FixtureAdapter(FIXTURES / "snapshot_a"))
    ingest(c, FixtureAdapter(FIXTURES / "snapshot_b"))  # declining usage lands here
    yield c
    c.close()


def _set_stage(conn, aid, stage, raw=None):
    conn.execute("UPDATE accounts SET stage=?, raw_json=? WHERE account_id=?",
                 (stage, json.dumps(raw or {"arr": 25000, "health": "green"}), aid))
    conn.commit()


def test_declining_account_with_money_ranks_first(conn):
    actions = next_best_actions(conn, today=TODAY)
    assert actions[0].account_id == "acct_demo_01"
    assert actions[0].play == "reengage"


def test_every_action_carries_the_components_that_produced_it(conn):
    """A ranking you cannot argue with is a ranking you can only believe."""
    top = next_best_actions(conn, today=TODAY)[0]
    assert top.components
    assert top.reasons
    assert sum(top.components.values()) == pytest.approx(top.score)
    assert any("ARR" in r for r in top.reasons)


def test_recoverable_churn_is_a_win_back_not_a_write_off(conn):
    _set_stage(conn, "acct_demo_01", "churned")
    persist_claims(conn, [Claim(
        account_id="acct_demo_01", claim_type="churn_reason", subject="cause",
        value="Lapsed because we did not follow up",
        source_doc_id="doc_a1",
        verbatim_quote="we are flying two docks daily")])
    # Rewrite the quote to one that reads as our fault, via a real document.
    conn.execute("UPDATE claims SET verbatim_quote=? WHERE account_id=?",
                 ("this fell through the cracks on my end", "acct_demo_01"))
    conn.commit()

    action = next(a for a in next_best_actions(conn, today=TODAY)
                  if a.account_id == "acct_demo_01")
    assert action.play == "win_back"
    assert "recoverable" in " ".join(action.reasons)
    assert action.evidence  # the quote that drove the verdict travels with it


def test_structural_churn_is_not_a_win_back(conn):
    """Two churned accounts are not the same problem — the brief asked whether
    *either* is worth winning back, and 'no' has to be reachable."""
    _set_stage(conn, "acct_demo_02", "churned", {"arr": 900})
    action = next(a for a in next_best_actions(conn, today=TODAY)
                  if a.account_id == "acct_demo_02")
    assert action.play == "confirm_churn"


def test_dormant_account_is_a_save_play_not_a_check_in(conn):
    conn.execute("UPDATE usage_periods SET flight_hours=0, missions=0 "
                 "WHERE account_id='acct_demo_01' AND period IN ('2026-02','2026-03')")
    conn.commit()
    action = next(a for a in next_best_actions(conn, today=TODAY)
                  if a.account_id == "acct_demo_01")
    assert action.play == "save_play"


def test_silence_under_a_month_is_normal_cadence(conn):
    """Contact recency should only score once it is unusual, or every account
    looks urgent and the queue stops meaning anything."""
    actions = {a.account_id: a for a in next_best_actions(conn, today=date(2026, 2, 20))}
    assert "silence" not in actions["acct_demo_01"].components


def test_renewal_picture_buckets_and_states_its_blind_spot(conn):
    picture = renewal_picture(conn, today=TODAY)
    assert set(picture["buckets"]) == {"lost", "at_risk", "watch", "secure", "pipeline"}
    assert picture["totals"]["at_risk"] > 0
    # An forecast that hides what it does not know is worse than no forecast.
    assert any("renewal date" in c for c in picture["caveats"])


def test_expansion_signal_on_a_churned_account_is_a_trap(conn):
    _set_stage(conn, "acct_demo_01", "churned")
    persist_claims(conn, [Claim(
        account_id="acct_demo_01", claim_type="opportunity", subject="third site",
        value="Wants a third site by Q3", source_doc_id="doc_a1",
        verbatim_quote="want a third site by Q3")])
    reg = expansion_register(conn, today=TODAY)
    trap = next(e for e in reg["traps"] if e["account_id"] == "acct_demo_01")
    assert "already churned" in " ".join(trap["disqualifiers"])
    assert not any(e["account_id"] == "acct_demo_01" for e in reg["real"])


def test_growing_usage_nobody_has_acted_on_is_a_latent_opportunity(conn):
    conn.execute("UPDATE usage_periods SET flight_hours=400 "
                 "WHERE account_id='acct_demo_01' AND period='2026-03'")
    conn.commit()
    reg = expansion_register(conn, today=date(2026, 6, 1))
    entry = next((e for e in reg["real"] if e["account_id"] == "acct_demo_01"), None)
    assert entry is not None and entry["latent"] is True

"""
================================================================================
ASSUMPTIONS MANAGER — Part 2 validation
================================================================================
What this file does:
    Validates engine/assumptions_manager.py's AssumptionSet — the
    user-editable, named, saveable assumption bundle behind the
    dashboard's Assumptions page and every /assumptions/* API endpoint.

    Does NOT test the API endpoints themselves (that needs a live/TestClient
    server + database — see the manual smoke test run alongside this file
    for GET /assumptions/defaults, POST /pricing with a custom override,
    save/list/reload, and the ValuationRun audit trail) — this file covers
    the pure dataclass logic: Ghana defaults, to_dict()/from_dict()
    round-tripping, and the bridge into engine.assumptions.ProductAssumptions
    that engine/runner.py's run_pricing()/run_ifrs17()/run_rate_table() use.
================================================================================
"""

import pytest

from engine.assumptions import LapseSchedule, ProductAssumptions
from engine.assumptions_manager import AssumptionSet
from engine.runner import run_pricing


# ── Ghana defaults ───────────────────────────────────────────────────────────

def test_ghana_defaults_values():
    d = AssumptionSet.ghana_defaults()
    assert d.mortality.table == "SA 85/90"
    assert d.mortality.loading == pytest.approx(-0.20)
    assert d.mortality.gender_basis == "unisex"
    assert d.lapses[1] == pytest.approx(0.25)
    assert d.lapses[13] == pytest.approx(0.02)
    assert d.commissions[1] == pytest.approx(0.12)
    assert d.commissions[2] == pytest.approx(0.12)
    assert d.commissions[3] == pytest.approx(0.0)
    assert d.expenses.policy_fee == pytest.approx(1.00)
    assert d.expenses.acquisition_cost == pytest.approx(8.00)
    assert d.expenses.renewal_expense == pytest.approx(18.00)
    assert d.expenses.claims_admin == pytest.approx(5.00)
    assert d.economic.valuation_rate == pytest.approx(0.165)
    assert d.economic.investment_return == pytest.approx(0.14)
    assert d.economic.expense_inflation == pytest.approx(0.12)
    assert d.economic.fx_rate == pytest.approx(15.50)
    assert d.risk_adjustment.coc_rate == pytest.approx(0.06)
    assert d.risk_adjustment.solvency_capital == pytest.approx(500_000)
    assert d.pricing.profit_margin == pytest.approx(0.15)
    assert d.pricing.collection_rate == pytest.approx(0.70)


# ── Serialisation round-trip ─────────────────────────────────────────────────

def test_to_dict_from_dict_round_trip():
    original = AssumptionSet.ghana_defaults()
    restored = AssumptionSet.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()


def test_to_dict_uses_string_keys_for_json_safety():
    d = AssumptionSet.ghana_defaults().to_dict()
    assert all(isinstance(k, str) for k in d["lapses"].keys())
    assert all(isinstance(k, str) for k in d["commissions"].keys())


def test_from_dict_handles_partial_payload():
    """A caller only overriding mortality loading shouldn't need to supply every other field."""
    custom = AssumptionSet.from_dict({"name": "Stress Test", "mortality": {"loading": 0.10}})
    assert custom.mortality.loading == pytest.approx(0.10)
    assert custom.mortality.table == "SA 85/90"   # untouched default
    assert custom.economic.valuation_rate == pytest.approx(0.165)   # untouched default


# ── Bridge into ProductAssumptions ───────────────────────────────────────────

def test_to_product_assumptions_overrides_base():
    base = ProductAssumptions(lapse_schedule=LapseSchedule(rates={1: 0.30}), entry_age_main=40, company_name="Test Co")
    custom = AssumptionSet.ghana_defaults()
    custom.mortality.loading = 0.10

    result = custom.to_product_assumptions(base=base, entry_age_main=35)

    assert result.mortality_loading == pytest.approx(0.10)
    assert result.entry_age_main == 35
    assert result.company_name == "Test Co"        # untouched — not part of AssumptionSet
    assert result.lapse_schedule.get_annual_rate(1) == pytest.approx(0.25)   # overridden by custom.lapses


def test_to_product_assumptions_does_not_mutate_base():
    base = ProductAssumptions(lapse_schedule=LapseSchedule(rates={1: 0.30}), entry_age_main=40)
    custom = AssumptionSet.ghana_defaults()
    custom.to_product_assumptions(base=base, entry_age_main=35)
    assert base.entry_age_main == 40   # base untouched


# ── End-to-end through run_pricing() ────────────────────────────────────────

def test_run_pricing_without_assumption_set_is_unchanged():
    r = run_pricing(client_id="pic", product_name="whole_life_tier1", entry_age=35, verbose=False)
    assert r["monthly_premium"] == pytest.approx(16.29, abs=0.05)
    assert "assumptions_used" in r


def test_run_pricing_with_stressed_mortality_loading_raises_premium():
    baseline = run_pricing(client_id="pic", product_name="whole_life_tier1", entry_age=35, verbose=False)

    stressed = AssumptionSet.ghana_defaults()
    stressed.mortality.loading = 0.10   # from -20% to +10% — much heavier mortality
    result = run_pricing(
        client_id="pic", product_name="whole_life_tier1", entry_age=35,
        assumption_set=stressed, verbose=False,
    )

    assert result["monthly_premium"] > baseline["monthly_premium"]
    assert result["assumptions_used"]["mortality_loading"] == pytest.approx(0.10)


def test_run_pricing_assumption_set_reproduces_identical_result():
    """Saving/reloading an AssumptionSet (to_dict/from_dict, as the DB round-trip does) must reproduce the exact same premium."""
    custom = AssumptionSet.ghana_defaults()
    custom.mortality.loading = 0.10
    saved_then_reloaded = AssumptionSet.from_dict(custom.to_dict())

    r1 = run_pricing(client_id="pic", product_name="whole_life_tier1", entry_age=35, assumption_set=custom, verbose=False)
    r2 = run_pricing(client_id="pic", product_name="whole_life_tier1", entry_age=35, assumption_set=saved_then_reloaded, verbose=False)

    assert r1["monthly_premium"] == r2["monthly_premium"]

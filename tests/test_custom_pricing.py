"""
================================================================================
CUSTOM PRODUCT PRICING — Part 4 validation
================================================================================
What this file does:
    Validates engine/custom_pricing.py — the ad-hoc product builder behind
    the dashboard's Part 4 pricing platform (POST /pricing/custom and
    friends). Covers: building a Product from a raw request dict, the
    maturity-benefit rider special-case, per-dependant benefit overrides,
    and the annual cash flow / reserve / profit signature roll-ups.
================================================================================
"""

import pytest

from engine.assumptions_manager import AssumptionSet
from engine.custom_pricing import (
    build_custom_product, run_custom_pricing, run_custom_rate_table, run_custom_sensitivity,
)


def _ghana_assumptions(entry_age=35):
    return AssumptionSet.ghana_defaults().to_product_assumptions(entry_age_main=entry_age)


# ── build_custom_product ─────────────────────────────────────────────────────

def test_build_custom_product_basic_riders():
    spec = {
        "product_name": "Test Product", "product_type": "whole_life", "policy_term_years": None,
        "riders": [
            {"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 5000},
            {"name": "TPD", "benefit_type": "tpd", "benefit_amount": 2500},
        ],
        "dependants": [{"relationship": "spouse", "age": 32}],
    }
    product = build_custom_product(spec)
    assert product.name == "Test Product"
    assert len(product.riders) == 2
    death = next(r for r in product.riders if r.rider_type == "death")
    assert death.incidence_basis == "mortality"
    tpd = next(r for r in product.riders if r.rider_type == "tpd")
    assert tpd.incidence_basis == "tpd"
    assert tpd.annual_incidence_rate > 0
    assert len(product.dependants) == 1
    assert product.dependants[0].relationship == "spouse"


def test_maturity_rider_becomes_product_level_maturity_benefit():
    spec = {
        "product_name": "Test Endowment", "product_type": "endowment", "policy_term_years": 20,
        "riders": [
            {"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 50000},
            {"name": "Maturity Value", "benefit_type": "maturity", "benefit_amount": 50000, "rider_term_years": 20},
        ],
    }
    product = build_custom_product(spec)
    # Only the death rider becomes a Rider — maturity is Product-level.
    assert len(product.riders) == 1
    assert product.riders[0].rider_type == "death"
    assert product.maturity_benefits == {20: 50000.0}


def test_savings_rider_is_a_safe_placeholder_not_a_silent_mischarge():
    spec = {
        "product_name": "Test Micro", "product_type": "micro_life", "policy_term_years": 2,
        "riders": [{"name": "Investment Account", "benefit_type": "savings", "benefit_amount": 1000}],
    }
    product = build_custom_product(spec)
    rider = product.riders[0]
    assert rider.incidence_basis == "savings_placeholder"
    assert rider.annual_incidence_rate == 0.0
    assert rider.avg_events_per_year == 0.0


# ── Dependant benefit overrides ──────────────────────────────────────────────

def test_dependant_benefit_override_takes_priority_over_multiplier():
    spec = {
        "product_name": "Test", "product_type": "whole_life",
        "riders": [{"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 5000}],
        "dependants": [{"relationship": "spouse", "age": 32, "benefit_overrides": {"Death Benefit": 2000}}],
    }
    product = build_custom_product(spec)
    dep = product.dependants[0]
    assert dep.get_dependant_benefit("Death Benefit", 5000.0) == 2000.0
    # A rider not listed in benefit_overrides falls back to multiplier x rider benefit.
    assert dep.get_dependant_benefit("Other Rider", 1000.0) == 1000.0  # multiplier defaults to 1.0


# ── run_custom_pricing ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def whole_life_result():
    spec = {
        "product_name": "Test Whole Life", "product_type": "whole_life", "policy_term_years": None,
        "riders": [{"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 5000}],
    }
    product = build_custom_product(spec)
    return run_custom_pricing(product, _ghana_assumptions(35))


def test_run_custom_pricing_hits_target_margin(whole_life_result):
    assert whole_life_result["profit_margin"] == pytest.approx(0.15, abs=1e-6)
    assert whole_life_result["monthly_premium"] > 0


def test_annual_cashflow_rollup_shape(whole_life_result):
    rows = whole_life_result["annual_cashflow"]
    assert rows[0]["policy_year"] == 1
    assert rows[0]["opening_lives"] == pytest.approx(1.0)
    assert "Death Benefit" in rows[0]["claims_by_rider"]
    # Years must be contiguous starting at 1.
    assert [r["policy_year"] for r in rows] == list(range(1, len(rows) + 1))


def test_reserve_projection_starts_and_ends_reasonably(whole_life_result):
    reserve = whole_life_result["reserve_projection"]
    assert len(reserve) == len(whole_life_result["annual_cashflow"])
    # Closing reserve of the final year should be ~0 (nothing owed after the last month).
    assert reserve[-1]["closing_reserve"] == pytest.approx(0.0, abs=1.0)


def test_profit_signature_breakeven_detected(whole_life_result):
    sig = whole_life_result["profit_signature"]
    assert sig["breakeven_year"] is not None
    # cumulative_profit at the breakeven year must actually be >= 0.
    breakeven_row = next(r for r in sig["profit_by_year"] if r["policy_year"] == sig["breakeven_year"])
    assert breakeven_row["cumulative_profit"] >= 0


def test_endowment_maturity_benefit_appears_in_final_year_cashflow():
    spec = {
        "product_name": "Test Endowment", "product_type": "endowment", "policy_term_years": 20,
        "riders": [
            {"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 50000},
            {"name": "Maturity Value", "benefit_type": "maturity", "benefit_amount": 50000, "rider_term_years": 20},
        ],
    }
    product = build_custom_product(spec)
    result = run_custom_pricing(product, _ghana_assumptions(35))
    rows = result["annual_cashflow"]
    assert rows[-1]["policy_year"] == 20
    assert rows[-1]["maturity_benefit"] > 0
    assert all(r["maturity_benefit"] == 0 for r in rows[:-1])


# ── Rate table & sensitivity ─────────────────────────────────────────────────

def test_rate_table_premium_increases_with_age():
    spec = {
        "product_name": "Test", "product_type": "whole_life",
        "riders": [{"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 5000}],
    }
    product = build_custom_product(spec)
    table = run_custom_rate_table(product, _ghana_assumptions(35), age_start=30, age_end=40)
    assert set(table.keys()) == set(range(30, 41))
    assert table[40]["monthly_premium"] > table[30]["monthly_premium"]


def test_sensitivity_directions_are_correct():
    spec = {
        "product_name": "Test", "product_type": "whole_life",
        "riders": [{"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 5000}],
    }
    product = build_custom_product(spec)
    results = run_custom_sensitivity(product, _ghana_assumptions(35))
    by_label = {r["assumption_stressed"]: r for r in results}

    assert by_label["Base case"]["difference"] == 0.0
    # Heavier mortality (+10%) must raise the premium; lighter (-10%) must lower it.
    assert by_label["Mortality +10%"]["stressed_premium"] > by_label["Base case"]["stressed_premium"]
    assert by_label["Mortality -10%"]["stressed_premium"] < by_label["Base case"]["stressed_premium"]
    # Higher expenses must raise the premium; lower expenses must lower it.
    assert by_label["Expenses +10%"]["stressed_premium"] > by_label["Base case"]["stressed_premium"]
    assert by_label["Expenses -10%"]["stressed_premium"] < by_label["Base case"]["stressed_premium"]
    # A higher discount rate reduces the PV of future outflows relative to premiums -> lower premium needed.
    assert by_label["Valuation rate +1%"]["stressed_premium"] < by_label["Base case"]["stressed_premium"]
    assert by_label["Valuation rate -1%"]["stressed_premium"] > by_label["Base case"]["stressed_premium"]

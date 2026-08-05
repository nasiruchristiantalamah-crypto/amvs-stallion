"""
================================================================================
FIXED-SCALE COMMISSION — "Transport Commission Scale (GHS 700 Base)"
================================================================================
What this file does:
    Validates engine.assumptions.FixedScaleCommission — a flat GHS amount
    per in-force policy per year (not a percentage of premium), used for
    distribution structures like a "transport support" allowance scaled
    off a GHS 700 base rate: 130% / 120% / 100% / 80% / 70% of that base
    for policy years 1 / 2 / 3 / 4 / 5+, i.e. GHS 910 / 840 / 700 / 560 /
    490 per policy per year.

    Covers: the schedule's own step-function lookup, serialization
    round-tripping (and that it's distinguishable from a percentage
    CommissionSchedule when deserialized), the AssumptionSet bridge, and
    the actual cash flow calculation this exists to drive — engine/
    cashflows.py must apply it as GHS per in-force policy, not as a % of
    that month's premium.
================================================================================
"""

import pytest

from engine.assumptions import (
    ProductAssumptions, LapseSchedule, CommissionSchedule, FixedScaleCommission,
    transport_commission_scale_ghs700_base,
)
from engine.assumptions_manager import AssumptionSet
from engine.product import Product, Rider
from engine.decrement import run_decrement_projection
from engine.cashflows import calculate_cash_flows


# ── FixedScaleCommission itself ──────────────────────────────────────────────

def test_transport_commission_scale_preset_values():
    scale = transport_commission_scale_ghs700_base()
    assert scale.rates == {1: 910.0, 2: 840.0, 3: 700.0, 4: 560.0, 5: 490.0}


def test_step_function_holds_at_last_defined_year():
    scale = transport_commission_scale_ghs700_base()
    assert scale.get_annual_amount_for_policy_year(1) == 910.0
    assert scale.get_annual_amount_for_policy_year(4) == 560.0
    assert scale.get_annual_amount_for_policy_year(5) == 490.0
    assert scale.get_annual_amount_for_policy_year(20) == 490.0   # held from year 5 onward


def test_monthly_amount_is_annual_divided_by_12():
    scale = transport_commission_scale_ghs700_base()
    assert scale.get_monthly_amount_for_policy_year(1) == pytest.approx(910.0 / 12)
    assert scale.get_monthly_amount_for_policy_year(3) == pytest.approx(700.0 / 12)


def test_serialization_round_trip():
    scale = transport_commission_scale_ghs700_base()
    d = scale.to_dict()
    assert d["type"] == "fixed_scale"
    restored = FixedScaleCommission.from_dict(d)
    assert restored.rates == scale.rates


def test_negative_amount_is_rejected():
    with pytest.raises(ValueError):
        FixedScaleCommission.from_dict({"rates": {"1": -100.0}})


# ── ProductAssumptions.from_dict must tell the two commission types apart ────

def test_product_assumptions_round_trip_distinguishes_commission_type():
    asmp = ProductAssumptions(
        entry_age_main=35, lapse_schedule=LapseSchedule(rates={1: 0.25}),
        commission=transport_commission_scale_ghs700_base(),
    )
    d = asmp.to_dict()
    restored = ProductAssumptions.from_dict(d)
    assert isinstance(restored.commission, FixedScaleCommission)
    assert restored.commission.rates == {1: 910.0, 2: 840.0, 3: 700.0, 4: 560.0, 5: 490.0}

    asmp2 = ProductAssumptions(
        entry_age_main=35, lapse_schedule=LapseSchedule(rates={1: 0.25}),
        commission=CommissionSchedule(initial_rate=0.15, renewal_rate=0.02),
    )
    restored2 = ProductAssumptions.from_dict(asmp2.to_dict())
    assert isinstance(restored2.commission, CommissionSchedule)
    assert restored2.commission.initial_rate == pytest.approx(0.15)


# ── AssumptionSet bridge (the dashboard's Assumptions page model) ───────────

def test_assumption_set_selects_fixed_scale_when_commission_type_is_set():
    aset = AssumptionSet.ghana_defaults()
    aset.commission_type = "fixed_scale"
    aset.fixed_commission_scale = {1: 910.0, 2: 840.0, 3: 700.0, 4: 560.0, 5: 490.0}
    pa = aset.to_product_assumptions(entry_age_main=35)
    assert isinstance(pa.commission, FixedScaleCommission)
    assert pa.commission.rates == {1: 910.0, 2: 840.0, 3: 700.0, 4: 560.0, 5: 490.0}


def test_assumption_set_defaults_to_percentage_commission():
    aset = AssumptionSet.ghana_defaults()
    pa = aset.to_product_assumptions(entry_age_main=35)
    assert isinstance(pa.commission, CommissionSchedule)


# ── The actual cash flow calculation ─────────────────────────────────────────

def test_year_one_commission_is_75_83_per_policy_per_month():
    """The exact figure specified: GHS 910/year / 12 = GHS 75.83/month in policy year 1."""
    product = Product(name="Test", riders=[
        Rider(rider_type="death", name="Main Benefit", benefit_main=50000, incidence_basis="mortality"),
    ])
    assumptions = ProductAssumptions(
        entry_age_main=35, lapse_schedule=LapseSchedule(rates={1: 0.0}),
        commission=transport_commission_scale_ghs700_base(),
    )
    dec_rows = run_decrement_projection(assumptions, product)
    cf_rows = calculate_cash_flows(dec_rows, assumptions, product, monthly_premium=25.0)

    month_one = cf_rows[0]
    assert month_one.policy_year == 1
    assert round(month_one.commission, 2) == 75.83


def test_commission_scales_with_in_force_lives_not_premium():
    # Unlike a percentage commission, the fixed scale must NOT move when
    # the premium changes — it's GHS per in-force policy, full stop.
    product = Product(name="Test", riders=[
        Rider(rider_type="death", name="Main Benefit", benefit_main=50000, incidence_basis="mortality"),
    ])
    assumptions = ProductAssumptions(
        entry_age_main=35, lapse_schedule=LapseSchedule(rates={1: 0.0}),
        commission=transport_commission_scale_ghs700_base(),
    )
    dec_rows = run_decrement_projection(assumptions, product)

    cf_low = calculate_cash_flows(dec_rows, assumptions, product, monthly_premium=10.0)
    cf_high = calculate_cash_flows(dec_rows, assumptions, product, monthly_premium=200.0)
    assert cf_low[0].commission == pytest.approx(cf_high[0].commission)
    assert cf_low[0].commission == pytest.approx(910.0 / 12)

    # But it DOES shrink as the cohort decrements (lx falls over time),
    # same as every other lx-weighted cash flow line.
    later_month = cf_low[11]   # start of policy year 2, some lapses/deaths already happened
    assert later_month.commission < cf_low[0].commission


def test_percentage_commission_unaffected_by_this_change():
    # Regression guard: adding the fixed-scale branch must not touch the
    # existing percentage-of-premium behaviour at all.
    product = Product(name="Test", riders=[
        Rider(rider_type="death", name="Main Benefit", benefit_main=50000, incidence_basis="mortality"),
    ])
    assumptions = ProductAssumptions(
        entry_age_main=35, lapse_schedule=LapseSchedule(rates={1: 0.0}),
        commission=CommissionSchedule(initial_rate=0.15, renewal_rate=0.02),
    )
    dec_rows = run_decrement_projection(assumptions, product)
    cf_rows = calculate_cash_flows(dec_rows, assumptions, product, monthly_premium=25.0)
    month_one = cf_rows[0]
    assert month_one.commission == pytest.approx(25.0 * 0.15)

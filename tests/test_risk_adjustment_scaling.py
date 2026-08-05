"""
================================================================================
PER-POLICY RISK ADJUSTMENT SCALING — regression
================================================================================
What this file does:
    Regression test for a real bug: engine/present_value.py used to set a
    single policy's Risk Adjustment to assumptions.risk_adjustment, which is
    coc_rate x company-wide solvency_capital (default 6% x GHS 500,000 =
    GHS 30,000) — a portfolio-level capital figure, applied unscaled to
    every individual policy regardless of size. Confirmed directly: a policy
    with PV premiums of ~GHS 290 got a GHS 30,000 Risk Adjustment, forcing
    is_onerous=True with a ~GHS 30,000 fabricated loss component — for
    essentially every policy priced through the engine, independent of how
    well it was actually priced.

    Fixed by scaling RA to the policy's own PV(benefits) instead
    (assumptions.coc_rate * pv_benefits) — a per-contract Cost-of-Capital
    proxy, standard simplified practice, and proportional to the actual
    risk being priced. assumptions.risk_adjustment (the company-wide
    figure) is untouched and still used as-is for the in-force-cohort
    roll-forward in engine/ifrs17.py, a genuinely portfolio-level context.
================================================================================
"""

import pytest

from api.main import CustomProductRequest, _build_product_from_request, _resolve_custom_assumptions
from engine.custom_pricing import run_custom_pricing


def _price(sum_assured):
    req = CustomProductRequest(product_name="Test", product_type="whole_life", sum_assured=sum_assured, entry_age=35)
    product = _build_product_from_request(req)
    assumptions = _resolve_custom_assumptions(req)
    return run_custom_pricing(product, assumptions)


def test_a_reasonably_priced_small_policy_is_not_onerous():
    # Under the old bug this was ALWAYS True, regardless of sum assured,
    # because RA (GHS 30,000) dwarfed any individual policy's cash flows.
    result = _price(5000)
    assert result["is_onerous"] is False
    assert result["csm_at_inception"] > 0
    assert result["loss_component"] == 0.0


def test_risk_adjustment_scales_with_policy_size():
    small = _price(5000)
    large = _price(500000)
    # RA must move with the size of the policy being priced, not sit at a
    # fixed company-wide constant regardless of what's being priced.
    assert small["risk_adjustment"] < large["risk_adjustment"]
    assert small["risk_adjustment"] != pytest.approx(30000.0)
    assert large["risk_adjustment"] != pytest.approx(30000.0)


def test_risk_adjustment_equals_coc_rate_times_pv_benefits():
    result = _price(50000)
    req = CustomProductRequest(product_name="Test", product_type="whole_life", sum_assured=50000, entry_age=35)
    assumptions = _resolve_custom_assumptions(req)
    # Both sides are independently-rounded API outputs (2dp), so compare
    # with an absolute tolerance rather than expecting bit-exact equality.
    expected_ra = assumptions.coc_rate * result["pv_benefits"]
    assert result["risk_adjustment"] == pytest.approx(expected_ra, abs=0.01)

"""
================================================================================
CUSTOM PRICING REQUEST WIRING — Sum Assured / rider regression
================================================================================
What this file does:
    Regression test for a real bug: the dashboard's Pricing page always had
    at least one row in its riders table (auto-added on page load), and the
    API's old `CustomProductRequest.riders_or_default` was an either/or — if
    `riders` was non-empty, `sum_assured` was silently ignored entirely. A
    user could change Sum Assured from 1,000 to 200,000 and the premium
    would not move. Confirmed directly against the engine before the fix:
    identical premium (GHS 11.54) across that whole range.

    _build_product_from_request (api/main.py) now always builds a "Main
    Benefit" rider from sum_assured (when > 0), with `riders` treated as
    strictly ADDITIONAL benefits layered on top — never a replacement.
    These tests exercise that function directly, the same path every
    /pricing/custom* endpoint uses, so this bug class can't silently
    return.
================================================================================
"""

import pytest

from api.main import CustomProductRequest, _build_product_from_request
from engine.assumptions_manager import AssumptionSet
from engine.custom_pricing import run_custom_pricing


def _ghana_assumptions(entry_age=35):
    return AssumptionSet.ghana_defaults().to_product_assumptions(entry_age_main=entry_age)


def test_sum_assured_alone_builds_a_main_benefit_rider():
    req = CustomProductRequest(product_name="Test", product_type="whole_life", sum_assured=50000, entry_age=35)
    product = _build_product_from_request(req)
    assert len(product.riders) == 1
    assert product.riders[0].name == "Main Benefit"
    assert product.riders[0].rider_type == "death"
    assert product.riders[0].benefit_main == 50000.0


def test_sum_assured_is_used_even_when_riders_table_has_rows():
    # This is the exact failure mode: a non-empty riders list must not
    # cause sum_assured to be dropped.
    req = CustomProductRequest(
        product_name="Test", product_type="whole_life", sum_assured=50000, entry_age=35,
        riders=[{"name": "TPD", "benefit_type": "tpd", "benefit_amount": 25000, "waiting_period_months": 0}],
    )
    product = _build_product_from_request(req)
    assert len(product.riders) == 2
    main = next(r for r in product.riders if r.name == "Main Benefit")
    tpd = next(r for r in product.riders if r.name == "TPD")
    assert main.benefit_main == 50000.0
    assert tpd.benefit_main == 25000.0


def test_premium_responds_to_sum_assured_changes():
    def price_for(sum_assured):
        req = CustomProductRequest(product_name="Test", product_type="whole_life", sum_assured=sum_assured, entry_age=35)
        product = _build_product_from_request(req)
        return run_custom_pricing(product, _ghana_assumptions(35))["monthly_premium"]

    premiums = [price_for(sa) for sa in (1000, 5000, 50000, 200000)]
    # Strictly increasing — a higher sum assured must mean a higher premium.
    assert premiums == sorted(premiums)
    assert len(set(premiums)) == len(premiums)   # no two are equal


def test_premium_responds_to_adding_a_rider():
    req_base = CustomProductRequest(product_name="Test", product_type="whole_life", sum_assured=50000, entry_age=35)
    req_plus_ci = CustomProductRequest(
        product_name="Test", product_type="whole_life", sum_assured=50000, entry_age=35,
        riders=[{"name": "CI", "benefit_type": "critical_illness", "benefit_amount": 50000, "waiting_period_months": 0}],
    )
    asmp = _ghana_assumptions(35)
    premium_base = run_custom_pricing(_build_product_from_request(req_base), asmp)["monthly_premium"]
    premium_plus_ci = run_custom_pricing(_build_product_from_request(req_plus_ci), asmp)["monthly_premium"]
    assert premium_plus_ci > premium_base


def test_zero_sum_assured_with_no_riders_produces_no_riders():
    req = CustomProductRequest(product_name="Test", product_type="whole_life", sum_assured=0, entry_age=35)
    product = _build_product_from_request(req)
    assert product.riders == []


def test_sum_assured_maps_to_the_right_primary_benefit_per_product_type():
    cases = [
        ("whole_life", "death"),
        ("micro_life", "death"),
        ("hospital_cash", "hospital_cash"),
        ("critical_illness", "critical_illness"),
        ("income_protection", "income_protection"),
    ]
    for product_type, expected_rider_type in cases:
        req = CustomProductRequest(product_name="Test", product_type=product_type, sum_assured=1000, entry_age=35)
        product = _build_product_from_request(req)
        assert product.riders[0].rider_type == expected_rider_type, product_type


def test_rider_level_incidence_rate_override():
    req_default = CustomProductRequest(
        product_name="Test", product_type="whole_life", sum_assured=50000, entry_age=35,
        riders=[{"name": "Hosp", "benefit_type": "hospital_cash", "benefit_amount": 900, "waiting_period_months": 0}],
    )
    req_override = CustomProductRequest(
        product_name="Test", product_type="whole_life", sum_assured=50000, entry_age=35,
        riders=[{"name": "Hosp", "benefit_type": "hospital_cash", "benefit_amount": 900,
                 "waiting_period_months": 0, "annual_incidence_rate": 0.0002}],
    )
    hosp_default = next(r for r in _build_product_from_request(req_default).riders if r.name == "Hosp")
    hosp_override = next(r for r in _build_product_from_request(req_override).riders if r.name == "Hosp")
    assert hosp_default.annual_incidence_rate == pytest.approx(0.0025)   # engine default, unchanged
    assert hosp_override.annual_incidence_rate == pytest.approx(0.0002)  # caller override applied


def test_mortality_multiple_rider_is_wired_through():
    req = CustomProductRequest(
        product_name="Test", product_type="whole_life", sum_assured=50000, entry_age=45,
        riders=[{"name": "TPD", "benefit_type": "tpd", "benefit_amount": 25000,
                 "waiting_period_months": 0, "mortality_multiplier": 0.20}],
    )
    tpd = next(r for r in _build_product_from_request(req).riders if r.name == "TPD")
    assert tpd.incidence_basis == "mortality_multiple"
    assert tpd.mortality_multiplier == pytest.approx(0.20)


def test_mortality_multiple_rider_cost_scales_with_age_like_death_benefit():
    # A rider priced as "X% of mortality" must scale with age the same way
    # the death benefit does — unlike a flat annual_incidence_rate, which
    # would hold the exact same cost at every age.
    def tpd_premium(age):
        req = CustomProductRequest(
            product_name="Test", product_type="whole_life", sum_assured=1, entry_age=age,
            riders=[{"name": "TPD", "benefit_type": "tpd", "benefit_amount": 25000,
                     "waiting_period_months": 0, "mortality_multiplier": 0.20}],
        )
        product = _build_product_from_request(req)
        return run_custom_pricing(product, _ghana_assumptions(age))["monthly_premium"]

    premiums = [tpd_premium(a) for a in (35, 45, 55, 65)]
    assert premiums == sorted(premiums)
    assert len(set(premiums)) == len(premiums)   # strictly increasing, no two equal

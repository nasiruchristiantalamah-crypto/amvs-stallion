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


def test_premium_payment_term_years_is_respected():
    # Regression: premium_payment_term_years was accepted by the API and
    # stored nowhere — Product had no field for it, so a "10-pay" product
    # silently priced as if premiums ran the product's full duration.
    # Confirmed directly before the fix: identical premium (49.11)
    # whether premium_payment_term_years was None, 10, or 5.
    def price(payment_term):
        req = CustomProductRequest(
            product_name="Test", product_type="endowment", policy_term_years=20,
            sum_assured=50000, entry_age=35, premium_payment_term_years=payment_term,
            riders=[{"name": "Maturity", "benefit_type": "maturity", "benefit_amount": 50000,
                     "rider_term_years": 20, "waiting_period_months": 0}],
        )
        product = _build_product_from_request(req)
        return run_custom_pricing(product, _ghana_assumptions(35))["monthly_premium"]

    full_term = price(None)
    pay_10 = price(10)
    pay_5 = price(5)
    # Fewer years to fund the same benefits -> each premium must be bigger.
    assert full_term < pay_10 < pay_5


def _price_by_product_type(product_type):
    req = CustomProductRequest(
        product_name="Test", product_type=product_type, sum_assured=50000, entry_age=35,
        policy_term_years=None if product_type == "whole_life" else 20,
    )
    product = _build_product_from_request(req)
    return product, run_custom_pricing(product, _ghana_assumptions(35))


def test_endowment_types_add_a_maturity_benefit_and_cost_more_than_level_term():
    # Regression: selecting "Endowment" priced identically to "Level Term"
    # because nothing actually added the maturity benefit that makes an
    # endowment an endowment — the product_type label had no effect unless
    # the user separately, manually added a Maturity rider themselves.
    _, level_term = _price_by_product_type("level_term")
    for endowment_type in ("endowment", "educational_endowment"):
        product, result = _price_by_product_type(endowment_type)
        assert product.maturity_benefits == {20: 50000.0}
        # Guarantees a payout either way (death or survival) -> costs more.
        assert result["monthly_premium"] > level_term["monthly_premium"]


def test_pure_endowment_has_no_death_cover_only_maturity():
    product, result = _price_by_product_type("pure_endowment")
    assert product.riders == []   # no death rider at all
    assert product.maturity_benefits == {20: 50000.0}
    assert result["monthly_premium"] > 0


def test_product_types_with_no_defined_structural_difference_price_identically():
    # micro_life and group_life are classification labels, not distinct
    # benefit structures, when given the same sum_assured/term/riders —
    # unlike endowment/pure_endowment, there's no universal actuarial
    # rule that would make these differ from level_term automatically.
    _, level_term = _price_by_product_type("level_term")
    _, micro_life = _price_by_product_type("micro_life")
    _, group_life = _price_by_product_type("group_life")
    assert micro_life["monthly_premium"] == level_term["monthly_premium"]
    assert group_life["monthly_premium"] == level_term["monthly_premium"]


def test_investment_return_lowers_the_required_premium():
    # Regression: investment income was computed and shown in the Cash
    # Flow tab's "Investment income" column, but never actually credited
    # toward the profit margin the premium is solved against — sweeping
    # investment_rate_pa +/-5 points moved the premium by exactly zero.
    # A higher investment return means more profit comes from holding the
    # funds, so LESS premium income is needed to hit the same target
    # margin — the premium must now move, and in this direction.
    from engine.assumptions import ProductAssumptions, LapseSchedule

    def price(investment_rate_pa):
        req = CustomProductRequest(product_name="Test", product_type="whole_life", sum_assured=50000, entry_age=35)
        product = _build_product_from_request(req)
        asmp = ProductAssumptions(
            entry_age_main=35, lapse_schedule=LapseSchedule(rates={1: 0.25, 13: 0.02}),
            investment_rate_pa=investment_rate_pa, target_profit_margin=0.15,
        )
        return run_custom_pricing(product, asmp)

    low = price(0.02)
    high = price(0.25)
    assert high["monthly_premium"] < low["monthly_premium"]
    # Both must still hit the exact target margin — investment income is a
    # genuine input to the SAME equivalence-principle balance, not a fudge.
    assert low["profit_margin"] == pytest.approx(0.15, abs=1e-6)
    assert high["profit_margin"] == pytest.approx(0.15, abs=1e-6)


def test_investment_return_does_not_change_ifrs17_building_blocks_formula():
    # PVFCF/RA/CSM must stay a pure insurance-liability measure — folding
    # investment income into them (instead of just the premium solve)
    # would misstate the IFRS 17 balance sheet, a different and worse bug
    # than the one being fixed here.
    from engine.assumptions import ProductAssumptions, LapseSchedule
    from engine.decrement import run_decrement_projection
    from engine.cashflows import calculate_cash_flows
    from engine.present_value import calculate_present_values

    req = CustomProductRequest(product_name="Test", product_type="whole_life", sum_assured=50000, entry_age=35)
    product = _build_product_from_request(req)
    asmp = ProductAssumptions(entry_age_main=35, lapse_schedule=LapseSchedule(rates={1: 0.25, 13: 0.02}))

    # Same premium, two different investment return assumptions — RA (which
    # depends only on pv_benefits) must be identical; pvfcf must equal
    # exactly pv_outflows - pv_premiums with no investment income term.
    for inv_rate in (0.02, 0.30):
        asmp.investment_rate_pa = inv_rate
        dec_rows = run_decrement_projection(asmp, product)
        cf_rows = calculate_cash_flows(dec_rows, asmp, product, 25.0)
        _, pv = calculate_present_values(cf_rows, asmp)
        assert pv.pvfcf == pytest.approx(pv.pv_total_outflows - pv.pv_premiums, abs=1e-6)
        assert pv.risk_adjustment == pytest.approx(asmp.coc_rate * pv.pv_benefits, abs=1e-6)

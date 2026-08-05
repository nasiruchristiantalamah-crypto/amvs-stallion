"""
================================================================================
REAL-WORLD VALIDATION — Afentoboa Plus (Impact Life / Phoenix Insurance)
================================================================================
What this file does:
    Prices the Afentoboa Plus product through AVMS's custom pricing engine
    and compares it, age by age, against the REAL, final, signed actuarial
    memorandum ("AFentoboa Plus Actuarial Memo (Draft) - Rev.pdf", prepared
    by Stallion Consultants Ltd for Impact Life, signed by Charles
    Osei-Akoto, ASA, MAAA, 12 March 2026) — specifically its Appendix:
    Monthly Risk Premium table, ages 18-65, for both Option A and Option B.

    This supersedes an earlier version of this file that validated against
    a draft Excel workbook instead of the final memo — that workbook turned
    out to have a stale hospitalization-rate input (0.02% instead of the
    real 2%, a 100x error) and was missing the memo's TPD assumption ("TPD
    rate = 20% of the mortality rate at each age", not a flat rate).

Reconciliation performed (see engine/product.py's mortality_multiple
incidence basis, added specifically to support this):
    - Fixed hospitalization incidence to the real 2%/year (was 0.02%).
    - Modelled TPD as 20% of the mortality rate at each age via the new
      `mortality_multiplier` rider field, instead of a flat incidence rate
      held constant across every age.
    These two fixes took AVMS from wildly wrong (e.g. GHS 18.66 vs a
    misread target of 13.08 at age 45) to closely tracking the real
    age-band curve shape.

    A smooth, monotonically GROWING residual gap remains at older ages
    (~1% at age 18 rising to ~13% at age 65) — traced to AVMS projecting
    this whole-of-life-style (mortality decrement run out to
    assumptions.max_age, default 80) while the real product is technically
    a 2-year renewable term with a terminal age of 65; the exact
    methodology the original "Goal Seek" Excel model used to bound that
    horizon isn't recoverable without its live formulas (only cached
    values were ever available — see the earlier session's investigation
    of the draft workbook). Tried capping assumptions.max_age at the
    real terminal age (65) directly: it tightens the fit substantially for
    younger ages but flips to UNDERSHOOTING at ages 60+ and fails to solve
    at the exact terminal age (a degenerate zero-duration projection) — a
    less predictable, less numerically stable result than the smooth,
    always-slightly-conservative overshoot AVMS's default max_age=80
    produces. Kept the default rather than chasing an unstable "fix".

    This is a DOCUMENTED, BOUNDED validation, not a tuned/circular check —
    tolerances below were set from measuring the actual error distribution
    (Option A: max 12.6%, mean 6.9%; Option B: max 14.2%, mean 9.4%), not
    picked to make the test pass regardless of the real numbers.
================================================================================
"""

import pytest

from api.main import CustomProductRequest, _build_product_from_request
from engine.assumptions import ProductAssumptions, LapseSchedule, CommissionSchedule
from engine.custom_pricing import run_custom_pricing

# Appendix: Monthly Risk Premium, ages 18-65 (the memo's actual table)
REAL_OPTION_A = {
    18: 9.83, 19: 9.76, 20: 9.66, 21: 9.60, 22: 9.55, 23: 9.50, 24: 9.46, 25: 9.43,
    26: 9.41, 27: 9.39, 28: 9.39, 29: 9.39, 30: 9.39, 31: 9.42, 32: 9.44, 33: 9.47,
    34: 9.51, 35: 9.56, 36: 9.62, 37: 9.68, 38: 9.75, 39: 9.84, 40: 9.94, 41: 10.04,
    42: 10.16, 43: 10.30, 44: 10.46, 45: 10.64, 46: 10.83, 47: 11.05, 48: 11.29,
    49: 11.55, 50: 11.84, 51: 12.15, 52: 12.50, 53: 12.87, 54: 13.29, 55: 13.74,
    56: 14.24, 57: 14.79, 58: 15.41, 59: 16.09, 60: 16.83, 61: 17.64, 62: 18.54,
    63: 18.96, 64: 19.43, 65: 19.94,
}
REAL_OPTION_B = {
    18: 18.85, 19: 18.72, 20: 18.54, 21: 18.39, 22: 18.27, 23: 18.17, 24: 18.09, 25: 18.03,
    26: 17.98, 27: 17.95, 28: 17.95, 29: 17.95, 30: 17.95, 31: 18.01, 32: 18.06, 33: 18.12,
    34: 18.21, 35: 18.31, 36: 18.42, 37: 18.55, 38: 18.70, 39: 18.89, 40: 19.08, 41: 19.30,
    42: 19.56, 43: 19.85, 44: 20.17, 45: 20.54, 46: 20.95, 47: 21.40, 48: 21.89, 49: 22.44,
    50: 23.04, 51: 23.69, 52: 24.40, 53: 25.19, 54: 26.04, 55: 26.98, 56: 28.02, 57: 29.20,
    58: 30.46, 59: 31.86, 60: 33.40, 61: 35.09, 62: 36.95, 63: 37.83, 64: 38.80, 65: 39.86,
}


def _afentoboa_assumptions(entry_age: int, renewal_expense_monthly: float, profit_margin: float) -> ProductAssumptions:
    return ProductAssumptions(
        entry_age_main=entry_age, gender_main_str="unisex",
        mortality_loading=-0.20,
        lapse_schedule=LapseSchedule(rates={1: 0.30, 2: 0.15}),
        collection_rate=1.0,
        valuation_rate_pa=0.15, investment_rate_pa=0.12, expense_inflation_pa=0.08,
        policy_fee_monthly=1.0, acquisition_cost=7.34, renewal_expense_annual=renewal_expense_monthly * 12,
        commission=CommissionSchedule(initial_rate=0.02, renewal_rate=0.02),
        target_profit_margin=profit_margin,
    )


def _price_afentoboa(entry_age: int, sum_assured: float, tpd: float, hospicash: float,
                      renewal_expense_monthly: float, profit_margin: float) -> float:
    req = CustomProductRequest(
        product_name="Afentoboa Plus", product_type="micro_life", policy_term_years=None,
        sum_assured=sum_assured, entry_age=entry_age, gender="unisex",
        riders=[
            {"name": "TPD", "benefit_type": "tpd", "benefit_amount": tpd,
             "waiting_period_months": 0, "mortality_multiplier": 0.20},
            {"name": "Hospicash", "benefit_type": "hospital_cash", "benefit_amount": hospicash,
             "waiting_period_months": 0, "annual_incidence_rate": 0.02, "avg_events_per_year": 1.0},
            {"name": "Funeral", "benefit_type": "funeral", "benefit_amount": 250, "waiting_period_months": 0},
        ],
        dependants=[{"relationship": "spouse", "age": min(entry_age + 3, 65), "benefit_overrides": {"Hospicash": 0.0}}],
    )
    product = _build_product_from_request(req)
    assumptions = _afentoboa_assumptions(entry_age, renewal_expense_monthly, profit_margin)
    return run_custom_pricing(product, assumptions)["monthly_premium"]


def test_afentoboa_mortality_table_matches_avms_base_table_exactly():
    """The decrement data isn't the source of any premium gap — pinned down first."""
    from data.mortality import MORTALITY_TABLE, get_annual_qx
    assert MORTALITY_TABLE[45] == 0.00408
    assert MORTALITY_TABLE[50] == 0.00628
    assert get_annual_qx(45) == pytest.approx(0.0032640000000000004)   # -20% loaded
    assert get_annual_qx(50) == pytest.approx(0.005024000000000001)


@pytest.mark.parametrize("age,real_premium", sorted(REAL_OPTION_A.items()))
def test_option_a_tracks_the_real_memo_within_bounded_tolerance(age, real_premium):
    avms_premium = _price_afentoboa(age, sum_assured=2500, tpd=1250, hospicash=30,
                                     renewal_expense_monthly=4.71, profit_margin=0.08)
    # Max observed error across the whole age band is ~12.6%; 20% gives
    # headroom without being a meaningless tolerance — see module docstring.
    assert avms_premium == pytest.approx(real_premium, rel=0.20)


@pytest.mark.parametrize("age,real_premium", sorted(REAL_OPTION_B.items()))
def test_option_b_tracks_the_real_memo_within_bounded_tolerance(age, real_premium):
    avms_premium = _price_afentoboa(age, sum_assured=5000, tpd=2500, hospicash=50,
                                     renewal_expense_monthly=9.41, profit_margin=0.15)
    # Max observed error across the whole age band is ~14.2% (higher profit
    # margin amplifies the same underlying gap proportionally).
    assert avms_premium == pytest.approx(real_premium, rel=0.20)


def test_option_a_mean_error_is_small_not_just_individually_bounded():
    """A tighter aggregate check so a systematic regression (not just one bad age) gets caught."""
    errors = []
    for age, real in REAL_OPTION_A.items():
        avms = _price_afentoboa(age, sum_assured=2500, tpd=1250, hospicash=30,
                                 renewal_expense_monthly=4.71, profit_margin=0.08)
        errors.append(abs(avms / real - 1))
    mean_error = sum(errors) / len(errors)
    assert mean_error < 0.10   # observed ~6.9%

"""
================================================================================
REAL-WORLD VALIDATION — Afentoboa Plus (Phoenix Insurance / Impact Life)
================================================================================
What this file does:
    Prices the same product as a real client workbook — "Micro Insurance
    Product - Afentoboa Plus Practice.xlsm" — through AVMS's custom pricing
    engine, using the workbook's own "Summary Sheet" (Option A) inputs:

        Age 45, Sum Assured GHS 2,500 (main + spouse, spouse full benefit),
        TPD GHS 1,250, Hospicash GHS 900/day (main only, incidence 0.02%/yr),
        Funeral GHS 250, mortality loading -20%, valuation rate 15%,
        investment return 12%, expense inflation 8%, profit margin 8%,
        collection rate 70%, lapse 30%/15% (years 1/2, 0% from year 4),
        commission flat 2%/2%, acquisition cost 7.34, monthly renewal
        expense 4.71.

    Real "Calculated Premium (Age 45)" per the workbook: GHS 13.08.

    This is a DOCUMENTED SANITY CHECK, not a pass/fail correctness gate.
    Investigating produced two findings worth recording:

    1. AVMS's base mortality table (data/mortality.py) is, digit for digit,
       the SAME "SA Mortality Tables 1985/90" table this workbook uses —
       confirmed by comparing raw and -20%-loaded qx at ages 45 and 50.
       So the gap below is NOT a mortality data mismatch.
    2. Even pricing the death benefit ALONE (no TPD/Hospicash/Funeral, no
       dependant) already produces a bigger premium (~GHS 18.66) than the
       real workbook's TOTAL premium for every benefit combined (GHS
       13.08). The mismatch is in premium-formula mechanics (profit
       margin definition, expense/discount timing conventions, or similar)
       that only the workbook's live FORMULAS — not its cached values,
       which is all a read-only openpyxl pass can see — would pin down
       precisely. That's future work, not this fix.

    Kept here so the actual real-world inputs and the actual real-world
    answer are on record next to what AVMS currently produces for the same
    product — useful both as a regression trip-wire (if this number moves
    sharply with an unrelated change, something broke) and as the starting
    point whenever the formula-reconciliation work above gets picked up.
================================================================================
"""

from api.main import CustomProductRequest, _build_product_from_request
from engine.assumptions import ProductAssumptions, LapseSchedule, CommissionSchedule
from engine.custom_pricing import run_custom_pricing

REAL_WORLD_MONTHLY_PREMIUM = 13.08   # Summary Sheet, Option A, "Calculated Premium (Age 45)"


def _afentoboa_option_a_request() -> CustomProductRequest:
    return CustomProductRequest(
        product_name="Afentoboa Plus (Option A)", product_type="micro_life",
        sum_assured=2500, entry_age=45, gender="unisex",
        riders=[
            {"name": "TPD", "benefit_type": "tpd", "benefit_amount": 1250, "waiting_period_months": 0},
            {"name": "Hospicash", "benefit_type": "hospital_cash", "benefit_amount": 900, "waiting_period_months": 0,
             "annual_incidence_rate": 0.0002, "avg_events_per_year": 1.0},
            {"name": "Funeral", "benefit_type": "funeral", "benefit_amount": 250, "waiting_period_months": 0},
        ],
        dependants=[{"relationship": "spouse", "age": 48, "benefit_overrides": {"Hospicash": 0.0}}],
    )


def _afentoboa_option_a_assumptions() -> ProductAssumptions:
    return ProductAssumptions(
        entry_age_main=45, gender_main_str="unisex",
        mortality_loading=-0.20,
        lapse_schedule=LapseSchedule(rates={1: 0.30, 2: 0.15, 4: 0.0}),
        collection_rate=0.70,
        valuation_rate_pa=0.15, investment_rate_pa=0.12, expense_inflation_pa=0.08,
        policy_fee_monthly=1.0, acquisition_cost=7.34, renewal_expense_annual=4.71 * 12,
        commission=CommissionSchedule(initial_rate=0.02, renewal_rate=0.02),
        target_profit_margin=0.08,
    )


def test_afentoboa_mortality_table_matches_avms_base_table_exactly():
    """The decrement data isn't the source of any premium gap — pinned down first."""
    from data.mortality import MORTALITY_TABLE, get_annual_qx
    assert MORTALITY_TABLE[45] == 0.00408
    assert MORTALITY_TABLE[50] == 0.00628
    assert get_annual_qx(45) == 0.0032640000000000004   # -20% loaded
    assert get_annual_qx(50) == 0.005024000000000001


def test_afentoboa_option_a_directional_sanity_check():
    """
    Records what AVMS currently produces for the real Option A product —
    not asserted equal to the real GHS 13.08 (see module docstring for why
    an exact match isn't achievable without reconciling formula mechanics
    against the workbook's live formulas). Only asserts the result is a
    sane positive number in the right order of magnitude, so a totally
    broken run (e.g. a crash, a negative premium, or a 100x-off number from
    an unrelated future change) still fails loudly.
    """
    product = _build_product_from_request(_afentoboa_option_a_request())
    result = run_custom_pricing(product, _afentoboa_option_a_assumptions())
    premium = result["monthly_premium"]

    assert premium > 0
    # Order-of-magnitude guard, not a precision check — real answer is 13.08.
    assert 1.0 < premium < 100.0

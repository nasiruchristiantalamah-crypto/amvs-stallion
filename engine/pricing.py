"""
================================================================================
PREMIUM SOLVER
================================================================================
What this file does:
    Finds the monthly premium that achieves the target profit margin.

    In Excel, you used Goal Seek:
        "Set cell BF8 (profit margin) to 15%
         by changing cell BE3 (monthly premium)"

    This Python function does the exact same thing mathematically,
    but automatically, for any age, any product, any tier.

    This is what makes the system powerful:
        - In Excel: run Goal Seek manually for each age = slow
        - In Python: loop through all ages, solve automatically = seconds

    Algorithm used: Brent's Method (scipy.optimize.brentq)
    This is the industry-standard root-finding algorithm.
    It is guaranteed to find the solution if one exists within the bounds.

How pricing works:
    1. Start with a trial premium (e.g. GHS 10)
    2. Run the full projection: decrement → cash flows → present values
    3. Check: is profit margin = target (15%)?
    4. If profit margin too LOW → premium is too low → increase it
    5. If profit margin too HIGH → premium is too high → decrease it
    6. Repeat until profit margin = target exactly
    7. The premium at that point is the answer

Excel equivalent:
    Data → What-If Analysis → Goal Seek
    Set cell: BF8
    To value: 0.15
    By changing cell: BE3
================================================================================
"""

from typing import Optional
import sys

# We use scipy for the numerical solver
# This is the Python equivalent of Excel's Goal Seek algorithm
try:
    from scipy.optimize import brentq
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

from engine.assumptions import ProductAssumptions
from engine.product import Product
from engine.decrement import run_decrement_projection
from engine.cashflows import calculate_cash_flows
from engine.present_value import calculate_present_values, PVResults


def calculate_profit_margin(
    monthly_premium:    float,
    assumptions:        ProductAssumptions,
    product:             Product,
) -> float:
    """
    Calculate the profit margin for a given monthly premium.

    This is the function that the solver calls repeatedly until
    the profit margin equals the target.

    Parameters:
        monthly_premium : The premium to test (GHS per month)
        assumptions     : Pricing/valuation assumptions
        product          : Product structure (riders, dependants)

    Returns:
        Profit margin as a decimal (e.g. 0.15 = 15%)

    Excel equivalent:
        Manually changing BE3 and reading BF8.
    """
    # Run the full projection with this premium
    dec_rows = run_decrement_projection(assumptions, product)
    cf_rows  = calculate_cash_flows(dec_rows, assumptions, product, monthly_premium)
    _, pv    = calculate_present_values(cf_rows, assumptions)
    return pv.profit_margin


def solve_premium(
    assumptions:            ProductAssumptions,
    product:                 Product,
    target_margin:          Optional[float] = None,
    min_premium:            float = 0.01,
    max_premium:            float = 100_000.0,
    tolerance:              float = 1e-8,
) -> tuple[float, PVResults]:
    """
    Find the monthly premium that achieves the target profit margin.

    This is the Python equivalent of running Goal Seek in Excel.

    Parameters:
        assumptions   : Pricing/valuation assumptions
        product       : Product structure (riders, dependants)
        target_margin : Target profit margin (default: from assumptions = 15%)
        min_premium   : Lower bound for search (GHS 0.01)
        max_premium   : Upper bound for search (GHS 100,000)
        tolerance     : How precisely to find the solution (1e-8 = very precise)

    Returns:
        (solved_premium, pv_results)
            solved_premium : Monthly premium in GHS
            pv_results     : Full PV summary at the solved premium

    Example:
        assumptions = load_assumptions("pic", "whole_life_tier1")
        product     = load_product("pic", "whole_life_tier1")
        premium, results = solve_premium(assumptions, product)
        print(f"Monthly premium for age 35: GHS {premium:.2f}")
        print(f"Profit margin: {results.profit_margin:.1%}")
    """
    if target_margin is None:
        target_margin = assumptions.target_profit_margin

    if not SCIPY_AVAILABLE:
        # Fallback: simple bisection if scipy not installed
        return _bisection_solve(assumptions, product, target_margin, min_premium, max_premium, tolerance)

    # ── Define the function to solve ───────────────────────────────────────
    # We want: profit_margin(premium) - target_margin = 0
    # brentq finds the root of this function (where it crosses zero)
    def objective(premium: float) -> float:
        return calculate_profit_margin(premium, assumptions, product) - target_margin

    # ── Check that a solution exists in the range ──────────────────────────
    # For brentq to work, objective(min) and objective(max) must have
    # opposite signs (one positive, one negative)
    try:
        margin_at_min = calculate_profit_margin(min_premium, assumptions, product)
        margin_at_max = calculate_profit_margin(max_premium, assumptions, product)

        obj_at_min = margin_at_min - target_margin
        obj_at_max = margin_at_max - target_margin

        if obj_at_min * obj_at_max > 0:
            # No sign change — solution might not exist in this range
            # Try with a wider range
            max_premium = max_premium * 10
            margin_at_max = calculate_profit_margin(max_premium, assumptions, product)
            obj_at_max = margin_at_max - target_margin

    except Exception as e:
        raise ValueError(f"Failed to evaluate profit margin: {e}")

    # ── Run the solver ─────────────────────────────────────────────────────
    try:
        solved_premium = brentq(
            objective,
            min_premium,
            max_premium,
            xtol=tolerance,
            maxiter=200,
        )
    except ValueError as e:
        raise ValueError(
            f"Premium solver failed for age {assumptions.entry_age_main}. "
            f"Target margin {target_margin:.1%} may not be achievable. "
            f"Error: {e}"
        )

    # ── Run one final projection with the solved premium ───────────────────
    # to get the complete PV results
    dec_rows = run_decrement_projection(assumptions, product)
    cf_rows  = calculate_cash_flows(dec_rows, assumptions, product, solved_premium)
    _, pv    = calculate_present_values(cf_rows, assumptions)

    return solved_premium, pv


def _bisection_solve(
    assumptions:    ProductAssumptions,
    product:         Product,
    target_margin:  float,
    lo:             float,
    hi:             float,
    tolerance:      float,
    max_iter:       int = 200,
) -> tuple[float, PVResults]:
    """
    Simple bisection fallback if scipy is not available.
    Slower than Brent's method but always works.
    """
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        margin_mid = calculate_profit_margin(mid, assumptions, product)

        if abs(margin_mid - target_margin) < tolerance or (hi - lo) / 2 < tolerance:
            break

        if (margin_mid - target_margin) * (calculate_profit_margin(lo, assumptions, product) - target_margin) < 0:
            hi = mid
        else:
            lo = mid

    solved = (lo + hi) / 2
    dec_rows = run_decrement_projection(assumptions, product)
    cf_rows  = calculate_cash_flows(dec_rows, assumptions, product, solved)
    _, pv    = calculate_present_values(cf_rows, assumptions)
    return solved, pv


def build_rate_table(
    assumptions_template:   ProductAssumptions,
    product:                Product,
    age_range:              range = range(18, 71),
    target_margin:          Optional[float] = None,
    verbose:                bool = True,
) -> dict:
    """
    Generate a complete premium rate table for a range of entry ages.

    This is the Python equivalent of your Excel VBA macro
    GeneratePremiumRates() which looped through ages 18-70,
    ran Goal Seek for each, and filled the Rate_Table sheet.

    Parameters:
        assumptions_template : Base assumptions (entry_age will be updated)
        age_range            : Ages to price (default 18-70)
        target_margin        : Profit margin target (default from assumptions)
        verbose              : Print progress to console

    Returns:
        dict with age as key and results as value:
        {
            35: {
                "monthly_premium": 35.70,
                "annual_premium":  428.40,
                "profit_margin":   0.15,
                "pv_premiums":     ...,
                "csm":             ...,
                ...
            },
            ...
        }

    Usage:
        assumptions = tier1_whole_life()
        table = build_rate_table(assumptions, age_range=range(18, 71))
        for age, result in table.items():
            print(f"Age {age}: GHS {result['monthly_premium']:.2f}/month")
    """
    import copy

    results = {}
    total = len(age_range)

    for i, age in enumerate(age_range, 1):
        if verbose:
            print(f"  Pricing age {age}... ({i}/{total})", end="\r")

        # Copy assumptions and update entry age
        asmp = copy.deepcopy(assumptions_template)
        asmp.entry_age_main = age

        try:
            premium, pv = solve_premium(asmp, product, target_margin)
            results[age] = {
                "monthly_premium":      round(premium, 2),
                "annual_premium":       round(premium * 12, 2),
                "profit_margin":        round(pv.profit_margin, 4),
                "pv_premiums":          round(pv.pv_premiums, 2),
                "pv_benefits":          round(pv.pv_benefits, 2),
                "pv_expenses":          round(pv.pv_expenses, 2),
                "pv_profits":           round(pv.pv_profits, 2),
                "csm_at_inception":     round(pv.csm_at_inception, 2),
                "risk_adjustment":      round(pv.risk_adjustment, 2),
                "lrc_total":            round(pv.lrc_total, 2),
                "is_onerous":           pv.is_onerous,
                "monthly_premium_usd":  round(premium / asmp.fx_rate_ghs_usd, 2),
            }
        except ValueError as e:
            if verbose:
                print(f"\n  Warning: Could not price age {age}: {e}")
            results[age] = {"error": str(e)}

    if verbose:
        print(f"\n  Rate table complete: {len([r for r in results.values() if 'error' not in r])} ages priced successfully.")

    return results

"""
================================================================================
DECREMENT MODEL
================================================================================
What this file does:
    Projects how many lives remain in force month by month, from policy
    inception to policy expiry — for the main life AND any number of
    dependants (engine/product.py's Product.dependants), not just one.

    lx = number of lives in force at the START of month x
    dx = number of deaths during month x  (lx x monthly_qx)
    wx = number of lapses during month x  (lx x monthly_lapse_rate)
    lx+1 = lx - dx - wx

Key design point:
    Every covered life (main + each dependant) gets its own LifeDecrement
    path, keyed by that dependant's index in product.dependants. Adding a
    third or fourth dependant to a product needs no code change here.
================================================================================
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from engine.assumptions import ProductAssumptions
from engine.product import Product, Dependant
from data.mortality import get_annual_qx, annual_to_monthly_qx


# ── One life's decrement path for one month ─────────────────────────────────

@dataclass
class LifeDecrement:
    """Decrement values for one covered life (main or a dependant) for one month."""
    age:        float
    lx:         float   # Lives in force at start of month
    qx_annual:  float
    qx_monthly: float
    dx:         float   # Expected deaths this month
    wx:         float   # Expected lapses this month
    lx_end:     float   # Lives in force at end of month


# ── One month of the decrement projection ────────────────────────────────────

@dataclass
class DecrementRow:
    """One row in the decrement projection table — one month."""
    month:       int
    policy_year: int
    main:        LifeDecrement
    dependants:  Dict[int, LifeDecrement] = field(default_factory=dict)   # keyed by index into product.dependants


def get_projection_months(assumptions: ProductAssumptions, product: Product) -> int:
    """
    Number of monthly projection steps: from entry age to coverage end
    (policy_term_years for a term product, or max_age for whole life).
    """
    end_age = product.coverage_end_age(assumptions.entry_age_main, assumptions.max_age)
    return (end_age - assumptions.entry_age_main) * 12


def run_decrement_projection(
    assumptions: ProductAssumptions,
    product:     Product,
) -> List[DecrementRow]:
    """
    Project the decrement table for the full policy lifetime, for the main
    life and every dependant on the product.

    How it works (month by month), per covered life:
        1. Start with lx = 1.0 (representing one policy / 100% of a cohort)
        2. Look up annual qx from the mortality table for the current age
        3. Convert annual qx to monthly qx
        4. Deaths:  dx = lx x monthly_qx
        5. Lapses:  wx = lx x monthly_lapse_rate  (applied after deaths, on survivors)
        6. End-of-month lives: lx_end = lx - dx - wx
        7. Age increases by 1 every 12 months
    """
    rows: List[DecrementRow] = []

    n_months = get_projection_months(assumptions, product)

    lx_main = 1.0
    lx_deps: Dict[int, float] = {i: 1.0 for i in range(len(product.dependants))}

    for month in range(1, n_months + 1):
        years_elapsed = (month - 1) // 12
        age_main      = assumptions.entry_age_main + years_elapsed
        policy_year   = years_elapsed + 1

        end_age = product.coverage_end_age(assumptions.entry_age_main, assumptions.max_age)
        if age_main > end_age:
            break

        monthly_lapse = assumptions.lapse_schedule.get_monthly_rate(policy_year)

        # ── Main life ────────────────────────────────────────────────────
        annual_qx_main  = get_annual_qx(age=age_main, gender=assumptions.gender_main_str,
                                         loading=assumptions.mortality_loading)
        monthly_qx_main = annual_to_monthly_qx(annual_qx_main)
        dx_main = lx_main * monthly_qx_main
        wx_main = (lx_main - dx_main) * monthly_lapse
        lx_main_end = max(0.0, lx_main - dx_main - wx_main)

        main_dec = LifeDecrement(
            age=age_main, lx=lx_main, qx_annual=annual_qx_main, qx_monthly=monthly_qx_main,
            dx=dx_main, wx=wx_main, lx_end=lx_main_end,
        )

        # ── Dependants ───────────────────────────────────────────────────
        dep_decs: Dict[int, LifeDecrement] = {}
        for i, dependant in enumerate(product.dependants):
            lx_dep = lx_deps.get(i, 0.0)
            if lx_dep <= 1e-10:
                continue
            dep_age = product.get_dependant_age(dependant, assumptions.entry_age_main) + years_elapsed
            annual_qx_dep  = get_annual_qx(age=dep_age, loading=assumptions.mortality_loading)
            monthly_qx_dep = annual_to_monthly_qx(annual_qx_dep)
            dx_dep = lx_dep * monthly_qx_dep
            wx_dep = (lx_dep - dx_dep) * monthly_lapse
            lx_dep_end = max(0.0, lx_dep - dx_dep - wx_dep)

            dep_decs[i] = LifeDecrement(
                age=dep_age, lx=lx_dep, qx_annual=annual_qx_dep, qx_monthly=monthly_qx_dep,
                dx=dx_dep, wx=wx_dep, lx_end=lx_dep_end,
            )
            lx_deps[i] = lx_dep_end

        rows.append(DecrementRow(month=month, policy_year=policy_year, main=main_dec, dependants=dep_decs))

        lx_main = lx_main_end
        if lx_main <= 1e-10:
            break

    return rows


def summarise_decrement(rows: List[DecrementRow]) -> dict:
    """
    Produce a summary of the decrement projection — quick checks and
    NIC reporting.
    """
    if not rows:
        return {}

    total_deaths = sum(r.main.dx for r in rows)
    total_lapses = sum(r.main.wx for r in rows)
    final_lx     = rows[-1].main.lx_end

    first_year_rows = [r for r in rows if r.month <= 12]
    first_year_lapses = sum(r.main.wx for r in first_year_rows)

    return {
        "total_months":       len(rows),
        "total_deaths_main":  round(total_deaths, 6),
        "total_lapses_main":  round(total_lapses, 6),
        "survival_rate":      round(final_lx, 6),
        "first_year_lapse":   round(first_year_lapses, 4),
        "coverage_units":     sum(r.main.lx for r in rows),
    }

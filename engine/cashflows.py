"""
================================================================================
CASH FLOWS MODULE
================================================================================
What this file does:
    Takes the decrement projection (who is still alive each month) and
    calculates all the money flows:

    INFLOWS:  Premium income (gross/net), investment income on reserves
    OUTFLOWS: One benefit line PER RIDER on the product (whatever riders
              the client has configured — death, TPD, hospitalization,
              critical illness, funeral, education, income protection, or
              anything else), plus commissions and expenses.

    NET CASH FLOW = Inflows - Outflows

    Key design point: benefit outflows are no longer four hardcoded lines.
    Each rider in product.riders contributes its own benefit_lines entry,
    computed either from the decrement table's dx (rider.incidence_basis
    == "mortality") or from an explicit annual incidence rate (everything
    else — TPD, hospitalization, critical illness, etc., generalizing what
    used to be separately hardcoded per benefit type). Adding a new rider
    to a product's YAML needs no code change here.
================================================================================
"""

from dataclasses import dataclass, field
from typing import Dict, List

from engine.assumptions import ProductAssumptions
from engine.product import Product
from engine.decrement import DecrementRow


# ── One month of cash flows ───────────────────────────────────────────────────

@dataclass
class CashFlowRow:
    """All cash flows for one month of the projection."""
    month:              int
    policy_year:        int
    age_main:           float

    # ── INFLOWS ──────────────────────────────────────────────────────────
    gross_premium:      float   # lx x monthly_premium
    net_premium:        float   # gross x collection_rate
    investment_income:  float

    # ── BENEFIT OUTFLOWS ─────────────────────────────────────────────────
    benefit_lines:      Dict[str, float] = field(default_factory=dict)   # keyed by rider name
    total_benefits:     float = 0.0                                       # sum of benefit_lines

    # ── EXPENSE OUTFLOWS ─────────────────────────────────────────────────
    commission:         float = 0.0
    acquisition_cost:   float = 0.0
    renewal_expense:    float = 0.0
    claims_admin:       float = 0.0
    total_expenses:     float = 0.0

    # ── NET POSITION ─────────────────────────────────────────────────────
    net_cashflow:        float = 0.0
    cumulative_cashflow: float = 0.0


def _rider_active(rider, month: int) -> bool:
    if month <= rider.waiting_period_months:
        return False
    if rider.max_duration_months is not None and month > rider.max_duration_months:
        return False
    return True


def calculate_cash_flows(
    decrement_rows:     List[DecrementRow],
    assumptions:        ProductAssumptions,
    product:             Product,
    monthly_premium:    float,
) -> List[CashFlowRow]:
    """
    Calculate the complete cash flow projection for every month, driven by
    whatever riders and dependants are on `product`.

    Parameters:
        decrement_rows  : Output from run_decrement_projection()
        assumptions     : Pricing/valuation assumptions
        product         : Product structure (riders, dependants)
        monthly_premium : Monthly premium per policy (GHS) — the variable
                           the premium solver changes

    Returns:
        List of CashFlowRow objects, one per month
    """
    rows: List[CashFlowRow] = []
    cumulative_cf = 0.0

    for dec in decrement_rows:
        month    = dec.month
        pol_year = dec.policy_year
        lx       = dec.main.lx
        age_main = dec.main.age

        inv_rate     = assumptions.investment_rate_monthly
        exp_inf_rate = assumptions.expense_inflation_monthly

        # ── GROSS / NET PREMIUM ─────────────────────────────────────────
        gross_prem = lx * monthly_premium
        net_prem   = gross_prem * assumptions.collection_rate

        # ── COMMISSION ───────────────────────────────────────────────────
        comm_rate  = assumptions.commission.get_rate_for_policy_year(pol_year)
        commission = gross_prem * comm_rate

        # ── EXPENSES ─────────────────────────────────────────────────────
        if month == 1:
            acq_cost    = lx * assumptions.acquisition_cost
            renewal_exp = lx * (assumptions.policy_fee_monthly + assumptions.renewal_expense_monthly)
        else:
            acq_cost = 0.0
            inflation_factor = (1 + exp_inf_rate) ** (month - 1)
            renewal_exp = lx * (assumptions.policy_fee_monthly + assumptions.renewal_expense_monthly) * inflation_factor

        # ── BENEFIT LINES — one per rider, generic ───────────────────────
        benefit_lines: Dict[str, float] = {}
        expected_events = 0.0   # drives claims_admin cost

        for rider in product.riders:
            if not _rider_active(rider, month):
                benefit_lines[rider.name] = 0.0
                continue

            benefit_mult = rider.get_benefit_multiplier(pol_year)   # 1.0 unless a decreasing-term schedule is set

            if rider.incidence_basis == "mortality":
                amount = dec.main.dx * rider.benefit_main * benefit_mult
                expected_events += dec.main.dx
                for i, dependant in enumerate(product.dependants):
                    dep_dec = dec.dependants.get(i)
                    if dep_dec is None or dependant.benefit_multiplier == 0:
                        continue
                    amount += dep_dec.dx * rider.benefit_dependant * benefit_mult * dependant.benefit_multiplier
                    expected_events += dep_dec.dx
            else:
                monthly_incidence = rider.annual_incidence_rate / 12
                amount = lx * monthly_incidence * rider.benefit_main * benefit_mult * rider.avg_events_per_year
                expected_events += lx * monthly_incidence
                for i, dependant in enumerate(product.dependants):
                    dep_dec = dec.dependants.get(i)
                    if dep_dec is None or dependant.benefit_multiplier == 0:
                        continue
                    dep_lx = dep_dec.lx
                    amount += dep_lx * monthly_incidence * rider.benefit_dependant * benefit_mult * dependant.benefit_multiplier * rider.avg_events_per_year
                    expected_events += dep_lx * monthly_incidence

            benefit_lines[rider.name] = amount

        # ── MATURITY BENEFIT — paid to survivors at the end of specific ──
        # policy years (product.maturity_benefits) — what makes a term
        # product an endowment/educational endowment. Weighted by lx_end
        # (survivors after this month's deaths/lapses), since anyone who
        # died this month already received the death benefit above instead.
        if month % 12 == 0 and pol_year in product.maturity_benefits:
            benefit_lines["Maturity Benefit"] = dec.main.lx_end * product.maturity_benefits[pol_year]

        total_ben = sum(benefit_lines.values())
        claims_admin = expected_events * assumptions.claims_admin_cost
        total_expenses = acq_cost + renewal_exp + claims_admin

        # ── NET CASH FLOW ─────────────────────────────────────────────────
        net_cf = net_prem - total_ben - commission - total_expenses
        inv_income = net_cf * inv_rate
        cumulative_cf += (net_cf + inv_income)

        rows.append(CashFlowRow(
            month               = month,
            policy_year         = pol_year,
            age_main            = age_main,
            gross_premium       = gross_prem,
            net_premium         = net_prem,
            investment_income   = inv_income,
            benefit_lines       = benefit_lines,
            total_benefits      = total_ben,
            commission          = commission,
            acquisition_cost    = acq_cost,
            renewal_expense     = renewal_exp,
            claims_admin        = claims_admin,
            total_expenses      = total_expenses,
            net_cashflow        = net_cf,
            cumulative_cashflow = cumulative_cf,
        ))

    return rows

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

from engine.assumptions import ProductAssumptions, FixedScaleCommission
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
    benefit_lines:      Dict[str, float] = field(default_factory=dict)   # keyed by rider name — every life's cost for that rider, combined
    total_benefits:     float = 0.0                                       # sum of benefit_lines

    # Same benefit cost, but split out by WHICH covered life it belongs to
    # — {"Main Life": {rider_name: amount}, "Spouse": {...}, ...}. Additive
    # only: benefit_lines above is unchanged, and summing every life's
    # entry for a given rider reproduces benefit_lines[rider] exactly —
    # this is that same total attributed to who it's actually for, needed
    # for a regulator-facing per-life audit trail (see
    # engine/custom_pricing.py's per-life annual rollup).
    benefit_lines_by_life: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # ── EXPENSE OUTFLOWS ─────────────────────────────────────────────────
    commission:         float = 0.0
    acquisition_cost:   float = 0.0
    renewal_expense:    float = 0.0
    claims_admin:       float = 0.0
    total_expenses:     float = 0.0

    # ── NET POSITION ─────────────────────────────────────────────────────
    net_cashflow:        float = 0.0
    cumulative_cashflow: float = 0.0


def _dependant_life_labels(product: Product) -> List[str]:
    """
    A readable, stable label per dependant for the per-life breakdown —
    "Spouse", "Parent", "Child", ... or "Child 1" / "Child 2" if a product
    has more than one dependant with the same relationship (only possible
    when using build_custom_product/Product directly with a hand-built
    dependants list, since the dashboard caps relationship types at one
    each — this still resolves unambiguously either way).
    """
    counts: Dict[str, int] = {}
    for d in product.dependants:
        counts[d.relationship] = counts.get(d.relationship, 0) + 1
    seen: Dict[str, int] = {}
    labels = []
    for d in product.dependants:
        label = d.relationship.title()
        if counts[d.relationship] > 1:
            seen[d.relationship] = seen.get(d.relationship, 0) + 1
            label = f"{label} {seen[d.relationship]}"
        labels.append(label)
    return labels


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
    life_labels = _dependant_life_labels(product)

    for dec in decrement_rows:
        month    = dec.month
        pol_year = dec.policy_year
        lx       = dec.main.lx
        age_main = dec.main.age

        inv_rate     = assumptions.investment_rate_monthly
        exp_inf_rate = assumptions.expense_inflation_monthly

        # ── GROSS / NET PREMIUM ─────────────────────────────────────────
        # "Limited pay" products (product.premium_payment_term_years set)
        # stop collecting premium after that many policy years while cover
        # continues — e.g. a 10-pay whole life plan. Commission below is
        # derived from gross_prem, so it correctly stops too once premium
        # does, with no separate check needed.
        premium_still_payable = (
            product.premium_payment_term_years is None
            or pol_year <= product.premium_payment_term_years
        )
        gross_prem = lx * monthly_premium if premium_still_payable else 0.0
        net_prem   = gross_prem * assumptions.collection_rate

        # ── COMMISSION ───────────────────────────────────────────────────
        # Two structurally different bases: a percentage of that month's
        # premium (CommissionSchedule — the common case), or a flat GHS
        # amount per in-force policy regardless of what premium was
        # charged (FixedScaleCommission — e.g. a "transport support"
        # allowance). lx-weighted either way, same as every other line
        # here, so it correctly shrinks as the cohort decrements.
        if isinstance(assumptions.commission, FixedScaleCommission):
            commission = lx * assumptions.commission.get_monthly_amount_for_policy_year(pol_year)
        else:
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
        benefit_lines_by_life: Dict[str, Dict[str, float]] = {"Main Life": {}}
        for label in life_labels:
            benefit_lines_by_life[label] = {}
        expected_events = 0.0   # drives claims_admin cost

        def _add_to_life(life_label: str, rider_name: str, value: float) -> None:
            if value == 0:
                return
            benefit_lines_by_life[life_label][rider_name] = benefit_lines_by_life[life_label].get(rider_name, 0.0) + value

        for rider in product.riders:
            if not _rider_active(rider, month):
                benefit_lines[rider.name] = 0.0
                continue

            benefit_mult = rider.get_benefit_multiplier(pol_year)   # 1.0 unless a decreasing-term schedule is set

            if rider.incidence_basis == "mortality":
                main_amount = dec.main.dx * rider.benefit_main * benefit_mult
                amount = main_amount
                expected_events += dec.main.dx
                _add_to_life("Main Life", rider.name, main_amount)
                for i, dependant in enumerate(product.dependants):
                    dep_dec = dec.dependants.get(i)
                    if dep_dec is None:
                        continue
                    dep_benefit = dependant.get_dependant_benefit(rider.name, rider.benefit_dependant)
                    if dep_benefit == 0:
                        continue
                    dep_amount = dep_dec.dx * dep_benefit * benefit_mult
                    amount += dep_amount
                    expected_events += dep_dec.dx
                    _add_to_life(life_labels[i], rider.name, dep_amount)
            elif rider.incidence_basis == "mortality_multiple":
                # Frequency = a fixed multiple of the SAME dx used for the
                # death benefit — e.g. TPD priced as "20% of the mortality
                # rate," so it scales with age exactly like death does,
                # unlike a flat annual_incidence_rate held constant across
                # every age.
                mult = rider.mortality_multiplier or 0.0
                main_amount = dec.main.dx * mult * rider.benefit_main * benefit_mult
                amount = main_amount
                expected_events += dec.main.dx * mult
                _add_to_life("Main Life", rider.name, main_amount)
                for i, dependant in enumerate(product.dependants):
                    dep_dec = dec.dependants.get(i)
                    if dep_dec is None:
                        continue
                    dep_benefit = dependant.get_dependant_benefit(rider.name, rider.benefit_dependant)
                    if dep_benefit == 0:
                        continue
                    dep_amount = dep_dec.dx * mult * dep_benefit * benefit_mult
                    amount += dep_amount
                    expected_events += dep_dec.dx * mult
                    _add_to_life(life_labels[i], rider.name, dep_amount)
            else:
                monthly_incidence = rider.annual_incidence_rate / 12
                main_amount = lx * monthly_incidence * rider.benefit_main * benefit_mult * rider.avg_events_per_year
                amount = main_amount
                expected_events += lx * monthly_incidence
                _add_to_life("Main Life", rider.name, main_amount)
                for i, dependant in enumerate(product.dependants):
                    dep_dec = dec.dependants.get(i)
                    if dep_dec is None:
                        continue
                    dep_benefit = dependant.get_dependant_benefit(rider.name, rider.benefit_dependant)
                    if dep_benefit == 0:
                        continue
                    dep_lx = dep_dec.lx
                    dep_amount = dep_lx * monthly_incidence * dep_benefit * benefit_mult * rider.avg_events_per_year
                    amount += dep_amount
                    expected_events += dep_lx * monthly_incidence
                    _add_to_life(life_labels[i], rider.name, dep_amount)

            benefit_lines[rider.name] = amount

        # ── MATURITY BENEFIT — paid to survivors at the end of specific ──
        # policy years (product.maturity_benefits) — what makes a term
        # product an endowment/educational endowment. Weighted by lx_end
        # (survivors after this month's deaths/lapses), since anyone who
        # died this month already received the death benefit above instead.
        # Attributed to Main Life — maturity_benefits is product-level, not
        # per-rider, so there's no per-dependant benefit to split out here.
        if month % 12 == 0 and pol_year in product.maturity_benefits:
            maturity_amount = dec.main.lx_end * product.maturity_benefits[pol_year]
            benefit_lines["Maturity Benefit"] = maturity_amount
            _add_to_life("Main Life", "Maturity Benefit", maturity_amount)

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
            benefit_lines_by_life = benefit_lines_by_life,
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

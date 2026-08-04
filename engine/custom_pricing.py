"""
================================================================================
CUSTOM PRODUCT PRICING — ad-hoc products built directly from a request
================================================================================
What this file does:
    Every other entry point in this engine (run_pricing, run_ifrs17, ...)
    prices a product that's already been saved as clients/<id>/products/
    <name>.yaml. The dashboard's Part 4 product builder needs the opposite:
    an insurer typing up an arbitrary product on the fly (any riders, any
    dependants, any product type) and pricing it immediately, with nothing
    saved to disk first.

    build_custom_product() turns that raw shape into a real
    engine.product.Product; run_custom_pricing() runs the full decrement →
    cashflow → present-value → premium-solve pipeline against it (reusing
    every validated engine module unchanged) and additionally produces the
    annual roll-ups a pricing platform's results page needs that a bare
    ProductAssumptions/PVResults pair doesn't give you:
        - annual cash flow roll-up (Tab 2)
        - reserve projection — prospective net premium reserve, i.e. PV(
          future outflows) - PV(future premiums) evaluated at the START of
          each policy year, via a standard backward recursion (Tab 3)
        - profit signature — undiscounted expected profit emerging each
          policy year (net cash flow + investment income, summed per
          year), with the first year cumulative profit turns positive
          flagged as breakeven (Tab 4)

    Product type coverage — IMPORTANT:
        This module correctly prices anything the underlying engine
        actually models: a mortality/lapse decrement with any number of
        mortality-driven riders (death, funeral, maturity) and any number
        of incidence-driven riders (TPD, critical illness, hospital cash,
        income protection — approximated as a single incidence rate x
        severity, not a full multi-state continuance model for income
        protection specifically). It does NOT correctly price two families
        the dashboard's dropdown lists for completeness:
            - Annuities (immediate/deferred) — cash flow direction is
              inverted (premium in, survival-contingent payments out) and
              needs its own engine, not a bolt-on to the death-benefit loop.
            - Non-life classes (Motor, Fire, Marine, Engineering, Bond,
              ...) — general insurance pricing is loss-ratio/exposure
              based, not mortality-decrement based; AMVS already has a
              real (different) engine for non-life RESERVING
              (engine/ifrs17_nonlife.py) but no non-life PRICING engine.
        CustomProductRequest.PRICING_UNSUPPORTED_TYPES (api/main.py) is
        checked before this module is ever called, so an unsupported type
        selection fails fast with an honest message instead of silently
        producing a wrong number.
================================================================================
"""

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from engine.assumptions import ProductAssumptions
from engine.product import Dependant, Product, Rider
from engine.decrement import run_decrement_projection
from engine.cashflows import calculate_cash_flows, CashFlowRow
from engine.present_value import calculate_present_values
from engine.pricing import solve_premium


# Default annual incidence rate / average-events-per-year for benefit types
# that aren't mortality-driven — used when the dashboard's rider row (which
# only captures a benefit AMOUNT, not a frequency) doesn't ask the user for
# one. Broadly consistent with the incidence assumptions already validated
# against real Ghanaian products elsewhere in this engine (see
# clients/pic/products/whole_life_tier1.yaml, micro_life.yaml).
DEFAULT_MORBIDITY_RATES: Dict[str, Dict[str, float]] = {
    "tpd":                {"annual_incidence_rate": 0.0010, "avg_events_per_year": 1.0},
    "critical_illness":   {"annual_incidence_rate": 0.0015, "avg_events_per_year": 1.0},
    "hospital_cash":      {"annual_incidence_rate": 0.0025, "avg_events_per_year": 5.0},   # GHS/day x avg 5 nights/admission
    "lump_sum_hospital":  {"annual_incidence_rate": 0.0025, "avg_events_per_year": 1.0},   # one lump sum per admission
    "income_protection":  {"annual_incidence_rate": 0.0020, "avg_events_per_year": 6.0},   # monthly benefit x avg 6 months/claim
}

# Rider types with no incidence/frequency at all — paid on death (mortality
# table drives frequency) or not incidence-driven in the usual sense.
MORTALITY_DRIVEN_TYPES = {"death", "funeral"}


def build_custom_product(spec: Dict[str, Any]) -> Product:
    """
    Build a Product from the dashboard's ad-hoc CustomProductRequest shape
    (see api/main.py). Every rider except "maturity" becomes a
    engine.product.Rider; "maturity" entries become Product.maturity_benefits
    instead (paid to survivors at a policy year-end, not per-rider — see
    engine/cashflows.py's maturity benefit logic).
    """
    policy_term_years = spec.get("policy_term_years")
    riders: List[Rider] = []
    maturity_benefits: Dict[int, float] = {}

    for r in spec.get("riders", []):
        benefit_type = r["benefit_type"]
        rider_term_years = r.get("rider_term_years") or policy_term_years

        if benefit_type == "maturity":
            year = rider_term_years or policy_term_years
            if year:
                maturity_benefits[int(year)] = maturity_benefits.get(int(year), 0.0) + float(r["benefit_amount"])
            continue

        if benefit_type in MORTALITY_DRIVEN_TYPES:
            incidence_basis = "mortality"
            annual_incidence_rate = 0.0
            avg_events_per_year = 1.0
        elif benefit_type == "savings":
            # No account-value/unit-linked mechanic exists in this engine
            # yet (see the Afentoboa investigation) — modelled as a benefit
            # that never triggers, so it's visible on the product but
            # doesn't silently misprice as if it were a real investment
            # account roll-forward.
            incidence_basis = "savings_placeholder"
            annual_incidence_rate = 0.0
            avg_events_per_year = 0.0
        else:
            defaults = DEFAULT_MORBIDITY_RATES.get(benefit_type, {"annual_incidence_rate": 0.0010, "avg_events_per_year": 1.0})
            incidence_basis = benefit_type
            # A caller-supplied rate overrides the engine default — needed
            # whenever a client's real experience differs from the generic
            # default (e.g. a specific product's actual hospitalisation
            # incidence, which is rarely the same across insurers).
            annual_incidence_rate = r.get("annual_incidence_rate")
            if annual_incidence_rate is None:
                annual_incidence_rate = defaults["annual_incidence_rate"]
            avg_events_per_year = r.get("avg_events_per_year")
            if avg_events_per_year is None:
                avg_events_per_year = defaults["avg_events_per_year"]

        # "Decreasing Term" is a PRODUCT-type choice, not a per-rider field
        # in the dashboard's rider row — apply a standard straight-line
        # reduction to 0 by the end of the term to every mortality-driven
        # rider automatically, rather than requiring the user to hand-build
        # a benefit_schedule (which the rider row doesn't expose).
        benefit_schedule = None
        if spec.get("product_type") == "decreasing_term" and benefit_type in MORTALITY_DRIVEN_TYPES and policy_term_years:
            n = int(policy_term_years)
            benefit_schedule = {y: round(1.0 - (y - 1) / n, 4) for y in range(1, n + 1)}

        riders.append(Rider(
            rider_type              = benefit_type,
            name                     = r["name"],
            benefit_main             = float(r["benefit_amount"]),
            benefit_dependant        = float(r["benefit_amount"]),
            incidence_basis          = incidence_basis,
            annual_incidence_rate    = annual_incidence_rate,
            avg_events_per_year      = avg_events_per_year,
            waiting_period_months    = int(r.get("waiting_period_months", 0)),
            max_duration_months      = int(rider_term_years) * 12 if rider_term_years else None,
            benefit_schedule          = benefit_schedule,
        ))

    dependants: List[Dependant] = []
    for d in spec.get("dependants", []):
        dependants.append(Dependant(
            relationship        = d["relationship"],
            age                   = d.get("age"),
            benefit_multiplier      = 1.0,
            benefit_overrides          = {str(k): float(v) for k, v in (d.get("benefit_overrides") or {}).items()},
        ))

    return Product(
        name                 = spec.get("product_name", "Custom Product"),
        policy_term_years    = policy_term_years,
        premium_mode         = spec.get("premium_mode", "monthly"),
        measurement_model    = "paa" if policy_term_years and policy_term_years <= 1 else "gmm",
        product_type         = spec.get("product_type", "custom"),
        riders               = riders,
        dependants           = dependants,
        maturity_benefits    = maturity_benefits,
    )


# ── Annual roll-ups ──────────────────────────────────────────────────────────

def _annual_cashflow_rollup(cf_rows: List[CashFlowRow], dec_rows, product: Product) -> List[dict]:
    """One row per POLICY YEAR: in-force lives (at the year's first month), premium income, claims paid by rider, expenses, commission, net cash flow, investment income, cumulative profit."""
    opening_lives_by_year = {}
    for dec in dec_rows:
        opening_lives_by_year.setdefault(dec.policy_year, dec.main.lx)

    years: Dict[int, dict] = {}
    cumulative = 0.0
    for cf in cf_rows:
        y = cf.policy_year
        if y not in years:
            years[y] = {
                "policy_year": y, "opening_lives": round(opening_lives_by_year.get(y, 0.0), 6),
                "premium_income": 0.0,
                "claims_by_rider": {r.name: 0.0 for r in product.riders},
                "maturity_benefit": 0.0, "expenses": 0.0, "commission": 0.0,
                "net_cash_flow": 0.0, "investment_income": 0.0, "cumulative_profit": 0.0,
            }
        row = years[y]
        row["premium_income"]      += cf.gross_premium
        row["expenses"]             += cf.acquisition_cost + cf.renewal_expense + cf.claims_admin
        row["commission"]           += cf.commission
        row["investment_income"]    += cf.investment_income
        row["net_cash_flow"]         += cf.net_cashflow
        for rider_name, amount in cf.benefit_lines.items():
            if rider_name == "Maturity Benefit":
                row["maturity_benefit"] += amount
            elif rider_name in row["claims_by_rider"]:
                row["claims_by_rider"][rider_name] += amount

    result = []
    for y in sorted(years.keys()):
        row = years[y]
        cumulative += row["net_cash_flow"] + row["investment_income"]
        row["cumulative_profit"] = round(cumulative, 2)
        row["premium_income"] = round(row["premium_income"], 2)
        row["expenses"] = round(row["expenses"], 2)
        row["commission"] = round(row["commission"], 2)
        row["investment_income"] = round(row["investment_income"], 2)
        row["net_cash_flow"] = round(row["net_cash_flow"], 2)
        row["maturity_benefit"] = round(row["maturity_benefit"], 2)
        row["claims_by_rider"] = {k: round(v, 2) for k, v in row["claims_by_rider"].items()}
        row["total_claims"] = round(sum(row["claims_by_rider"].values()) + row["maturity_benefit"], 2)
        result.append(row)
    return result


def _reserve_projection(cf_rows: List[CashFlowRow], monthly_discount_rate: float) -> List[dict]:
    """
    Prospective net premium reserve at the START of each policy year:
    PV(future benefits + expenses + commission) - PV(future net premiums),
    evaluated as of that point — via a standard backward recursion
    (V[N+1] = 0; V[t] = (net_outflow[t] + V[t+1]) / (1+r)).
    """
    n = len(cf_rows)
    v_next = 0.0
    reserve_by_month: Dict[int, float] = {}
    for cf in reversed(cf_rows):
        net_outflow = cf.total_benefits + cf.total_expenses + cf.commission - cf.net_premium
        v_this = (net_outflow + v_next) / (1.0 + monthly_discount_rate)
        reserve_by_month[cf.month] = v_this
        v_next = v_this

    result = []
    prior_closing = 0.0
    seen_years = sorted({cf.policy_year for cf in cf_rows})
    by_year_first_month = {}
    for cf in cf_rows:
        by_year_first_month.setdefault(cf.policy_year, cf.month)

    for y in seen_years:
        opening = reserve_by_month.get(by_year_first_month[y], 0.0)
        year_rows = [cf for cf in cf_rows if cf.policy_year == y]
        premium = sum(cf.net_premium for cf in year_rows)
        claims = sum(cf.total_benefits for cf in year_rows)
        expenses = sum(cf.total_expenses + cf.commission for cf in year_rows)
        inv_income = sum(cf.investment_income for cf in year_rows)
        last_month = year_rows[-1].month
        closing = reserve_by_month.get(last_month + 1, 0.0) if (last_month + 1) in reserve_by_month else 0.0
        result.append({
            "policy_year":       y,
            "opening_reserve":   round(opening, 2),
            "premium":           round(premium, 2),
            "claims":            round(claims, 2),
            "expenses":          round(expenses, 2),
            "investment_income": round(inv_income, 2),
            "closing_reserve":   round(closing, 2),
        })
    return result


def _profit_signature(cf_rows: List[CashFlowRow]) -> dict:
    """Undiscounted expected profit emerging each policy year, plus the first year cumulative profit turns positive (breakeven)."""
    years: Dict[int, float] = {}
    for cf in cf_rows:
        years[cf.policy_year] = years.get(cf.policy_year, 0.0) + cf.net_cashflow + cf.investment_income

    rows = []
    cumulative = 0.0
    breakeven_year = None
    for y in sorted(years.keys()):
        cumulative += years[y]
        rows.append({"policy_year": y, "profit": round(years[y], 2), "cumulative_profit": round(cumulative, 2)})
        if breakeven_year is None and cumulative >= 0:
            breakeven_year = y

    return {"profit_by_year": rows, "breakeven_year": breakeven_year}


# ── Top-level entry point ───────────────────────────────────────────────────

def run_custom_pricing(
    product:         Product,
    assumptions:     ProductAssumptions,
    target_margin:   Optional[float] = None,
    verbose:         bool            = False,
) -> dict:
    """
    Full pricing run for an ad-hoc product: solves the premium, then builds
    every roll-up the Part 4 results tabs need from the SAME monthly cash
    flow rows (computed once).
    """
    premium, pv = solve_premium(assumptions, product, target_margin)

    dec_rows = run_decrement_projection(assumptions, product)
    cf_rows  = calculate_cash_flows(dec_rows, assumptions, product, premium)

    annual_cashflow = _annual_cashflow_rollup(cf_rows, dec_rows, product)
    reserve         = _reserve_projection(cf_rows, assumptions.valuation_rate_monthly)
    profit_sig      = _profit_signature(cf_rows)

    return {
        "product_name":         product.name,
        "product_type":         product.product_type,
        "monthly_premium":      round(premium, 2),
        "annual_premium":       round(premium * 12, 2),
        "single_premium_equiv": round(pv.pv_premiums, 2),
        "profit_margin":        round(pv.profit_margin, 4),
        "pv_premiums":          round(pv.pv_premiums, 2),
        "pv_benefits":          round(pv.pv_benefits, 2),
        "pv_expenses":          round(pv.pv_expenses, 2),
        "pv_commissions":       round(pv.pv_commissions, 2),
        "pvfcf":                round(pv.pvfcf, 2),
        "risk_adjustment":      round(pv.risk_adjustment, 2),
        "csm_at_inception":     round(pv.csm_at_inception, 2),
        "lrc_total":            round(pv.lrc_total, 2),
        "is_onerous":           pv.is_onerous,
        "loss_component":       round(pv.loss_component, 2),
        "annual_cashflow":      annual_cashflow,
        "reserve_projection":   reserve,
        "profit_signature":     profit_sig,
        "assumptions_used":     assumptions.to_dict(),
    }


def run_custom_rate_table(
    product:        Product,
    assumptions:    ProductAssumptions,
    age_start:      int = 18,
    age_end:        int = 70,
    target_margin:  Optional[float] = None,
) -> Dict[int, dict]:
    """Solve the premium for every age in [age_start, age_end] for the same ad-hoc product. Mirrors engine.pricing.build_rate_table()'s shape."""
    table: Dict[int, dict] = {}
    for age in range(age_start, age_end + 1):
        asmp = copy.deepcopy(assumptions)
        asmp.entry_age_main = age
        try:
            premium, pv = solve_premium(asmp, product, target_margin)
            table[age] = {
                "monthly_premium": round(premium, 2),
                "annual_premium":  round(premium * 12, 2),
                "profit_margin":   round(pv.profit_margin, 4),
                "is_onerous":      pv.is_onerous,
            }
        except ValueError as e:
            table[age] = {"error": str(e)}
    return table


# One-at-a-time stresses for the sensitivity tab — additive for rates
# already expressed as +/- loadings (mortality), multiplicative for
# scalars where a relative shift is the natural stress (expenses,
# lapses), and a flat +/-100bps for the valuation rate (the conventional
# way an actuary stresses a discount rate, not a % change of it).
SENSITIVITY_STRESSES = [
    ("Mortality +10%",     lambda a: setattr(a, "mortality_loading", a.mortality_loading + 0.10)),
    ("Mortality -10%",     lambda a: setattr(a, "mortality_loading", a.mortality_loading - 0.10)),
    ("Lapses +10%",        lambda a: _scale_lapses(a, 1.10)),
    ("Lapses -10%",        lambda a: _scale_lapses(a, 0.90)),
    ("Expenses +10%",      lambda a: _scale_expenses(a, 1.10)),
    ("Expenses -10%",      lambda a: _scale_expenses(a, 0.90)),
    ("Valuation rate +1%", lambda a: setattr(a, "valuation_rate_pa", a.valuation_rate_pa + 0.01)),
    ("Valuation rate -1%", lambda a: setattr(a, "valuation_rate_pa", a.valuation_rate_pa - 0.01)),
]


def _scale_lapses(a: ProductAssumptions, factor: float) -> None:
    from engine.assumptions import LapseSchedule
    a.lapse_schedule = LapseSchedule(rates={y: min(0.99, r * factor) for y, r in a.lapse_schedule.rates.items()})


def _scale_expenses(a: ProductAssumptions, factor: float) -> None:
    a.policy_fee_monthly     *= factor
    a.acquisition_cost        *= factor
    a.renewal_expense_annual   *= factor
    a.claims_admin_cost          *= factor


def run_custom_sensitivity(
    product:        Product,
    assumptions:    ProductAssumptions,
    target_margin:  Optional[float] = None,
) -> List[dict]:
    """Re-solve the premium with each SENSITIVITY_STRESSES entry applied one at a time; base case included first for reference."""
    base_premium, _ = solve_premium(assumptions, product, target_margin)

    results = [{
        "assumption_stressed": "Base case", "stressed_premium": round(base_premium, 2),
        "difference": 0.0, "pct_difference": 0.0,
    }]
    for label, apply_stress in SENSITIVITY_STRESSES:
        asmp = copy.deepcopy(assumptions)
        apply_stress(asmp)
        try:
            premium, _ = solve_premium(asmp, product, target_margin)
            diff = premium - base_premium
            results.append({
                "assumption_stressed": label,
                "stressed_premium":     round(premium, 2),
                "difference":            round(diff, 2),
                "pct_difference":         round(diff / base_premium, 4) if base_premium else 0.0,
            })
        except ValueError as e:
            results.append({"assumption_stressed": label, "error": str(e)})
    return results

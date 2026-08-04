"""
================================================================================
STRESS TESTS — 5 prescribed scenarios against the base GIRBC solvency position
================================================================================
What this file does:
    Re-runs the GIRBC solvency calculation under 5 prescribed stress
    scenarios, each perturbing a specific risk module (or its underlying
    exposures, where the scenario needs a genuine recalculation rather than
    a flat scale-up):

        1. Expense Inflation Stress   — Operational risk charge x1.30
        2. Credit Counterparty Loss    — Credit risk charge x1.50
        3. Equity and Property Fall 25% — listed_equities and real_estate
                                          exposures cut 25%, Market risk
                                          genuinely RECALCULATED from them
                                          (not just the total scaled)
        4. Claims Increase 10%            — net_claims_reserve by class cut
                                          up 10%, Insurance risk genuinely
                                          RECALCULATED from it
        5. Catastrophe Event                 — Catastrophe charge x1.25

    Each scenario reports the resulting Overall Risk Charge, MCR, CAR,
    solvency status, and the CAR change versus the base (unstressed)
    position.

    Scope note — these are RISK-CHARGE-ONLY stresses, as specified: none of
    the 5 scenarios reduce Qualifying Capital Resources, only the relevant
    risk charge(s). This is a genuine, disclosed modelling limitation for
    scenarios 3 and 4 specifically: a real 25% equity/property market fall
    or a genuine claims deterioration would normally also hit the
    insurer's own balance sheet (lower asset values / higher liabilities
    both reduce net assets, i.e. capital), not just the regulatory risk
    charge computed against the post-stress exposure. Because a SMALLER
    exposure produces a SMALLER risk charge (and therefore a smaller MCR),
    it is mathematically possible for CAR to *improve* under scenario 3 or
    4 when capital resources are held constant and the base position's
    Overall Risk Charge is comfortably above the fixed GHS 50,000,000
    floor — i.e. "stress" can show a false improvement rather than a
    deterioration. For every real client validated so far (QIC), this
    doesn't manifest because the floor binds before and after the stress —
    but flag this to whoever reviews results for a client whose Overall
    Risk Charge is NOT floor-bound, and consider adding a capital-side
    impact to scenarios 3/4 before relying on them for that client.
================================================================================
"""

from dataclasses import dataclass, replace
from typing import List

from engine.rbc.aggregation import SolvencyResult, calculate_solvency
from engine.rbc.credit_risk import calculate_credit_risk
from engine.rbc.data_model import (
    CreditRiskExposures, InsuranceRiskExposures, MarketRiskExposures,
    OperationalRiskExposures, QualifyingCapitalResources,
)
from engine.rbc.insurance_risk import calculate_insurance_risk
from engine.rbc.market_risk import calculate_market_risk
from engine.rbc.operational_risk import calculate_operational_risk


@dataclass
class StressTestResult:
    scenario_name:   str
    description:       str
    new_scr:              float   # overall_risk_charge under this scenario
    new_mcr:                 float
    new_car:                    float
    status:                        str   # "STRONG" | "ADEQUATE" | "BREACH"
    car_change:                       float   # new_car - base CAR
    passed:                              bool   # CAR >= 1.00 (MCR) under this scenario


def _to_stress_result(name: str, description: str, stressed: SolvencyResult, base_car: float) -> StressTestResult:
    return StressTestResult(
        scenario_name=name, description=description,
        new_scr=stressed.overall_risk_charge, new_mcr=stressed.mcr, new_car=stressed.car,
        status=stressed.status, car_change=round(stressed.car - base_car, 4),
        passed=stressed.car >= 1.00,
    )


def run_stress_tests(
    insurance_exposures:      InsuranceRiskExposures,
    net_non_life_insurance_revenue: float,
    market_exposures:                  MarketRiskExposures,
    credit_exposures:                     CreditRiskExposures,
    operational_exposures:                   OperationalRiskExposures,
    capital_resources:                          QualifyingCapitalResources,
) -> List[StressTestResult]:
    base_ins = calculate_insurance_risk(insurance_exposures, net_non_life_insurance_revenue)
    base_mkt = calculate_market_risk(market_exposures)
    base_cred = calculate_credit_risk(credit_exposures)
    base_op = calculate_operational_risk(operational_exposures)
    base_sol = calculate_solvency(base_ins, base_mkt, base_cred, base_op, capital_resources)

    results: List[StressTestResult] = []

    # 1. Expense Inflation Stress — Operational risk charge +30%
    op1 = replace(base_op, total_operational_risk_scr=round(base_op.total_operational_risk_scr * 1.30, 2))
    sol1 = calculate_solvency(base_ins, base_mkt, base_cred, op1, capital_resources)
    results.append(_to_stress_result(
        "Expense Inflation Stress", "Operational risk charge increased 30%", sol1, base_sol.car,
    ))

    # 2. Credit Counterparty Loss — Credit risk charge +50%
    cred2 = replace(base_cred, total_credit_risk_scr=round(base_cred.total_credit_risk_scr * 1.50, 2))
    sol2 = calculate_solvency(base_ins, base_mkt, cred2, base_op, capital_resources)
    results.append(_to_stress_result(
        "Credit Counterparty Loss", "Credit risk charge increased 50%", sol2, base_sol.car,
    ))

    # 3. Equity and Property Fall 25% — genuinely recalculated from stressed exposures
    stressed_equities = {k: v * 0.75 for k, v in market_exposures.listed_equities.items()}
    stressed_real_estate = {k: v * 0.75 for k, v in market_exposures.real_estate.items()}
    mkt_exp3 = replace(market_exposures, listed_equities=stressed_equities, real_estate=stressed_real_estate)
    mkt3 = calculate_market_risk(mkt_exp3)
    sol3 = calculate_solvency(base_ins, mkt3, base_cred, base_op, capital_resources)
    results.append(_to_stress_result(
        "Equity and Property Fall 25%", "Equity and property values reduced 25%, Market risk recalculated", sol3, base_sol.car,
    ))

    # 4. Claims Increase 10% — genuinely recalculated from stressed exposures (reserve only, not premium)
    stressed_reserve = {k: v * 1.10 for k, v in insurance_exposures.net_claims_reserve.items()}
    ins_exp4 = replace(insurance_exposures, net_claims_reserve=stressed_reserve)
    ins4 = calculate_insurance_risk(ins_exp4, net_non_life_insurance_revenue)
    sol4 = calculate_solvency(ins4, base_mkt, base_cred, base_op, capital_resources)
    results.append(_to_stress_result(
        "Claims Increase 10%", "Net claims reserve by class increased 10%, Insurance risk recalculated", sol4, base_sol.car,
    ))

    # 5. Catastrophe Event — Catastrophe charge +25%
    stressed_cat = round(base_ins.catastrophe_charge * 1.25, 2)
    ins5 = replace(
        base_ins, catastrophe_charge=stressed_cat,
        total_insurance_risk_scr=round(base_ins.insurance_risk_before_cat + stressed_cat, 2),
    )
    sol5 = calculate_solvency(ins5, base_mkt, base_cred, base_op, capital_resources)
    results.append(_to_stress_result(
        "Catastrophe Event", "Catastrophe charge increased 25%", sol5, base_sol.car,
    ))

    return results

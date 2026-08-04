"""
================================================================================
AGGREGATION — Overall Risk Charge, MCR, PCR, CAR
================================================================================
What this file does:
    GIRBC1's top-level aggregation: combines FOUR risk-module totals —
    Insurance Risk (excluding Catastrophe), Catastrophe, Market, Credit —
    via the sqrt-correlation method at 25% pairwise correlation, then adds
    Operational Risk on top with no diversification credit, giving the
    Overall Risk Charge, then derives MCR/PCR/CAR.

        Overall Risk Charge = sqrt(charges^T . Corr(0.25) . charges) + Operational
        MCR = MAX(Overall Risk Charge / 1.5, GHS 50,000,000 fixed floor)
        PCR = MCR x 1.5   (this also naturally floors PCR at floor x 1.5, since MCR is already floored)
        CAR = Total Qualifying Capital Resources / MCR

    Catastrophe is correlated here as its OWN 4th item (using
    insurance_risk.insurance_risk_before_cat and
    insurance_risk.catastrophe_charge separately — NOT
    insurance_risk.total_insurance_risk_scr, which still exists on
    InsuranceRiskResult as a display convenience but is not what feeds this
    aggregation) — this matches the real GIRBC1 template exactly (confirmed
    directly: reproduces QIC's real 2025 Overall Risk Charge of
    GHS 74,172,806.95 and CAR of 104.19% to the cent when fed real module
    totals). An earlier version of this file folded Catastrophe into
    Insurance Risk's own total and correlated only 3 items — that
    understated diversification credit and materially overstated CAR's
    denominator; corrected 2026-08-03.

    Fixed minimum QCR floor used here (GHS 50,000,000) is the NON-
    REINSURER insurer floor. Reinsurers use GHS 125,000,000 instead — not
    modelled here since QIC (and every client this was built against) is a
    direct insurer, not a reinsurer; pass fixed_minimum_qcr explicitly if
    that ever needs to change.
================================================================================
"""

from dataclasses import dataclass

from engine.rbc.correlation import correlation_aggregate
from engine.rbc.credit_risk import CreditRiskResult
from engine.rbc.data_model import QualifyingCapitalResources
from engine.rbc.insurance_risk import InsuranceRiskResult
from engine.rbc.market_risk import MarketRiskResult
from engine.rbc.operational_risk import OperationalRiskResult

TOP_LEVEL_CORR = 0.25
FIXED_MINIMUM_QCR = 50_000_000.0   # GHS — non-reinsurer insurer floor (reinsurers: 125,000,000)
MCR_TO_PCR_MULTIPLE = 1.5

STRONG_THRESHOLD = 1.50    # CAR >= 150% -> STRONG (at/above the PCR supervisory target)
ADEQUATE_THRESHOLD = 1.00  # CAR >= 100% (but < 150%) -> ADEQUATE (at/above MCR, below PCR)
                            # CAR < 100% -> BREACH


@dataclass
class SolvencyResult:
    insurance_risk:     InsuranceRiskResult
    market_risk:          MarketRiskResult
    credit_risk:             CreditRiskResult
    operational_risk:           OperationalRiskResult
    capital_resources:              QualifyingCapitalResources

    overall_risk_charge_before_operational: float   # Insurance/Market/Credit combined at 25% correlation
    overall_risk_charge:                       float   # + Operational, no diversification credit
    mcr:                                          float
    pcr:                                            float
    total_qcr:                                        float
    car:                                                 float
    capital_composition_valid:                             bool   # Tier 1 Unlimited >= 50% of QCR
    status:                                                   str   # "STRONG" | "ADEQUATE" | "BREACH"


def calculate_solvency(
    insurance_risk:      InsuranceRiskResult,
    market_risk:            MarketRiskResult,
    credit_risk:                CreditRiskResult,
    operational_risk:              OperationalRiskResult,
    capital_resources:                  QualifyingCapitalResources,
    fixed_minimum_qcr:                       float = FIXED_MINIMUM_QCR,
) -> SolvencyResult:
    charges = [
        insurance_risk.insurance_risk_before_cat,
        insurance_risk.catastrophe_charge,
        market_risk.total_market_risk_scr,
        credit_risk.total_credit_risk_scr,
    ]
    n = len(charges)
    corr = [[1.0 if i == j else TOP_LEVEL_CORR for j in range(n)] for i in range(n)]
    overall_before_operational = correlation_aggregate(charges, corr)
    overall = overall_before_operational + operational_risk.total_operational_risk_scr

    mcr = max(overall / MCR_TO_PCR_MULTIPLE, fixed_minimum_qcr)
    pcr = mcr * MCR_TO_PCR_MULTIPLE

    total_qcr = capital_resources.total_qcr
    car = (total_qcr / mcr) if mcr > 0 else 0.0

    if car >= STRONG_THRESHOLD:
        status = "STRONG"
    elif car >= ADEQUATE_THRESHOLD:
        status = "ADEQUATE"
    else:
        status = "BREACH"

    return SolvencyResult(
        insurance_risk=insurance_risk, market_risk=market_risk, credit_risk=credit_risk,
        operational_risk=operational_risk, capital_resources=capital_resources,
        overall_risk_charge_before_operational=round(overall_before_operational, 2),
        overall_risk_charge=round(overall, 2),
        mcr=round(mcr, 2), pcr=round(pcr, 2), total_qcr=round(total_qcr, 2),
        car=round(car, 4), capital_composition_valid=capital_resources.composition_valid,
        status=status,
    )

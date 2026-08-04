"""
================================================================================
OPERATIONAL RISK — Premium / Liability / Growth-based charge
================================================================================
What this file does:
    GIRBC6's Operational Risk charge (non-life insurer version):

        premium_charge  = 2.75% x current_year_net_premium
        liability_charge = 2.75% x current_year_net_liabilities
        growth_charge     = 2.75% x MAX(0, current_year_net_premium - 1.2 x prior_year_net_premium)
        operational_charge = MAX(premium_charge, liability_charge) + growth_charge

    The growth charge is a penalty for premium growth beyond 20% year-on-
    year — confirmed directly from the real GIRBC6 template's own formula
    (`F16 = (D14 - 1.2*D15) * 0.0275`). Added on top of the base charge with
    no diversification credit — matches the top-level aggregator
    (aggregation.py) also adding Operational Risk without diversification.
================================================================================
"""

from dataclasses import dataclass

from engine.rbc.data_model import OperationalRiskExposures

OPERATIONAL_FACTOR = 0.0275   # 2.75%
GROWTH_THRESHOLD = 1.20        # 20% YoY growth threshold


@dataclass
class OperationalRiskResult:
    premium_charge:    float
    liability_charge:    float
    growth_charge:          float
    total_operational_risk_scr: float   # MAX(premium_charge, liability_charge) + growth_charge


def calculate_operational_risk(exposures: OperationalRiskExposures) -> OperationalRiskResult:
    premium_charge = OPERATIONAL_FACTOR * exposures.current_year_net_premium
    liability_charge = OPERATIONAL_FACTOR * exposures.current_year_net_liabilities

    growth_excess = max(0.0, exposures.current_year_net_premium - GROWTH_THRESHOLD * exposures.prior_year_net_premium)
    growth_charge = OPERATIONAL_FACTOR * growth_excess

    total = max(premium_charge, liability_charge) + growth_charge

    return OperationalRiskResult(
        premium_charge=round(premium_charge, 2),
        liability_charge=round(liability_charge, 2),
        growth_charge=round(growth_charge, 2),
        total_operational_risk_scr=round(total, 2),
    )

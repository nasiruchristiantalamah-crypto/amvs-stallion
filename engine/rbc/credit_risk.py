"""
================================================================================
CREDIT RISK — Counterparty, Mortgage, and "Other" exposure risk
================================================================================
What this file does:
    GIRBC5's Credit Risk charge: three exposure groups (rated counterparty
    exposures, mortgages by loan-to-value band, and a fixed list of "other"
    balance-sheet items), each charged at its own factor, then combined by
    SIMPLE SUM — Credit Risk is the one module the directive does NOT apply
    the correlation-matrix diversification method to (confirmed directly
    from the real GIRBC5 template: `G154 = SUM(...)`, no correlation
    matrix).

    Reinsurance recoverables: rated RI recoverables belong in
    counterparty_exposures (identical RC1-RC7 table as any other rated
    counterparty — no separate factor table needed). CreditRiskExposures'
    own reinsurance_recoverables field is UNRATED only (20%).
    mandatory_pool_recoverables is a distinct, much lower factor (0.7%) —
    do not fold mandatory-pool receivables into reinsurance_recoverables,
    they are a materially different risk (see data_model.py).
================================================================================
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple

from engine.rbc.data_model import CreditRiskExposures

COUNTERPARTY_FACTORS: Dict[str, float] = {
    "RC1": 0.013, "RC2": 0.016, "RC3": 0.045, "RC4": 0.075,
    "RC5": 0.10, "RC6": 0.165, "RC7": 0.20, "Unrated": 0.20, "Default": 1.00,
}

MORTGAGE_LTV_FACTORS: Dict[str, float] = {
    "<50%": 0.015, "50-60%": 0.02, "60-70%": 0.035, "70-80%": 0.07, "80-90%": 0.15, ">90%": 0.35,
}

# "Other" exposures — fixed factors applied to CreditRiskExposures' own fields.
OTHER_FACTORS = {
    "cash_and_deposits":         0.00,
    "premium_receivables":        0.10,
    "reinsurance_recoverables":     0.20,   # unrated only — see module docstring
    "mandatory_pool_recoverables":    0.007,
    "deferred_tax_assets":               0.10,
    "related_party_loans":                  0.45,
    "other_receivables":                       0.20,
}


@dataclass
class CreditRiskResult:
    counterparty_charge:        float
    counterparty_by_rating:       Dict[str, float]
    mortgage_charge:                 float
    mortgage_by_ltv_band:               Dict[str, float]
    other_charge:                          float
    other_by_category:                        Dict[str, float]
    total_credit_risk_scr:                       float   # simple sum — no diversification credit


def _sum_by_key(exposures: List[Tuple[float, str]], factors: Dict[str, float]) -> Tuple[float, Dict[str, float]]:
    by_key: Dict[str, float] = {}
    for amount, key in exposures:
        factor = factors.get(key, factors.get("Unrated", factors.get("Default", 0.0)))
        by_key[key] = by_key.get(key, 0.0) + factor * amount
    return round(sum(by_key.values()), 2), {k: round(v, 2) for k, v in by_key.items()}


def calculate_credit_risk(exposures: CreditRiskExposures) -> CreditRiskResult:
    counterparty_charge, counterparty_by_rating = _sum_by_key(exposures.counterparty_exposures, COUNTERPARTY_FACTORS)
    mortgage_charge, mortgage_by_ltv_band = _sum_by_key(exposures.mortgage_exposures, MORTGAGE_LTV_FACTORS)

    other_by_category = {
        field_name: round(OTHER_FACTORS[field_name] * getattr(exposures, field_name), 2)
        for field_name in OTHER_FACTORS
    }
    other_charge = round(sum(other_by_category.values()), 2)

    total = round(counterparty_charge + mortgage_charge + other_charge, 2)

    return CreditRiskResult(
        counterparty_charge=counterparty_charge, counterparty_by_rating=counterparty_by_rating,
        mortgage_charge=mortgage_charge, mortgage_by_ltv_band=mortgage_by_ltv_band,
        other_charge=other_charge, other_by_category=other_by_category,
        total_credit_risk_scr=total,
    )

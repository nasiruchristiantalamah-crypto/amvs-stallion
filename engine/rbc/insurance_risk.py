"""
================================================================================
INSURANCE RISK — Non-Life premium + claims reserve risk, plus Catastrophe
================================================================================
What this file does:
    GIRBC3's Non-Life Insurance risk charge: each class's net premium and
    net claims reserve are stressed by a segment-specific factor, the two
    resulting charges are combined (25% correlation), classes combine into
    their segment (50% correlation), and the 5 segments combine into
    insurance_risk_before_cat using a cross-segment correlation matrix that
    is 25% specifically among {Liability, Property, Miscellaneous} (the
    real template's "Other" category) and 50% for every other pair (Motor
    and Credit correlate at 50% with everything — corrected 2026-08-03,
    was a uniform 50% everywhere before). catastrophe_charge (5% of net
    non-life insurance revenue) is kept as a SEPARATE field, not pre-added
    into insurance_risk_before_cat — see aggregation.py, which correlates
    it as its own top-level item, matching the real GIRBC1 template exactly
    (confirmed: reproduces QIC's real 2025 figures to the cent).
    total_insurance_risk_scr (= insurance_risk_before_cat + catastrophe_charge)
    is still computed here as a display convenience for an "Insurance Risk"
    UI tab, but is NOT what aggregation.py consumes.

    Class-to-segment mapping — see data_model.py's module docstring for the
    "Miscellaneous class vs Miscellaneous segment" naming collision this
    resolves.
================================================================================
"""

from dataclasses import dataclass
from typing import Dict, List

from engine.rbc.correlation import correlation_aggregate
from engine.rbc.data_model import InsuranceRiskExposures

# ── Class -> segment mapping ────────────────────────────────────────────────
CLASS_TO_SEGMENT: Dict[str, str] = {
    "Motor":         "Motor",
    "Fire":          "Property",
    "Engineering":   "Property",
    "Aviation":      "Property",
    "Marine":        "Property",
    "Liability":     "Liability",
    "Accident":      "Miscellaneous",
    "Bond":          "Miscellaneous",
    "Travel":        "Miscellaneous",
    "Weather":       "Miscellaneous",
    "Miscellaneous": "Credit",   # raw catch-all CLASS -> "Credit" SEGMENT — see data_model.py docstring
}

# ── Segment risk factors (premium, claims reserve) ─────────────────────────
SEGMENT_FACTORS: Dict[str, Dict[str, float]] = {
    "Motor":         {"premium": 0.35, "reserve": 0.25},
    "Property":      {"premium": 0.35, "reserve": 0.35},
    "Liability":     {"premium": 0.45, "reserve": 0.35},
    "Miscellaneous": {"premium": 0.45, "reserve": 0.36},
    "Credit":        {"premium": 0.50, "reserve": 0.40},
}

SEGMENTS: List[str] = ["Motor", "Property", "Liability", "Miscellaneous", "Credit"]
NON_LIFE_CLASSES_IN_MODEL_ORDER: List[str] = list(CLASS_TO_SEGMENT.keys())

CLASS_PREMIUM_RESERVE_CORR = [[1.0, 0.25], [0.25, 1.0]]   # within one class: premium charge vs reserve charge
WITHIN_SEGMENT_CORR = 0.50                                # across classes within the same segment
CATASTROPHE_FACTOR = 0.05                                 # 5.0% of net non-life insurance revenue

# Cross-segment correlation: 25% specifically among {Liability, Property,
# Miscellaneous} (the real template's "Other" category); 50% for every
# other pair (Motor and Credit correlate at 50% with everything, including
# each other).
_LOW_CORR_SEGMENTS = {"Liability", "Property", "Miscellaneous"}


def _cross_segment_corr(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if a in _LOW_CORR_SEGMENTS and b in _LOW_CORR_SEGMENTS:
        return 0.25
    return 0.50


@dataclass
class ClassCharge:
    class_name:      str
    segment:          str
    premium_charge:    float
    reserve_charge:     float
    combined_charge:      float   # premium_charge & reserve_charge combined at 25% correlation


@dataclass
class SegmentCharge:
    segment:  str
    classes:   List[str]
    charge:     float


@dataclass
class InsuranceRiskResult:
    class_charges:            List[ClassCharge]
    segment_charges:           List[SegmentCharge]
    insurance_risk_before_cat:  float   # 5 segments combined via _cross_segment_corr()
    catastrophe_charge:            float   # kept SEPARATE — aggregation.py correlates this as its own top-level item
    total_insurance_risk_scr:         float   # insurance_risk_before_cat + catastrophe_charge — display convenience only, not fed to aggregation.py


def _segment_classes(segment: str) -> List[str]:
    return [cls for cls, seg in CLASS_TO_SEGMENT.items() if seg == segment]


def calculate_insurance_risk(
    exposures: InsuranceRiskExposures,
    net_non_life_insurance_revenue: float,
) -> InsuranceRiskResult:
    """
    Parameters:
        exposures                          : per-class net premium / net claims reserve
        net_non_life_insurance_revenue      : total insurance revenue net of ceded reinsurance
                                               premiums, trailing 12 months — the Catastrophe
                                               charge's exposure base

    Returns:
        InsuranceRiskResult with the full per-class / per-segment breakdown
        and the total_insurance_risk_scr (insurance risk + catastrophe).
    """
    class_charges: List[ClassCharge] = []
    for cls in NON_LIFE_CLASSES_IN_MODEL_ORDER:
        premium = exposures.net_premium.get(cls, 0.0)
        reserve = exposures.net_claims_reserve.get(cls, 0.0)
        if premium == 0.0 and reserve == 0.0:
            continue
        segment = CLASS_TO_SEGMENT[cls]
        factors = SEGMENT_FACTORS[segment]
        premium_charge = factors["premium"] * premium
        reserve_charge = factors["reserve"] * reserve
        combined = correlation_aggregate([premium_charge, reserve_charge], CLASS_PREMIUM_RESERVE_CORR)
        class_charges.append(ClassCharge(
            class_name=cls, segment=segment,
            premium_charge=round(premium_charge, 2), reserve_charge=round(reserve_charge, 2),
            combined_charge=round(combined, 2),
        ))

    segment_charges: List[SegmentCharge] = []
    for segment in SEGMENTS:
        members = [cc for cc in class_charges if cc.segment == segment]
        if not members:
            segment_charges.append(SegmentCharge(segment=segment, classes=[], charge=0.0))
            continue
        charges = [cc.combined_charge for cc in members]
        n = len(charges)
        if n == 1:
            segment_total = charges[0]
        else:
            corr = [[1.0 if i == j else WITHIN_SEGMENT_CORR for j in range(n)] for i in range(n)]
            segment_total = correlation_aggregate(charges, corr)
        segment_charges.append(SegmentCharge(
            segment=segment, classes=[cc.class_name for cc in members], charge=round(segment_total, 2),
        ))

    segment_totals = [sc.charge for sc in segment_charges]
    corr_seg = [[_cross_segment_corr(a, b) for b in SEGMENTS] for a in SEGMENTS]
    insurance_risk_before_cat = correlation_aggregate(segment_totals, corr_seg)

    catastrophe_charge = CATASTROPHE_FACTOR * max(0.0, net_non_life_insurance_revenue)

    return InsuranceRiskResult(
        class_charges=class_charges,
        segment_charges=segment_charges,
        insurance_risk_before_cat=round(insurance_risk_before_cat, 2),
        catastrophe_charge=round(catastrophe_charge, 2),
        total_insurance_risk_scr=round(insurance_risk_before_cat + catastrophe_charge, 2),
    )

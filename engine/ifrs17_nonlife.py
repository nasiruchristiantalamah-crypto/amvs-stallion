"""
================================================================================
NON-LIFE IFRS 17 PAA STATEMENTS
================================================================================
What this file does:
    Takes the already-validated reserving results (engine/runner.py's
    run_nic_summary() — IBNR from chain_ladder.py, OCR/ULAE/UPR/DAC from
    data_loader.py, all validated against PIC's real workpapers) and
    produces the PAA balance sheet building blocks, by class of business,
    Gross/Net/RI:

        LRC (Liability for Remaining Coverage) = UPR - DAC
        LIC (Liability for Incurred Claims)    = IBNR + OCR + Effect of
                                                  Discounting + ULAE + RA
        Total insurance contract liability     = LRC + LIC

    This mirrors the structure of PIC's own "Balance Sheet 2025" /
    "RI Balance Sheet 2025" sheets in Combined IFRS 17 Accounts_Dec
    2025_v2.xlsb (IBNR+OCR -> Effect of Discounting -> Risk Adjustment ->
    LIC subtotal -> UPR -> DAC -> LRC subtotal -> Total Reserve).

Two documented modelling choices (neither is in the raw reserving data,
both are genuine actuarial judgement — flagged the same way the rest of
this engine flags its simplifications):

    1. Effect of Discounting: IBNR/OCR from data_loader.py are UNDISCOUNTED
       nominal reserve estimates. This module discounts them using the
       NIC's own published risk-free yield curve (data/yield_curve.py) at
       a single assumed average claim-payment duration
       (`discount_duration_years`, default 1.5 years — a short-tail P&C
       assumption; the reserving engine doesn't produce a full run-off
       payment pattern to duration-match more precisely than that).
    2. Risk Adjustment: computed as `ra_loading` (default 15%) x the
       undiscounted best estimate (IBNR + OCR) — a simple percentage
       margin, not a full confidence-level/cost-of-capital model. This is
       the same kind of simplification as the life side's Cost-of-Capital
       RA (engine/assumptions.py) — a defensible placeholder methodology,
       not a stochastic model.

PAA onerous contract test (IFRS 17 Para. 57-58):
    A class is onerous when LRC would be negative — i.e. when Deferred
    Acquisition Costs exceed the Unearned Premium Reserve net of them,
    which is the standard PAA proxy trigger for immediate loss
    recognition. This is a simplified proxy: a fuller test would compare
    UPR against the present value of expected future claims cash flows
    for the unexpired risk, which this reserving engine doesn't produce
    (it estimates already-incurred claims via chain ladder, not future
    unexpired-risk cash flows) — flagged here rather than silently assumed
    away.
================================================================================
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from data.yield_curve import load_yield_curve, discount_factor
from engine.runner import run_nic_summary

DEFAULT_RA_LOADING = 0.15                  # 15% margin over best-estimate IBNR+OCR
DEFAULT_DISCOUNT_DURATION_YEARS = 1.5      # short-tail claims run-off assumption

BASES = ("gross", "net", "ri")


@dataclass
class ClassLiability:
    """LRC/LIC building blocks for one class of business, one basis (gross/net/ri)."""
    class_of_business:      str
    basis:                   str
    ibnr:                     float
    ocr:                       float
    ulae:                       float
    upr:                          float
    dac:                            float
    effect_of_discounting:            float
    risk_adjustment:                    float
    lic:                                  float   # ibnr + ocr + effect_of_discounting + ulae + risk_adjustment
    lrc:                                    float   # upr - dac
    is_onerous:                               bool
    loss_component:                             float
    total_liability:                              float   # lic + lrc


def _build_class_liability(
    class_of_business:        str,
    basis:                     str,
    reserving:                  Dict[str, float],
    ra_loading:                   float,
    discount_curve:                 Optional[Dict[int, float]],
    discount_duration_years:          float,
) -> ClassLiability:
    ibnr = reserving["ibnr"]
    ocr  = reserving["ocr"]
    ulae = reserving["ulae"]
    upr  = reserving["upr"]
    dac  = reserving["dac"]

    best_estimate = ibnr + ocr
    if discount_curve:
        df = discount_factor(discount_duration_years, discount_curve)
        effect_of_discounting = best_estimate * (df - 1.0)   # negative: discounting reduces the liability
    else:
        effect_of_discounting = 0.0

    risk_adjustment = ra_loading * best_estimate
    lic = ibnr + ocr + effect_of_discounting + ulae + risk_adjustment

    lrc = upr - dac
    is_onerous = lrc < 0
    loss_component = max(0.0, -lrc) if is_onerous else 0.0

    return ClassLiability(
        class_of_business      = class_of_business,
        basis                    = basis,
        ibnr                       = round(ibnr, 2),
        ocr                          = round(ocr, 2),
        ulae                           = round(ulae, 2),
        upr                               = round(upr, 2),
        dac                                 = round(dac, 2),
        effect_of_discounting                 = round(effect_of_discounting, 2),
        risk_adjustment                          = round(risk_adjustment, 2),
        lic                                         = round(lic, 2),
        lrc                                           = round(lrc, 2),
        is_onerous                                      = is_onerous,
        loss_component                                    = round(loss_component, 2),
        total_liability                                     = round(lic + lrc, 2),
    )


def _aggregate(class_liabilities: List[ClassLiability], basis: str) -> ClassLiability:
    """Sum a list of per-class ClassLiability into a "Total" row for one basis."""
    def s(field_name: str) -> float:
        return round(sum(getattr(c, field_name) for c in class_liabilities), 2)

    return ClassLiability(
        class_of_business = "Total",
        basis              = basis,
        ibnr                = s("ibnr"),
        ocr                   = s("ocr"),
        ulae                    = s("ulae"),
        upr                        = s("upr"),
        dac                          = s("dac"),
        effect_of_discounting          = s("effect_of_discounting"),
        risk_adjustment                   = s("risk_adjustment"),
        lic                                  = s("lic"),
        lrc                                    = s("lrc"),
        is_onerous                                = any(c.is_onerous for c in class_liabilities),
        loss_component                              = s("loss_component"),
        total_liability                                = s("total_liability"),
    )


def generate_nonlife_paa_statements(
    client_id:                 str             = "pic",
    period:                    str             = "FY2025",
    ra_loading:                float           = DEFAULT_RA_LOADING,
    discount_duration_years:  float           = DEFAULT_DISCOUNT_DURATION_YEARS,
    use_discounting:            bool            = True,
    verbose:                      bool            = True,
) -> dict:
    """
    Build the full non-life PAA liability set — LRC, LIC, onerous test, and
    Gross/Net/RI totals — for every reserving class of business, for any
    configured client (see clients/<client_id>/client.yaml).

    Parameters:
        client_id                 : Which insurer (clients/<client_id>/)
        period                    : Reporting period label, e.g. "FY2025"
        ra_loading                : Risk adjustment as a fraction of best-estimate
                                      IBNR+OCR (see module docstring)
        discount_duration_years  : Assumed average duration for discounting
                                      IBNR+OCR (see module docstring)
        use_discounting            : Set False to skip discounting entirely
                                      (effect_of_discounting = 0 for all classes)
        verbose                     : Print progress

    Returns:
        {
            "period": ..., "classes": [...],
            "by_class": {class: {basis: ClassLiability}},
            "totals":   {basis: ClassLiability},   # class_of_business="Total"
            "ra_loading": ..., "discount_duration_years": ...,
            "reserving_summary": <run_nic_summary() output, for traceability>,
        }
    """
    if verbose:
        print(f"\n{'='*60}")
        print(f"  AMVS NON-LIFE PAA STATEMENTS — {period} — client: {client_id}")
        print(f"{'='*60}")

    summary = run_nic_summary(client_id=client_id, verbose=False)
    return _build_statements_from_summary(summary, period, ra_loading, discount_duration_years, use_discounting, verbose)


def generate_nonlife_paa_statements_granular(
    client_id:                 str             = "pic",
    period:                    str             = "FY2025",
    ra_loading:                float           = DEFAULT_RA_LOADING,
    discount_duration_years:  float           = DEFAULT_DISCOUNT_DURATION_YEARS,
    use_discounting:            bool            = True,
    verbose:                      bool            = True,
) -> dict:
    """
    Same as generate_nonlife_paa_statements(), but broken out into PIC's
    real 6-class breakdown (Motor/Fire/Accident/Bonds/Engineering/Marine)
    via engine.runner.run_nic_summary_granular() instead of the 4-class
    Motor/Fire/Accident/Others grouping — see that function's docstring
    for exactly how each class's figures are sourced (direct vs allocated).
    """
    from engine.runner import run_nic_summary_granular

    if verbose:
        print(f"\n{'='*60}")
        print(f"  AMVS NON-LIFE PAA STATEMENTS (6-class) — {period} — client: {client_id}")
        print(f"{'='*60}")

    summary = run_nic_summary_granular(client_id=client_id, verbose=False)
    return _build_statements_from_summary(summary, period, ra_loading, discount_duration_years, use_discounting, verbose)


def _build_statements_from_summary(
    summary: dict, period: str, ra_loading: float, discount_duration_years: float,
    use_discounting: bool, verbose: bool,
) -> dict:
    classes = summary["classes"]

    discount_curve = load_yield_curve() if use_discounting else None
    if verbose and discount_curve:
        print(f"  Discounting: NIC RFR curve, {discount_duration_years}yr assumed duration, "
              f"spot rate ~{discount_curve[round(discount_duration_years)]:.2%}")

    by_class: Dict[str, Dict[str, ClassLiability]] = {}
    for cls in classes:
        by_class[cls] = {}
        for basis in BASES:
            by_class[cls][basis] = _build_class_liability(
                cls, basis, summary["by_class"][cls][basis],
                ra_loading, discount_curve, discount_duration_years,
            )

    totals: Dict[str, ClassLiability] = {}
    for basis in BASES:
        totals[basis] = _aggregate([by_class[cls][basis] for cls in classes], basis)

    if verbose:
        for basis in BASES:
            t = totals[basis]
            print(f"  {basis.upper():5s}  LRC={t.lrc:>14,.0f}  LIC={t.lic:>14,.0f}  "
                  f"Total={t.total_liability:>14,.0f}  Onerous classes: "
                  f"{[c for c in classes if by_class[c][basis].is_onerous]}")
        print(f"{'='*60}\n")

    return {
        "period":                    period,
        "classes":                   classes,
        "by_class":                  by_class,
        "totals":                    totals,
        "ra_loading":                ra_loading,
        "discount_duration_years":  discount_duration_years if use_discounting else None,
        "reserving_summary":         summary,
    }

"""
================================================================================
CHAIN LADDER MODULE
================================================================================
What this file does:
    Projects IBNR (Incurred But Not Reported) reserves from a cumulative
    claims triangle using the standard Chain Ladder method — the general
    insurance (non-life) equivalent of present_value.py on the life side.

    1. Calculates age-to-age (link) development factors — volume-weighted
       average across all origin years that have both the earlier and later
       development age observed
    2. Builds cumulative development factors (CDF) to ultimate for each age
    3. Projects each origin year's latest known cumulative incurred to its
       ultimate loss:  Ultimate[i] = Latest[i] * CDF[age of latest diagonal]
    4. IBNR[i] = Ultimate[i] - Latest[i]

Key actuarial concepts implemented here:
    - Age-to-age factor f(k) = sum(C[i, k+1]) / sum(C[i, k])
      over origin years i that have both columns k and k+1 observed
      (volume-weighted average — the standard Chain Ladder selection)
    - CDF to ultimate at age k = product of f(k), f(k+1), ..., f(last)
    - No tail factor beyond the last observed development period
      (i.e. CDF at the oldest/most-developed age = 1.0) — this matches
      the assumption used in PIC's own reserving workpapers, where
      development factors settle to 1.0 by the last observed age.

Reference workpaper / validation:
    Provident Insurance (PIC) — "2025 IBNR Projection (Gross & Net) - Final.xlsx",
    MOTOR sheet. PIC's own "Selected Gross IBNR" for Motor = GHS 23,004,071.47
    and "Selected Net IBNR" = GHS 13,735,877.64 (blends chain ladder with an
    expected-loss-ratio cross-check and judgmental selection). This module's
    pure volume-weighted Chain Ladder output is validated against those
    figures as a sanity benchmark, not expected to match to the cedi —
    PIC's "Selected" factors include actuarial judgement this module doesn't
    apply.

    ── ACCIDENT CLASS REQUIRES MANUAL OUTLIER EXCLUSION ──────────────────
    Validated against all 4 PIC classes (see tests/test_chain_ladder.py):
    Chain Ladder matched PIC's Selected IBNR closely or exactly for Motor,
    Fire, and Others. Accident did NOT — raw volume-weighted factors came in
    66%+ off PIC's own figure (Accident Net). Cause: PIC's own workbook
    excludes specific outlier origin-year data points from the volume-weighted
    average before selecting a development factor for certain ages (their
    sheet carries explicit True/False inclusion flags per origin year per
    age — Accident's raw age-to-age factors swing as high as 58.9x in one
    year, which this module includes by default and PIC's own process does
    not). Running Accident through this module without first identifying
    and passing the same exclusions via `outlier_exclusion_periods` (see
    calculate_development_factors() / run_chain_ladder() below) will NOT
    reproduce PIC's Selected IBNR for that class — this is expected, not a
    bug, and the exclusion set must be supplied by a reviewing actuary
    (there is no reliable automatic outlier detection here).
================================================================================
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from engine.claims_triangle import ClaimsTriangle


@dataclass
class DevelopmentFactors:
    """
    Age-to-age (link) development factors and their cumulative-to-ultimate products.

    age_to_age      : age_to_age[k] = development factor from age k to age k+1
                       (length = num_periods - 1)
    cdf_to_ultimate : cdf_to_ultimate[k] = factor to project a value observed
                       at age k all the way to ultimate
                       = product(age_to_age[k:])
                       (length = num_periods; last entry is always 1.0 — no
                       tail beyond the last observed development period)
    """
    age_to_age:      List[float]
    cdf_to_ultimate: List[float]


@dataclass
class ChainLadderResult:
    """Full Chain Ladder projection output for one class of business."""
    class_of_business:      str
    origin_years:            List[int]
    development_factors:     DevelopmentFactors
    latest_cumulative:       Dict[int, float]   # origin_year -> latest observed cumulative incurred
    ultimate_losses:         Dict[int, float]   # origin_year -> projected ultimate loss
    ibnr_by_year:            Dict[int, float]   # origin_year -> IBNR (ultimate - latest)
    total_latest_cumulative: float
    total_ultimate:          float
    total_ibnr:              float


def calculate_development_factors(
    triangle:                   ClaimsTriangle,
    outlier_exclusion_periods:  Optional[Dict[int, List[int]]] = None,
) -> DevelopmentFactors:
    """
    Volume-weighted average age-to-age development factors.

    Excel equivalent:
        f(k) = SUM(column k+1 values where column k+1 is observed)
             / SUM(column k values for those same origin years)

    Parameters:
        triangle                   : Cumulative claims triangle
        outlier_exclusion_periods  : optional {development_age_index -> [origin_years
                                       to exclude from that age's factor]}. Mirrors
                                       PIC's own True/False inclusion flags per
                                       origin-year/age cell (see the Accident-class
                                       caveat in this module's header docstring) —
                                       there is no automatic outlier detection here,
                                       the exclusion set must come from a reviewing
                                       actuary's inspection of the raw factors.
                                       e.g. {4: [2021]} excludes origin year 2021's
                                       contribution to the age 4->5 factor only.
    """
    n = triangle.num_periods
    exclusions = outlier_exclusion_periods or {}
    age_to_age: List[float] = []

    for k in range(n - 1):
        excluded_years = set(exclusions.get(k, []))
        numerator   = 0.0
        denominator = 0.0
        for oy in triangle.origin_years:
            if oy in excluded_years:
                continue
            row = triangle.triangle[oy]
            if len(row) > k + 1:      # this origin year has observed both age k and k+1
                denominator += row[k]
                numerator   += row[k + 1]
        age_to_age.append(numerator / denominator if denominator > 0 else 1.0)

    # CDF to ultimate: product of all factors from age k to the tail.
    # No tail beyond the last observed development period (factor = 1.0).
    cdf = [1.0] * n
    running = 1.0
    for k in range(n - 2, -1, -1):
        running *= age_to_age[k]
        cdf[k] = running

    return DevelopmentFactors(age_to_age=age_to_age, cdf_to_ultimate=cdf)


def run_chain_ladder(
    triangle:                   ClaimsTriangle,
    outlier_exclusion_periods:  Optional[Dict[int, List[int]]] = None,
) -> ChainLadderResult:
    """
    Run the full Chain Ladder projection: development factors -> ultimate -> IBNR.

    Parameters:
        triangle                   : A cumulative claims triangle for one class of business
        outlier_exclusion_periods  : optional {development_age_index -> [origin_years to
                                       exclude]} passed through to calculate_development_factors().
                                       Required for the Accident class to reproduce PIC's
                                       Selected IBNR — see this module's header docstring.

    Returns:
        ChainLadderResult with development factors, ultimate losses, and
        IBNR by origin year, plus totals across all origin years.
    """
    triangle.validate()
    factors = calculate_development_factors(triangle, outlier_exclusion_periods)

    latest_cumulative: Dict[int, float] = {}
    ultimate_losses:   Dict[int, float] = {}
    ibnr_by_year:      Dict[int, float] = {}

    for oy in triangle.origin_years:
        latest = triangle.latest_cumulative(oy)
        age    = triangle.latest_dev_age(oy)
        cdf    = factors.cdf_to_ultimate[age]
        ultimate = latest * cdf

        latest_cumulative[oy] = latest
        ultimate_losses[oy]   = ultimate
        ibnr_by_year[oy]      = ultimate - latest

    return ChainLadderResult(
        class_of_business      = triangle.class_of_business,
        origin_years            = triangle.origin_years,
        development_factors     = factors,
        latest_cumulative       = latest_cumulative,
        ultimate_losses         = ultimate_losses,
        ibnr_by_year             = ibnr_by_year,
        total_latest_cumulative = sum(latest_cumulative.values()),
        total_ultimate          = sum(ultimate_losses.values()),
        total_ibnr              = sum(ibnr_by_year.values()),
    )

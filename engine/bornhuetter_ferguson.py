"""
================================================================================
BORNHUETTER-FERGUSON & CREDIBILITY-BLENDED RESERVING MODULE
================================================================================
What this file does:
    Fixes Chain Ladder's classic weak spot — the least-mature origin year,
    where a single small observed diagonal gets multiplied by a large
    development factor and swings wildly. Bornhuetter-Ferguson (BF) instead
    anchors the immature year to an a priori expected loss ratio applied to
    earned premium, and only lets the (small, credible) reported claims to
    date reduce that expectation — rather than projecting off the reported
    claims directly the way Chain Ladder does.

    1. Expected Ultimate = Expected Loss Ratio x Earned Premium  (a priori,
       independent of what's been reported so far)
    2. % Reported = 1 / CDF-to-ultimate at the origin year's current age
       (how much of the eventual claims a Chain Ladder run implies has
       already emerged)
    3. BF IBNR = Expected Ultimate x (1 - % Reported)
    4. BF Ultimate = Latest Cumulative Incurred + BF IBNR

    Then blends Chain Ladder and BF per origin year using % Reported as a
    credibility weight — full weight on Chain Ladder once a year is mature
    (% Reported -> 1), full weight on BF while a year is still thin
    (% Reported -> 0). This is the standard credibility-weighted approach
    (equivalent to one iteration of the Benktander method) and mirrors the
    parallel Chain-Ladder / Expected-Loss-Ratio sections seen side by side
    in PIC's own reserving workpaper — this module blends them
    mathematically instead of by manual override.

Reference workpaper / validation:
    Provident Insurance (PIC) — "2025 IBNR Projection (Gross & Net) - Final.xlsx",
    MOTOR sheet, "Expected Loss Ratio (Gross/Net)" section (earned premium by
    underwriting year) alongside the Chain Ladder triangle.

When to actually use this (validated against all 4 PIC classes — see
tests/test_chain_ladder.py):
    Chain Ladder alone is the default and should be used unless a class has
    BOTH a materially immature latest origin year AND a large, stable enough
    premium history to trust an ELR prior. Validated against PIC's own
    "Selected" IBNR: for Fire and Others, pure Chain Ladder matched PIC's
    figure exactly — BF blending made both WORSE (off by 10x+ for Others,
    because a single large lumpy claim distorts the premium-based ELR prior
    for a small class). BF only helped for Motor Gross (large class, material
    immature year), cutting the variance from PIC's figure roughly in half.
    Don't apply this blindly per class — check against history first.

    ── ACCIDENT CLASS REQUIRES MANUAL OUTLIER EXCLUSION ──────────────────
    Same caveat as chain_ladder.py: this module's blend was off from PIC's
    Selected IBNR by 245%+ on Accident Net. The underlying Chain Ladder
    factors this module builds on (via calculate_development_factors) are
    the root cause — PIC excludes specific outlier origin-year data points
    from those factors before running their own BF step, which this module
    does not do automatically. Pass the same `outlier_exclusion_periods`
    used with chain_ladder.run_chain_ladder() through to
    run_bornhuetter_ferguson() / run_blended_reserving() for Accident, or
    the result will not reproduce PIC's Selected IBNR for that class.
================================================================================
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from engine.claims_triangle import ClaimsTriangle
from engine.chain_ladder import calculate_development_factors, run_chain_ladder


@dataclass
class BFResult:
    """Bornhuetter-Ferguson projection output for one class of business."""
    class_of_business:  str
    origin_years:         List[int]
    expected_loss_ratio:  float
    earned_premium:       Dict[int, float]
    percent_reported:     Dict[int, float]   # 1 / CDF at each origin year's latest observed age
    expected_ultimate:    Dict[int, float]   # a priori: ELR x earned premium
    latest_cumulative:    Dict[int, float]
    bf_ibnr:               Dict[int, float]   # expected_ultimate x (1 - percent_reported)
    bf_ultimate:            Dict[int, float]   # latest_cumulative + bf_ibnr
    total_bf_ibnr:          float
    total_bf_ultimate:      float


@dataclass
class BlendedResult:
    """
    Credibility-weighted blend of Chain Ladder and Bornhuetter-Ferguson,
    per origin year, using % Reported as the credibility weight Z.
    """
    class_of_business:   str
    origin_years:          List[int]
    credibility_weight:    Dict[int, float]   # Z = percent_reported (chain ladder's implied maturity)
    chain_ladder_ultimate: Dict[int, float]
    bf_ultimate:            Dict[int, float]
    blended_ultimate:       Dict[int, float]   # Z * CL_ultimate + (1 - Z) * BF_ultimate
    latest_cumulative:      Dict[int, float]
    blended_ibnr:            Dict[int, float]   # blended_ultimate - latest_cumulative
    total_blended_ibnr:      float
    total_blended_ultimate:  float


def run_bornhuetter_ferguson(
    triangle:                   ClaimsTriangle,
    earned_premium:             Dict[int, float],
    expected_loss_ratio:        float,
    outlier_exclusion_periods:  Optional[Dict[int, List[int]]] = None,
) -> BFResult:
    """
    Run the Bornhuetter-Ferguson projection for a claims triangle.

    Parameters:
        triangle                   : Cumulative claims triangle
        earned_premium             : origin_year -> earned premium (the "exposure"
                                       the expected loss ratio is applied to)
        expected_loss_ratio        : a priori loss ratio assumption (e.g. selected
                                       from the fully-developed years, or a pricing
                                       loss ratio assumption)
        outlier_exclusion_periods  : optional {development_age_index -> [origin_years
                                       to exclude]}, passed straight through to
                                       calculate_development_factors(). Required for
                                       the Accident class — see this module's header.

    Returns:
        BFResult with expected ultimate, % reported, and BF IBNR by origin year.
    """
    triangle.validate()
    factors = calculate_development_factors(triangle, outlier_exclusion_periods)

    percent_reported:  Dict[int, float] = {}
    expected_ultimate: Dict[int, float] = {}
    latest_cumulative: Dict[int, float] = {}
    bf_ibnr:            Dict[int, float] = {}
    bf_ultimate:         Dict[int, float] = {}

    for oy in triangle.origin_years:
        latest = triangle.latest_cumulative(oy)
        age    = triangle.latest_dev_age(oy)
        cdf    = factors.cdf_to_ultimate[age]
        pct_reported = 1.0 / cdf if cdf > 0 else 1.0

        ep       = earned_premium.get(oy, 0.0)
        exp_ult  = expected_loss_ratio * ep
        ibnr     = exp_ult * (1.0 - pct_reported)

        latest_cumulative[oy] = latest
        percent_reported[oy]  = pct_reported
        expected_ultimate[oy] = exp_ult
        bf_ibnr[oy]            = ibnr
        bf_ultimate[oy]         = latest + ibnr

    return BFResult(
        class_of_business     = triangle.class_of_business,
        origin_years            = triangle.origin_years,
        expected_loss_ratio     = expected_loss_ratio,
        earned_premium           = earned_premium,
        percent_reported         = percent_reported,
        expected_ultimate        = expected_ultimate,
        latest_cumulative        = latest_cumulative,
        bf_ibnr                   = bf_ibnr,
        bf_ultimate                = bf_ultimate,
        total_bf_ibnr              = sum(bf_ibnr.values()),
        total_bf_ultimate           = sum(bf_ultimate.values()),
    )


def estimate_expected_loss_ratio(
    triangle:                   ClaimsTriangle,
    earned_premium:             Dict[int, float],
    maturity_threshold:         float = 0.95,
    outlier_exclusion_periods:  Optional[Dict[int, List[int]]] = None,
) -> float:
    """
    Estimate an a priori expected loss ratio for BF from the triangle's own
    mature origin years — a premium-weighted average of (Chain Ladder
    ultimate / earned premium) across origin years that are at least
    `maturity_threshold` developed (% Reported >= threshold).

    This is a practical default when no separate pricing/industry loss ratio
    assumption is supplied. Immature years are excluded because their Chain
    Ladder ultimate is exactly the volatile figure BF exists to correct for
    — using it to derive the BF prior would defeat the purpose.

    outlier_exclusion_periods : optional {development_age_index -> [origin_years
        to exclude]}, passed straight through to calculate_development_factors().
    """
    triangle.validate()
    factors = calculate_development_factors(triangle, outlier_exclusion_periods)

    mature_ultimate = 0.0
    mature_premium  = 0.0
    for oy in triangle.origin_years:
        latest = triangle.latest_cumulative(oy)
        age    = triangle.latest_dev_age(oy)
        cdf    = factors.cdf_to_ultimate[age]
        pct_reported = 1.0 / cdf if cdf > 0 else 1.0
        if pct_reported >= maturity_threshold:
            mature_ultimate += latest * cdf
            mature_premium  += earned_premium.get(oy, 0.0)

    if mature_premium <= 0:
        raise ValueError(
            f"{triangle.class_of_business}: no origin years reached the "
            f"{maturity_threshold:.0%} maturity threshold — cannot estimate "
            f"an expected loss ratio from this triangle; supply one explicitly."
        )
    return mature_ultimate / mature_premium


def run_blended_reserving(
    triangle:                   ClaimsTriangle,
    earned_premium:             Dict[int, float],
    expected_loss_ratio:        float,
    outlier_exclusion_periods:  Optional[Dict[int, List[int]]] = None,
) -> BlendedResult:
    """
    Run Chain Ladder and Bornhuetter-Ferguson together and blend them per
    origin year using % Reported (chain ladder's implied maturity) as the
    credibility weight Z:

        Blended Ultimate = Z * CL_Ultimate + (1 - Z) * BF_Ultimate

    Mature years (Z -> 1) land on Chain Ladder; the thin, most-recent
    origin year (Z -> 0) lands on the BF a priori estimate instead of the
    Chain Ladder's own tail-heavy projection.

    outlier_exclusion_periods : optional {development_age_index -> [origin_years
        to exclude]}, passed straight through to both the Chain Ladder and BF
        legs so the two stay on the same (adjusted) development factors.
    """
    cl = run_chain_ladder(triangle, outlier_exclusion_periods)
    bf = run_bornhuetter_ferguson(triangle, earned_premium, expected_loss_ratio, outlier_exclusion_periods)

    credibility_weight:   Dict[int, float] = {}
    blended_ultimate:      Dict[int, float] = {}
    blended_ibnr:           Dict[int, float] = {}

    for oy in triangle.origin_years:
        z = bf.percent_reported[oy]
        blended = z * cl.ultimate_losses[oy] + (1.0 - z) * bf.bf_ultimate[oy]

        credibility_weight[oy] = z
        blended_ultimate[oy]   = blended
        blended_ibnr[oy]        = blended - cl.latest_cumulative[oy]

    return BlendedResult(
        class_of_business       = triangle.class_of_business,
        origin_years              = triangle.origin_years,
        credibility_weight         = credibility_weight,
        chain_ladder_ultimate       = cl.ultimate_losses,
        bf_ultimate                  = bf.bf_ultimate,
        blended_ultimate              = blended_ultimate,
        latest_cumulative              = cl.latest_cumulative,
        blended_ibnr                    = blended_ibnr,
        total_blended_ibnr               = sum(blended_ibnr.values()),
        total_blended_ultimate            = sum(blended_ultimate.values()),
    )

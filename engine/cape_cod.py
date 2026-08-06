"""
================================================================================
CAPE COD METHOD (STANARD-BÜHLMANN) — self-calibrating IBNR reserving
================================================================================
What this file does:
    A variant of Bornhuetter-Ferguson that removes BF's one manual input —
    the a priori expected loss ratio — by deriving it directly from the
    triangle itself instead of requiring a pricing/industry assumption.

    Where BF's ELR is supplied externally (or, via
    bornhuetter_ferguson.estimate_expected_loss_ratio(), backed out from
    only the FULLY MATURE origin years), Cape Cod uses EVERY origin year,
    weighted by how much of each year's premium has actually "used up" its
    exposure so far:

        Used-Up Premium[oy] = Earned Premium[oy] x % Reported[oy]
        Cape Cod ELR = sum(Latest Cumulative) / sum(Used-Up Premium)

    % Reported[oy] is the same "1 / CDF-to-ultimate at this year's current
    age" figure bornhuetter_ferguson.py computes — a thin, immature year
    contributes little of its premium to the ELR estimate (appropriately,
    since little of its claims experience has emerged yet), while a mature
    year contributes nearly all of it.

    Once the ELR is derived this way, the rest of the calculation is
    IDENTICAL to Bornhuetter-Ferguson (Expected Ultimate = ELR x Earned
    Premium; IBNR = Expected Ultimate x (1 - % Reported)) — this module
    reuses run_bornhuetter_ferguson() directly rather than re-implementing
    that step, so "Cape Cod" here is really "BF with a self-calibrated
    prior," not a separate projection engine.

When to use this over Bornhuetter-Ferguson:
    Prefer Cape Cod when there's no external a priori loss ratio assumption
    to feed BF (no pricing/industry benchmark available or trusted), but an
    "expected" anchor for immature years is still wanted rather than Chain
    Ladder's raw extrapolation. Cape Cod's ELR uses ALL origin years
    (premium-weighted by % reported), so it can still be distorted by the
    same lumpy-claim / thin-class issues documented in chain_ladder.py's and
    bornhuetter_ferguson.py's own module headers — it is not immune to
    those, just self-calibrating.

    Validated against all 4 PIC classes (see tests/test_cape_cod.py, same
    triangles/premium as test_chain_ladder.py). Result: Cape Cod helped for
    Motor Gross (+1.2% vs PIC's Selected IBNR, materially better than pure
    Chain Ladder's +26.5%) — a large class with a stable premium history,
    the case this method is meant for. It did NOT help anywhere else: Fire
    landed at -15.6% (worse than Chain Ladder's exact match), and Accident
    and Others — small, lumpy classes — blew up to +144.7%/+1569.9%
    (Gross) and +619.1%/+1251.0% (Net). Same root cause documented for BF:
    weighting by used-up premium lets one volatile origin year's claims
    distort the derived ELR, which then gets applied to every year's full
    earned premium. Don't apply this blindly per class — check against
    history first, exactly as with Bornhuetter-Ferguson.
================================================================================
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from engine.claims_triangle import ClaimsTriangle
from engine.chain_ladder import calculate_development_factors
from engine.bornhuetter_ferguson import BFResult, run_bornhuetter_ferguson


@dataclass
class CapeCodResult:
    """
    Cape Cod projection output for one class of business — a Bornhuetter-
    Ferguson run using a self-calibrated (rather than externally supplied)
    expected loss ratio. Per-origin-year % reported, expected ultimate,
    IBNR, and ultimate all live on bf_result (identical shape to a direct
    run_bornhuetter_ferguson() call).
    """
    class_of_business:    str
    origin_years:          List[int]
    used_up_premium:        Dict[int, float]   # earned_premium[oy] x percent_reported[oy] — Cape Cod's exposure weight
    expected_loss_ratio:    float               # derived: sum(latest_cumulative) / sum(used_up_premium)
    bf_result:               BFResult


def _used_up_premium_and_elr(
    triangle:                   ClaimsTriangle,
    earned_premium:             Dict[int, float],
    outlier_exclusion_periods:  Optional[Dict[int, List[int]]] = None,
) -> Tuple[Dict[int, float], float]:
    triangle.validate()
    factors = calculate_development_factors(triangle, outlier_exclusion_periods)

    used_up_premium:      Dict[int, float] = {}
    total_latest           = 0.0
    total_used_up_premium  = 0.0
    for oy in triangle.origin_years:
        latest = triangle.latest_cumulative(oy)
        age    = triangle.latest_dev_age(oy)
        cdf    = factors.cdf_to_ultimate[age]
        pct_reported = 1.0 / cdf if cdf > 0 else 1.0

        used_up = earned_premium.get(oy, 0.0) * pct_reported
        used_up_premium[oy]    = used_up
        total_latest          += latest
        total_used_up_premium += used_up

    if total_used_up_premium <= 0:
        raise ValueError(
            f"{triangle.class_of_business}: total used-up premium is zero or "
            f"negative — cannot derive a Cape Cod expected loss ratio from this triangle."
        )
    return used_up_premium, total_latest / total_used_up_premium


def estimate_cape_cod_loss_ratio(
    triangle:                   ClaimsTriangle,
    earned_premium:             Dict[int, float],
    outlier_exclusion_periods:  Optional[Dict[int, List[int]]] = None,
) -> float:
    """
    The self-calibrated expected loss ratio Cape Cod derives from the
    triangle — sum(latest cumulative) / sum(used-up premium). Mirrors
    bornhuetter_ferguson.estimate_expected_loss_ratio()'s signature, but
    uses every origin year (premium-weighted by % reported) rather than
    only the fully mature ones.
    """
    _, elr = _used_up_premium_and_elr(triangle, earned_premium, outlier_exclusion_periods)
    return elr


def run_cape_cod(
    triangle:                   ClaimsTriangle,
    earned_premium:             Dict[int, float],
    outlier_exclusion_periods:  Optional[Dict[int, List[int]]] = None,
) -> CapeCodResult:
    """
    Run the Cape Cod projection for a claims triangle: derive the expected
    loss ratio from the triangle's own used-up premium, then project each
    origin year exactly as Bornhuetter-Ferguson does with that ELR.

    Parameters:
        triangle                   : Cumulative claims triangle
        earned_premium             : origin_year -> earned premium
        outlier_exclusion_periods  : optional {development_age_index -> [origin_years
                                       to exclude]}, passed straight through to
                                       calculate_development_factors() and the
                                       underlying BF run.

    Returns:
        CapeCodResult with the derived ELR, used-up premium by origin year,
        and the full BF projection run with that ELR.
    """
    used_up_premium, elr = _used_up_premium_and_elr(triangle, earned_premium, outlier_exclusion_periods)
    bf = run_bornhuetter_ferguson(triangle, earned_premium, elr, outlier_exclusion_periods)

    return CapeCodResult(
        class_of_business  = triangle.class_of_business,
        origin_years         = triangle.origin_years,
        used_up_premium        = used_up_premium,
        expected_loss_ratio      = elr,
        bf_result                  = bf,
    )

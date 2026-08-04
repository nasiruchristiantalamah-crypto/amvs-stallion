"""
================================================================================
CORRELATION AGGREGATION — shared sqrt(quadratic form) helper
================================================================================
What this file does:
    The GIRBC directive aggregates most risk-charge groups (within a class's
    premium/reserve charges, across classes within a segment, across
    segments, across market risk sub-modules, and across the top-level risk
    categories) the same way: not a simple sum, but

        combined charge = sqrt( charges^T . CorrelationMatrix . charges )

    i.e. the square root of a quadratic form — confirmed directly from the
    real GIRBC SDR Excel template's own formulas (e.g.
    "=SQRT(SUMPRODUCT(charges,MMULT(CorrMatrix,charges)))"). This file is
    the one place that method is implemented, so every risk module
    (insurance_risk.py, market_risk.py) and the top-level aggregator
    (aggregation.py) all use the identical, tested logic — only the
    correlation matrix and inputs differ per use.

    Credit Risk is the one module the directive aggregates by simple sum
    instead (see credit_risk.py) — it does NOT use this helper.
================================================================================
"""

from typing import List


def correlation_aggregate(charges: List[float], correlation_matrix: List[List[float]]) -> float:
    """
    sqrt(charges^T . correlation_matrix . charges).

    Parameters:
        charges              : list of n non-negative capital charges
        correlation_matrix   : n x n matrix, correlation_matrix[i][j] = correlation between charges[i] and charges[j]
                                (diagonal should be 1.0)

    Returns:
        The diversified combined charge — always <= sum(charges) whenever
        every correlation is <= 1.0, since diversification can only reduce
        (never increase) the combined capital requirement relative to a
        naive sum.
    """
    n = len(charges)
    if n == 0:
        return 0.0
    if len(correlation_matrix) != n or any(len(row) != n for row in correlation_matrix):
        raise ValueError(
            f"correlation_matrix must be {n}x{n} to match {n} charges — got "
            f"{len(correlation_matrix)}x{len(correlation_matrix[0]) if correlation_matrix else 0}"
        )

    quadratic_form = 0.0
    for i in range(n):
        row_sum = sum(correlation_matrix[i][j] * charges[j] for j in range(n))
        quadratic_form += charges[i] * row_sum

    # Guard against a tiny negative value from floating-point rounding when
    # the true result is exactly 0 (e.g. all charges 0).
    return quadratic_form ** 0.5 if quadratic_form > 0 else 0.0

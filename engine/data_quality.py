"""
================================================================================
DATA QUALITY — pre-flight checks for uploaded triangle/premium data
================================================================================
What this file does:
    Runs a set of actuarially-meaningful sanity checks on a class's claims
    triangle and premium data BEFORE it's fed into Chain Ladder /
    Bornhuetter-Ferguson / Cape Cod (engine.runner.run_reserving()) —
    catching the kind of data entry error that would otherwise silently
    produce a wrong or misleading IBNR estimate rather than an obvious
    crash. This runs alongside engine/claims_triangle.py's
    ClaimsTriangle.validate() (which only checks the triangle's SHAPE is a
    valid upper-left triangle), not instead of it — this checks the
    VALUES make actuarial sense.

    Nothing here blocks a run — every check returns a reviewable
    DataQualityIssue instead of raising, because some of what it flags (a
    claim recovery producing a small cumulative decrease, a genuinely
    volatile small class) can be legitimate and the decision belongs to a
    reviewing actuary, not this pipeline. See the dashboard's Reserving
    page, which surfaces these alongside the computed IBNR rather than
    hiding them behind a successful-looking run.

Checks implemented:
    1. Decreasing cumulative claims  — a cumulative triangle should never
       decrease along a row; almost always a data entry error (transposed
       columns, wrong sign) rather than a genuine claim recovery.
    2. Negative claims values — cumulative incurred claims are not
       expected to be negative; flagged, not blocked (a large reinsurance
       recovery on the Net side is a real, if unusual, possibility).
    3. Missing or non-positive premium for an origin year that has
       triangle data — Bornhuetter-Ferguson/Cape Cod would silently treat
       that year's expected ultimate as GHS 0 rather than erroring, which
       understates IBNR without any visible sign anything's wrong.
    4. Premium with no matching triangle data — an orphan origin year,
       usually a copy/paste mismatch between the triangle and premium
       tables.
    5. Net exceeds Gross — net of reinsurance is usually LESS than gross;
       when it isn't, the Gross/Net blocks may have been read from the
       wrong columns (see data_loader.py's own NET_COLUMN_OFFSET comment
       for exactly this failure mode) — but confirmed against PIC's own
       real Fire triangle that this genuinely happens with a legitimate
       cause too (facultative/treaty timing can book a Net recovery ahead
       of the matching Gross claim), so this is a "look at this" signal,
       not proof of a parsing error.
    6. Outlier age-to-age development — one origin year's contribution to
       an age-to-age factor is wildly larger than the others at that same
       age. This is the exact manual eyeballing chain_ladder.py's and
       bornhuetter_ferguson.py's own module docstrings describe having to
       do for PIC's Accident class (factors up to 58.9x) — surfacing it
       automatically turns that from "an actuary has to notice it" into
       "the pipeline points at it," with outlier_exclusion_periods already
       there to act on the suggestion.
================================================================================
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from engine.claims_triangle import ClaimsTriangle

SEVERITY_WARNING = "warning"


@dataclass
class DataQualityIssue:
    """One flagged issue. Never raised — always returned in a list for a caller to review/display."""
    severity:           str             # "warning" for every check today — every one of them has a legitimate real-world explanation, not just a data-entry-error one (see module docstring)
    check:              str             # short machine-readable check name, e.g. "decreasing_cumulative"
    class_of_business:   str
    message:              str
    origin_year:            Optional[int] = None
    basis:                    Optional[str] = None   # "gross" or "net", where relevant


def _check_monotonic_and_negative(triangle: Dict[int, List[float]], class_of_business: str, basis: str) -> List[DataQualityIssue]:
    issues = []
    for oy, row in triangle.items():
        for k, v in enumerate(row):
            if v < 0:
                issues.append(DataQualityIssue(
                    severity=SEVERITY_WARNING, check="negative_claims", class_of_business=class_of_business,
                    origin_year=oy, basis=basis,
                    message=(f"{basis.title()} cumulative claims at development age {k} for origin year {oy} "
                              f"is negative (GHS {v:,.2f}) — unusual for a cumulative triangle; confirm this isn't a sign error."),
                ))
            if k > 0 and v < row[k - 1] - 1e-6:
                issues.append(DataQualityIssue(
                    severity=SEVERITY_WARNING, check="decreasing_cumulative", class_of_business=class_of_business,
                    origin_year=oy, basis=basis,
                    message=(f"{basis.title()} cumulative claims for origin year {oy} DECREASE from age {k - 1} "
                              f"(GHS {row[k - 1]:,.2f}) to age {k} (GHS {v:,.2f}) — cumulative claims shouldn't fall "
                              f"unless this reflects a genuine claim recovery/reversal."),
                ))
    return issues


def _check_premium_alignment(triangle: Dict[int, List[float]], premium: Dict[int, float], class_of_business: str, basis: str) -> List[DataQualityIssue]:
    issues = []
    for oy in triangle:
        p = premium.get(oy)
        if p is None or p <= 0:
            issues.append(DataQualityIssue(
                severity=SEVERITY_WARNING, check="missing_premium", class_of_business=class_of_business,
                origin_year=oy, basis=basis,
                message=(f"Origin year {oy} has {basis} claims data but no positive {basis} premium — "
                          f"Bornhuetter-Ferguson/Cape Cod would treat its expected ultimate as GHS 0 for this "
                          f"year, silently understating IBNR."),
            ))
    for oy in premium:
        if oy not in triangle:
            issues.append(DataQualityIssue(
                severity=SEVERITY_WARNING, check="orphan_premium", class_of_business=class_of_business,
                origin_year=oy, basis=basis,
                message=f"Origin year {oy} has {basis} premium but no matching claims triangle row — check for a copy/paste mismatch between the two tables.",
            ))
    return issues


def _check_net_vs_gross(
    gross_triangle: Dict[int, List[float]], net_triangle: Dict[int, List[float]],
    gross_premium: Dict[int, float], net_premium: Dict[int, float], class_of_business: str,
) -> List[DataQualityIssue]:
    issues = []
    for oy, gross_row in gross_triangle.items():
        net_row = net_triangle.get(oy, [])
        for k, gv in enumerate(gross_row):
            nv = net_row[k] if k < len(net_row) else None
            if nv is not None and nv > gv + 1e-6:
                issues.append(DataQualityIssue(
                    severity=SEVERITY_WARNING, check="net_exceeds_gross", class_of_business=class_of_business,
                    origin_year=oy,
                    message=(f"Net cumulative claims (GHS {nv:,.2f}) exceed Gross (GHS {gv:,.2f}) at development "
                              f"age {k} for origin year {oy} — usually means the Gross/Net columns were read "
                              f"from the wrong offset, but can also be legitimate (a Net recovery booked ahead "
                              f"of the matching Gross claim under some treaty structures) — worth a second look either way."),
                ))
    for oy, gp in gross_premium.items():
        np_ = net_premium.get(oy)
        if np_ is not None and np_ > gp + 1e-6:
            issues.append(DataQualityIssue(
                severity=SEVERITY_WARNING, check="net_exceeds_gross", class_of_business=class_of_business,
                origin_year=oy,
                message=f"Net premium (GHS {np_:,.2f}) exceeds Gross premium (GHS {gp:,.2f}) for origin year {oy} — worth confirming this is intentional.",
            ))
    return issues


def _check_outlier_development(
    triangle: Dict[int, List[float]], class_of_business: str, basis: str, outlier_multiple: float = 5.0,
) -> List[DataQualityIssue]:
    """
    Flag an origin year whose age-to-age factor at a given age is at least
    `outlier_multiple` times the average factor every OTHER origin year
    shows at that same age — the automatic version of the manual
    "True/False inclusion flag per cell" review chain_ladder.py's and
    bornhuetter_ferguson.py's own docstrings describe PIC doing by hand
    for the Accident class.
    """
    ct = ClaimsTriangle(class_of_business=class_of_business, origin_years=sorted(triangle.keys()), triangle=triangle)
    try:
        ct.validate()
    except ValueError:
        return []  # shape problems are ClaimsTriangle.validate()'s job, not this check's

    issues = []
    for k in range(ct.num_periods - 1):
        per_year_factor: Dict[int, float] = {}
        for oy in ct.origin_years:
            row = ct.triangle[oy]
            if len(row) > k + 1 and row[k] != 0:
                per_year_factor[oy] = row[k + 1] / row[k]

        if len(per_year_factor) < 2:
            continue

        for oy, factor in per_year_factor.items():
            others = [f for other_oy, f in per_year_factor.items() if other_oy != oy]
            avg_others = sum(others) / len(others)
            if avg_others > 0 and factor > outlier_multiple * avg_others:
                issues.append(DataQualityIssue(
                    severity=SEVERITY_WARNING, check="outlier_development_factor", class_of_business=class_of_business,
                    origin_year=oy, basis=basis,
                    message=(
                        f"Origin year {oy}'s {basis} development factor from age {k} to {k + 1} is {factor:.2f}x "
                        f"— {factor / avg_others:.1f}x the average of every other origin year at this age "
                        f"({avg_others:.2f}x). Consider excluding it via outlier_exclusion_periods "
                        f"({{{k}: [{oy}]}}) before running Chain Ladder/BF/Cape Cod."
                    ),
                ))
    return issues


def check_triangle_quality(
    class_of_business:   str,
    gross_triangle:        Dict[int, List[float]],
    net_triangle:            Dict[int, List[float]],
    gross_premium:             Optional[Dict[int, float]] = None,
    net_premium:                  Optional[Dict[int, float]] = None,
) -> List[DataQualityIssue]:
    """
    Run every data-quality check against one class's Gross/Net triangle
    and (if supplied) premium data. Returns every flagged issue —
    reviewable, not blocking; callers decide what to do with error- vs
    warning-severity issues (see this module's docstring).
    """
    issues: List[DataQualityIssue] = []
    issues += _check_monotonic_and_negative(gross_triangle, class_of_business, "gross")
    issues += _check_monotonic_and_negative(net_triangle, class_of_business, "net")
    issues += _check_outlier_development(gross_triangle, class_of_business, "gross")
    issues += _check_outlier_development(net_triangle, class_of_business, "net")
    issues += _check_net_vs_gross(gross_triangle, net_triangle, gross_premium or {}, net_premium or {}, class_of_business)
    if gross_premium is not None:
        issues += _check_premium_alignment(gross_triangle, gross_premium, class_of_business, "gross")
    if net_premium is not None:
        issues += _check_premium_alignment(net_triangle, net_premium, class_of_business, "net")
    return issues

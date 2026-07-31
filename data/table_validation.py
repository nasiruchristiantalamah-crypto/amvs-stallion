"""
================================================================================
RATE TABLE VALIDATION
================================================================================
What this file does:
    A shared reasonableness check for any duration-keyed rate table — a
    mortality table keyed by age, a lapse schedule keyed by policy year, or
    anything else of the same shape ({int: float} where the float is a
    probability/rate). Used by data/mortality.py (validate_mortality_table)
    and engine/assumptions.py (validate_lapse_schedule).

    Checks for:
        - an empty table (nothing to look up)
        - negative rates (a probability can't be negative)
        - rates above 100% (a probability can't exceed 1.0)
        - non-numeric or non-integer keys
        - gaps in duration coverage (only when the caller requires
          contiguous coverage — a mortality table looked up by direct
          dict[age] access needs every age present, or it KeyErrors at
          runtime; a lapse schedule is deliberately allowed to be sparse —
          see engine/assumptions.py's LapseSchedule.get_annual_rate, which
          holds the last defined rate forward — so contiguity isn't
          required there)

    Collects every problem found and raises them together in one
    ValueError, rather than stopping at the first — fixing a bad table one
    error at a time is tedious; seeing everything wrong with it at once
    isn't.
================================================================================
"""

from typing import Dict


def validate_rate_table(
    table:                Dict[int, float],
    *,
    label:                str,
    key_label:            str   = "duration",
    require_contiguous:   bool  = False,
    max_rate:             float = 1.0,
) -> None:
    """
    Validate a duration-keyed rate table for reasonableness.

    Parameters:
        table                : {duration: rate} to validate
        label                : What to call this table in error messages,
                                 e.g. "mortality table", "lapse schedule"
        key_label            : What to call the key in error messages,
                                 e.g. "age", "policy year"
        require_contiguous   : If True, every integer duration between the
                                 table's min and max key must be present
                                 (no gaps) — required for tables looked up
                                 by direct key access; not required for
                                 tables with step-function fallback.
        max_rate             : Upper bound for a rate (default 1.0 = 100%)

    Raises:
        ValueError, listing every problem found, if the table isn't valid.
        Returns None (no exception) if the table is clean.
    """
    problems = []

    if not table:
        raise ValueError(f"{label} is empty — at least one {key_label} is required.")

    for key, rate in table.items():
        if not isinstance(key, int) or isinstance(key, bool):
            problems.append(f"{key_label} {key!r} is not an integer")
            continue
        if not isinstance(rate, (int, float)) or isinstance(rate, bool):
            problems.append(f"{key_label} {key}: rate {rate!r} is not a number")
            continue
        if rate < 0:
            problems.append(f"{key_label} {key}: rate {rate} is negative — rates must be >= 0")
        if rate > max_rate:
            problems.append(
                f"{key_label} {key}: rate {rate} ({rate:.1%}) exceeds the maximum "
                f"of {max_rate} ({max_rate:.0%}) — a probability can't exceed 100%"
            )

    if require_contiguous:
        int_keys = sorted(k for k in table.keys() if isinstance(k, int) and not isinstance(k, bool))
        if int_keys:
            expected = set(range(int_keys[0], int_keys[-1] + 1))
            missing = sorted(expected - set(int_keys))
            if missing:
                shown = ", ".join(str(m) for m in missing[:10])
                more = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
                problems.append(
                    f"missing {key_label}(s) {shown}{more} — {label} must cover every "
                    f"{key_label} from {int_keys[0]} to {int_keys[-1]} with no gaps, "
                    f"since it's looked up by direct key access"
                )

    if problems:
        bullet_list = "\n".join(f"  - {p}" for p in problems)
        raise ValueError(f"{label} failed validation:\n{bullet_list}")

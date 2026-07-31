"""
================================================================================
BASE MORTALITY TABLE
================================================================================
qx = the probability that a life aged exactly x will die within one year.

SOURCE (read this before using):
    No official "Ghana National Mortality Table (GNM 2020)" was found in any
    client data available to this system. Provident Insurance (PIC) is a
    general/non-life insurer and holds no mortality data at all. GLICO
    Insurance (a Ghana-licensed life insurer) supplied the best available
    real alternative: this table is the "SA Mortality Tables 1985/90" column
    from GLICO's own 2025 GMM actuarial valuation assumptions workbook
    (Assumptions2025GMM - GLICO Life.xlsx, sheet "DECREMENTS"), ages 1-120.

    This is the SAME base table a real Ghana-licensed life insurer applies
    (with its own loading — see below) in its actual NIC-filed IFRS 17
    valuation as at 31 December 2025. It is genuine West African market
    mortality experience in current regulatory use, not a synthetic
    approximation — but it is NOT the specific "GNM 2020" table the request
    asked for, because that table could not be located in any available
    client data.

LIMITATIONS (documented, not hidden):
    1. Origin: this is a South African-origin table (SA 1985/90), used by
       GLICO as their Ghanaian base table in the absence of a robust
       indigenous Ghana table — common regional actuarial practice, but
       worth knowing if you're citing "Ghana-specific" mortality.
    2. No gender split: GLICO's source workbook has a single qx column, not
       separate male/female columns. get_annual_qx()'s `gender` parameter is
       kept for API compatibility and future extension, but currently
       returns the SAME base rate regardless of male/female/unisex — there
       is no real gender differentiation in this table yet.
    3. Ages 100-120 are flat at 0.43966 in the source — GLICO's own
       workbook caps the table there rather than modelling a smoothly
       rising ultimate rate. Carried through as-is (the real source value),
       not smoothed or extrapolated by this module.
    4. This is the UNLOADED base table. GLICO's own actual 2025 valuation
       applies a 70% loading on top of it (i.e. calls get_annual_qx with
       loading=-0.30) — down from 90% in 2024. Callers must supply their
       own appropriate loading via the `loading` parameter; don't assume
       this table alone is a valuation-ready rate.

Structure:
    MORTALITY_TABLE = { age: qx }

How to read it:
    A life aged 35 has a 0.225% chance of dying within the next year,
    before any loading is applied.
================================================================================
"""

from typing import Dict, Optional

from data.table_validation import validate_rate_table

MORTALITY_TABLE = {
    1: 0.00189,   2: 0.00189,   3: 0.00189,   4: 0.00189,   5: 0.00189,
    6: 0.00189,   7: 0.00189,   8: 0.00189,   9: 0.00189,   10: 0.00189,
    11: 0.00189,  12: 0.00189,  13: 0.00189,  14: 0.00189,  15: 0.00189,
    16: 0.0024,   17: 0.00295,  18: 0.0033,   19: 0.00335,  20: 0.00302,
    21: 0.00283,  22: 0.00266,  23: 0.00251,  24: 0.00239,  25: 0.00228,
    26: 0.0022,   27: 0.00213,  28: 0.00208,  29: 0.00205,  30: 0.00204,
    31: 0.00205,  32: 0.00208,  33: 0.00212,  34: 0.00217,  35: 0.00225,
    36: 0.00233,  37: 0.00244,  38: 0.00256,  39: 0.00272,  40: 0.00286,
    41: 0.00304,  42: 0.00325,  43: 0.0035,   44: 0.00377,  45: 0.00408,
    46: 0.00443,  47: 0.00482,  48: 0.00526,  49: 0.00574,  50: 0.00628,
    51: 0.00686,  52: 0.0075,   53: 0.0082,   54: 0.00896,  55: 0.00979,
    56: 0.0107,   57: 0.0117,   58: 0.0128,   59: 0.01401,  60: 0.01536,
    61: 0.01684,  62: 0.01847,  63: 0.02027,  64: 0.02224,  65: 0.0244,
    66: 0.02675,  67: 0.02932,  68: 0.03211,  69: 0.03513,  70: 0.0384,
    71: 0.04192,  72: 0.04572,  73: 0.0498,   74: 0.05419,  75: 0.05894,
    76: 0.06411,  77: 0.06974,  78: 0.07588,  79: 0.08259,  80: 0.08992,
    81: 0.09792,  82: 0.10664,  83: 0.11613,  84: 0.12645,  85: 0.13764,
    86: 0.14976,  87: 0.16286,  88: 0.17699,  89: 0.1922,   90: 0.20842,
    91: 0.2258,   92: 0.2444,   93: 0.26425,  94: 0.28538,  95: 0.30784,
    96: 0.33161,  97: 0.35671,  98: 0.38311,  99: 0.41078,  100: 0.43966,
    101: 0.43966, 102: 0.43966, 103: 0.43966, 104: 0.43966, 105: 0.43966,
    106: 0.43966, 107: 0.43966, 108: 0.43966, 109: 0.43966, 110: 0.43966,
    111: 0.43966, 112: 0.43966, 113: 0.43966, 114: 0.43966, 115: 0.43966,
    116: 0.43966, 117: 0.43966, 118: 0.43966, 119: 0.43966, 120: 0.43966,
}

_MIN_AGE = min(MORTALITY_TABLE)
_MAX_AGE = max(MORTALITY_TABLE)


def validate_mortality_table(table: Optional[Dict[int, float]] = None) -> None:
    """
    Check a mortality table for reasonableness: no negative qx, none above
    100%, and no gaps in age coverage (get_annual_qx does a direct
    table[age] lookup after clamping to the table's own min/max age, so a
    missing age in the middle of the range would raise a bare KeyError at
    projection time instead of a clear message at load time).

    Validates the module's own MORTALITY_TABLE if no table is supplied —
    this runs automatically at import time (see bottom of this file), so
    a broken edit to the table above is caught immediately, not at
    projection time.

    Raises:
        ValueError, listing every problem found, if the table isn't valid.
    """
    validate_rate_table(
        table if table is not None else MORTALITY_TABLE,
        label="mortality table", key_label="age", require_contiguous=True,
    )


def get_annual_qx(age: int, gender: str = "unisex", loading: float = -0.20) -> float:
    """
    Get the annual mortality rate (qx) for a given age, with an optional
    loading factor applied.

    Parameters:
        age     : Integer age of the life (clamped to [1, 120] — the source
                  table's range)
        gender  : "male", "female", or "unisex" — accepted for API
                  compatibility, but currently has no effect: the source
                  table has no gender split (see module docstring, point 2).
        loading : Adjustment to the base table. -0.20 means 20% better than
                  table. Positive = worse mortality, negative = better.
                  GLICO's own actual 2025 basis is -0.30 (70% of table).

    Returns:
        Loaded annual qx as a float, clamped to [0, 1].

    Example:
        get_annual_qx(35, loading=-0.30)
        -> table qx at age 35 (0.00225), reduced by 30% -> 0.001575
    """
    age = max(_MIN_AGE, min(age, _MAX_AGE))
    base_qx = MORTALITY_TABLE[age]

    # Loaded qx = base_qx * (1 + loading)
    # e.g. loading = -0.20 -> loaded_qx = base_qx * 0.80
    loaded_qx = base_qx * (1 + loading)

    return max(0.0, min(1.0, loaded_qx))


def annual_to_monthly_qx(annual_qx: float) -> float:
    """
    Convert an annual mortality rate to a monthly rate.

    Formula: monthly_qx = 1 - (1 - annual_qx)^(1/12)
    Standard actuarial conversion assuming constant force of mortality.

    Parameters:
        annual_qx : Annual probability of death (e.g. 0.00225)

    Returns:
        Monthly probability of death

    Example:
        annual_to_monthly_qx(0.00225) -> approximately 0.0001878
    """
    return 1 - (1 - annual_qx) ** (1 / 12)


# Self-check at import time: catch a broken edit to MORTALITY_TABLE above
# immediately, rather than at projection time.
validate_mortality_table()

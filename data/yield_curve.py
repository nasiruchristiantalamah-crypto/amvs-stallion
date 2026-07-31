"""
================================================================================
NIC RISK-FREE RATE (RFR) YIELD CURVE
================================================================================
What this file does:
    Loads the National Insurance Commission's published risk-free rate term
    structure, used to discount IBNR/OCR claims reserves to present value
    for the non-life Liability for Incurred Claims (LIC) — IFRS 17 Para 36
    / B72-B85, the same "bottom-up" discount methodology referenced on the
    life side of this engine (engine/assumptions.py's valuation_rate_pa).

SOURCE:
    C:\\Users\\Christian\\OneDrive - Stallion Consultants Ltd\\Data_Nasiru
    \\NIC Reports and Directives\\NIC Rates, Curves, and Reports
    \\20251231_NIC_RFR_16012025.xlsx
    Sheet "CurrentYearResult" — Ghana Cedis spot rate column, years 1-80,
    "Risk-free curves as of 31 December 2025", produced by the NIC (GHS
    fitted from GFIM observations; USD/GBP/EUR sourced separately and also
    available via this loader).

    This is the genuine NIC-published curve, not a client-specific copy —
    confirmed by cross-checking against the identical curve embedded in
    GLICO Insurance's own 2025 GMM valuation assumptions file
    (20251231_NIC_RFR_YieldCurve.xlsx), which carries the same figures.

Structure:
    {duration_year: annual spot rate}, e.g. {1: 0.1356, 2: 0.1498, ...}
================================================================================
"""

from typing import Dict

import openpyxl

DEFAULT_YIELD_CURVE_PATH = (
    r"C:\Users\Christian\OneDrive - Stallion Consultants Ltd\Data_Nasiru"
    r"\NIC Reports and Directives\NIC Rates, Curves, and Reports\20251231_NIC_RFR_16012025.xlsx"
)

# 0-indexed column offset of each currency's Spot Rate column, in the
# "CurrentYearResult" sheet's row tuples (Year, GHS Spot, GHS Fwd, blank,
# USD Spot, USD Fwd, blank, GBP Spot, GBP Fwd, blank, EUR Spot, EUR Fwd).
_CURRENCY_SPOT_COLUMNS = {"GHS": 1, "USD": 4, "GBP": 7, "EUR": 10}


def load_yield_curve(path: str = DEFAULT_YIELD_CURVE_PATH, currency: str = "GHS") -> Dict[int, float]:
    """
    Read the NIC RFR yield curve and return {year: spot_rate}.

    Parameters:
        path     : workbook path (defaults to the NIC-published curve)
        currency : "GHS" (default), "USD", "GBP", or "EUR"

    Returns:
        {duration_year: annual spot rate}
    """
    if currency not in _CURRENCY_SPOT_COLUMNS:
        raise ValueError(f"Unknown currency '{currency}' — choose from {sorted(_CURRENCY_SPOT_COLUMNS)}")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["CurrentYearResult"]

        header_row_num = None
        for row in ws.iter_rows(values_only=False):
            if row and row[0].value == "Year":
                header_row_num = row[0].row
                break
        if header_row_num is None:
            raise ValueError(f"Could not find the 'Year' header row in {path}, sheet 'CurrentYearResult'")

        spot_col = _CURRENCY_SPOT_COLUMNS[currency]
        curve: Dict[int, float] = {}
        for row in ws.iter_rows(min_row=header_row_num + 1, values_only=True):
            year = row[0]
            if year is None:
                break
            curve[int(year)] = float(row[spot_col])
        return curve
    finally:
        wb.close()


def discount_factor(duration_years: float, curve: Dict[int, float]) -> float:
    """
    Discount factor for a cash flow `duration_years` from now, using the
    curve's spot rate at the nearest whole year to that duration (a
    duration beyond the curve's longest published year uses that year's
    rate — the curve here runs to 80 years, so this only matters for
    unusually long-tailed liabilities).

    DF = 1 / (1 + spot_rate) ** duration_years
    """
    if not curve:
        raise ValueError("Empty yield curve — cannot compute a discount factor")
    years = sorted(curve.keys())
    nearest = min(years, key=lambda y: abs(y - duration_years))
    spot = curve[nearest]
    return 1.0 / (1.0 + spot) ** duration_years

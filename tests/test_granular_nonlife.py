"""
================================================================================
6-CLASS GRANULAR NON-LIFE VALIDATION — Provident Insurance (PIC)
================================================================================
What this file does:
    Validates engine.runner.run_nic_summary_granular() /
    engine.ifrs17_nonlife.generate_nonlife_paa_statements_granular() against
    PIC's real published 6-class Balance Sheet ("Combined IFRS 17
    Accounts_Dec 2025_v2.xlsb"'s "Balance Sheet 2025" sheet, and "RI
    Balance sheet & Income statement_v2.xlsx"'s "RI Balancesheet 2025").

    UPR, DAC, and LRC are exact for every one of the 6 classes (Motor,
    Fire, Accident, Bonds, Engineering, Marine) — read directly from PIC's
    own source data at native 6-class resolution, no allocation involved.
    IBNR is exact for Motor, Fire, and Accident (PIC's own IBNR/URR split
    workbook provides these directly per class) but only approximate for
    Bonds/Engineering/Marine (allocated from a combined 4-bucket "Others"
    total — PIC's real per-class split reflects large-claims judgement not
    visible in the source files; see engine/runner.py's
    run_nic_summary_granular() docstring) — those three are deliberately
    NOT tested to a tight tolerance here.

Run with:
    cd amvs
    pytest tests/test_granular_nonlife.py -s
================================================================================
"""

import pytest

from engine.ifrs17_nonlife import generate_nonlife_paa_statements_granular
from engine.runner import run_nic_summary_granular

# PIC's real published Balance Sheet 2025 (gross), GHS — from
# "Combined IFRS 17 Accounts_Dec 2025_v2.xlsb"
REAL_GROSS_UPR = {
    "Accident": 2689479.0, "Bonds": 7287423.0, "Engineering": 2994339.0,
    "Fire": 3997560.0, "Marine": 415635.0, "Motor": 42228842.0,
}
REAL_GROSS_DAC = {
    "Accident": 489373.0, "Bonds": 1135499.0, "Engineering": 554081.0,
    "Fire": 727631.0, "Marine": 75124.0, "Motor": 7719490.0,
}
REAL_GROSS_LRC = {
    "Accident": 2200106.0, "Bonds": 6151924.0, "Engineering": 2440258.0,
    "Fire": 3269928.0, "Marine": 340510.0, "Motor": 34509352.0,
}
# PIC's own IBNR/URR split workbook — exact for Motor/Fire/Accident (direct
# per-class chain-ladder result), Bonds/Engineering/Marine intentionally excluded.
REAL_GROSS_IBNR_EXACT_CLASSES = {"Motor": 6958867.0, "Fire": 221453.0, "Accident": 782518.0}


@pytest.fixture(scope="module")
def statements():
    return generate_nonlife_paa_statements_granular(client_id="pic", period="FY2025", verbose=False)


@pytest.fixture(scope="module")
def summary():
    return run_nic_summary_granular(client_id="pic", verbose=False)


def test_six_classes_present(statements):
    assert set(statements["classes"]) == {"Motor", "Fire", "Accident", "Bonds", "Engineering", "Marine"}


@pytest.mark.parametrize("cls", ["Accident", "Bonds", "Engineering", "Fire", "Marine", "Motor"])
def test_upr_dac_lrc_exact_for_every_class(statements, cls):
    """UPR/DAC/LRC are read directly from PIC's own native 6-class source
    data — no allocation or approximation involved, so these should match
    PIC's real published figures almost exactly for all 6 classes."""
    c = statements["by_class"][cls]["gross"]
    assert abs(c.upr - REAL_GROSS_UPR[cls]) / REAL_GROSS_UPR[cls] < 0.001, f"{cls}: UPR mismatch"
    assert abs(c.dac - REAL_GROSS_DAC[cls]) / REAL_GROSS_DAC[cls] < 0.001, f"{cls}: DAC mismatch"
    assert abs(c.lrc - REAL_GROSS_LRC[cls]) / REAL_GROSS_LRC[cls] < 0.001, f"{cls}: LRC mismatch"


@pytest.mark.parametrize("cls", ["Motor", "Fire", "Accident"])
def test_ibnr_exact_for_classes_with_direct_split(summary, cls):
    """Motor/Fire/Accident IBNR comes directly from PIC's own IBNR/URR
    split workbook per class — no allocation — so this should match PIC's
    real published Gross IBNR almost exactly."""
    ours = summary["by_class"][cls]["gross"]["ibnr"]
    real = REAL_GROSS_IBNR_EXACT_CLASSES[cls]
    assert abs(ours - real) / real < 0.001, f"{cls}: IBNR mismatch (ours={ours:,.0f}, real={real:,.0f})"


def test_total_gross_ibnr_matches_pic_published_total(summary):
    """Sum across all 6 classes should match PIC's real published total
    (8,251k) even though the Bonds/Engineering/Marine split within it is
    only an approximation — allocation redistributes the 'Others' total,
    it doesn't change it."""
    total = summary["totals"]["gross"]["ibnr"]
    assert abs(total - 8251210.0) / 8251210.0 < 0.001


def test_allocated_classes_sum_to_others_total(summary):
    """Bonds + Engineering + Marine IBNR should sum to exactly the same
    'Others' total the 4-class engine computes — proves the allocation
    redistributes without gaining or losing money, even though the
    individual class split is approximate."""
    from engine.runner import run_nic_summary
    base = run_nic_summary(client_id="pic", verbose=False)
    others_total = base["by_class"]["Others"]["gross"]["ibnr"]
    allocated_sum = sum(summary["by_class"][c]["gross"]["ibnr"] for c in ("Bonds", "Engineering", "Marine"))
    assert abs(allocated_sum - others_total) < 0.01


def test_gross_lrc_total_matches_pic_published_total(statements):
    t = statements["totals"]["gross"]
    assert abs(t.lrc - 48912077.0) / 48912077.0 < 0.001

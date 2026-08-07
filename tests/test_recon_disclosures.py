"""
================================================================================
RECON DISCLOSURES — validation
================================================================================
What this file does:
    Validates engine/recon_disclosures.py — the IFRS 17 LRC/LIC roll-
    forward disclosure table, matching PIC's own real "Recon Disclosures"
    sheet layout. Covers the internal consistency invariant (closing_total
    must always reconcile to ClassLiability.total_liability, since that's
    the whole point of a disclosure note — it can't show a different
    number than the balance sheet it's footnoting) on both a synthetic
    non-onerous class and the real PIC portfolio.

Run with:
    cd amvs
    pytest tests/test_recon_disclosures.py -s
================================================================================
"""

import pytest

from engine.ifrs17_nonlife import ClassLiability, generate_nonlife_paa_statements
from engine.data_loader import load_paid_claims
from engine.journals import compute_movements
from engine.recon_disclosures import build_recon_disclosure


def _liability(**overrides) -> ClassLiability:
    base = dict(
        class_of_business="Test", basis="gross", ibnr=0.0, ocr=0.0, ulae=0.0, upr=0.0, dac=0.0,
        effect_of_discounting=0.0, risk_adjustment=0.0, lic=0.0, lrc=0.0, is_onerous=False,
        loss_component=0.0, total_liability=0.0,
    )
    base.update(overrides)
    return ClassLiability(**base)


def test_closing_total_matches_total_liability_non_onerous():
    current = _liability(ibnr=1000.0, ocr=200.0, risk_adjustment=100.0, lic=1300.0, upr=500.0, dac=50.0, lrc=450.0, total_liability=1750.0)
    movements = compute_movements(current, paid_claims=0.0)
    rd = build_recon_disclosure(current, movements, "gross")
    assert rd.closing_total == pytest.approx(current.total_liability)
    assert rd.closing_loss_component == 0.0
    assert rd.closing_lrc_excl_lc == current.lrc


def test_opening_balance_carries_from_prior_period():
    prior = _liability(lrc=100.0, lic=200.0, loss_component=0.0, total_liability=300.0)
    current = _liability(lrc=150.0, lic=250.0, loss_component=0.0, total_liability=400.0)
    movements = compute_movements(current, paid_claims=0.0, prior=prior)
    rd = build_recon_disclosure(current, movements, "gross", prior=prior)
    assert rd.opening_lrc_excl_lc == 100.0
    assert rd.opening_lic == 200.0
    assert rd.opening_total == 300.0


def test_day_one_recognition_has_zero_opening_balance():
    current = _liability(lrc=450.0, lic=1300.0, total_liability=1750.0)
    movements = compute_movements(current, paid_claims=0.0)
    rd = build_recon_disclosure(current, movements, "gross")   # prior omitted -> day 1
    assert rd.opening_total == 0.0
    assert rd.changes_past_service == 0.0   # no prior estimate to have changed relative to


def test_insurance_service_result_is_revenue_minus_expenses():
    current = _liability(ibnr=1000.0, upr=500.0, lrc=500.0, lic=1000.0, total_liability=1500.0)
    prior = _liability(upr=200.0, lrc=200.0)   # gives premium_earned a nonzero value
    movements = compute_movements(current, paid_claims=0.0, prior=prior)
    rd = build_recon_disclosure(current, movements, "gross", prior=prior)
    assert rd.insurance_service_result == pytest.approx(rd.insurance_revenue - rd.insurance_service_expenses_total)


def test_ri_basis_is_labelled_correctly():
    current = _liability(basis="ri", lrc=100.0, lic=200.0, total_liability=300.0)
    movements = compute_movements(current, paid_claims=0.0)
    rd = build_recon_disclosure(current, movements, "ri")
    assert rd.basis == "ri"


# ── Real PIC portfolio total ─────────────────────────────────────────────────

def test_real_pic_gross_recon_closes_to_the_real_total_liability():
    statements = generate_nonlife_paa_statements(verbose=False)
    paid = load_paid_claims()
    total_paid = sum(paid.values())

    gross_total = statements["totals"]["gross"]
    movements = compute_movements(gross_total, total_paid)
    rd = build_recon_disclosure(gross_total, movements, "gross")

    assert rd.closing_total == pytest.approx(gross_total.total_liability)
    print(f"\nGross Recon Disclosures — Net closing balance: GHS {rd.closing_total:,.2f} "
          f"(matches ClassLiability.total_liability exactly)")


def test_real_pic_ri_recon_closes_to_the_real_total_liability():
    statements = generate_nonlife_paa_statements(verbose=False)
    ri_total = statements["totals"]["ri"]
    movements = compute_movements(ri_total, paid_claims=0.0)
    rd = build_recon_disclosure(ri_total, movements, "ri")

    assert rd.closing_total == pytest.approx(ri_total.total_liability)
    print(f"\nRI Recon Disclosures — Net closing balance: GHS {rd.closing_total:,.2f} "
          f"(matches ClassLiability.total_liability exactly)")

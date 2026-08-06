"""
================================================================================
INVESTMENT ANALYSIS + COMPREHENSIVE SENSITIVITY — validation
================================================================================
What this file does:
    Validates engine/investment_analysis.py (the savings-fund projection
    behind the Sensitivity Analysis page's Investment Analysis panel) against
    a real client workbook's own figures, and validates the extended
    engine/custom_pricing.py sensitivity sweep (SENSITIVITY_STRESSES /
    SENSITIVITY_STRESSES_SUMMARY split, IFRS 17 fields per stress) added
    alongside it.
================================================================================
"""

import pytest

from engine.assumptions_manager import AssumptionSet
from engine.custom_pricing import (
    build_custom_product, run_custom_pricing, run_custom_rate_table, run_custom_sensitivity,
    SENSITIVITY_STRESSES, SENSITIVITY_STRESSES_SUMMARY,
)
from engine.investment_analysis import project_investment_fund, summarise_investment_fund


def _ghana_assumptions(entry_age=35):
    return AssumptionSet.ghana_defaults().to_product_assumptions(entry_age_main=entry_age)


# ── engine/investment_analysis.py ────────────────────────────────────────────

def test_matches_real_workbook_figure_exactly():
    # Afentoboa Plus's own InvestAnalysis sheet: GHS 5/day (GHS 150/month)
    # contribution, GHS 30/month risk premium -> GHS 120/month investment
    # portion, 24 months at 5% p.a. credited -> GHS 3,022.31 (cell G12,
    # to the cent). This is the real, external reference point this
    # feature was built to reproduce.
    rows = project_investment_fund(monthly_contribution=150.0, risk_premium=30.0, credited_rate_pa=0.05, term_months=24)
    assert len(rows) == 24
    assert rows[-1]["closing_balance"] == pytest.approx(3022.31, abs=0.01)


def test_monthly_rows_are_month_indexed_not_annual():
    rows = project_investment_fund(200.0, 50.0, 0.05, 36)
    assert [r["month"] for r in rows] == list(range(1, 37))


def test_balance_only_grows_from_the_investment_portion_not_the_full_contribution():
    rows = project_investment_fund(monthly_contribution=100.0, risk_premium=40.0, credited_rate_pa=0.06, term_months=12)
    assert all(r["investment_portion"] == pytest.approx(60.0) for r in rows)
    assert all(r["contribution"] == pytest.approx(100.0) for r in rows)


def test_risk_premium_exceeding_contribution_clamps_investment_portion_to_zero():
    # A product whose risk premium is already more than the illustrative
    # contribution has nothing left to save -> fund never grows, never
    # goes negative.
    rows = project_investment_fund(monthly_contribution=50.0, risk_premium=80.0, credited_rate_pa=0.05, term_months=12)
    assert all(r["investment_portion"] == 0.0 for r in rows)
    assert all(r["closing_balance"] == 0.0 for r in rows)


def test_summary_reconciles_to_the_monthly_rows():
    rows = project_investment_fund(150.0, 30.0, 0.05, 24)
    summary = summarise_investment_fund(rows)
    assert summary["closing_balance"] == rows[-1]["closing_balance"]
    assert summary["term_months"] == 24
    assert summary["total_invested"] == pytest.approx(120.0 * 24, abs=0.01)
    assert summary["total_interest_credited"] == pytest.approx(summary["closing_balance"] - summary["total_invested"], abs=0.01)


def test_summary_of_empty_projection_is_zeroed_not_an_error():
    summary = summarise_investment_fund([])
    assert summary == {
        "investment_portion_monthly": 0.0, "total_invested": 0.0,
        "total_interest_credited": 0.0, "closing_balance": 0.0, "term_months": 0,
    }


# ── engine/custom_pricing.py — extended sensitivity sweep ───────────────────

def test_summary_sweep_is_a_strict_subset_of_the_comprehensive_sweep():
    comprehensive_labels = {s[0] for s in SENSITIVITY_STRESSES}
    summary_labels = {s[0] for s in SENSITIVITY_STRESSES_SUMMARY}
    assert summary_labels.issubset(comprehensive_labels)
    # The comprehensive sweep must be genuinely bigger -- finer gradations
    # (+-5%/10%/20%) and assumption types the summary tab never stressed.
    assert len(SENSITIVITY_STRESSES) > len(SENSITIVITY_STRESSES_SUMMARY)
    assert "Investment return +2%" in comprehensive_labels
    assert "Investment return +2%" not in summary_labels
    assert "Mortality +5%" in comprehensive_labels
    assert "Mortality +5%" not in summary_labels


def test_comprehensive_sweep_includes_ifrs17_fields_per_stress():
    spec = {
        "product_name": "Test", "product_type": "whole_life",
        "riders": [{"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 5000}],
    }
    product = build_custom_product(spec)
    results = run_custom_sensitivity(product, _ghana_assumptions(35), stresses=SENSITIVITY_STRESSES)
    assert len(results) == len(SENSITIVITY_STRESSES) + 1  # + Base case
    for row in results:
        if "error" in row:
            continue
        assert "is_onerous" in row
        assert "csm_at_inception" in row


def test_finer_mortality_gradations_scale_monotonically():
    spec = {
        "product_name": "Test", "product_type": "whole_life",
        "riders": [{"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 5000}],
    }
    product = build_custom_product(spec)
    results = run_custom_sensitivity(product, _ghana_assumptions(35), stresses=SENSITIVITY_STRESSES)
    by_label = {r["assumption_stressed"]: r["stressed_premium"] for r in results}
    # Heavier mortality stresses must raise the premium by MORE the larger the stress.
    assert by_label["Mortality +5%"] < by_label["Mortality +10%"] < by_label["Mortality +20%"]
    assert by_label["Mortality -5%"] > by_label["Mortality -10%"] > by_label["Mortality -20%"]


# ── engine/custom_pricing.py — rate table now carries IFRS 17 fields ────────

def test_rate_table_rows_carry_csm_and_lrc():
    spec = {
        "product_name": "Test", "product_type": "whole_life",
        "riders": [{"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 5000}],
    }
    product = build_custom_product(spec)
    table = run_custom_rate_table(product, _ghana_assumptions(35), age_start=35, age_end=35)
    row = table[35]
    assert "csm_at_inception" in row
    assert "lrc_total" in row


# ── outputs/custom_pricing_excel_exporter.py — native charts ────────────────

def test_excel_export_embeds_charts_on_every_relevant_sheet():
    import openpyxl
    import os
    from outputs.custom_pricing_excel_exporter import export_custom_pricing_to_excel

    spec = {
        "product_name": "Chart Export Test", "product_type": "endowment", "policy_term_years": 10,
        "riders": [{"name": "Death Benefit", "benefit_type": "death", "benefit_amount": 5000}],
    }
    product = build_custom_product(spec)
    assumptions = _ghana_assumptions(35)
    result = run_custom_pricing(product, assumptions, verbose=False)
    rate_table = run_custom_rate_table(product, assumptions, 30, 40)
    sensitivity = run_custom_sensitivity(product, assumptions, stresses=SENSITIVITY_STRESSES)

    risk_premium = result["monthly_premium"]
    rows = project_investment_fund(150.0, risk_premium, 0.05, 24)
    investment_analysis = {
        "risk_premium": risk_premium, "credited_rate_pa": 0.05, "term_months": 24,
        "funds": [{"monthly_contribution": 150.0, "monthly_projection": rows, "summary": summarise_investment_fund(rows)}],
        "product_name": product.name,
    }

    path = export_custom_pricing_to_excel(
        result, {"product_name": product.name}, rate_table=rate_table,
        sensitivity=sensitivity, investment_analysis=investment_analysis,
    )
    try:
        wb = openpyxl.load_workbook(path)
        for sheet_name in ("Reserve Projection", "Profit Signature", "Sensitivity Analysis", "Investment Analysis"):
            assert sheet_name in wb.sheetnames
            assert len(wb[sheet_name]._charts) >= 1, f"{sheet_name} has no embedded chart"
    finally:
        os.remove(path)

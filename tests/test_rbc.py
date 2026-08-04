"""
================================================================================
RBC / GIRBC SOLVENCY — validation tests
================================================================================
What this file does:
    Unit tests for engine/rbc/ (correlation math, the four risk modules,
    aggregation) using hand-computable synthetic cases, PLUS validation
    against QIC's real 2025 GIRBC filing (Worksheets/
    QIC_GIRBC_Official_Return_2025 (version 1).xlsx) — real figures
    hardcoded here (not read live) so this test suite doesn't depend on a
    real client's private workbook being present on whatever machine runs
    it, matching this project's existing pattern (see
    tests/test_chain_ladder.py's header comment for the same convention).

    Operational Risk and the top-level correlation aggregation (given the
    REAL 4-way structure — Insurance/Catastrophe/Market/Credit, not this
    engine's own simplified 3-way default, see aggregation.py's module
    docstring) both reproduce QIC's real filed figures EXACTLY. Every other
    module currently has a small, disclosed, quantified gap vs the real
    filing — see each module's own docstring for the specific cause; these
    tests assert against THIS engine's own current behaviour (documented
    simplifications and all), not against the real filing, except where
    noted as an exact-match check.

    A separate, live-file integration test (test_load_rbc_solvency_data_qic)
    calls engine.data_loader.load_rbc_solvency_data("qic") against the real
    workbook and is skipped automatically if that file isn't present on
    this machine.
================================================================================
"""

import os

import pytest

from engine.clients import load_client
from engine.rbc.aggregation import calculate_solvency
from engine.rbc.correlation import correlation_aggregate
from engine.rbc.credit_risk import calculate_credit_risk
from engine.rbc.data_model import (
    CreditRiskExposures, InsuranceRiskExposures, LegacySolvencyInputs, MarketRiskExposures,
    OperationalRiskExposures, QualifyingCapitalResources,
)
from engine.rbc.insurance_risk import calculate_insurance_risk
from engine.rbc.legacy_solvency import calculate_legacy_solvency
from engine.rbc.market_risk import calculate_market_risk
from engine.rbc.operational_risk import calculate_operational_risk


# ── correlation.py ───────────────────────────────────────────────────────────

def test_correlation_perfect_correlation_equals_sum():
    assert correlation_aggregate([10.0, 20.0], [[1, 1], [1, 1]]) == pytest.approx(30.0)


def test_correlation_zero_correlation_equals_pythagorean_sum():
    assert correlation_aggregate([3.0, 4.0], [[1, 0], [0, 1]]) == pytest.approx(5.0)


def test_correlation_empty_is_zero():
    assert correlation_aggregate([], []) == 0.0


def test_correlation_diversification_never_exceeds_naive_sum():
    charges = [10.0, 20.0, 5.0]
    corr = [[1.0 if i == j else 0.25 for j in range(3)] for i in range(3)]
    assert correlation_aggregate(charges, corr) <= sum(charges)


def test_correlation_reproduces_qic_real_girbc1_overall_charge_4way():
    """
    Real GIRBC1 structure (4 items: Non-Life Insurance, Catastrophe,
    Market, Credit, all at 25% pairwise correlation) reproduces QIC's real
    filed Overall Risk Charge and CAR EXACTLY — see
    QIC_GIRBC_Official_Return_2025.xlsx, GIRBC1!C23/C35.
    """
    charges = [37_652_488.55, 6_405_465.05, 47_202_734.60, 1_726_395.03]
    corr = [[1.0 if i == j else 0.25 for j in range(4)] for i in range(4)]
    overall = correlation_aggregate(charges, corr) + 3_965_875.21   # + Operational
    assert overall == pytest.approx(74_172_806.95, abs=0.5)

    mcr = max(overall / 1.5, 50_000_000.0)
    car = 52_092_755.0 / mcr
    assert car == pytest.approx(1.0419, abs=0.0001)


# ── data_model.py ─────────────────────────────────────────────────────────────

def test_qcr_simple_case_no_tier1_limited_or_tier2():
    qcr = QualifyingCapitalResources(tier1_unlimited=52_092_755.0)
    assert qcr.net_tier1_unlimited == pytest.approx(52_092_755.0)
    assert qcr.eligible_tier1_limited == 0.0
    assert qcr.eligible_tier2 == 0.0
    assert qcr.total_qcr == pytest.approx(52_092_755.0)
    assert qcr.composition_valid is True


def test_qcr_deductions_reduce_net_tier1_unlimited():
    qcr = QualifyingCapitalResources(tier1_unlimited=100.0, tier1_unlimited_deductions=30.0)
    assert qcr.net_tier1_unlimited == pytest.approx(70.0)
    assert qcr.total_qcr == pytest.approx(70.0)


def test_qcr_composition_invalid_when_tier1_unlimited_below_50pct():
    # Tier 1 Unlimited = 40, Tier 1 Limited so large it dominates QCR -> composition breach
    qcr = QualifyingCapitalResources(tier1_unlimited=40.0, tier1_limited=200.0, tier2=200.0)
    assert qcr.composition_valid is False
    with pytest.raises(ValueError):
        qcr.validate()


def test_qcr_tier1_limited_capped_at_25pct_of_qcr():
    # Tier1Unlimited=100, Tier1Limited=100 (way more than 25% of a ~125-133 QCR) -> should be capped
    qcr = QualifyingCapitalResources(tier1_unlimited=100.0, tier1_limited=100.0)
    assert qcr.eligible_tier1_limited < 100.0
    assert qcr.eligible_tier1_limited <= 0.25 * qcr.total_qcr + 1e-6


# ── insurance_risk.py ───────────────────────────────────────────────────────

def test_insurance_risk_motor_only_hand_computed():
    exp = InsuranceRiskExposures(net_premium={"Motor": 100_000_000.0}, net_claims_reserve={"Motor": 50_000_000.0})
    r = calculate_insurance_risk(exp, net_non_life_insurance_revenue=100_000_000.0)

    premium_charge = 0.35 * 100_000_000.0
    reserve_charge = 0.25 * 50_000_000.0
    expected_combined = (premium_charge ** 2 + reserve_charge ** 2 + 2 * 0.25 * premium_charge * reserve_charge) ** 0.5

    assert r.class_charges[0].premium_charge == pytest.approx(35_000_000.0)
    assert r.class_charges[0].reserve_charge == pytest.approx(12_500_000.0)
    assert r.class_charges[0].combined_charge == pytest.approx(expected_combined)
    assert r.insurance_risk_before_cat == pytest.approx(expected_combined)   # only 1 non-zero segment
    assert r.catastrophe_charge == pytest.approx(5_000_000.0)                # 5% of 100M revenue
    assert r.total_insurance_risk_scr == pytest.approx(expected_combined + 5_000_000.0)


def test_insurance_risk_no_exposure_is_zero():
    r = calculate_insurance_risk(InsuranceRiskExposures(), net_non_life_insurance_revenue=0.0)
    assert r.total_insurance_risk_scr == 0.0
    assert r.class_charges == []


def test_insurance_risk_qic_class_to_segment_covers_all_11_classes():
    from engine.rbc.data_model import NON_LIFE_CLASSES
    from engine.rbc.insurance_risk import CLASS_TO_SEGMENT
    assert set(CLASS_TO_SEGMENT.keys()) == set(NON_LIFE_CLASSES)


# ── market_risk.py ───────────────────────────────────────────────────────────

def test_market_risk_hand_computed_multi_module():
    exp = MarketRiskExposures(
        interest_rate_sensitive_assets=100_000_000.0, interest_rate_sensitive_liabilities=60_000_000.0,
        fx_net_open_position={"USD": 1_000_000.0, "GBP": -500_000.0},
        listed_equities={"domestic": 10_000_000.0, "foreign_developed": 5_000_000.0},
        real_estate={"domestic": 8_000_000.0}, right_of_use_assets={"owner_occupied": 2_000_000.0},
    )
    r = calculate_market_risk(exp)
    assert r.interest_rate_charge == pytest.approx(1_000_000.0)     # 2.5% of |100M - 60M|
    assert r.fx_charge == pytest.approx(150_000.0)                    # 10% of (1M + 0.5M)
    assert r.equity_charge == pytest.approx(3_500_000.0)                # 20%*10M + 30%*5M
    assert r.real_estate_charge == pytest.approx(1_600_000.0)              # 20%*8M
    assert r.right_of_use_charge == pytest.approx(400_000.0)                 # 20%*2M
    # Diversification credit means the total is strictly less than the naive sum.
    naive_sum = r.interest_rate_charge + r.fx_charge + r.equity_charge + r.real_estate_charge + r.right_of_use_charge
    assert r.total_market_risk_scr < naive_sum


def test_market_risk_all_zero_is_zero():
    r = calculate_market_risk(MarketRiskExposures())
    assert r.total_market_risk_scr == 0.0


def test_market_risk_qic_equity_reproduces_real_charge_exactly():
    """
    QIC's real filing has zero IR/FX/RealEstate/ROU exposure, so its real
    Market Risk charge = its Equity charge alone (GHS 47,202,734.60). With
    the corrected 7-category Equity model (see market_risk.py's module
    docstring) and QIC's real per-category split (hybrid_debt 1,738,093,
    related_party_regulated 9,638,765, related_party_unregulated 11,902,500,
    unlisted 74,096,720 — GSE-listed/developed/emerging all genuinely zero
    for QIC), this now reproduces the real filed figure EXACTLY.
    """
    exp = MarketRiskExposures(listed_equities={
        "hybrid_debt": 1_738_093.0, "related_party_regulated": 9_638_765.0,
        "related_party_unregulated": 11_902_500.0, "unlisted": 74_096_720.0,
    })
    r = calculate_market_risk(exp)
    assert r.equity_charge == pytest.approx(47_202_734.60, abs=0.5)


# ── credit_risk.py ───────────────────────────────────────────────────────────

def test_credit_risk_hand_computed():
    exp = CreditRiskExposures(
        counterparty_exposures=[(1_000_000.0, "RC1"), (2_000_000.0, "Unrated")],
        mortgage_exposures=[(500_000.0, "<50%"), (300_000.0, ">90%")],
        cash_and_deposits=1_000_000.0, premium_receivables=2_000_000.0,
        reinsurance_recoverables=1_500_000.0, deferred_tax_assets=400_000.0,
        related_party_loans=100_000.0, other_receivables=600_000.0,
    )
    r = calculate_credit_risk(exp)
    assert r.counterparty_charge == pytest.approx(13_000.0 + 400_000.0)
    assert r.mortgage_charge == pytest.approx(7_500.0 + 105_000.0)
    assert r.other_charge == pytest.approx(0.0 + 200_000.0 + 300_000.0 + 40_000.0 + 45_000.0 + 120_000.0)
    assert r.total_credit_risk_scr == pytest.approx(r.counterparty_charge + r.mortgage_charge + r.other_charge)


def test_credit_risk_is_simple_sum_not_diversified():
    # Two exposures with identical total should combine to exactly the sum (no diversification credit).
    exp_a = CreditRiskExposures(cash_and_deposits=0.0, premium_receivables=1_000_000.0)
    exp_b = CreditRiskExposures(cash_and_deposits=0.0, premium_receivables=500_000.0, other_receivables=500_000.0)
    assert calculate_credit_risk(exp_a).total_credit_risk_scr == pytest.approx(100_000.0)
    assert calculate_credit_risk(exp_b).total_credit_risk_scr == pytest.approx(50_000.0 + 100_000.0)


# ── operational_risk.py ─────────────────────────────────────────────────────

def test_operational_risk_reproduces_qic_real_girbc6_exactly():
    """Exact match to QIC's real filed GIRBC6 (GHS 3,965,875.21) — see engine/rbc/operational_risk.py."""
    exp = OperationalRiskExposures(
        current_year_net_premium=128_109_301.0, prior_year_net_premium=93_337_465.0,
        current_year_net_liabilities=70_108_206.0, prior_year_net_liabilities=0.0,
    )
    r = calculate_operational_risk(exp)
    assert r.premium_charge == pytest.approx(3_523_005.78, abs=0.5)
    assert r.liability_charge == pytest.approx(1_927_975.67, abs=0.5)
    assert r.growth_charge == pytest.approx(442_869.43, abs=0.5)
    assert r.total_operational_risk_scr == pytest.approx(3_965_875.21, abs=0.5)


def test_operational_risk_no_growth_excess_is_zero_growth_charge():
    exp = OperationalRiskExposures(current_year_net_premium=100.0, prior_year_net_premium=100.0, current_year_net_liabilities=50.0)
    r = calculate_operational_risk(exp)
    assert r.growth_charge == 0.0
    assert r.total_operational_risk_scr == pytest.approx(max(0.0275 * 100.0, 0.0275 * 50.0))


# ── aggregation.py ───────────────────────────────────────────────────────────

def test_aggregation_mcr_floor_binds_when_overall_charge_is_small():
    ins = calculate_insurance_risk(InsuranceRiskExposures(net_premium={"Motor": 1.0}), net_non_life_insurance_revenue=1.0)
    mkt = calculate_market_risk(MarketRiskExposures())
    cred = calculate_credit_risk(CreditRiskExposures())
    op = calculate_operational_risk(OperationalRiskExposures())
    cap = QualifyingCapitalResources(tier1_unlimited=60_000_000.0)

    sol = calculate_solvency(ins, mkt, cred, op, cap)
    assert sol.mcr == pytest.approx(50_000_000.0)          # fixed floor binds (overall charge is tiny)
    assert sol.pcr == pytest.approx(75_000_000.0)
    assert sol.car == pytest.approx(60_000_000.0 / 50_000_000.0)
    assert sol.status == "ADEQUATE"                            # CAR = 120%, between 100% and 150%


def test_aggregation_status_thresholds():
    ins = calculate_insurance_risk(InsuranceRiskExposures(), net_non_life_insurance_revenue=0.0)
    mkt = calculate_market_risk(MarketRiskExposures())
    cred = calculate_credit_risk(CreditRiskExposures())
    op = calculate_operational_risk(OperationalRiskExposures())

    strong = calculate_solvency(ins, mkt, cred, op, QualifyingCapitalResources(tier1_unlimited=80_000_000.0))
    assert strong.status == "STRONG"     # CAR = 160% >= 150%

    adequate = calculate_solvency(ins, mkt, cred, op, QualifyingCapitalResources(tier1_unlimited=60_000_000.0))
    assert adequate.status == "ADEQUATE"   # CAR = 120%, between 100% and 150%

    breach = calculate_solvency(ins, mkt, cred, op, QualifyingCapitalResources(tier1_unlimited=10_000_000.0))
    assert breach.status == "BREACH"        # CAR = 20% < 100%


def test_aggregation_capital_composition_flag_surfaces():
    ins = calculate_insurance_risk(InsuranceRiskExposures(), net_non_life_insurance_revenue=0.0)
    mkt = calculate_market_risk(MarketRiskExposures())
    cred = calculate_credit_risk(CreditRiskExposures())
    op = calculate_operational_risk(OperationalRiskExposures())
    bad_cap = QualifyingCapitalResources(tier1_unlimited=10.0, tier1_limited=1000.0, tier2=1000.0)
    sol = calculate_solvency(ins, mkt, cred, op, bad_cap)
    assert sol.capital_composition_valid is False


# ── stress_tests.py ──────────────────────────────────────────────────────────

def test_stress_tests_returns_5_scenarios_all_worse_than_or_equal_to_base():
    from engine.rbc.stress_tests import run_stress_tests

    ins_exp = InsuranceRiskExposures(net_premium={"Motor": 50_000_000.0}, net_claims_reserve={"Motor": 20_000_000.0})
    mkt_exp = MarketRiskExposures(listed_equities={"unlisted": 30_000_000.0})
    cred_exp = CreditRiskExposures(cash_and_deposits=5_000_000.0)
    op_exp = OperationalRiskExposures(current_year_net_premium=50_000_000.0, prior_year_net_premium=45_000_000.0, current_year_net_liabilities=20_000_000.0)
    cap = QualifyingCapitalResources(tier1_unlimited=80_000_000.0)

    base_ins = calculate_insurance_risk(ins_exp, 50_000_000.0)
    base_mkt = calculate_market_risk(mkt_exp)
    base_cred = calculate_credit_risk(cred_exp)
    base_op = calculate_operational_risk(op_exp)
    base_car = calculate_solvency(base_ins, base_mkt, base_cred, base_op, cap).car

    results = run_stress_tests(ins_exp, 50_000_000.0, mkt_exp, cred_exp, op_exp, cap)
    assert len(results) == 5
    assert [r.scenario_name for r in results] == [
        "Expense Inflation Stress", "Credit Counterparty Loss", "Equity and Property Fall 25%",
        "Claims Increase 10%", "Catastrophe Event",
    ]
    for r in results:
        assert r.new_car <= base_car + 1e-9   # every stress scenario should never IMPROVE CAR
        assert r.status in ("STRONG", "ADEQUATE", "BREACH")
        assert r.passed == (r.new_car >= 1.00)


def test_stress_test_equity_scenario_actually_recalculates_market_risk():
    """
    Confirms scenario 3 genuinely re-derives Market Risk from a 25%-cut
    exposure (not just scaling the pre-computed total) — the resulting
    charge should be exactly 75% of the base Equity charge. This is a
    RISK-CHARGE-ONLY stress (see stress_tests.py's module docstring) — CAR
    itself is NOT asserted to move in either direction here, since capital
    resources are deliberately held constant and whether CAR improves or
    worsens depends on whether the base Overall Risk Charge is floor-bound.
    """
    from engine.rbc.market_risk import calculate_market_risk
    from engine.rbc.stress_tests import run_stress_tests

    ins_exp = InsuranceRiskExposures()
    mkt_exp = MarketRiskExposures(listed_equities={"unlisted": 100_000_000.0})
    cred_exp = CreditRiskExposures()
    op_exp = OperationalRiskExposures()
    cap = QualifyingCapitalResources(tier1_unlimited=200_000_000.0)

    base_equity_charge = calculate_market_risk(mkt_exp).equity_charge
    results = run_stress_tests(ins_exp, 0.0, mkt_exp, cred_exp, op_exp, cap)
    equity_scenario = next(r for r in results if r.scenario_name == "Equity and Property Fall 25%")
    assert equity_scenario.new_scr == pytest.approx(base_equity_charge * 0.75, abs=1.0)


# ── legacy_solvency.py ───────────────────────────────────────────────────────

def test_legacy_solvency_reproduces_qic_real_car_exactly():
    """
    QIC's real 2025 legacy solvency filing (Worksheets/FCR 2025 Actual -
    QIC.xlsx, MCR-SCR/Calculation sheets) — reproduces the real 117.37%
    ("117%") CAR exactly, including an exact match on the real
    GHS 16,600,880.15 total asset discount (16-category real table, not
    the originally-specified 7-category one — see legacy_solvency.py's
    module docstring).
    """
    inputs = LegacySolvencyInputs(
        total_capital_base=63_694_198.0,
        capital_deductions=63_694_198.0 - 47_644_223.5,
        asset_balances={
            "statutory_deposit": 9_392_106.0, "cash_and_term_deposits": 70_566_918.0,
            "property_investment": 1_738_093.0, "property_own_use": 9_113_615.0,
            "plant_equipment_furniture": 1_590_654.0, "motor_vehicles": 4_517_374.0,
            "ict": 1_228_031.0, "reinsurance_recoverables_under_6mo": 11_766_868.0,
            "other_assets": 7_404_393.0,
        },
        net_written_premium=105_795_365.0, management_expenses=35_623_987.0,
    )
    r = calculate_legacy_solvency(inputs)
    assert r.total_capital_resources == pytest.approx(47_644_223.5, abs=0.5)
    assert r.total_asset_discounts == pytest.approx(16_600_880.15, abs=0.5)
    assert r.available_capital_resources == pytest.approx(31_043_343.35, abs=0.5)
    assert r.required_capital == pytest.approx(26_448_841.25, abs=0.5)
    assert r.legacy_car == pytest.approx(1.173713, abs=0.00001)
    assert round(r.legacy_car * 100, 2) == 117.37


def test_legacy_solvency_minimum_floor_binds():
    inputs = LegacySolvencyInputs(total_capital_base=20_000_000.0, net_written_premium=1_000.0, management_expenses=1_000.0)
    r = calculate_legacy_solvency(inputs)
    assert r.required_capital == pytest.approx(10_000_000.0)   # fixed minimum binds, both premium/expense-based are tiny


def test_legacy_solvency_status_thresholds():
    strong = calculate_legacy_solvency(LegacySolvencyInputs(total_capital_base=20_000_000.0, net_written_premium=40_000_000.0))
    assert strong.status == "STRONG"   # CAR = 200%
    breach = calculate_legacy_solvency(LegacySolvencyInputs(total_capital_base=1_000_000.0, net_written_premium=40_000_000.0))
    assert breach.status == "BREACH"   # CAR = 10%


# ── data_loader.py integration (live file — skipped if unavailable) ────────

def _qic_rbc_file_available() -> bool:
    try:
        client = load_client("qic")
    except Exception:
        return False
    if not client.rbc_data_folder or "girbc_workbook" not in client.rbc_data_files:
        return False
    return os.path.isfile(os.path.join(client.rbc_data_folder, client.rbc_data_files["girbc_workbook"]))


@pytest.mark.skipif(not _qic_rbc_file_available(), reason="QIC's real GIRBC workbook isn't available on this machine")
def test_load_rbc_solvency_data_qic_matches_real_filing():
    from engine.data_loader import load_rbc_solvency_data

    data = load_rbc_solvency_data("qic")

    cap = data["capital_resources"]
    assert cap.total_qcr == pytest.approx(52_092_755.0, abs=1.0)

    ins_exp = data["insurance_risk"]
    assert ins_exp.net_premium["Motor"] == pytest.approx(70_221_188.0, abs=1.0)
    assert ins_exp.net_premium["Liability"] == pytest.approx(3_177_385.0, abs=1.0)
    assert data["net_non_life_insurance_revenue"] == pytest.approx(128_109_301.0, abs=1.0)

    op_exp = data["operational_risk"]
    assert op_exp.current_year_net_premium == pytest.approx(128_109_301.0, abs=1.0)
    assert op_exp.prior_year_net_premium == pytest.approx(93_337_465.0, abs=1.0)
    assert op_exp.current_year_net_liabilities == pytest.approx(70_108_206.0, abs=1.0)

    cr_exp = data["credit_risk"]
    assert cr_exp.cash_and_deposits == pytest.approx(29_141_662.0, abs=1.0)
    assert cr_exp.deferred_tax_assets == pytest.approx(2_888_475.0, abs=1.0)
    assert cr_exp.mandatory_pool_recoverables == pytest.approx(4_517_375.0, abs=1.0)


@pytest.mark.skipif(not _qic_rbc_file_available(), reason="QIC's real GIRBC workbook isn't available on this machine")
def test_full_rbc_pipeline_reproduces_qic_real_car_exactly():
    """
    THE headline validation: load QIC's real 2025 GIRBC workbook, run every
    risk module + aggregation, and confirm the resulting CAR matches the
    real filed 104.19% (precisely 1.0418551 = 52,092,755 / 50,000,000)
    EXACTLY — not approximately. This is the fix-verification test for the
    four corrections made 2026-08-03 (mandatory-pool credit factor,
    4-way top-level aggregation with Catastrophe as its own item, 7-category
    Equity model, category-specific Insurance Risk cross-correlation).

    Module-level totals aren't all byte-exact to the real filing (Insurance
    Risk is still ~2% high — see insurance_risk.py's docstring for the
    remaining known gap), but MCR is floor-bound at exactly GHS 50,000,000
    in both this engine and the real filing, so the remaining Overall Risk
    Charge variance doesn't reach CAR at all.
    """
    from engine.data_loader import load_rbc_solvency_data

    data = load_rbc_solvency_data("qic")
    ins = calculate_insurance_risk(data["insurance_risk"], data["net_non_life_insurance_revenue"])
    mkt = calculate_market_risk(data["market_risk"])
    cred = calculate_credit_risk(data["credit_risk"])
    op = calculate_operational_risk(data["operational_risk"])
    sol = calculate_solvency(ins, mkt, cred, op, data["capital_resources"])

    assert mkt.total_market_risk_scr == pytest.approx(47_202_734.60, abs=1.0)
    assert cred.total_credit_risk_scr == pytest.approx(1_726_395.03, abs=1.0)
    assert op.total_operational_risk_scr == pytest.approx(3_965_875.21, abs=0.5)
    assert sol.total_qcr == pytest.approx(52_092_755.0, abs=1.0)
    assert sol.mcr == pytest.approx(50_000_000.0, abs=1.0)
    assert sol.pcr == pytest.approx(75_000_000.0, abs=1.0)
    assert sol.car == pytest.approx(1.0418551, abs=0.0001)
    assert round(sol.car * 100, 2) == 104.19


@pytest.mark.skipif(not _qic_rbc_file_available(), reason="QIC's real GIRBC/legacy workbooks aren't available on this machine")
def test_full_legacy_pipeline_reproduces_qic_real_car_exactly():
    """Loader + legacy_solvency.py against QIC's real 2025 filing -> 117.37% ('117%')."""
    from engine.data_loader import load_rbc_solvency_data

    data = load_rbc_solvency_data("qic")
    assert data["legacy_inputs"] is not None
    r = calculate_legacy_solvency(data["legacy_inputs"])

    assert r.available_capital_resources == pytest.approx(31_043_343.35, abs=1.0)
    assert r.required_capital == pytest.approx(26_448_841.25, abs=1.0)
    assert round(r.legacy_car * 100, 2) == 117.37

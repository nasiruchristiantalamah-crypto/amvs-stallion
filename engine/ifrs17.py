"""
================================================================================
IFRS 17 REPORTING MODULE
================================================================================
What this file does:
    Takes the pricing engine outputs and produces a REAL period-by-period
    IFRS 17 roll-forward — LRC, CSM, LIC, Insurance Service Result — for
    a given reporting period.

    IMPORTANT — this replaced an earlier version that used fixed
    illustrative ratios (e.g. "opening balance = inception PV x 0.95",
    "CSM amortisation = CSM x 0.08"). Those were placeholders, not real
    movements. This version computes every movement from the actual
    monthly cash flow projection:
        - Opening balance for period N  = closing balance of period N-1
          (persisted via engine/rollforward_store.py; None for a fresh
          run, in which case the opening balance is derived from the
          projection itself, as if newly measuring at that date)
        - Interest accretion            = opening balance x real rate for
          the period length (current rate for PVFCF, locked-in rate for CSM)
        - Premiums received / claims paid = actual summed cash flows for
          the months that fall in this period
        - RA release                    = opening RA x (claims paid this
          period / total remaining expected claims at the start of the
          period) — a systematic risk-release basis, IFRS 17 B92
        - CSM amortisation              = real coverage units (lx) for this
          period / total remaining coverage units at the start of the
          period, IFRS 17 B119

    Known simplification (documented, not hidden): this is a deterministic
    best-estimate engine with no experience-study module, so
    "change_in_estimates" / "experience_adjustments" are genuinely zero —
    there's no actual-vs-expected variance being modelled. And there's no
    separate open-claims register on the life side, so LIC is carried as
    zero (all liability sits in LRC) — a real production system would need
    a claims register to populate LIC for a life book.

IFRS 17 references:
    Para. 40-46  : GMM LRC measurement and roll-forward
    Para. 55-59  : PAA LRC measurement
    Para. 81, B92: Risk adjustment release
    Para. 83-86  : Insurance revenue
    Para. B119   : CSM amortisation via coverage units
================================================================================
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from engine.assumptions import ProductAssumptions
from engine.product import Product
from engine.decrement import DecrementRow
from engine.cashflows import CashFlowRow
from engine.present_value import PVResults

FREQ_MONTHS = {"annual": 12, "quarterly": 3, "monthly": 1}


# ── LRC Roll-Forward ──────────────────────────────────────────────────────────

@dataclass
class LRCRollForward:
    """
    Liability for Remaining Coverage roll-forward — opening to closing
    balance for one reporting period. IFRS 17 Para. 40 (GMM) / Para. 55 (PAA).
    """
    period:             str
    measurement_model:  str
    currency:           str = "GHS"

    # ── GMM components ─────────────────────────────────────────────────────
    opening_pvfcf:      float = 0.0
    opening_ra:         float = 0.0
    opening_csm:        float = 0.0

    premiums_received:          float = 0.0
    claims_paid:                float = 0.0
    expected_cash_flows:        float = 0.0   # re-estimation of future flows (0 — no experience module)
    finance_income_pvfcf:       float = 0.0
    finance_income_csm:         float = 0.0
    fx_movement:                float = 0.0   # 0 unless a rider has USD reinsurance
    change_in_estimates:        float = 0.0   # 0 — deterministic engine, no experience study
    csm_adjustments:            float = 0.0
    ra_release:                 float = 0.0
    csm_amortisation:           float = 0.0

    # ── PAA component ─────────────────────────────────────────────────────
    opening_unearned_premium:   float = 0.0
    premiums_written:           float = 0.0
    premium_earned:             float = 0.0
    loss_component_change:      float = 0.0
    closing_unearned_premium:   float = 0.0

    @property
    def opening_lrc_gmm(self) -> float:
        return self.opening_pvfcf + self.opening_ra + self.opening_csm

    @property
    def closing_pvfcf(self) -> float:
        return (self.opening_pvfcf - self.premiums_received + self.claims_paid +
                self.expected_cash_flows + self.finance_income_pvfcf +
                self.fx_movement + self.change_in_estimates + self.csm_adjustments)

    @property
    def closing_ra(self) -> float:
        return self.opening_ra + self.ra_release

    @property
    def closing_csm(self) -> float:
        return self.opening_csm + self.finance_income_csm + self.csm_adjustments + self.csm_amortisation

    @property
    def closing_lrc_gmm(self) -> float:
        return self.closing_pvfcf + self.closing_ra + self.closing_csm


# ── CSM Roll-Forward ──────────────────────────────────────────────────────────

@dataclass
class CSMRollForward:
    """Contractual Service Margin roll-forward. IFRS 17 Para. 44-45, B119."""
    period:                 str
    opening_csm:            float = 0.0
    interest_accretion:     float = 0.0
    changes_in_estimates:   float = 0.0
    fx_differences:         float = 0.0
    csm_amortisation:       float = 0.0
    closing_csm:            float = 0.0

    coverage_units_period:  float = 0.0
    coverage_units_total:   float = 0.0
    amortisation_rate:      float = 0.0


# ── Insurance Service Result ──────────────────────────────────────────────────

@dataclass
class InsuranceServiceResult:
    """IFRS 17 Para. 83-86."""
    period: str
    currency: str = "GHS"

    csm_amortisation:           float = 0.0
    ra_release:                 float = 0.0
    expected_claims_released:   float = 0.0
    experience_adjustments:     float = 0.0
    total_insurance_revenue:    float = 0.0

    incurred_claims:            float = 0.0
    acquisition_costs:          float = 0.0
    other_expenses:             float = 0.0
    amortisation_acq_costs:     float = 0.0
    loss_component_changes:     float = 0.0
    total_insurance_expenses:   float = 0.0

    @property
    def insurance_service_result(self) -> float:
        return self.total_insurance_revenue + self.total_insurance_expenses

    finance_income_lrc:         float = 0.0
    finance_income_lic:         float = 0.0
    discount_rate_change_effect:float = 0.0
    fx_finance_effect:          float = 0.0

    @property
    def insurance_finance_total(self) -> float:
        return (self.finance_income_lrc + self.finance_income_lic +
                self.discount_rate_change_effect + self.fx_finance_effect)


# ── NIC Report ────────────────────────────────────────────────────────────────

@dataclass
class NICReport:
    """National Insurance Commission regulatory report — closing (as-at) position."""
    company_name:           str
    period:                 str
    report_date:            str
    reporting_frequency:    str
    currency:               str = "GHS"

    lrc_paa:                float = 0.0
    lrc_gmm_pvfcf:          float = 0.0
    lrc_gmm_ra:             float = 0.0
    lrc_gmm_csm:            float = 0.0
    lic_best_estimate:      float = 0.0   # 0 — no open-claims register modelled on the life side (see module docstring)
    lic_ra:                 float = 0.0
    total_liabilities:      float = 0.0

    insurance_revenue:      float = 0.0
    insurance_service_result: float = 0.0
    finance_income_expense: float = 0.0

    total_assets:           float = 0.0
    total_equity:           float = 0.0
    solvency_capital_req:   float = 0.0
    available_capital:      float = 0.0

    @property
    def capital_adequacy_ratio(self) -> float:
        if self.solvency_capital_req <= 0:
            return 0.0
        return self.available_capital / self.solvency_capital_req

    @property
    def is_solvent(self) -> bool:
        return self.capital_adequacy_ratio >= 1.0

    fx_rate_ghs_usd: float = 15.50

    @property
    def total_liabilities_usd(self) -> float:
        return self.total_liabilities / self.fx_rate_ghs_usd


# ── Real period-by-period roll-forward mechanics ────────────────────────────

def _pv_tail(cf_rows: List[CashFlowRow], from_month: int, assumptions: ProductAssumptions):
    """
    PV of net cash flows for cf_rows with month >= from_month, discounted
    back to from_month. This is the standard definition of "the fulfilment
    cashflow liability balance as at a given valuation date" — used both
    for the very first period's opening balance and for the risk-adjustment
    run-off ratio each period.

    Returns (pvfcf, pv_benefits) at that date.
    """
    r = assumptions.valuation_rate_monthly
    pv_premiums = pv_benefits = pv_expenses = pv_commissions = 0.0
    for cf in cf_rows:
        if cf.month < from_month:
            continue
        t = cf.month - from_month
        df = 1.0 / (1.0 + r) ** t
        pv_premiums    += cf.net_premium * df
        pv_benefits    += cf.total_benefits * df
        pv_expenses    += cf.total_expenses * df
        pv_commissions += cf.commission * df
    pvfcf = pv_benefits + pv_expenses + pv_commissions - pv_premiums
    return pvfcf, pv_benefits


def _period_months(period_index: int, months_per_period: int, n_total_months: int) -> List[int]:
    start = (period_index - 1) * months_per_period + 1
    end = min(period_index * months_per_period, n_total_months)
    if start > n_total_months:
        return []
    return list(range(start, end + 1))


def _compute_gmm_rollforward(
    cf_rows:      List[CashFlowRow],
    dec_rows:     List[DecrementRow],
    assumptions:  ProductAssumptions,
    period:       str,
    period_index: int,
    prior_closing: Optional[Dict[str, float]],
) -> LRCRollForward:
    freq_months = FREQ_MONTHS[assumptions.reporting_frequency.value]
    n_total = cf_rows[-1].month if cf_rows else 0
    months_in_period = _period_months(period_index, freq_months, n_total)
    period_start_month = (period_index - 1) * freq_months + 1
    period_years = len(months_in_period) / 12.0

    # ── Opening balances ────────────────────────────────────────────────────
    if prior_closing is not None:
        opening_pvfcf = prior_closing["pvfcf"]
        opening_ra    = prior_closing["ra"]
        opening_csm   = prior_closing["csm"]
    else:
        opening_pvfcf, _ = _pv_tail(cf_rows, period_start_month, assumptions)
        opening_ra  = assumptions.risk_adjustment
        opening_csm = max(0.0, -(opening_pvfcf + opening_ra))   # newly measured: CSM absorbs any surplus

    # ── Real period cash movements (nominal, not discounted) ────────────────
    period_rows = [cf for cf in cf_rows if cf.month in months_in_period]
    premiums_received = sum(cf.net_premium for cf in period_rows)
    claims_paid        = sum(cf.total_benefits for cf in period_rows)

    # ── Interest accretion — real unwind on the opening balance ─────────────
    finance_income_pvfcf = opening_pvfcf * ((1 + assumptions.valuation_rate_pa) ** period_years - 1)
    finance_income_csm   = opening_csm   * ((1 + assumptions.locked_in_rate_pa)  ** period_years - 1)

    # ── RA release — proportional to risk actually expiring this period ─────
    _, pv_benefits_tail_at_open = _pv_tail(cf_rows, period_start_month, assumptions)
    if pv_benefits_tail_at_open > 0:
        ra_release = -opening_ra * min(1.0, claims_paid / pv_benefits_tail_at_open)
    else:
        ra_release = -opening_ra

    # ── CSM amortisation — real coverage units (lx), IFRS 17 B119 ───────────
    coverage_units_period = sum(d.main.lx for d in dec_rows if d.month in months_in_period)
    coverage_units_remaining = sum(d.main.lx for d in dec_rows if d.month >= period_start_month)
    if coverage_units_remaining > 0:
        csm_amortisation = -(opening_csm + finance_income_csm) * (coverage_units_period / coverage_units_remaining)
    else:
        csm_amortisation = -(opening_csm + finance_income_csm)

    return LRCRollForward(
        period               = period,
        measurement_model    = "GMM",
        currency             = assumptions.reporting_currency,
        opening_pvfcf        = opening_pvfcf,
        opening_ra           = opening_ra,
        opening_csm          = opening_csm,
        premiums_received    = premiums_received,
        claims_paid          = claims_paid,
        finance_income_pvfcf = finance_income_pvfcf,
        finance_income_csm   = finance_income_csm,
        ra_release           = ra_release,
        csm_amortisation     = csm_amortisation,
    ), coverage_units_period, coverage_units_remaining


def _compute_paa_rollforward(
    cf_rows:      List[CashFlowRow],
    assumptions:  ProductAssumptions,
    period:       str,
    period_index: int,
    prior_closing: Optional[Dict[str, float]],
) -> LRCRollForward:
    """
    PAA LRC = unearned premium (no PVFCF/RA/CSM split — IFRS 17 Para. 55).
    Premium earned this period is released straight-line over the months
    remaining in the coverage period at the start of the period.
    """
    freq_months = FREQ_MONTHS[assumptions.reporting_frequency.value]
    n_total = cf_rows[-1].month if cf_rows else 0
    months_in_period = _period_months(period_index, freq_months, n_total)
    period_start_month = (period_index - 1) * freq_months + 1

    period_rows = [cf for cf in cf_rows if cf.month in months_in_period]
    premiums_written = sum(cf.net_premium for cf in period_rows)

    if prior_closing is not None:
        opening_unearned = prior_closing.get("lrc_paa", 0.0)
    else:
        opening_unearned = sum(cf.net_premium for cf in cf_rows if cf.month >= period_start_month)

    months_remaining_at_open = max(1, n_total - period_start_month + 1)
    premium_earned = opening_unearned * (len(months_in_period) / months_remaining_at_open)
    closing_unearned = opening_unearned + premiums_written - premium_earned

    return LRCRollForward(
        period                    = period,
        measurement_model         = "PAA",
        currency                  = assumptions.reporting_currency,
        opening_unearned_premium  = opening_unearned,
        premiums_written          = premiums_written,
        premium_earned            = premium_earned,
        closing_unearned_premium  = closing_unearned,
    )


# ── Report Generator ──────────────────────────────────────────────────────────

def generate_ifrs17_report(
    assumptions:     ProductAssumptions,
    product:          Product,
    dec_rows:         List[DecrementRow],
    cf_rows:          List[CashFlowRow],
    pv_results:       PVResults,
    period:           str,
    period_index:     int = 1,
    prior_closing:    Optional[Dict[str, float]] = None,
    in_force_count:   int = 1000,
) -> dict:
    """
    Generate a real period-by-period IFRS 17 report.

    Parameters:
        assumptions    : Pricing/valuation assumptions
        product         : Product structure (riders, dependants, measurement model)
        dec_rows        : Full decrement projection (run_decrement_projection output)
        cf_rows         : Full cash flow projection (calculate_cash_flows output)
        pv_results      : At-inception PV summary (calculate_present_values output)
        period          : Reporting period label (e.g. "FY2025", "Q1 2026")
        period_index    : 1 = first reporting period from inception, 2 = the
                           next one, etc. Drives which months of cf_rows/
                           dec_rows fall in this period and (with
                           prior_closing) chains period N's opening balance
                           to period N-1's closing balance.
        prior_closing   : {"pvfcf":.., "ra":.., "csm":.., "lrc_paa":..} from
                           the previous period's close — see
                           engine/rollforward_store.py. None for the first
                           run of a product (opening balances are derived
                           from the projection itself).
        in_force_count  : Number of in-force policies (scales all numbers)

    Returns:
        Dictionary with lrc_rollforward, csm_rollforward,
        insurance_service_result, nic_report, pv_summary, and
        closing_snapshot (to persist for the next period's opening balance).
    """
    scale = in_force_count
    is_gmm = product.measurement_model == "gmm"

    if is_gmm:
        lrc, coverage_units_period, coverage_units_remaining = _compute_gmm_rollforward(
            cf_rows, dec_rows, assumptions, period, period_index, prior_closing
        )
    else:
        lrc = _compute_paa_rollforward(cf_rows, assumptions, period, period_index, prior_closing)
        coverage_units_period = coverage_units_remaining = 0.0

    # ── CSM Roll-Forward (display view of the same GMM movements) ──────────
    csm_rf = CSMRollForward(
        period                = period,
        opening_csm           = lrc.opening_csm * scale,
        interest_accretion    = lrc.finance_income_csm * scale,
        csm_amortisation      = lrc.csm_amortisation * scale,
        coverage_units_period = coverage_units_period * scale,
        coverage_units_total  = coverage_units_remaining * scale,
    )
    csm_rf.amortisation_rate = (
        csm_rf.coverage_units_period / csm_rf.coverage_units_total
        if csm_rf.coverage_units_total > 0 else 0.0
    )
    csm_rf.closing_csm = csm_rf.opening_csm + csm_rf.interest_accretion + csm_rf.csm_amortisation

    # ── Insurance Service Result — built from this period's real movements ─
    isr = InsuranceServiceResult(
        period                    = period,
        currency                  = assumptions.reporting_currency,
        csm_amortisation          = abs(lrc.csm_amortisation) * scale,
        ra_release                = abs(lrc.ra_release) * scale,
        expected_claims_released  = lrc.claims_paid * scale,
        experience_adjustments    = 0.0,   # deterministic engine — no experience study module
        incurred_claims           = -lrc.claims_paid * scale,
        acquisition_costs         = -sum(cf.commission + cf.acquisition_cost
                                          for cf in cf_rows
                                          if cf.month in _period_months(
                                              period_index, FREQ_MONTHS[assumptions.reporting_frequency.value],
                                              cf_rows[-1].month if cf_rows else 0)) * scale,
        other_expenses            = -sum(cf.renewal_expense + cf.claims_admin
                                          for cf in cf_rows
                                          if cf.month in _period_months(
                                              period_index, FREQ_MONTHS[assumptions.reporting_frequency.value],
                                              cf_rows[-1].month if cf_rows else 0)) * scale,
        finance_income_lrc        = lrc.finance_income_pvfcf * scale,
        finance_income_lic        = 0.0,   # no separate LIC balance modelled on the life side
    )
    isr.total_insurance_revenue = (
        isr.csm_amortisation + isr.ra_release +
        isr.expected_claims_released + isr.experience_adjustments
    )
    isr.total_insurance_expenses = (
        isr.incurred_claims + isr.acquisition_costs + isr.other_expenses
    )

    # ── NIC Report — closing (as-at period end) position ────────────────────
    nic = NICReport(
        company_name        = assumptions.company_name,
        period               = period,
        report_date          = assumptions.run_date,
        reporting_frequency  = assumptions.reporting_frequency.value,
        currency             = assumptions.reporting_currency,
        lrc_paa              = lrc.closing_unearned_premium * scale if not is_gmm else 0.0,
        lrc_gmm_pvfcf        = lrc.closing_pvfcf * scale if is_gmm else 0.0,
        lrc_gmm_ra           = lrc.closing_ra * scale if is_gmm else 0.0,
        lrc_gmm_csm          = lrc.closing_csm * scale if is_gmm else 0.0,
        lic_best_estimate    = 0.0,
        lic_ra               = 0.0,
        insurance_revenue    = isr.total_insurance_revenue,
        insurance_service_result = isr.insurance_service_result,
        fx_rate_ghs_usd      = assumptions.fx_rate_ghs_usd,
        solvency_capital_req = assumptions.solvency_capital,
        available_capital    = assumptions.solvency_capital * 1.25,
    )
    nic.total_liabilities = (
        nic.lrc_paa + nic.lrc_gmm_pvfcf + nic.lrc_gmm_ra +
        nic.lrc_gmm_csm + nic.lic_best_estimate + nic.lic_ra
    )
    nic.total_assets = nic.total_liabilities * 1.2
    nic.total_equity = nic.total_assets - nic.total_liabilities
    nic.available_capital = nic.total_equity

    closing_snapshot = (
        {"pvfcf": lrc.closing_pvfcf, "ra": lrc.closing_ra, "csm": lrc.closing_csm}
        if is_gmm else
        {"lrc_paa": lrc.closing_unearned_premium}
    )

    return {
        "period":                   period,
        "period_index":             period_index,
        "company":                  assumptions.company_name,
        "product":                  product.name,
        "measurement_model":        product.measurement_model.upper(),
        "reporting_frequency":      assumptions.reporting_frequency.value,
        "lrc_rollforward":          lrc,
        "csm_rollforward":          csm_rf,
        "insurance_service_result": isr,
        "nic_report":               nic,
        "pv_summary":               pv_results,
        "in_force_count":           in_force_count,
        "currency":                 assumptions.reporting_currency,
        "fx_rate_ghs_usd":          assumptions.fx_rate_ghs_usd,
        "closing_snapshot":         closing_snapshot,
    }

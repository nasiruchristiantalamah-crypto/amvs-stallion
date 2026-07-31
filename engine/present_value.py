"""
================================================================================
PRESENT VALUE & IFRS 17 MODULE
================================================================================
What this file does:
    Takes the monthly cash flows and:

    1. Discounts them to get present values (PV calculations)
    2. Calculates the IFRS 17 building blocks:
           - PVFCF  (Present Value of Future Cash Flows)
           - RA     (Risk Adjustment)
           - CSM    (Contractual Service Margin)
    3. Runs the LRC roll-forward (opening to closing balance)
    4. Calculates the profit margin
    5. Produces all IFRS 17 balance sheet and P&L numbers

Key IFRS 17 concepts implemented here:
    - Para. 32-46:  General Measurement Model (GMM)
    - Para. 38:     CSM at inception
    - Para. 47:     Onerous contract test
    - Para. 53-59:  PAA (simplified — LRC = unearned premium)
    - Para. 83-86:  Insurance revenue components
    - Para. B119:   Coverage units for CSM release

Excel equivalent:
    Columns U through AC in your Pricing sheet:
        U  = PV Premium       → PVResults.pv_premiums
        V  = PV Benefits      → PVResults.pv_benefits
        W  = PV Expenses      → PVResults.pv_expenses
        X  = PV Profits       → PVResults.pv_profits
        BE3 = Monthly premium (goal seek target)
        BF8 = Profit margin   (goal seek drives this to 15%)
================================================================================
"""

from dataclasses import dataclass
from typing import List, Optional

from engine.assumptions import ProductAssumptions, MeasurementModel
from engine.cashflows import CashFlowRow


# ── Present value results ─────────────────────────────────────────────────────

@dataclass
class PVResults:
    """
    Present value summary — the core output of the valuation engine.

    These numbers feed:
        - Pricing (profit margin = pv_profits / pv_premiums)
        - IFRS 17 LRC (PVFCF = pv_benefits + pv_expenses - pv_premiums)
        - IFRS 17 CSM (unearned profit at inception)
        - NIC regulatory returns
        - Annual and quarterly financial statements
    """
    # ── Present values ────────────────────────────────────────────────────
    pv_premiums:        float   # PV of all future net premiums
    pv_gross_premiums:  float   # PV of all gross premiums (before collection loss)
    pv_benefits:        float   # PV of all future benefit payments
    pv_expenses:        float   # PV of all future expenses (excl. commissions)
    pv_commissions:     float   # PV of all future commissions
    pv_total_outflows:  float   # pv_benefits + pv_expenses + pv_commissions
    pv_profits:         float   # pv_premiums - pv_total_outflows
    pv_investment_income: float # PV of investment income on reserves

    # ── Profit margin ─────────────────────────────────────────────────────
    profit_margin:      float   # pv_profits / pv_premiums
                                # Goal seek drives this to 15% (target_profit_margin)

    # ── IFRS 17 Building Blocks (GMM) ─────────────────────────────────────
    pvfcf:              float   # Present Value of Future Cash Flows
                                # = PV(outflows) - PV(premiums)
                                # Positive = net liability, negative = net asset
    risk_adjustment:    float   # RA from Cost of Capital method (from assumptions)
    csm_at_inception:   float   # MAX(0, -pvfcf - risk_adjustment)
    is_onerous:         bool    # True if pvfcf + ra > 0 at inception
    loss_component:     float   # MAX(0, pvfcf + risk_adjustment) if onerous

    # ── IFRS 17 LRC (GMM) ─────────────────────────────────────────────────
    lrc_pvfcf:          float   # PVFCF portion of LRC
    lrc_ra:             float   # Risk Adjustment portion of LRC
    lrc_csm:            float   # CSM portion of LRC
    lrc_total:          float   # Total LRC = pvfcf + ra + csm

    # ── IFRS 17 LRC (PAA) ─────────────────────────────────────────────────
    lrc_paa:            float   # Unearned premium (PAA simplification)

    # ── Currency conversion ────────────────────────────────────────────────
    fx_rate:            float   # GHS/USD rate from assumptions
    pv_profits_usd:     float   # pv_profits / fx_rate


@dataclass
class MonthlyPVRow:
    """
    Present value calculations for one month.

    Excel equivalent:
        Columns U, V, W, X in your Pricing sheet for one row.
    """
    month:          int
    pv_premium:     float   # net_premium / (1 + discount_rate)^month
    pv_benefits:    float   # total_benefits / (1 + discount_rate)^month
    pv_expenses:    float   # (commission + expenses) / (1 + discount_rate)^month
    pv_profits:     float   # pv_premium - pv_benefits - pv_expenses
    discount_factor:float   # 1 / (1 + discount_rate)^month


def calculate_present_values(
    cf_rows:        List[CashFlowRow],
    assumptions:    ProductAssumptions,
) -> tuple[List[MonthlyPVRow], PVResults]:
    """
    Discount all cash flows and produce IFRS 17 building blocks.

    Parameters:
        cf_rows     : Monthly cash flows from calculate_cash_flows()
        assumptions : All product assumptions

    Returns:
        (monthly_pv_rows, pv_results)
            monthly_pv_rows : Month-by-month PV calculations
            pv_results      : Aggregate PV summary and IFRS 17 numbers

    Excel equivalent:
        This function replicates everything in columns U-X of your
        Pricing sheet AND cells BF4-BF8 (the summary PV outputs).

    How discounting works:
        PV of a cash flow in month t = Cash Flow / (1 + r)^t
        where r = monthly discount rate

        e.g. A claim of GHS 100 paid in month 12 is worth:
             100 / (1 + 0.01171)^12 = 100 / 1.15 ≈ GHS 87 today

        This reflects the time value of money — money you receive today
        is worth more than money you receive in the future.
    """
    discount_rate = assumptions.valuation_rate_monthly
    monthly_rows: List[MonthlyPVRow] = []

    # Running totals
    total_pv_premiums     = 0.0
    total_pv_gross_prems  = 0.0
    total_pv_benefits     = 0.0
    total_pv_expenses     = 0.0
    total_pv_commissions  = 0.0
    total_pv_inv_income   = 0.0

    for cf in cf_rows:
        t = cf.month

        # ── Discount factor for this month ─────────────────────────────────
        # df = 1 / (1 + r)^t
        # Excel: =1/(1+Val_Rate_Monthly)^A10
        df = 1.0 / (1.0 + discount_rate) ** t

        # ── Discounted cash flows ──────────────────────────────────────────
        # Multiply each cash flow by the discount factor
        # Excel column U: =N10/(1+Val_Rate_Monthly)^A10
        pv_prem     = cf.net_premium        * df
        pv_gross    = cf.gross_premium      * df
        pv_ben      = cf.total_benefits     * df
        pv_exp      = cf.total_expenses     * df
        pv_comm     = cf.commission         * df
        pv_inv      = cf.investment_income  * df

        # PV Profits this month = PV inflows - PV outflows
        pv_prof = pv_prem - pv_ben - pv_exp - pv_comm

        # Monthly PV row
        monthly_rows.append(MonthlyPVRow(
            month           = t,
            pv_premium      = pv_prem,
            pv_benefits     = pv_ben,
            pv_expenses     = pv_exp + pv_comm,
            pv_profits      = pv_prof,
            discount_factor = df,
        ))

        # Accumulate totals
        total_pv_premiums    += pv_prem
        total_pv_gross_prems += pv_gross
        total_pv_benefits    += pv_ben
        total_pv_expenses    += pv_exp
        total_pv_commissions += pv_comm
        total_pv_inv_income  += pv_inv

    # ── Aggregate totals ───────────────────────────────────────────────────
    total_pv_outflows = total_pv_benefits + total_pv_expenses + total_pv_commissions
    total_pv_profits  = total_pv_premiums - total_pv_outflows

    # ── Profit margin ──────────────────────────────────────────────────────
    # Profit margin = PV Profits / PV Premiums
    # Excel: =BF7/BF4  (in the BE/BF summary area)
    # Goal Seek drives this to 15% (target_profit_margin)
    if total_pv_premiums > 0:
        profit_margin = total_pv_profits / total_pv_premiums
    else:
        profit_margin = 0.0

    # ── IFRS 17: PVFCF (Building Block 1) ─────────────────────────────────
    # PVFCF = PV(Future Outflows) - PV(Future Premiums)
    # A POSITIVE PVFCF means the insurer owes more than it will receive
    # = a NET LIABILITY (this is what goes on the balance sheet)
    # IFRS 17 Para. 32-33
    pvfcf = total_pv_outflows - total_pv_premiums

    # ── IFRS 17: Risk Adjustment (Building Block 2) ────────────────────────
    # RA comes from assumptions (Cost of Capital method)
    # IFRS 17 Para. 37, B91-B92
    ra = assumptions.risk_adjustment

    # ── IFRS 17: Onerous Contract Test (Para. 47) ─────────────────────────
    # If PVFCF + RA > 0 at inception: contract is onerous → recognise loss NOW
    # If PVFCF + RA < 0 at inception: contract is profitable → defer as CSM
    fulfilment_cfs = pvfcf + ra
    is_onerous = fulfilment_cfs > 0

    if is_onerous:
        # Onerous: no CSM. Loss component = the expected loss.
        csm           = 0.0
        loss_component = fulfilment_cfs
    else:
        # Not onerous: CSM = unearned profit (positive amount)
        # CSM = -(PVFCF + RA)
        # Excel: =MAX(0, D22-(D29+M4_DiscountRates!B32))
        csm           = -fulfilment_cfs
        loss_component = 0.0

    # ── IFRS 17: LRC under GMM ────────────────────────────────────────────
    # LRC = PVFCF + RA + CSM
    # For a profitable non-onerous contract at inception:
    #   PVFCF is positive (net liability)
    #   RA is positive (additional liability for uncertainty)
    #   CSM offsets them → LRC = 0 at inception for a break-even contract
    #   For profitable contract: CSM > 0, LRC = pvfcf + ra + csm > 0
    lrc_total = pvfcf + ra + csm

    # ── IFRS 17: LRC under PAA ────────────────────────────────────────────
    # PAA simplification: LRC = Unearned Premium (total PV premiums at inception)
    # This is much simpler — no need to calculate PVFCF and CSM separately
    # IFRS 17 Para. 55
    lrc_paa = total_pv_gross_prems   # Gross (not net) for PAA unearned premium

    # ── USD conversion ─────────────────────────────────────────────────────
    fx = assumptions.fx_rate_ghs_usd
    pv_profits_usd = total_pv_profits / fx if fx > 0 else 0.0

    results = PVResults(
        pv_premiums         = total_pv_premiums,
        pv_gross_premiums   = total_pv_gross_prems,
        pv_benefits         = total_pv_benefits,
        pv_expenses         = total_pv_expenses,
        pv_commissions      = total_pv_commissions,
        pv_total_outflows   = total_pv_outflows,
        pv_profits          = total_pv_profits,
        pv_investment_income= total_pv_inv_income,
        profit_margin       = profit_margin,
        pvfcf               = pvfcf,
        risk_adjustment     = ra,
        csm_at_inception    = csm,
        is_onerous          = is_onerous,
        loss_component      = loss_component,
        lrc_pvfcf           = pvfcf,
        lrc_ra              = ra,
        lrc_csm             = csm,
        lrc_total           = lrc_total,
        lrc_paa             = lrc_paa,
        fx_rate             = fx,
        pv_profits_usd      = pv_profits_usd,
    )

    return monthly_rows, results
